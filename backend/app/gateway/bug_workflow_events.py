"""Canonical, persisted state transitions for Bug Workbench workflows."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

BugWorkflowActorSource = Literal["system", "workbench", "feishu", "main_agent"]

_MAX_EVENTS = 80


def advance_bug_workflow(
    workflow: dict[str, Any],
    *,
    status: str,
    actor_source: BugWorkflowActorSource,
    event_type: str | None = None,
    summary: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a new workflow state with one monotonic, auditable revision."""
    previous_status = str(workflow.get("status") or "")
    revision = max(int(workflow.get("revision") or 0), 0) + 1
    occurred_at = datetime.now(UTC).isoformat()
    updated = {**workflow, **(details or {}), "status": status, "revision": revision, "updated_at": occurred_at}
    event = {
        "revision": revision,
        "event_type": event_type or ("status_changed" if previous_status != status else "state_updated"),
        "from_status": previous_status or None,
        "to_status": status,
        "actor_source": actor_source,
        "summary": (summary or "").strip()[:500] or None,
        "occurred_at": occurred_at,
    }
    events = [item for item in workflow.get("events", []) if isinstance(item, dict)]
    updated["events"] = [*events, event][-_MAX_EVENTS:]
    updated["last_event"] = event
    return updated
