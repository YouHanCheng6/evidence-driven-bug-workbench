"""Main-agent adapters for ZenTao selection and durable Workbench batches."""

from __future__ import annotations

from typing import Annotated, Literal

from langchain.tools import InjectedToolCallId, tool
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.graph import END
from langgraph.types import Command

from deerflow.tools.types import Runtime


@tool("query_zentao_bug_selection")
async def query_zentao_bug_selection_tool(
    runtime: Runtime,
    assignees: Annotated[
        list[str],
        "一个或多个禅道指派人的真实姓名或账号；必须把全部负责人放在同一个字符串数组中并只调用一次，单人也使用单元素数组；只传姓名/账号本体，不得带入‘名下’等语法连接词。",
    ],
    match_kind: Annotated[Literal["auto", "account", "realname"], "默认同时精确匹配姓名/账号；同名歧义时指定 account。"] = "auto",
    product: Annotated[list[str] | None, "可选的一个或多个产品名称/ID；必须使用字符串数组一次传入，不要拆成多次查询。"] = None,
    status: Annotated[str, "禅道原始状态。用户说未解决时使用 active；这表示全部 active，不代表无备注。"] = "active",
    handling_stage: Annotated[
        Literal["all_active", "pending_analysis", "pending_input", "local_pending", "external_pending"],
        "处理阶段：未解决=all_active；待分析/无示例负责人备注=pending_analysis；待确认产品/设计/PRD输入=pending_input；本地可修复=local_pending；需其他技术端配合=external_pending。",
    ] = "all_active",
    note_author: Annotated[str, "处理阶段分类所依据的备注作者；默认示例负责人。"] = "示例负责人",
    opened_from: Annotated[str | None, "可选创建日期下限（含当天），格式 YYYY-MM-DD；仅用户提出日期范围时填写。"] = None,
    opened_to: Annotated[str | None, "可选创建日期上限（含当天），格式 YYYY-MM-DD；仅用户提出日期范围时填写。"] = None,
    limit: Annotated[int | None, "仅用户明确要求前 N 个时填写；不填表示全部匹配结果。"] = None,
    offset: Annotated[int, "完成创建日期过滤并按创建时间全局倒序后的起始偏移。"] = 0,
    include_ids: Annotated[bool, "只问数量时为 false，避免把整批编号塞进模型上下文；问清单时为 true。"] = True,
    save_selection: Annotated[bool, "普通查询必须为 false；只有用户明确要求保存这批编号供稍后运行时才为 true。"] = False,
) -> str:
    """对话中的唯一禅道 Bug 查询入口；一次批量查询全部负责人、原始状态、处理阶段和标准事实，可选保存编号但不启动分析。"""
    from app.gateway.main_agent_bug_selection import query_and_save_bug_selection

    return await query_and_save_bug_selection(
        runtime,
        assignees=assignees,
        match_kind=match_kind,
        product=product,
        status=status,
        handling_stage=handling_stage,
        note_author=note_author,
        opened_from=opened_from,
        opened_to=opened_to,
        limit=limit,
        offset=offset,
        include_ids=include_ids,
        save_selection=save_selection,
    )


@tool("start_selected_bug_workbench_batch")
async def start_selected_bug_workbench_batch_tool(
    runtime: Runtime,
    bug_ids: Annotated[
        list[int] | None,
        "当前用户消息明确列出的 Bug 编号。只要本轮列出了编号，就必须完整传入并只运行这些编号；仅在用户说‘运行这批/运行刚才选中的’而未列编号时省略。",
    ] = None,
    rerun: Annotated[bool, "仅用户明确要求重新运行同一集合时为 true。"] = False,
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
) -> Command:
    """启动串行 Bug 批次并立即结束当前轮；本轮明确编号优先，否则使用已保存集合。"""
    from app.gateway.main_agent_bug_batch import start_selected_bug_batch

    confirmation = await start_selected_bug_batch(runtime, bug_ids=bug_ids, rerun=rerun)
    return Command(
        update={
            "messages": [
                ToolMessage(content=confirmation, tool_call_id=tool_call_id),
                AIMessage(content=confirmation),
            ]
        },
        goto=END,
    )


@tool("read_bug_workbench_batch")
async def read_bug_workbench_batch_tool(
    runtime: Runtime,
    batch_id: Annotated[str | None, "可选批次 ID；不填时读当前会话批次或账号最近批次。"] = None,
    offset: Annotated[int, "批次结果的起始偏移。"] = 0,
    limit: Annotated[int, "本次读取条数，最多 100；批次总数不受此限制。"] = 50,
) -> str:
    """只读获取当前会话最近一次批次的进度和简短端别结果。"""
    from app.gateway.main_agent_bug_batch import read_bug_batch

    return await read_bug_batch(runtime, batch_id=batch_id, offset=offset, limit=limit)


@tool("cancel_bug_workbench_batch")
async def cancel_bug_workbench_batch_tool(
    runtime: Runtime,
    batch_id: Annotated[str | None, "可选批次 ID；用户说停止/停止分析/取消这批且未给 ID 时省略，工具会停止当前会话或账号最近批次。"] = None,
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
) -> Command:
    """真正停止 Bug 批次、排队项、当前本地工作流和 Codex 调查，并立即结束当前轮。"""
    from app.gateway.main_agent_bug_batch import cancel_bug_batch

    confirmation = await cancel_bug_batch(runtime, batch_id=batch_id)
    return Command(
        update={
            "messages": [
                ToolMessage(content=confirmation, tool_call_id=tool_call_id),
                AIMessage(content=confirmation),
            ]
        },
        goto=END,
    )
