"""Main-assistant adapter to the existing Gateway Bug Workbench."""

from typing import Annotated, Literal

from langchain.tools import tool

from deerflow.tools.types import Runtime


@tool("start_bug_workbench")
async def start_bug_workbench_tool(
    runtime: Runtime,
    bug_id: Annotated[int, "用户明确要求分析的禅道 Bug 编号，必须为正整数。"],
) -> str:
    """用户明确要求分析一个 Bug 时启动现有工作台；仅提到编号或询问报告时不要调用。"""
    if bug_id <= 0:
        return "请提供有效的 Bug 编号。"
    # Keep application orchestration outside the harness and investigation loop.
    from app.gateway.main_agent_workbench import start_from_main_agent

    return await start_from_main_agent(runtime, bug_id)


@tool("change_bug_workbench_code")
async def change_bug_workbench_code_tool(
    runtime: Runtime,
    bug_id: Annotated[int, "用户明确指定的 Bug 编号。"],
    action: Annotated[Literal["rollback", "restore"], "rollback 回退该 Bug 最近一次实际修改；restore 恢复该次已回退修改。"],
) -> str:
    """仅在用户明确要求回退或恢复 Bug 代码改动时调用。使用已存快照，不启动分析或重新生成修改。"""
    if bug_id <= 0:
        return "请提供有效的 Bug 编号。"
    from app.gateway.main_agent_workbench import change_code_from_main_agent

    return await change_code_from_main_agent(runtime, bug_id, action)
