"""Owner-scoped, read-only access to persisted Bug Workbench roots."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from deerflow.persistence.thread_meta import ThreadMetaStore

BugWorkbenchScope = Literal["latest", "list", "by_bug_id"]
_PAGE_SIZE = 100
_MAX_LIST_LIMIT = 50


@dataclass(frozen=True)
class BugWorkbenchReadResult:
    total: int
    items: list[dict]


def _workflow_from_root(row: dict) -> dict | None:
    metadata = row.get("metadata")
    workflow = metadata.get("bug_workflow") if isinstance(metadata, dict) else None
    if row.get("assistant_id") != "bug-workflow" or not isinstance(workflow, dict):
        return None
    return workflow


async def read_bug_workbench(
    store: ThreadMetaStore,
    *,
    owner_user_id: str,
    scope: BugWorkbenchScope,
    limit: int = 20,
    bug_id: int | None = None,
) -> BugWorkbenchReadResult:
    """Read only canonical Workbench roots belonging to one explicit owner."""
    rows: list[dict] = []
    offset = 0
    while True:
        page = await store.search(limit=_PAGE_SIZE, offset=offset, user_id=owner_user_id)
        rows.extend(page)
        if len(page) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE

    roots = [(row, workflow) for row in rows if (workflow := _workflow_from_root(row)) is not None]
    roots.sort(
        key=lambda entry: (
            str(entry[0].get("updated_at") or entry[1].get("updated_at") or ""),
            str(entry[0].get("thread_id") or entry[1].get("id") or ""),
        ),
        reverse=True,
    )
    if scope == "by_bug_id":
        roots = [entry for entry in roots if entry[1].get("bug_id") == bug_id]

    total = len(roots)
    bounded_limit = max(1, min(limit, _MAX_LIST_LIMIT))
    visible = roots[:bounded_limit] if scope == "list" else roots[:1]
    return BugWorkbenchReadResult(total=total, items=[dict(workflow) for _row, workflow in visible])
