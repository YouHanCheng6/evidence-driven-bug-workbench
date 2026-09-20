"""Private runtime context keys shared across DeerFlow runtime components."""

from typing import Final

CURRENT_RUN_PRE_EXISTING_MESSAGE_IDS_KEY: Final[str] = "__deerflow_pre_run_message_ids"

# Trusted channel runs may read a shared Bug Workbench without changing the
# ordinary chat owner used for memory, files, and thread persistence.  Gateway
# admits this key only from internally authenticated callers.
BUG_WORKBENCH_OWNER_USER_ID_CONTEXT_KEY: Final[str] = "bug_workbench_owner_user_id"

# Trusted autonomous workflows can opt out of durable user-memory recall and
# extraction for a short-lived internal run. The current run's checkpoint and
# tool messages remain intact; this only prevents cross-task memory traffic.
SKIP_MEMORY_CONTEXT_KEY: Final[str] = "skip_memory"

# Server-owned marker for a UI specialist source-navigation run. It carries no
# owner or fixed path; tools use it only for invariants shared by every UI Bug.
BUG_UI_ANALYSIS_CONTEXT_KEY: Final[str] = "bug_ui_analysis"

# Compact UI source focus supplied by the autonomous Bug workflow.
BUG_UI_PROBLEM_FOCUS_CONTEXT_KEY: Final[str] = "bug_ui_problem_focus"

# Runtime-map implementation candidates remain auxiliary until current-source
# search selects exactly one of them.
BUG_UI_MAP_SOURCE_CANDIDATES_CONTEXT_KEY: Final[str] = "bug_ui_map_source_candidates"
BUG_UI_ENTRY_SOURCE_PATH_CONTEXT_KEY: Final[str] = "bug_ui_entry_source_path"
BUG_UI_ALLOWED_SOURCE_PATHS_CONTEXT_KEY: Final[str] = "bug_ui_allowed_source_paths"
BUG_UI_ANCHOR_SOURCE_PATH_CONTEXT_KEY: Final[str] = "bug_ui_anchor_source_path"
BUG_UI_ANCHOR_CANDIDATES_CONTEXT_KEY: Final[str] = "bug_ui_anchor_candidates"
BUG_UI_ANCHOR_TRACED_CONTEXT_KEY: Final[str] = "bug_ui_anchor_traced"
BUG_UI_ANCHOR_FOLLOWUP_SOURCE_PATH_CONTEXT_KEY: Final[str] = "bug_ui_anchor_followup_source_path"
BUG_UI_ANCHOR_FOLLOWUP_CANDIDATES_CONTEXT_KEY: Final[str] = "bug_ui_anchor_followup_candidates"
BUG_UI_TRACED_ANCHOR_IDENTIFIERS_CONTEXT_KEY: Final[str] = "bug_ui_traced_anchor_identifiers"
BUG_UI_PENDING_STATE_SEARCH_TERMS_CONTEXT_KEY: Final[str] = "bug_ui_pending_state_search_terms"
BUG_UI_PROVEN_STATE_TERMS_CONTEXT_KEY: Final[str] = "bug_ui_proven_state_terms"
BUG_UI_PENDING_BEHAVIOR_FOCUS_CONTEXT_KEY: Final[str] = "bug_ui_pending_behavior_focus"
