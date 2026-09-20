"""Shared state contract and persistence boundary for Bug Workbench.

This module deliberately contains no model, ZenTao, OpenHands, Feishu, or HTTP
logic.  It is the single backend source of truth for workflow statuses and for
persisting one auditable transition.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, Protocol, TypedDict

from app.gateway.bug_workflow_events import BugWorkflowActorSource, advance_bug_workflow

WorkflowStatus = Literal[
    "routing",
    "analyzing",
    "reviewing",
    "awaiting_evidence",
    "awaiting_clarification",
    "writing_note",
    "analysis_incomplete",
    "note_written",
    "awaiting_repair_choice",
    "repairing",
    "awaiting_acceptance",
    "accepted",
    "rolled_back",
    "skipped",
    "cancelled",
    "failed",
]

BUSY_WORKFLOW_STATUSES = frozenset({"routing", "analyzing", "writing_note", "repairing"})
PERSISTED_RESUMABLE_STATUSES = frozenset(
    {
        "routing",
        "analyzing",
        "writing_note",
    }
)


class BugWorkflowState(TypedDict, total=False):
    workflow_id: str
    bug_id: int
    router_thread_id: str
    router_run_id: str
    analysis_thread_id: str
    origin_analysis_thread_id: str
    analysis_run_id: str
    route: str
    origin_route: str
    route_reason: str
    specialist_agent: str
    note_thread_id: str
    note_run_id: str
    handoff: str
    analysis_report: str
    change_advice: list[dict[str, Any]]
    code_changes: list[dict[str, Any]]
    note_projection: str
    repair_projection: dict[str, Any]
    repair_decision: str
    decision_reason: str
    repair_repository: Literal["sample_mobile_repo", "sample_platform_repo"]
    projection_warnings: list[str]
    handoff_quality_warning: dict[str, Any]
    note_content: str
    note_report: str
    note_verified: bool
    note_write_skipped: bool
    note_write_mode: Literal["created", "rewritten", "reused"]
    note_action_id: str | None
    failure_kind: str
    repair_attempted: bool
    auto_repair_enabled: bool
    repair_thread_id: str
    repair_run_id: str
    repair_engine: str
    repair_report: str
    changed_files: list[str]
    rollback: dict[str, Any]
    awaiting_clarification: bool
    clarification: dict[str, Any]
    clarification_type: str
    analysis_summary: str
    analysis_stage: str
    analysis_stage_started_at: str | None
    handoff_state: str
    cancelled: bool
    analysis_incomplete: bool
    source_evidence: list[str]
    remote_ui_origin_chain: list[dict[str, Any]]
    ui_evidence_graph: dict[str, Any]
    evidence_graph: dict[str, Any]
    ui_ownership_contract: dict[str, Any]
    analysis_engine: str
    review_thread_id: str
    review_run_id: str
    review_decision: str
    review_report: str
    review_target_status: str
    review_target_reason: str
    review_model: str
    review_model_call_count: int
    review_token_usage: dict[str, int]
    external_conversation_id: str
    external_conversations: dict[str, str]
    external_accumulated_cost: float
    external_execution_status: str
    external_request_diagnostics: dict[str, Any]
    external_models: list[str]
    external_model_call_count: int
    external_token_usage: dict[str, int]
    summary_model: str
    codex_thread_id: str
    codex_progress: dict[str, Any]
    summary_model_call_count: int
    summary_token_usage: dict[str, int]
    summary_model_attempts: list[dict[str, Any]]
    summary_handoff_manifest: dict[str, Any]
    summary_fallback_reason: str
    external_repair_framework_generated_report: bool
    investigation_source_evidence: list[str]
    investigation_phase_closure: str
    investigation_trajectory: list[dict[str, Any]]
    investigation_state: dict[str, Any]
    investigation_target_results: list[dict[str, Any]]
    final_targets: list[dict[str, Any]]
    causal_assessment: dict[str, Any]
    acceptance_fact_contract: dict[str, Any]
    investigation_hypotheses: list[dict[str, Any]]
    investigation_open_edges: list[str]
    code_intelligence_trace: list[dict[str, Any]]
    source_retrieval: dict[str, Any]
    source_query_preflight: dict[str, Any]
    navigation_packet: list[dict[str, Any]]
    navigation_rejections: list[dict[str, Any]]
    sufficiency_check: dict[str, Any]
    investigation_scope_violations: list[str]
    investigation_conditional_scope_evidence: list[str]
    investigation_missing_evidence: list[str]
    platform_resolution: dict[str, Any]
    platform_model: str
    platform_model_call_count: int
    platform_token_usage: dict[str, int]
    platform_model_attempts: list[dict[str, Any]]
    reported_clients: list[str]
    investigation_knowledge_context: dict[str, Any]
    confirmed_copy_scope: dict[str, Any]
    triage: dict[str, Any]
    triage_model: str
    triage_model_call_count: int
    triage_token_usage: dict[str, int]
    triage_error: str
    attachment_evidence: dict[str, Any]
    non_video_attachment_evidence: dict[str, Any]
    runtime_pre_scan: dict[str, Any]
    runtime_log_evidence: dict[str, Any]
    log_expert_model: str
    log_expert_model_call_count: int
    log_expert_token_usage: dict[str, int]
    pending_video_assets: list[dict[str, Any]]
    video_analysis_decision: Literal["analyze", "skip", "not_needed"]
    product_clarification_completed: bool


class BugWorkflowStore(Protocol):
    async def get(self, thread_id: str, *, user_id: str) -> dict[str, Any] | None: ...

    async def update_metadata(self, thread_id: str, metadata: dict[str, Any], *, user_id: str) -> Any: ...

    async def update_status(self, thread_id: str, status: str, *, user_id: str) -> Any: ...


class BugWorkflowRuntime:
    """Own one workflow's mutable persisted state without owning business policy."""

    def __init__(
        self,
        *,
        store: BugWorkflowStore,
        workflow_id: str,
        bug_id: int,
        owner_user_id: str,
        workflow: Mapping[str, Any] | None = None,
    ) -> None:
        self.store = store
        self.workflow_id = workflow_id
        self.bug_id = bug_id
        self.owner_user_id = owner_user_id
        self.workflow: dict[str, Any] = dict(workflow or {})
        self.workflow.update({"id": workflow_id, "bug_id": bug_id})

    async def is_cancelled(self) -> bool:
        get_record = getattr(self.store, "get", None)
        if get_record is None:
            return False
        record = await get_record(self.workflow_id, user_id=self.owner_user_id)
        workflow = record.get("metadata", {}).get("bug_workflow") if record else None
        return isinstance(workflow, dict) and workflow.get("status") == "cancelled"

    async def transition(
        self,
        status: str,
        *,
        guard_cancelled: bool = True,
        actor_source: BugWorkflowActorSource = "system",
        event_type: str | None = None,
        summary: str | None = None,
        thread_status: Literal["busy", "idle"] | None = None,
        **details: Any,
    ) -> bool:
        if guard_cancelled and status != "cancelled" and await self.is_cancelled():
            return False
        if self.workflow.get("status") == "analysis_incomplete" and status in {"writing_note", "note_written", "repairing", "awaiting_acceptance", "accepted"}:
            # A historical verified note is not evidence that the current
            # investigation completed. Preserve the terminal analysis state.
            return False
        updated = advance_bug_workflow(
            self.workflow,
            status=status,
            actor_source=actor_source,
            event_type=event_type,
            summary=summary,
            details=details,
        )
        self.workflow.clear()
        self.workflow.update(updated)
        await self.store.update_metadata(
            self.workflow_id,
            {"bug_workflow": self.workflow},
            user_id=self.owner_user_id,
        )
        await self.store.update_status(
            self.workflow_id,
            thread_status or ("busy" if status in BUSY_WORKFLOW_STATUSES else "idle"),
            user_id=self.owner_user_id,
        )
        return True
