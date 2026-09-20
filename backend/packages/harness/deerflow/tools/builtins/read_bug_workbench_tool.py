"""Read-only semantic access to the authenticated owner's Bug Workbench."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Literal

from langchain.tools import tool

from deerflow.persistence.bug_workbench import BugWorkbenchScope, read_bug_workbench
from deerflow.persistence.engine import get_session_factory
from deerflow.persistence.thread_meta import ThreadMetaStore, make_thread_store
from deerflow.runtime.context_keys import BUG_WORKBENCH_OWNER_USER_ID_CONTEXT_KEY
from deerflow.runtime.user_context import resolve_runtime_user_id
from deerflow.tools.types import Runtime


def _format_snapshot(state: Mapping) -> str:
    lines = [
        "<bug_workbench_snapshot>",
        "数据来源：当前账号的 Bug 工作台持久化状态（只读）。",
        f"Bug ID：{state.get('bug_id', '未知')}",
        f"当前状态：{state.get('status', '未知')}",
        f"状态版本：revision {state.get('revision', 0)}",
    ]
    analysis_report = state.get("analysis_report")
    if isinstance(analysis_report, str) and analysis_report.strip():
        lines.append("报告转述规则：展示原报告时保留原文；解释时保留确定性和未验证限制，不新增修复方案。")
        lines.append(f"权威完整分析报告：\n{analysis_report.strip()}")
    elif isinstance(state.get("handoff"), str) and state.get("handoff").strip():
        lines.append(f"分析交接：{state.get('handoff').strip()}")
    for key, label, limit in (
        ("updated_at", "最后更新", 100),
        ("route", "专项类型", 80),
        ("route_reason", "分流原因", 300),
        ("analysis_summary", "分析摘要", 1200),
        ("review_decision", "审核结论", 80),
        ("review_report", "审核报告", 600),
        ("note_content", "禅道备注", 4000),
        ("repair_engine", "修复路径", 80),
        ("repair_report", "修复结果", 800),
        ("completion_report", "完成说明", 500),
        ("error", "错误", 300),
    ):
        value = state.get(key)
        if isinstance(value, str) and value.strip():
            lines.append(f"{label}：{value.strip()[:limit]}")
    snapshot = state.get("bug_snapshot")
    if isinstance(snapshot, Mapping):
        title = snapshot.get("title")
        if isinstance(title, str) and title.strip():
            lines.append(f"标题：{title.strip()[:500]}")
    changed_files = state.get("changed_files")
    if isinstance(changed_files, list) and changed_files:
        lines.append("修改文件：" + "、".join(str(item) for item in changed_files[:8]))
    lines.append("</bug_workbench_snapshot>")
    return "\n".join(lines)


def _format_collection_item(state: Mapping) -> str:
    snapshot = state.get("bug_snapshot")
    title = snapshot.get("title") if isinstance(snapshot, Mapping) else None
    last_event = state.get("last_event")
    latest_summary = last_event.get("summary") if isinstance(last_event, Mapping) else None
    parts = [
        f"Bug #{state.get('bug_id', '未知')}",
        str(state.get("status") or "未知状态"),
        str(state.get("route") or "未分流"),
        str(state.get("updated_at") or "更新时间未知"),
    ]
    if isinstance(title, str) and title.strip():
        parts.append(title.strip()[:300])
    if isinstance(latest_summary, str) and latest_summary.strip():
        parts.append(latest_summary.strip()[:300])
    return "任务：" + " / ".join(parts)


async def _read_bug_workbench_impl(
    *,
    scope: BugWorkbenchScope,
    bug_id: int | None = None,
    limit: int = 20,
    runtime: Runtime | None,
    _store: ThreadMetaStore | None = None,
) -> str:
    if runtime is None:
        return "无法读取 Bug 工作台：缺少当前运行上下文。"
    if scope == "by_bug_id" and (bug_id is None or bug_id <= 0):
        return "scope=by_bug_id 时必须提供 bug_id。"

    context = runtime.context if isinstance(runtime.context, Mapping) else {}
    shared_owner = context.get(BUG_WORKBENCH_OWNER_USER_ID_CONTEXT_KEY)
    owner_user_id = shared_owner.strip() if context.get("is_internal") is True and isinstance(shared_owner, str) and shared_owner.strip() else resolve_runtime_user_id(runtime)
    store = _store or make_thread_store(get_session_factory(), runtime.store)
    result = await read_bug_workbench(
        store,
        owner_user_id=owner_user_id,
        scope=scope,
        limit=limit,
        bug_id=bug_id,
    )
    if not result.items:
        if scope == "by_bug_id":
            return f"当前账号的 Bug 工作台中没有 Bug #{bug_id} 的任务。"
        return "当前账号的 Bug 工作台暂无任务。"
    if scope != "list":
        return _format_snapshot(result.items[0])

    items = "\n".join(_format_collection_item(item) for item in result.items)
    return f"<bug_workbench_collection_snapshot>\n数据来源：当前认证用户的 Bug 工作台持久化任务，不是禅道列表。\n任务总数：{result.total}\n本次返回：{len(result.items)}\n{items}\n</bug_workbench_collection_snapshot>"


@tool("read_bug_workbench")
async def read_bug_workbench_tool(
    runtime: Runtime,
    scope: Annotated[Literal["latest", "list", "by_bug_id"], "读取范围：最新任务、任务列表或指定 Bug ID。"] = "latest",
    bug_id: Annotated[int | None, "scope=by_bug_id 时必填的禅道 Bug ID。"] = None,
    limit: Annotated[int, "list 返回条数，默认 20，最大 50。"] = 20,
) -> str:
    """读取当前登录账号自己的 Bug 工作台任务与进度，只读且不会发起禅道查询。"""
    return await _read_bug_workbench_impl(scope=scope, bug_id=bug_id, limit=limit, runtime=runtime)
