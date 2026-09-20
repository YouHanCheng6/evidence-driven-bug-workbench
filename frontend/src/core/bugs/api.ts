import { fetch } from "@/core/api/fetcher";
import { getBackendBaseURL } from "@/core/config";

export type BugWorkflowStatus =
  | "routing"
  | "analyzing"
  | "reviewing"
  | "awaiting_evidence"
  | "awaiting_clarification"
  | "writing_note"
  | "analysis_incomplete"
  | "awaiting_repair_choice"
  | "note_written"
  // Kept so a previously saved repair workflow remains readable.
  | "repairing"
  | "awaiting_acceptance"
  | "accepted"
  | "rolled_back"
  | "skipped"
  | "cancelled"
  | "failed";

export type BugWorkflow = {
  id: string;
  bug_id: number;
  status: BugWorkflowStatus;
  revision?: number;
  updated_at?: string | null;
  last_event?: {
    revision?: number;
    event_type?: string;
    from_status?: string | null;
    to_status?: string;
    actor_source?: "system" | "workbench" | "feishu" | "main_agent";
    summary?: string | null;
    occurred_at?: string;
  } | null;
  router_thread_id?: string | null;
  router_run_id?: string | null;
  analysis_thread_id: string;
  analysis_run_id?: string | null;
  analysis_engine?: "legacy" | "openhands" | "codex" | null;
  external_conversation_id?: string | null;
  external_conversations?: Record<string, string>;
  external_accumulated_cost?: number | null;
  external_execution_status?: string | null;
  external_request_diagnostics?: {
    route?: string;
    attempts?: number;
    timeouts?: number;
    faults?: number;
    last_failure?: string;
    last_outcome?: string;
    last_elapsed_ms?: number;
    message_count?: number;
    input_chars?: number;
    tool_count?: number;
  } | null;
  external_models?: string[];
  external_model_call_count?: number;
  external_token_usage?: {
    prompt_tokens: number;
    completion_tokens: number;
    total_tokens: number;
    cache_read_tokens: number;
    cache_write_tokens: number;
    reasoning_tokens: number;
  } | null;
  summary_model?: string | null;
  codex_thread_id?: string | null;
  codex_progress?: {
    thread_id?: string;
    turn_id?: string;
    phase?: string;
    elapsed_seconds?: number;
    completed_items?: number;
    input_tokens?: number;
    output_tokens?: number;
  } | null;
  summary_model_call_count?: number;
  summary_token_usage?: {
    input_tokens?: number;
    output_tokens?: number;
    total_tokens?: number;
  } | null;
  summary_model_attempts?: Array<{
    model?: string;
    outcome?: string;
    error_type?: string;
    finish_reason?: string;
  }>;
  summary_fallback_reason?: string | null;
  review_thread_id?: string | null;
  review_run_id?: string | null;
  review_model?: string | null;
  review_model_call_count?: number;
  review_token_usage?: {
    input_tokens?: number;
    output_tokens?: number;
    total_tokens?: number;
  } | null;
  route?: "ui" | "crash" | "api" | "investigation" | null;
  route_reason?: string | null;
  triage?: {
    problem_type?: "ui" | "crash" | "api" | "unknown";
    ui_subtype?:
      | "copy"
      | "layout_behavior"
      | "visual"
      | "interaction"
      | "unknown"
      | null;
    observed_clients?: string[];
    confidence?: "high" | "medium" | "low";
    reason?: string;
    direction?: string;
    evidence_need?: "visual_only" | "include_technical";
    investigation_focus?: {
      scenario: string;
      observed_behavior: string;
      expected_behavior: string;
      acceptance_details: string[];
      initial_hypotheses: string[];
      open_edges: string[];
      evidence_focuses: string[];
      observed_outcome: "mismatch" | "satisfied" | "unknown";
    } | null;
    // Historical workflows may still contain preliminary targets. New runs
    // discover final_targets only after the shared investigation.
    investigation_targets?: Array<{
      target_id: string;
      actual_behavior: string;
      target_behavior: string;
      fact_basis: string;
      target_source:
        | "expected"
        | "title"
        | "description"
        | "steps_or_actual"
        | "attachment";
      evidence_goal: string;
    }>;
    product_decision?: {
      question: string;
      reason: string;
      impact: string;
      options: Array<{ id: string; label: string; value: string }>;
    } | null;
    needs_product_confirmation?: boolean;
    display_name?: string;
  } | null;
  triage_model?: string | null;
  triage_model_call_count?: number;
  triage_token_usage?: {
    input_tokens?: number;
    output_tokens?: number;
    total_tokens?: number;
  } | null;
  platform_resolution?: {
    reported_clients?: Array<"android" | "ios" | "harmony">;
    repository_family?: "classic_mobile" | "harmony";
    primary_repository?: "sample_mobile_repo" | "sample_platform_repo";
    investigation_mode?:
      | "android"
      | "ios"
      | "android_ios_shared"
      | "classic_shared_unknown"
      | "harmony";
    client_scope_status?: "explicit" | "module_fallback";
    evidence?: Array<{ source?: string; quote?: string }>;
    reason?: string;
  } | null;
  platform_model?: string | null;
  platform_model_call_count?: number;
  platform_token_usage?: {
    input_tokens?: number;
    output_tokens?: number;
    total_tokens?: number;
  } | null;
  affected_clients?: string[];
  reported_clients?: string[];
  investigation_knowledge_context?: Record<string, unknown> | null;
  specialist_agent?: string | null;
  repair_thread_id?: string | null;
  repair_run_id?: string | null;
  repair_engine?: "deerflow" | "openhands" | "patch_executor" | null;
  patch_executor_model?: string | null;
  patch_executor_model_call_count?: number;
  patch_executor_token_usage?: {
    input_tokens?: number;
    output_tokens?: number;
    total_tokens?: number;
  } | null;
  patch_executor_fallback_reason?: string | null;
  external_repair_conversation_id?: string | null;
  external_repair_accumulated_cost?: number | null;
  external_repair_execution_status?: string | null;
  external_repair_models?: string[];
  external_repair_model_call_count?: number;
  external_repair_token_usage?: {
    prompt_tokens?: number;
    completion_tokens?: number;
    total_tokens?: number;
    cache_read_tokens?: number;
    cache_write_tokens?: number;
    reasoning_tokens?: number;
  } | null;
  handoff?: string | null;
  analysis_report?: string | null;
  note_projection?: string | null;
  repair_projection?: Record<string, unknown> | null;
  repair_decision?: "local_repair" | "report_only" | null;
  decision_reason?: string | null;
  repair_context?: Record<string, unknown> | null;
  repair_authorization?: Record<string, unknown> | null;
  projection_warnings?: string[];
  handoff_quality_warning?: {
    total_length: number;
    target_min: number;
    target_max: number;
    section_lengths: Record<string, number>;
    oversized_sections: Array<{
      section: string;
      length: number;
      limit: number;
    }>;
  } | null;
  source_evidence?: string[];
  investigation_source_evidence?: string[];
  investigation_missing_evidence?: string[];
  investigation_scope_violations?: string[];
  investigation_conditional_scope_evidence?: string[];
  final_targets?: Array<Record<string, unknown>>;
  causal_assessment?: Record<string, unknown> | null;
  evidence_graph?: Record<string, unknown> | null;
  investigation_hypotheses?: Array<Record<string, unknown>>;
  investigation_open_edges?: string[];
  code_intelligence_trace?: Array<Record<string, unknown>>;
  source_retrieval?: {
    schema_version?: number;
    provider?: string;
    repository?: string;
    source_revision?: string;
    status?: "ready" | "no_match" | "disabled" | "unavailable" | string;
    reason?: string;
    entries?: Array<{
      path?: string;
      entry_line?: number;
      view_range?: number[];
      symbol?: string;
      snippet?: string;
      score?: number;
      current_source_verified?: boolean;
      retrieval_stage?: "exact_anchor" | "concept_fallback" | "concept_expansion" | "module_architecture" | string;
      candidate_kind?: "implementation" | "resource" | "other" | string;
      product_scope?: string;
    }>;
    rejections?: Array<{ path?: string; reason?: string }>;
    metrics?: {
      query_chars?: number;
      raw_candidate_count?: number;
      ranked_candidate_count?: number;
      entry_count?: number;
      packet_chars?: number;
      anchor_count?: number;
      exact_anchors?: string[];
      fallback_concept_count?: number;
      fallback_concepts?: string[];
      product_scope_tokens?: string[];
      architecture_status?: string;
      architecture_model?: string;
      architecture_module_count?: number;
      retrieval_mode?: "exact_anchor" | "concept_fallback" | "module_architecture" | "layered" | "none" | string;
      semantic_status?: string;
    };
    architecture_resolution?: {
      status?: string;
      reason?: string;
      model?: string;
      config_path?: string;
      modules?: Array<{ name?: string; nickname?: string; implementation_paths?: string[] }>;
    };
  } | null;
  source_query_preflight?: Record<string, unknown> | null;
  navigation_packet?: Array<Record<string, unknown>>;
  navigation_rejections?: Array<Record<string, unknown>>;
  sufficiency_check?: {
    enough?: boolean;
    status?:
      | "sufficient"
      | "repository_gap"
      | "external_gap"
      | "unclear"
      | "check_failed";
    advice?: string;
    current_gap?: string;
    gap_kind?: "none" | "repository_local" | "external_evidence" | "unclear";
    continuation_allowed?: boolean;
    next_source_hint?: string;
    boundary_status?: string;
    coverage?: Record<string, unknown>;
    verified_source_references?: string[];
    continued?: boolean;
    context_condensed?: boolean;
  } | null;
  ui_evidence_graph?: Record<string, unknown> | null;
  ui_ownership_contract?: {
    state?: "owner_locked" | "target_locked" | "unresolved";
    observed_clients?: string[];
    implementation_owner?: string;
    execution_anchor?: { path?: string; line?: number; role?: string } | null;
    repair_targets?: Array<{ path?: string; line?: number; role?: string }>;
    implementation_consumers?: string[];
    excluded_candidates?: Array<{ owner?: string; reason?: string }>;
    missing_evidence?: string[];
  } | null;
  attachment_evidence?: {
    summary?: string;
    assets?: Array<{
      id?: string;
      name: string;
      source?: "inline_image" | "attachment" | string;
      media_type?: string;
      size?: number | null;
      status:
        | "queued"
        | "downloading"
        | "downloaded"
        | "processed"
        | "awaiting_confirmation"
        | "skipped"
        | "type_mismatch"
        | "failed";
      path?: string;
      error?: string;
    }>;
  } | null;
  review_report?: string | null;
  review_decision?: "pass" | "needs_evidence" | null;
  review_target_status?: "proven" | "needs_confirmation" | null;
  review_target_reason?: string | null;
  repair_report?: string | null;
  repair_attempted?: boolean;
  change_advice?: ChangeAdvice[];
  code_changes?: Array<{
    target_id?: string;
    source_id?: string;
    source_ref: string;
    before: string;
    after: string;
    effect: string;
    preserves: string;
  }>;
  rollback?: {
    available?: boolean;
    completed?: boolean;
    files?: string[];
    reason?: string;
  } | null;
  note_thread_id?: string | null;
  note_run_id?: string | null;
  note_content?: string | null;
  note_report?: string | null;
  note_verified?: boolean | null;
  note_write_skipped?: boolean;
  repair_choice?: "repair" | "reanalyze" | "end" | null;
  changed_files?: string[];
  completion_report?: string | null;
  analysis_summary?: string | null;
  handoff_state?: "analysis_draft" | "repair_ready" | "analysis_complete" | null;
  clarification?: {
    question?: string;
    response_mode?: "choice" | "exact_text" | "copy_scope";
    copy_scope_kind?: "default" | "harmony";
    options?: Array<{
      id: string;
      label: string;
      value: string;
    }>;
    allow_free_text?: boolean;
    decision_reason?: string;
    decision_impact?: string;
    input_label?: string;
    input_hint?: string;
    current_value?: string;
    items?: Array<{
      id: string;
      observed_clients?: string[];
      page?: string;
      actual_text?: string;
      expected_text?: string;
    }>;
  } | null;
  clarification_type?: "product" | "video" | "runtime" | "technical" | null;
  clarification_stage?: "pre_analysis" | "post_analysis" | null;
  clarification_round?: number | null;
  failure_kind?:
    | "analysis_execution_failed"
    | "platform_resolution_failed"
    | "analysis_protocol_error"
    | "summary_execution_failed"
    | "post_analysis_interrupted"
    | "review_preflight_failed"
    | "review_execution_failed"
    | "note_generation_failed"
    | "note_write_failed"
    | "workflow_failed"
    | null;
  error?: string | null;
};

async function readWorkflow(response: Response): Promise<BugWorkflow> {
  if (response.ok) {
    return response.json() as Promise<BugWorkflow>;
  }
  const body = (await response.json().catch(() => ({}))) as { detail?: string };
  throw new Error(
    body.detail ?? `Bug workflow request failed: ${response.status}`,
  );
}

export interface ChangeAdvice {
  target_id?: string;
  target_behavior?: string;
  kind: "local_change" | "external_action" | "needs_evidence" | "no_change";
  team: "backend" | "embedded" | "product" | "client" | "multiple" | "unknown";
  problem: string;
  action: string;
  basis: string;
  condition: string;
  decision_impact: string;
  acceptance: string;
  risk: string;
  evidence: string[];
  implementation_status: "not_implemented_not_verified";
}

export async function createBugWorkflow(bugId: string): Promise<BugWorkflow> {
  return readWorkflow(
    await fetch(`${getBackendBaseURL()}/api/bug-workflows`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ bug_id: Number(bugId), auto_repair: false }),
    }),
  );
}

export async function fetchBugWorkflow(
  workflowId: string,
): Promise<BugWorkflow> {
  return readWorkflow(
    await fetch(`${getBackendBaseURL()}/api/bug-workflows/${workflowId}`),
  );
}

export async function acceptBugWorkflow(
  workflowId: string,
): Promise<BugWorkflow> {
  return readWorkflow(
    await fetch(
      `${getBackendBaseURL()}/api/bug-workflows/${workflowId}/accept`,
      {
        method: "POST",
      },
    ),
  );
}

export async function rollbackBugWorkflow(
  workflowId: string,
): Promise<BugWorkflow> {
  return readWorkflow(
    await fetch(
      `${getBackendBaseURL()}/api/bug-workflows/${workflowId}/rollback`,
      { method: "POST" },
    ),
  );
}

export async function retryBugWorkflowNote(
  workflowId: string,
): Promise<BugWorkflow> {
  return readWorkflow(
    await fetch(
      `${getBackendBaseURL()}/api/bug-workflows/${workflowId}/retry-note`,
      { method: "POST" },
    ),
  );
}

export async function submitBugWorkflowClarification(
  workflowId: string,
  submission: {
    answer?: string;
    optionId?: string;
  },
): Promise<BugWorkflow> {
  return readWorkflow(
    await fetch(
      `${getBackendBaseURL()}/api/bug-workflows/${workflowId}/clarification`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          answer: submission.answer ?? "",
          option_id: submission.optionId,
        }),
      },
    ),
  );
}
