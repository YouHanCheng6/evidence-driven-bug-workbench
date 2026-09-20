"""Durable, bounded batches of existing read-only Bug Workbench workflows."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import httpx

from app.gateway.csrf_middleware import CSRF_COOKIE_NAME, CSRF_HEADER_NAME, generate_csrf_token
from app.gateway.internal_auth import create_internal_auth_headers
from deerflow.persistence.engine import get_session_factory
from deerflow.persistence.thread_meta import ThreadMetaStore, make_thread_store
from deerflow.runtime.context_keys import BUG_WORKBENCH_OWNER_USER_ID_CONTEXT_KEY
from deerflow.runtime.user_context import resolve_runtime_user_id

logger = logging.getLogger(__name__)
_TASKS: dict[str, asyncio.Task[None]] = {}
# Bug Workbench batches are deliberately serialized process-wide.  Holding the
# slot for the whole create-and-poll lifecycle prevents a later item (including
# one from another batch) from starting while an earlier analysis is running.
_GLOBAL_BUG_SLOTS = asyncio.Semaphore(1)
_POLL_SECONDS = 10
_POLL_STALL_SECONDS = 3600
_BATCH_TERMINAL = frozenset(
    {
        "note_written",
        "skipped",
        "cancelled",
        "analysis_incomplete",
        "failed",
        "awaiting_clarification",
        "awaiting_evidence",
        "awaiting_repair_choice",
        "awaiting_acceptance",
        "accepted",
        "rolled_back",
        "workflow_stalled",
    }
)


def _runtime_identity(runtime: Any) -> tuple[str, str, str]:
    context = runtime.context if isinstance(runtime.context, Mapping) else {}
    config = runtime.config if isinstance(runtime.config, Mapping) else {}
    configurable = config.get("configurable") if isinstance(config.get("configurable"), Mapping) else {}
    thread_id = context.get("thread_id") or configurable.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id:
        raise ValueError("当前会话没有持久化线程")
    user_id = resolve_runtime_user_id(runtime)
    shared_owner = context.get(BUG_WORKBENCH_OWNER_USER_ID_CONTEXT_KEY)
    owner = shared_owner.strip() if context.get("is_internal") is True and isinstance(shared_owner, str) and shared_owner.strip() else user_id
    return thread_id, user_id, owner


def _gateway_url() -> str:
    from app.channels.service import get_channel_service

    service = get_channel_service()
    return (service.manager._gateway_url if service is not None else os.environ.get("DEER_FLOW_GATEWAY_URL", "http://127.0.0.1:8001")).rstrip("/")


def _headers(owner: str) -> dict[str, str]:
    csrf = generate_csrf_token()
    headers = create_internal_auth_headers(owner_user_id=owner)
    headers.update({CSRF_HEADER_NAME: csrf, "Cookie": f"{CSRF_COOKIE_NAME}={csrf}", "X-DeerFlow-Bug-Action-Source": "main_agent"})
    return headers


def _short_report_fields(report: str) -> tuple[str, str]:
    fourth = report.split("四、修改范围与其他端风险", 1)[-1] if "四、修改范围与其他端风险" in report else ""
    cooperation_scope = fourth.split("本地代码修改", 1)[0]
    main = re.search(r"(?m)^\s*主要涉及端\s*[:：]\s*(.+)$", cooperation_scope)
    primary = main.group(1).strip()[:100] if main else "报告未给出主要涉及端"
    cooperating = []
    for label, field in (("嵌入式", "嵌入式配合"), ("后端", "后端配合")):
        match = re.search(rf"(?m)^\s*{field}\s*[:：]\s*(.*)$", cooperation_scope)
        if match and match.group(1).strip() and not re.match(r"^(?:无|无需|不需要|未见)", match.group(1).strip()):
            cooperating.append(label)
    return primary, "、".join(cooperating) if cooperating else "无"


async def _save(store: ThreadMetaStore, batch_id: str, owner: str, state: dict[str, Any]) -> None:
    state["updated_at"] = datetime.now(UTC).isoformat()
    await store.update_metadata(batch_id, {"bug_batch": state}, user_id=owner)


def _display_bug_ids(ids: list[int], *, limit: int = 50) -> str:
    shown = "、".join(str(identifier) for identifier in ids[:limit])
    return shown if len(ids) <= limit else f"{shown} 等 {len(ids)} 个"


async def start_selected_bug_batch(
    runtime: Any,
    *,
    bug_ids: list[int] | None = None,
    rerun: bool = False,
) -> str:
    """Start explicit current-turn IDs, otherwise the latest saved selection."""
    try:
        thread_id, user_id, owner = _runtime_identity(runtime)
        store = make_thread_store(get_session_factory(), runtime.store)
        thread = await store.get(thread_id, user_id=user_id)
        metadata = thread.get("metadata") if isinstance(thread, dict) else None
        if bug_ids is not None:
            ids = [identifier for identifier in bug_ids if isinstance(identifier, int) and not isinstance(identifier, bool) and identifier > 0]
            ids = list(dict.fromkeys(ids))
            if not ids:
                return "当前消息的明确编号中没有有效的正整数；未回退到上次保存的集合，也未启动任务。"
            identity = hashlib.sha256(",".join(str(identifier) for identifier in ids).encode()).hexdigest()[:20]
            selection = {
                "id": f"bug-selection-explicit-{identity}",
                "source": "explicit_ids",
                "bug_ids": ids,
                "created_at": datetime.now(UTC).isoformat(),
            }
            if not isinstance(metadata, dict):
                return "当前会话尚未建立持久化线程，不能保存明确编号集合。"
            await store.update_metadata(thread_id, {"zentao_bug_selection": selection}, user_id=user_id)
            metadata = {**metadata, "zentao_bug_selection": selection}
        else:
            selection = metadata.get("zentao_bug_selection") if isinstance(metadata, dict) else None
            if not isinstance(selection, dict) or not isinstance(selection.get("bug_ids"), list):
                return "当前消息没有明确编号，且当前会话没有已保存的禅道 Bug 列表；请先查询并选中编号。"
            ids = [identifier for identifier in selection["bug_ids"] if isinstance(identifier, int) and not isinstance(identifier, bool) and identifier > 0]
            ids = list(dict.fromkeys(ids))
            if not ids:
                return "本次选中 0 个 Bug，没有可启动的工作台任务。"
        selection_source = str(selection.get("source") or "saved_selection")
        prior_id = metadata.get("zentao_bug_batch_id")
        if isinstance(prior_id, str) and not rerun:
            prior = await store.get(prior_id, user_id=owner)
            prior_state = prior.get("metadata", {}).get("bug_batch") if isinstance(prior, dict) else None
            if isinstance(prior_state, dict) and prior_state.get("selection_id") == selection.get("id"):
                return f"这份集合已有批次 {prior_id}（{prior_state.get('status')}），不会重复启动；明确要求重新运行时才创建新批次。"
        source = metadata.get("channel_source")
        source = {key: source[key] for key in ("provider", "chat_id", "connection_id", "topic_id", "thread_ts") if isinstance(source, dict) and isinstance(source.get(key), str)}
        batch_id = f"bug-batch-{uuid.uuid4().hex}"
        state: dict[str, Any] = {
            "id": batch_id,
            "selection_id": selection.get("id"),
            "selection_source": selection_source,
            "assignee": selection.get("assignee"),
            "status": "running",
            "created_at": datetime.now(UTC).isoformat(),
            "channel_source": source,
            "notification_sent": False,
            "items": [{"bug_id": identifier, "workflow_id": None, "status": "queued"} for identifier in ids],
        }
        await store.create(
            batch_id,
            assistant_id="bug-batch",
            user_id=owner,
            display_name=f"禅道 Bug 批次（{len(ids)} 个）",
            metadata={"bug_batch": state},
        )
        await store.update_metadata(thread_id, {"zentao_bug_batch_id": batch_id}, user_id=user_id)
        launch_bug_batch(store, owner, batch_id)
        notification = "完成后会在当前飞书会话发送简短汇总" if source.get("provider") == "feishu" else "完成后可向主 Agent 查询简短汇总"
        return (
            f"已创建批次 {batch_id}，共 {len(ids)} 个 Bug；"
            f"本次实际运行：{_display_bug_ids(ids)}；"
            f"选择来源：{'当前消息明确编号' if selection_source == 'explicit_ids' else '当前会话已保存集合'}；"
            f"后台将按上述顺序逐个运行，前一个结束后才启动下一个；{notification}。"
        )
    except (ValueError, OSError) as exc:
        return f"未启动批量分析：{exc}"


async def read_bug_batch(runtime: Any, *, batch_id: str | None = None, offset: int = 0, limit: int = 50) -> str:
    try:
        thread_id, user_id, owner = _runtime_identity(runtime)
        store = make_thread_store(get_session_factory(), runtime.store)
        thread = await store.get(thread_id, user_id=user_id)
        batch_id = batch_id or (thread.get("metadata", {}).get("zentao_bug_batch_id") if isinstance(thread, dict) else None)
        if not isinstance(batch_id, str):
            # A fresh conversation can still inspect the owner's latest batch.
            search_offset = 0
            while True:
                rows = await store.search(limit=100, offset=search_offset, user_id=owner)
                latest = next((row for row in rows if row.get("assistant_id") == "bug-batch"), None)
                if latest is not None:
                    batch_id = latest.get("thread_id")
                    break
                if len(rows) < 100:
                    return "当前账号尚无 Bug 批次。"
                search_offset += 100
        root = await store.get(batch_id, user_id=owner)
        state = root.get("metadata", {}).get("bug_batch") if isinstance(root, dict) else None
        if not isinstance(state, dict):
            return "当前会话的 Bug 批次已不可读。"
        items = state.get("items") if isinstance(state.get("items"), list) else []
        bounded_offset = max(0, offset)
        bounded_limit = max(1, min(limit, 100))
        rows = [_format_item(item) for item in items[bounded_offset : bounded_offset + bounded_limit] if isinstance(item, dict)]
        return f"批次 {batch_id}：{state.get('status')}；总数 {len(items)}；本次显示 {len(rows)}（从第 {bounded_offset + 1} 个开始）。\n" + "\n".join(rows)
    except (ValueError, OSError) as exc:
        return f"无法读取 Bug 批次：{exc}"


def _format_item(item: Mapping[str, Any]) -> str:
    identifier = item.get("bug_id")
    status = str(item.get("status") or "queued")
    if status == "note_written":
        return f"#{identifier}｜主要涉及端：{item.get('primary_side') or '报告未给出'}｜需配合：{item.get('cooperation') or '无'}"
    if status == "awaiting_clarification":
        return f"#{identifier}｜待补充信息；请到 Bug 工作台处理"
    if status in {"awaiting_evidence", "awaiting_repair_choice", "awaiting_acceptance"}:
        return f"#{identifier}｜{status}；请到 Bug 工作台处理"
    if status == "skipped":
        return f"#{identifier}｜已解决或关闭，跳过"
    if status in {"failed", "analysis_incomplete", "cancelled", "launch_failed", "workflow_stalled", "accepted", "rolled_back"}:
        return f"#{identifier}｜{status}；请到 Bug 工作台查看"
    return f"#{identifier}｜{status}"


async def _notify_finished(owner: str, state: dict[str, Any]) -> bool:
    source = state.get("channel_source")
    if not isinstance(source, dict) or source.get("provider") != "feishu" or not source.get("chat_id"):
        return True  # A web chat reads the completed batch on demand.
    from app.channels.message_bus import InboundMessage
    from app.channels.service import get_channel_service

    service = get_channel_service()
    if service is None:
        return False
    msg = InboundMessage(
        channel_name="feishu",
        chat_id=source["chat_id"],
        user_id=owner,
        owner_user_id=owner,
        connection_id=source.get("connection_id"),
        topic_id=source.get("topic_id"),
        thread_ts=source.get("thread_ts"),
        text="",
    )
    items = [item for item in state.get("items", []) if isinstance(item, dict)]
    completed = sum(item.get("status") == "note_written" for item in items)
    header = f"Bug 批次 {state['id']} 跟踪已结束：共 {len(items)} 个，完成分析 {completed} 个。详细记录请看禅道备注或 Bug 工作台。"
    # Feishu messages are bounded; a large variable-size batch is chunked
    # rather than dropping individual Bug outcomes or sending full reports.
    chunk = header
    for item in items:
        row = _format_item(item)
        if len(chunk) + len(row) + 1 > 7500:
            await service.manager._publish_bug_workflow_message(msg, chunk)
            chunk = f"Bug 批次 {state['id']}（续）：\n{row}"
        else:
            chunk += "\n" + row
    await service.manager._publish_bug_workflow_message(msg, chunk)
    return True


async def _process_item(
    store: ThreadMetaStore,
    owner: str,
    state: dict[str, Any],
    item: dict[str, Any],
    lock: asyncio.Lock,
) -> None:
    async with _GLOBAL_BUG_SLOTS:
        identifier = int(item["bug_id"])
        if item.get("status") in _BATCH_TERMINAL or item.get("status") == "launch_failed":
            return
        if not isinstance(item.get("workflow_id"), str):
            headers = _headers(owner)
            last_error = ""
            for attempt in range(5):
                try:
                    async with httpx.AsyncClient(timeout=30.0) as client:
                        response = await client.post(
                            f"{_gateway_url()}/api/bug-workflows",
                            headers=headers,
                            json={"bug_id": identifier, "auto_repair": False, "idempotency_key": f"{state['id']}:{identifier}"},
                        )
                    response.raise_for_status()
                    payload = response.json()
                    workflow_id = payload.get("id") if isinstance(payload, dict) else None
                    if not isinstance(workflow_id, str):
                        raise ValueError("工作台创建结果没有任务 ID")
                    async with lock:
                        item["workflow_id"] = workflow_id
                        item["status"] = str(payload.get("status") or "routing")
                        await _save(store, state["id"], owner, state)
                    break
                except (httpx.RequestError, httpx.HTTPStatusError, ValueError) as exc:
                    last_error = type(exc).__name__
                    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500:
                        break
                    await asyncio.sleep(min(2**attempt, 15))
            if not isinstance(item.get("workflow_id"), str):
                async with lock:
                    item["status"] = "launch_failed"
                    item["error"] = last_error
                    await _save(store, state["id"], owner, state)
                return
        workflow_id = item["workflow_id"]
        last_marker: tuple[Any, Any, Any] | None = None
        last_progress = time.monotonic()
        while True:
            try:
                async with httpx.AsyncClient(timeout=20.0) as client:
                    response = await client.get(f"{_gateway_url()}/api/bug-workflows/{workflow_id}", headers=_headers(owner))
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("工作台状态不是对象")
                status = str(payload.get("status") or "")
                marker = (status, payload.get("revision"), payload.get("updated_at"))
                if marker != last_marker:
                    last_marker = marker
                    last_progress = time.monotonic()
                if status != item.get("status") or status in _BATCH_TERMINAL:
                    async with lock:
                        item["status"] = status
                        if status == "note_written":
                            item["primary_side"], item["cooperation"] = _short_report_fields(str(payload.get("analysis_report") or ""))
                        await _save(store, state["id"], owner, state)
                if status in _BATCH_TERMINAL:
                    return
            except asyncio.CancelledError:
                raise
            except (httpx.RequestError, httpx.HTTPStatusError, ValueError):
                logger.warning("Could not poll Bug #%s in batch %s", identifier, state["id"], exc_info=True)
            if time.monotonic() - last_progress >= _POLL_STALL_SECONDS:
                async with lock:
                    item["status"] = "workflow_stalled"
                    await _save(store, state["id"], owner, state)
                return
            await asyncio.sleep(_POLL_SECONDS)


async def _run_batch(store: ThreadMetaStore, owner: str, batch_id: str) -> None:
    root = await store.get(batch_id, user_id=owner)
    state = root.get("metadata", {}).get("bug_batch") if isinstance(root, dict) else None
    if not isinstance(state, dict):
        return
    if state.get("status") == "running":
        items = [item for item in state.get("items", []) if isinstance(item, dict)]
        lock = asyncio.Lock()

        for item in items:
            if item.get("status") in _BATCH_TERMINAL or item.get("status") == "launch_failed":
                continue
            while True:
                try:
                    await _process_item(store, owner, state, item, lock)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Bug batch %s could not advance item %s; retrying", batch_id, item.get("bug_id"))
                    await asyncio.sleep(15)
                    continue
                break
        state["status"] = "completed"
        await _save(store, batch_id, owner, state)
    if state.get("status") == "completed" and not state.get("notification_sent"):
        if await _notify_finished(owner, state):
            state["notification_sent"] = True
            await _save(store, batch_id, owner, state)


def launch_bug_batch(store: ThreadMetaStore, owner: str, batch_id: str) -> None:
    existing = _TASKS.get(batch_id)
    if existing is not None and not existing.done():
        return
    task = asyncio.create_task(_run_batch(store, owner, batch_id))
    _TASKS[batch_id] = task

    def finished(done: asyncio.Task[None]) -> None:
        _TASKS.pop(batch_id, None)
        if not done.cancelled() and (error := done.exception()) is not None:
            logger.error("Bug batch %s stopped unexpectedly: %s", batch_id, error)

    task.add_done_callback(finished)


async def resume_pending_bug_batches(store: ThreadMetaStore) -> int:
    """Resume persisted batches after a Gateway restart, without new IDs."""
    offset = 0
    resumed = 0
    while True:
        rows = await store.search(limit=100, offset=offset, user_id=None)
        for row in rows:
            state = row.get("metadata", {}).get("bug_batch") if isinstance(row.get("metadata"), dict) else None
            owner = row.get("user_id")
            if row.get("assistant_id") != "bug-batch" or not isinstance(state, dict) or not isinstance(owner, str):
                continue
            if state.get("status") == "running" or (state.get("status") == "completed" and not state.get("notification_sent")):
                launch_bug_batch(store, owner, state["id"])
                resumed += 1
        if len(rows) < 100:
            break
        offset += 100
    return resumed
