"""HTTP boundary for automated Bug analysis and ZenTao note writeback."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy.exc import IntegrityError

from app.gateway.bug_rollback import rollback_repair
from app.gateway.bug_workflow import (
    _public_bug_workflow_error,
    attach_workflow_task,
    cancel_workflow_tasks,
    run_bug_workflow,
)
from app.gateway.bug_workflow_events import BugWorkflowActorSource, advance_bug_workflow
from app.gateway.bug_workflow_models import (
    BugWorkflowListResponse,
    BugWorkflowResponse,
    BugWorkflowSummaryResponse,
    CreateBugWorkflowRequest,
    SubmitClarificationRequest,
)
from app.gateway.bug_workflow_state import PERSISTED_RESUMABLE_STATUSES, BugWorkflowRuntime
from app.gateway.deps import get_current_user, get_thread_store
from deerflow.persistence.bug_workbench import read_bug_workbench

router = APIRouter(prefix="/api/bug-workflows", tags=["bug-workflows"])
logger = logging.getLogger(__name__)
_TERMINAL_WORKFLOW_STATUSES = frozenset(
    {"note_written", "skipped", "cancelled", "analysis_incomplete", "failed", "accepted", "rolled_back"}
)


def _summary_response(workflow: dict) -> BugWorkflowSummaryResponse:
    bug_snapshot = workflow.get("bug_snapshot")
    title = bug_snapshot.get("title") if isinstance(bug_snapshot, dict) else None
    last_event = workflow.get("last_event")
    latest_summary = last_event.get("summary") if isinstance(last_event, dict) else None
    return BugWorkflowSummaryResponse(
        id=workflow.get("id"),
        bug_id=workflow.get("bug_id"),
        status=workflow.get("status"),
        title=title if isinstance(title, str) else None,
        route=workflow.get("route"),
        updated_at=workflow.get("updated_at"),
        latest_summary=latest_summary if isinstance(latest_summary, str) else None,
    )


def _action_source(request: Request) -> BugWorkflowActorSource:
    source = request.headers.get("X-DeerFlow-Bug-Action-Source", "").strip().lower()
    return source if source in {"feishu", "main_agent"} else "workbench"


async def _persist_transition(
    request: Request,
    *,
    workflow_id: str,
    owner_user_id: str,
    workflow: dict[str, Any],
    status: str,
    event_type: str,
    summary: str,
    details: dict[str, Any] | None = None,
    thread_status: Literal["busy", "idle"] = "idle",
) -> dict[str, Any]:
    store = get_thread_store(request)
    runtime = BugWorkflowRuntime(
        store=store,
        workflow_id=workflow_id,
        bug_id=int(workflow["bug_id"]),
        owner_user_id=owner_user_id,
        workflow=workflow,
    )
    await runtime.transition(
        status=status,
        actor_source=_action_source(request),
        guard_cancelled=False,
        event_type=event_type,
        summary=summary,
        thread_status=thread_status,
        **(details or {}),
    )
    updated = runtime.workflow
    if updated["last_event"]["actor_source"] == "workbench" and isinstance(updated.get("channel_key"), str):
        from app.channels.service import get_channel_service

        service = get_channel_service()
        if service is not None:
            try:
                await service.manager.publish_bug_workflow_external_action(updated)
            except Exception:
                # The canonical transition is already persisted. A temporary
                # channel failure must never make the web action look undone.
                logger.warning("Failed to publish Bug workflow transition to Feishu", exc_info=True)
    return updated


def _public_rollback(rollback: dict[str, Any]) -> dict[str, Any]:
    """Expose rollback state without leaking the server-only file snapshot."""
    files = rollback.get("files")
    return {
        "available": bool(rollback.get("available")),
        "completed": bool(rollback.get("completed")),
        "files": [entry["path"] for entry in files if isinstance(entry, dict) and isinstance(entry.get("path"), str)] if isinstance(files, list) else [],
        "reason": rollback.get("reason") if isinstance(rollback.get("reason"), str) else None,
    }


def _response(workflow: dict) -> BugWorkflowResponse:
    try:
        public_workflow = dict(workflow)
        if public_workflow.get("analysis_engine") == "openhands" and isinstance(public_workflow.get("error"), str):
            public_workflow["error"] = _public_bug_workflow_error(
                str(public_workflow.get("failure_kind") or "workflow_failed"),
                public_workflow["error"],
            )
        rollback = public_workflow.get("rollback")
        if isinstance(rollback, dict):
            public_workflow["rollback"] = _public_rollback(rollback)
        attachment_evidence = public_workflow.get("attachment_evidence")
        if isinstance(attachment_evidence, dict):
            # The evidence summary may contain log excerpts or screenshot text.
            # The workbench UI only needs per-file progress; keep the full summary
            # server-side for the router and selected specialist.
            assets = attachment_evidence.get("assets")
            public_workflow["attachment_evidence"] = {
                "assets": [{key: item[key] for key in ("id", "name", "source", "media_type", "size", "status", "error") if key in item} for item in assets if isinstance(item, dict)] if isinstance(assets, list) else []
            }
        source_retrieval = public_workflow.get("source_retrieval")
        if isinstance(source_retrieval, dict):
            # The assembled query repeats ticket and runtime facts. The UI only
            # needs the bounded retrieval plan, current-source candidates, and
            # rejection diagnostics; keep the raw query server-side.
            public_workflow["source_retrieval"] = {
                key: source_retrieval[key]
                for key in (
                    "schema_version",
                    "provider",
                    "repository",
                    "source_revision",
                    "status",
                    "reason",
                    "entries",
                    "rejections",
                    "metrics",
                )
                if key in source_retrieval
            }
        clarification = public_workflow.get("clarification")
        if public_workflow.get("clarification_type") == "video" and isinstance(clarification, dict):
            options = _saved_clarification_options(clarification)
            public_workflow["clarification"] = {
                **clarification,
                "response_mode": str(clarification.get("response_mode") or ("choice" if options else "exact_text")),
                "options": options,
                "allow_free_text": not bool(options),
            }
        if public_workflow.get("handoff_state") == "analysis_draft":
            # The model's pre-decision handoff may mention speculative write
            # scopes. Keep it internal until the backend binds a real target.
            public_workflow["handoff"] = None
            public_workflow["analysis_report"] = None
        return BugWorkflowResponse.model_validate(public_workflow)
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Bug workflow state is invalid") from exc


async def _get_workflow(request: Request, workflow_id: str) -> tuple[str, dict]:
    owner_user_id = await get_current_user(request)
    if not owner_user_id:
        raise HTTPException(status_code=401, detail="Authentication is required")
    record = await get_thread_store(request).get(workflow_id, user_id=owner_user_id)
    workflow = record.get("metadata", {}).get("bug_workflow") if record else None
    if not isinstance(workflow, dict):
        raise HTTPException(status_code=404, detail="Bug workflow not found")
    return owner_user_id, workflow


def _saved_clarification_options(clarification: dict[str, Any]) -> list[dict[str, str]]:
    """Normalize current and legacy saved choice options."""
    options: list[dict[str, str]] = []
    for index, raw in enumerate(clarification.get("options", []), start=1):
        if isinstance(raw, dict):
            option_id = raw.get("id")
            label = raw.get("label")
            value = raw.get("value")
            if all(isinstance(item, str) and item.strip() for item in (option_id, label, value)):
                options.append({"id": option_id.strip(), "label": label.strip(), "value": value.strip()})
        elif isinstance(raw, str) and raw.strip():
            value = raw.strip()
            options.append({"id": f"option_{index}", "label": value, "value": value})
    return options


def _resolve_video_clarification_submission(
    clarification: dict[str, Any],
    *,
    option_id: str | None,
    answer: str,
) -> tuple[str, dict[str, Any]]:
    """Return one saved analyze/skip video decision without free-text inference."""
    options = _saved_clarification_options(clarification)
    compact_answer = answer.strip()
    selected = next((option for option in options if option["id"] == option_id), None)
    if selected is None and not option_id and compact_answer:
        exact_matches = [option for option in options if option["value"] == compact_answer]
        selected = exact_matches[0] if len(exact_matches) == 1 else None
    if selected is None or selected["value"] not in {"analyze", "skip"}:
        raise HTTPException(status_code=422, detail="请选择分析视频或跳过视频。")
    value = selected["value"]
    return value, {"response_mode": "choice", "option_id": selected["id"], "value": value}


@router.post("", response_model=BugWorkflowResponse, status_code=202)
async def create_bug_workflow(body: CreateBugWorkflowRequest, request: Request) -> BugWorkflowResponse:
    """Start read-only investigation, advice delivery, and verified note writeback."""
    owner_user_id = await get_current_user(request)
    if not owner_user_id:
        raise HTTPException(status_code=401, detail="Authentication is required")
    if body.auto_repair:
        raise HTTPException(status_code=422, detail="当前流程仅调查并交付建议，不支持自动修复。")

    workflow_id = (
        f"bug-workflow-{uuid.uuid5(uuid.NAMESPACE_URL, f'{owner_user_id}:{body.idempotency_key}').hex}"
        if body.idempotency_key else f"bug-workflow-{uuid.uuid4().hex}"
    )
    store = get_thread_store(request)
    if body.idempotency_key:
        existing = await store.get(workflow_id, user_id=owner_user_id)
        saved = existing.get("metadata", {}).get("bug_workflow") if isinstance(existing, dict) else None
        if isinstance(saved, dict):
            if saved.get("bug_id") != body.bug_id:
                raise HTTPException(status_code=409, detail="幂等键已用于另一个 Bug")
            return _response(saved)
    # Kept in the persisted schema as ``router_thread_id`` for historical
    # records; new workflows use it only as the deterministic triage context.
    router_thread_id = f"bug-triage-{uuid.uuid4().hex}"
    analysis_thread_id = f"bug-analysis-{uuid.uuid4().hex}"
    note_thread_id = f"bug-note-{uuid.uuid4().hex}"
    workflow = advance_bug_workflow(
        {
            "id": workflow_id,
            "bug_id": body.bug_id,
            "router_thread_id": router_thread_id,
            "analysis_thread_id": analysis_thread_id,
            "note_thread_id": note_thread_id,
            "auto_repair_enabled": False,
            "result_contract_version": 2,
            **({"channel_key": body.channel_key, "channel_owner_user_id": owner_user_id} if body.channel_key else {}),
        },
        status="routing",
        actor_source="feishu" if body.channel_key else "workbench",
        event_type="workflow_started",
        summary="Bug Workbench 已开始读取工单事实",
    )
    metadata: dict[str, Any] = {"bug_workflow": workflow}
    if body.channel_key:
        metadata["bug_workflow_channel_key"] = body.channel_key
    try:
        await store.create(
            workflow_id,
            assistant_id="bug-workflow",
            user_id=owner_user_id,
            display_name=f"ZenTao Bug #{body.bug_id} 分析与建议",
            metadata=metadata,
        )
    except IntegrityError:
        # A concurrent submission with the same key may commit first. Reattach
        # to that exact root instead of launching a second investigation.
        existing = await store.get(workflow_id, user_id=owner_user_id)
        saved = existing.get("metadata", {}).get("bug_workflow") if isinstance(existing, dict) else None
        if not body.idempotency_key or not isinstance(saved, dict) or saved.get("bug_id") != body.bug_id:
            raise
        return _response(saved)
    task = asyncio.create_task(
        run_bug_workflow(
            app=request.app,
            workflow_id=workflow_id,
            bug_id=body.bug_id,
            owner_user_id=owner_user_id,
            router_thread_id=router_thread_id,
            analysis_thread_id=analysis_thread_id,
            note_thread_id=note_thread_id,
        )
    )
    attach_workflow_task(task, workflow_id=workflow_id)
    return _response(workflow)


@router.get("", response_model=BugWorkflowListResponse)
async def list_bug_workflows(request: Request, limit: int = 20) -> BugWorkflowListResponse:
    """List bounded Bug Workbench roots owned by the authenticated user."""
    owner_user_id = await get_current_user(request)
    if not owner_user_id:
        raise HTTPException(status_code=401, detail="Authentication is required")
    result = await read_bug_workbench(
        get_thread_store(request),
        owner_user_id=owner_user_id,
        scope="list",
        limit=max(1, min(limit, 50)),
    )
    return BugWorkflowListResponse(total=result.total, items=[_summary_response(workflow) for workflow in result.items])


@router.get("/active", response_model=BugWorkflowResponse | None)
async def get_active_bug_workflow(channel_key: str, request: Request) -> BugWorkflowResponse | None:
    """Recover the newest resumable IM Bug workflow for one exact chat."""
    owner_user_id = await get_current_user(request)
    if not owner_user_id:
        raise HTTPException(status_code=401, detail="Authentication is required")
    rows = await get_thread_store(request).search(
        metadata={"bug_workflow_channel_key": channel_key},
        limit=20,
        user_id=owner_user_id,
    )
    for row in rows:
        workflow = row.get("metadata", {}).get("bug_workflow")
        if isinstance(workflow, dict) and (
            workflow.get("status") in PERSISTED_RESUMABLE_STATUSES
            or (workflow.get("status") == "awaiting_clarification" and workflow.get("clarification_type") == "video" and workflow.get("clarification_stage") == "pre_analysis")
        ):
            return _response(workflow)
    return None


@router.get("/latest", response_model=BugWorkflowResponse | None)
async def get_latest_bug_workflow(request: Request, channel_key: str | None = None) -> BugWorkflowResponse | None:
    """Return the newest chat-bound or owner-wide Bug Workbench task."""
    owner_user_id = await get_current_user(request)
    if not owner_user_id:
        raise HTTPException(status_code=401, detail="Authentication is required")
    if channel_key:
        rows = await get_thread_store(request).search(
            metadata={"bug_workflow_channel_key": channel_key},
            limit=1,
            user_id=owner_user_id,
        )
    else:
        result = await read_bug_workbench(
            get_thread_store(request),
            owner_user_id=owner_user_id,
            scope="latest",
        )
        return _response(result.items[0]) if result.items else None
    for row in rows:
        metadata = row.get("metadata")
        workflow = metadata.get("bug_workflow") if isinstance(metadata, dict) else None
        if isinstance(workflow, dict):
            return _response(workflow)
    return None


@router.get("/by-bug/{bug_id}/latest", response_model=BugWorkflowResponse | None)
async def get_latest_bug_workflow_by_bug_id(bug_id: int, request: Request) -> BugWorkflowResponse | None:
    """Return the owner's newest canonical workflow for one Bug.

    The batch runner uses this read after an ambiguous create transport failure:
    if Gateway persisted the workflow but the HTTP response was lost, the runner
    resumes that exact root instead of submitting a duplicate analysis.
    """

    owner_user_id = await get_current_user(request)
    if not owner_user_id:
        raise HTTPException(status_code=401, detail="Authentication is required")
    result = await read_bug_workbench(
        get_thread_store(request),
        owner_user_id=owner_user_id,
        scope="by_bug_id",
        bug_id=bug_id,
    )
    return _response(result.items[0]) if result.items else None


@router.get("/{workflow_id}", response_model=BugWorkflowResponse)
async def get_bug_workflow(workflow_id: str, request: Request) -> BugWorkflowResponse:
    _owner_user_id, workflow = await _get_workflow(request, workflow_id)
    return _response(workflow)


@router.post("/{workflow_id}/retry-repair", response_model=BugWorkflowResponse)
async def retry_failed_bug_repair(workflow_id: str, request: Request) -> BugWorkflowResponse:
    """Retired execution endpoint; retained for an explicit old-client response."""
    await _get_workflow(request, workflow_id)
    raise HTTPException(status_code=410, detail="自动修复已停用；请查看分析报告中的修改/处理意见。")


@router.post("/{workflow_id}/retry-note", response_model=BugWorkflowResponse, status_code=202)
async def retry_failed_bug_note(workflow_id: str, request: Request) -> BugWorkflowResponse:
    """Regenerate the scoped note from persisted analysis, then retry writeback."""
    owner_user_id, workflow = await _get_workflow(request, workflow_id)
    failure_kind = str(workflow.get("failure_kind") or "")
    if (
        workflow.get("status") != "failed"
        or failure_kind not in {"note_generation_failed", "note_write_failed"}
        or workflow.get("note_verified") is True
        or not isinstance(workflow.get("analysis_report"), str)
        or not workflow.get("analysis_report")
    ):
        raise HTTPException(status_code=409, detail="当前任务没有可复用分析的失败备注")

    # A failed note may predate the current third/fourth-section contract.
    # Never send its persisted body verbatim on retry. ZenTao history remains
    # the authority for a prior ambiguous POST, so this does not add a second
    # automated backup or restart the source investigation.
    note_thread_id = f"bug-note-{uuid.uuid4().hex}"
    resumed = await _persist_transition(
        request,
        workflow_id=workflow_id,
        owner_user_id=owner_user_id,
        workflow=workflow,
        status="writing_note",
        event_type="note_retried",
        summary="从已保存分析重新生成第三、第四部分备注，不重新调查",
        details={
            "note_thread_id": note_thread_id,
            "note_run_id": None,
            "note_content": None,
            "note_verified": False,
            "failure_kind": None,
            "error": None,
        },
        thread_status="busy",
    )
    task = asyncio.create_task(
        run_bug_workflow(
            app=request.app,
            workflow_id=workflow_id,
            bug_id=int(workflow["bug_id"]),
            owner_user_id=owner_user_id,
            router_thread_id=str(workflow.get("router_thread_id") or f"bug-triage-{uuid.uuid4().hex}"),
            analysis_thread_id=str(workflow.get("analysis_thread_id") or f"bug-analysis-{uuid.uuid4().hex}"),
            note_thread_id=note_thread_id,
            bug_snapshot=workflow.get("bug_snapshot") if isinstance(workflow.get("bug_snapshot"), dict) else None,
            affected_clients=tuple(workflow.get("affected_clients", [])),
            write_note_only=True,
        )
    )
    attach_workflow_task(task, workflow_id=workflow_id)
    return _response(resumed)


@router.post("/{workflow_id}/clarification", response_model=BugWorkflowResponse, status_code=202)
async def submit_bug_workflow_clarification(
    workflow_id: str,
    body: SubmitClarificationRequest,
    request: Request,
) -> BugWorkflowResponse:
    """Resume after the retained pre-analysis video decision."""
    owner_user_id, workflow = await _get_workflow(request, workflow_id)
    if workflow.get("status") != "awaiting_clarification":
        raise HTTPException(status_code=409, detail="Bug workflow is not waiting for a clarification")
    route = workflow.get("route")
    clarification_type = workflow.get("clarification_type")
    if route != "investigation" or clarification_type != "video" or workflow.get("clarification_stage") != "pre_analysis":
        raise HTTPException(status_code=409, detail="Bug workflow has an invalid clarification context")
    saved_clarification = workflow.get("clarification")
    if not isinstance(saved_clarification, dict):
        raise HTTPException(status_code=409, detail="Bug workflow is missing its clarification contract")
    answer, clarification_response = _resolve_video_clarification_submission(
        saved_clarification,
        option_id=body.option_id,
        answer=body.answer,
    )
    router_thread_id = workflow.get("router_thread_id")
    analysis_thread_id = workflow.get("analysis_thread_id")
    router_run_id = workflow.get("router_run_id")
    if not isinstance(router_thread_id, str) or not isinstance(analysis_thread_id, str):
        raise HTTPException(status_code=500, detail="Bug workflow is missing its analysis context")

    note_thread_id = workflow.get("note_thread_id")
    if not isinstance(note_thread_id, str) or not note_thread_id:
        note_thread_id = f"bug-note-{uuid.uuid4().hex}"
    clarification_round = int(workflow.get("clarification_round", 0)) + 1
    resume_details = {
        "note_thread_id": note_thread_id,
        "clarification": None,
        "clarification_type": None,
        "clarification_round": clarification_round,
        "clarification_response": clarification_response,
        "video_analysis_decision": answer,
    }
    summary = "确认分析视频" if answer == "analyze" else "确认跳过视频"
    resumed = await _persist_transition(
        request,
        workflow_id=workflow_id,
        owner_user_id=owner_user_id,
        workflow=workflow,
        status="routing",
        event_type="video_analysis_confirmed",
        summary=summary,
        details=resume_details,
        thread_status="busy",
    )
    task = asyncio.create_task(
        run_bug_workflow(
            app=request.app,
            workflow_id=workflow_id,
            bug_id=workflow["bug_id"],
            owner_user_id=owner_user_id,
            router_thread_id=router_thread_id,
            analysis_thread_id=analysis_thread_id,
            note_thread_id=note_thread_id,
            router_run_id=router_run_id if isinstance(router_run_id, str) else None,
            clarification_round=clarification_round,
            bug_snapshot=workflow.get("bug_snapshot") if isinstance(workflow.get("bug_snapshot"), dict) else None,
            affected_clients=tuple(workflow.get("affected_clients", [])),
        )
    )
    attach_workflow_task(task, workflow_id=workflow_id)
    return _response(resumed)


@router.post("/{workflow_id}/cancel", response_model=BugWorkflowResponse)
async def cancel_bug_workflow(workflow_id: str, request: Request) -> BugWorkflowResponse:
    """Persist cancellation, then interrupt the process-local workflow/Codex handle."""
    owner_user_id, workflow = await _get_workflow(request, workflow_id)
    status = str(workflow.get("status") or "")
    if status == "cancelled":
        return _response(workflow)
    if status in _TERMINAL_WORKFLOW_STATUSES:
        raise HTTPException(status_code=409, detail="Bug workflow has already finished")
    cancelled = await _persist_transition(
        request,
        workflow_id=workflow_id,
        owner_user_id=owner_user_id,
        workflow=workflow,
        status="cancelled",
        event_type="workflow_cancelled",
        summary="取消当前 Bug 分析",
        details={"clarification": None, "awaiting_clarification": False, "cancel_reason": "用户取消当前 Bug 分析"},
    )
    await cancel_workflow_tasks(workflow_id)
    return _response(cancelled)


@router.post("/{workflow_id}/accept", response_model=BugWorkflowResponse)
async def accept_bug_workflow(workflow_id: str, request: Request) -> BugWorkflowResponse:
    owner_user_id, workflow = await _get_workflow(request, workflow_id)
    if workflow.get("status") != "awaiting_acceptance":
        raise HTTPException(status_code=409, detail="Bug workflow is not ready for acceptance")
    accepted = await _persist_transition(
        request,
        workflow_id=workflow_id,
        owner_user_id=owner_user_id,
        workflow=workflow,
        status="accepted",
        event_type="repair_accepted",
        summary="确认修复验收",
    )
    return _response(accepted)


@router.post("/{workflow_id}/rollback", response_model=BugWorkflowResponse)
async def rollback_bug_workflow(workflow_id: str, request: Request) -> BugWorkflowResponse:
    """Restore only the files changed by this completed automated repair."""
    owner_user_id, workflow = await _get_workflow(request, workflow_id)
    if workflow.get("status") != "awaiting_acceptance":
        raise HTTPException(status_code=409, detail="Bug workflow is not ready for rollback")
    rollback = workflow.get("rollback")
    if not isinstance(rollback, dict) or not rollback.get("available"):
        raise HTTPException(status_code=409, detail="This repair has no safe rollback snapshot")
    if rollback.get("completed"):
        raise HTTPException(status_code=409, detail="This repair has already been rolled back")
    try:
        restored = await asyncio.to_thread(rollback_repair, rollback)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    rolled_back = await _persist_transition(
        request,
        workflow_id=workflow_id,
        owner_user_id=owner_user_id,
        workflow=workflow,
        status="rolled_back",
        event_type="repair_rolled_back",
        summary="回退本次自动修复",
        details={"rollback": restored},
    )
    return _response(rolled_back)
