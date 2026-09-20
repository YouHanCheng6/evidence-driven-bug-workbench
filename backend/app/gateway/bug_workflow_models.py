"""HTTP models for the Bug Workbench API.

Keeping transport schemas outside the router prevents HTTP projection fields
from leaking into orchestration and provider adapters.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.gateway.bug_workflow_state import WorkflowStatus


class CreateBugWorkflowRequest(BaseModel):
    bug_id: int = Field(gt=0)
    channel_key: str | None = Field(default=None, min_length=1, max_length=512)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=160)
    # Compatibility field: requesting execution is rejected, never silently honored.
    auto_repair: bool = False


class SubmitClarificationRequest(BaseModel):
    answer: str = Field(default="", max_length=2000)
    option_id: str | None = Field(default=None, min_length=1, max_length=64)


class RollbackStatus(BaseModel):
    available: bool
    completed: bool = False
    files: list[str] = []
    reason: str | None = None


class BugWorkflowResponse(BaseModel):
    id: str
    bug_id: int
    status: WorkflowStatus
    router_thread_id: str | None = None
    router_run_id: str | None = None
    analysis_thread_id: str
    analysis_run_id: str | None = None
    analysis_stage: str | None = None
    analysis_stage_started_at: str | None = None
    analysis_engine: Literal["legacy", "openhands", "codex"] | None = None
    external_conversation_id: str | None = None
    external_conversations: dict[str, str] = Field(default_factory=dict)
    external_accumulated_cost: float | None = None
    external_execution_status: str | None = None
    external_request_diagnostics: dict[str, Any] | None = None
    external_models: list[str] = Field(default_factory=list)
    external_model_call_count: int = 0
    external_token_usage: dict[str, int] | None = None
    summary_model: str | None = None
    codex_thread_id: str | None = None
    codex_progress: dict[str, Any] | None = None
    summary_model_call_count: int = 0
    summary_token_usage: dict[str, int] | None = None
    summary_model_attempts: list[dict[str, Any]] = Field(default_factory=list)
    summary_fallback_reason: str | None = None
    review_thread_id: str | None = None
    review_run_id: str | None = None
    review_model: str | None = None
    review_model_call_count: int = 0
    review_token_usage: dict[str, int] | None = None
    route: Literal["ui", "crash", "api", "investigation"] | None = None
    route_reason: str | None = None
    triage: dict[str, Any] | None = None
    triage_model: str | None = None
    triage_model_call_count: int = 0
    triage_token_usage: dict[str, int] | None = None
    platform_resolution: dict[str, Any] | None = None
    platform_model: str | None = None
    platform_model_call_count: int = 0
    platform_token_usage: dict[str, int] | None = None
    platform_model_attempts: list[dict[str, Any]] = Field(default_factory=list)
    bug_snapshot: dict[str, Any] | None = None
    affected_clients: list[str] = Field(default_factory=list)
    reported_clients: list[str] = Field(default_factory=list)
    investigation_knowledge_context: dict[str, Any] | None = None
    specialist_agent: str | None = None
    repair_thread_id: str | None = None
    repair_run_id: str | None = None
    repair_attempted: bool = False
    auto_repair_enabled: bool = False
    change_advice: list[dict[str, Any]] = Field(default_factory=list)
    code_changes: list[dict[str, Any]] = Field(default_factory=list)
    repair_engine: Literal["deerflow", "openhands", "patch_executor"] | None = None
    patch_executor_model: str | None = None
    patch_executor_model_call_count: int = 0
    patch_executor_token_usage: dict[str, int] | None = None
    patch_executor_fallback_reason: str | None = None
    external_repair_conversation_id: str | None = None
    external_repair_accumulated_cost: float | None = None
    external_repair_execution_status: str | None = None
    external_repair_framework_generated_report: bool = False
    external_repair_models: list[str] = Field(default_factory=list)
    external_repair_model_call_count: int = 0
    external_repair_token_usage: dict[str, int] | None = None
    handoff: str | None = None
    analysis_report: str | None = None
    note_projection: str | None = None
    repair_projection: dict[str, Any] | None = None
    repair_decision: Literal["local_repair", "report_only"] | None = None
    decision_reason: str | None = None
    repair_context: dict[str, Any] | None = None
    repair_authorization: dict[str, Any] | None = None
    projection_warnings: list[str] = Field(default_factory=list)
    handoff_quality_warning: dict[str, Any] | None = None
    source_evidence: list[str] = Field(default_factory=list)
    investigation_source_evidence: list[str] = Field(default_factory=list)
    investigation_trajectory: list[dict[str, Any]] = Field(default_factory=list)
    investigation_missing_evidence: list[str] = Field(default_factory=list)
    investigation_scope_violations: list[str] = Field(default_factory=list)
    investigation_conditional_scope_evidence: list[str] = Field(default_factory=list)
    final_targets: list[dict[str, Any]] = Field(default_factory=list)
    causal_assessment: dict[str, Any] | None = None
    acceptance_fact_contract: dict[str, Any] | None = None
    evidence_graph: dict[str, Any] | None = None
    investigation_hypotheses: list[dict[str, Any]] = Field(default_factory=list)
    investigation_open_edges: list[str] = Field(default_factory=list)
    code_intelligence_trace: list[dict[str, Any]] = Field(default_factory=list)
    source_retrieval: dict[str, Any] | None = None
    source_query_preflight: dict[str, Any] | None = None
    navigation_packet: list[dict[str, Any]] = Field(default_factory=list)
    navigation_rejections: list[dict[str, Any]] = Field(default_factory=list)
    sufficiency_check: dict[str, Any] | None = None
    ui_evidence_graph: dict[str, Any] | None = None
    ui_ownership_contract: dict[str, Any] | None = None
    attachment_evidence: dict[str, Any] | None = None
    log_expert_model: str | None = None
    log_expert_model_call_count: int = 0
    log_expert_token_usage: dict[str, int] | None = None
    review_report: str | None = None
    review_decision: Literal["pass", "needs_evidence"] | None = None
    review_target_status: Literal["proven", "needs_confirmation"] | None = None
    review_target_reason: str | None = None
    repair_report: str | None = None
    rollback: RollbackStatus | None = None
    note_thread_id: str | None = None
    note_run_id: str | None = None
    note_content: str | None = None
    note_report: str | None = None
    note_verified: bool | None = None
    note_write_skipped: bool = False
    note_write_mode: Literal["created", "rewritten", "reused"] | None = None
    note_action_id: str | None = None
    repair_choice: Literal["repair", "reanalyze", "end"] | None = None
    changed_files: list[str] = Field(default_factory=list)
    completion_report: str | None = None
    analysis_summary: str | None = None
    handoff_state: Literal["analysis_draft", "repair_ready", "analysis_complete"] | None = None
    clarification: dict[str, Any] | None = None
    clarification_type: Literal["product", "video", "runtime", "technical"] | None = None
    clarification_stage: Literal["pre_analysis", "post_analysis"] | None = None
    confirmed_copy_scope: dict[str, Any] | None = None
    analysis_feedback: str | None = None
    clarification_round: int | None = None
    failure_kind: (
        Literal[
            "analysis_execution_failed",
            "platform_resolution_failed",
            "analysis_protocol_error",
            "summary_execution_failed",
            "post_analysis_interrupted",
            "review_preflight_failed",
            "review_execution_failed",
            "note_generation_failed",
            "note_write_failed",
            "workflow_failed",
        ]
        | None
    ) = None
    error: str | None = None
    revision: int = 0
    updated_at: str | None = None
    last_event: dict[str, Any] | None = None
    events: list[dict[str, Any]] = Field(default_factory=list)


class BugWorkflowSummaryResponse(BaseModel):
    id: str
    bug_id: int
    status: WorkflowStatus
    title: str | None = None
    route: Literal["ui", "crash", "api", "investigation"] | None = None
    updated_at: str | None = None
    latest_summary: str | None = None


class BugWorkflowListResponse(BaseModel):
    total: int
    items: list[BugWorkflowSummaryResponse]
