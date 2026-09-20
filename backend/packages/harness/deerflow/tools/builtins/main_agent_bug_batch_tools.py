"""Main-agent adapters for ZenTao selection and durable Workbench batches."""

from __future__ import annotations

from typing import Annotated, Literal

from langchain.tools import tool

from deerflow.tools.types import Runtime


@tool("query_zentao_bug_selection")
async def query_zentao_bug_selection_tool(
    runtime: Runtime,
    assignee: Annotated[str, "禅道指派人的真实姓名或账号，不是当前登录账号。"],
    match_kind: Annotated[Literal["auto", "account", "realname"], "默认同时精确匹配姓名/账号；同名歧义时指定 account。"] = "auto",
    product: Annotated[str | None, "可选产品名称或 ID；不指定时查询全部可见产品。"] = None,
    status: Annotated[str, "禅道状态；未解决使用 active。"] = "active",
    limit: Annotated[int | None, "需要前多少个时填写；不填表示全部匹配结果。"] = None,
    offset: Annotated[int, "按 Bug ID 从新到旧的起始偏移。"] = 0,
    include_ids: Annotated[bool, "只问数量时为 false，避免把整批编号塞进模型上下文；问清单时为 true。"] = True,
) -> str:
    """只读查询指定姓名的禅道 Bug，分页查全并保存本次精确编号集合；不启动分析。"""
    from app.gateway.main_agent_bug_selection import query_and_save_bug_selection

    return await query_and_save_bug_selection(
        runtime,
        assignee=assignee,
        match_kind=match_kind,
        product=product,
        status=status,
        limit=limit,
        offset=offset,
        include_ids=include_ids,
    )


@tool("start_selected_bug_workbench_batch")
async def start_selected_bug_workbench_batch_tool(
    runtime: Runtime,
    bug_ids: Annotated[
        list[int] | None,
        "当前用户消息明确列出的 Bug 编号。只要本轮列出了编号，就必须完整传入并只运行这些编号；仅在用户说‘运行这批/运行刚才选中的’而未列编号时省略。",
    ] = None,
    rerun: Annotated[bool, "仅用户明确要求重新运行同一集合时为 true。"] = False,
) -> str:
    """启动串行 Bug 批次；本轮明确编号优先，否则使用当前会话已保存集合。"""
    from app.gateway.main_agent_bug_batch import start_selected_bug_batch

    return await start_selected_bug_batch(runtime, bug_ids=bug_ids, rerun=rerun)


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
