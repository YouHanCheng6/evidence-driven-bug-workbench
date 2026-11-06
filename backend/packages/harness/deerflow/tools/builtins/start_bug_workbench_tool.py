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
    action: Annotated[
        Literal["rollback", "restore", "fetch", "submit"],
        "rollback 回退 example_user 修复库中的最近一次实际修改；restore 恢复该修改；fetch 仅在用户明确说‘拉取 #BugID’时，"
        "把中转修复写入个人本地代码库当前分支并保持未提交；submit 仅在用户明确说‘提交拉取并推送 #BugID’时，在同一个人本地代码库提交、拉取公司代码并推送。",
    ],
) -> str:
    """仅执行用户明确要求的回退、恢复、拉取到个人仓库或正式提交；普通修复先发布到 example_user 中转仓库。"""
    if bug_id <= 0:
        return "请提供有效的 Bug 编号。"
    from app.gateway.main_agent_workbench import change_code_from_main_agent

    return await change_code_from_main_agent(runtime, bug_id, action)
