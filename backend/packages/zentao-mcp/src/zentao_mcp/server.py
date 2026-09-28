"""MCP server entry point for read-only ZenTao resources."""

from __future__ import annotations

import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

from .client import ZentaoClient, ZentaoError
from .daily_report import build_daily_report_snapshot
from .feishu_bug_board import BoardError, FeishuBugBoard, sync_snapshot

mcp = FastMCP("ZenTao Bug Reader")


@mcp.tool()
async def get_bug(bug_id: int) -> dict[str, Any]:
    """Read a ZenTao Bug by numeric ID. This tool never changes data in ZenTao."""
    try:
        return {"ok": True, "bug": await ZentaoClient.from_environment().get_bug(bug_id)}
    except ZentaoError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
async def api_get(
    path: str,
    query: dict[str, str | int | float | bool] | None = None,
    fields: list[str] | None = None,
) -> dict[str, Any]:
    """Read an allowlisted ZenTao REST-v1 resource.

    Pass only a relative resource path such as ``/products`` or
    ``/products/285/bugs`` and optional scalar query parameters.  The server
    supplies authentication, performs only GET requests, rejects credential
    parameters and write/authentication routes, and never follows redirects.
    Optional ``fields`` projects list records locally (e.g. ``id``,
    ``assignedTo.account``, ``status``), preserving total/page/limit. Pagination
    and business filtering remain explicit so reusable skills can learn and
    audit the real ZenTao response shape.
    """
    try:
        return await ZentaoClient.from_environment().read_api(path, params=query, fields=fields)
    except ZentaoError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
async def daily_bug_report_snapshot(
    assignees: list[str],
    products: list[str],
    status: str = "active",
    note_author: str = "示例负责人",
    sync_board: bool = False,
) -> dict[str, Any]:
    """Build one compact scheduled ZenTao daily Bug report snapshot.

    Use this wrapper only when the task explicitly requests the complete
    scheduled report. Conversational lists and handling-stage questions use the
    main Agent's unified ZenTao query tool instead. This tool resolves products
    exactly with a unique optional ``家用`` prefix
    canonicalized back to the real ZenTao name, paginates each product once,
    filters assignees and status deterministically, and reads matching Bug histories
    with bounded concurrency. It returns the complete compact report and never
    exposes note bodies to the model. ``sync_board=true`` additionally writes
    its already classified rows to the configured Feishu Bitable, without any
    extra ZenTao reads. Return ``report`` unchanged and do not
    follow this tool with another tool call when ``ok`` is true.
    """
    try:
        sync_error: str | None = None
        try:
            board = FeishuBugBoard.from_environment() if sync_board else None
        except BoardError as exc:
            board = None
            sync_error = str(exc)
        if sync_board and board is None and not sync_error:
            sync_error = "飞书 Bug 看板尚未配置"
        client = ZentaoClient.from_environment()

        async def publish(rows: list[dict[str, Any]]) -> None:
            nonlocal sync_error
            if board is None:
                return
            try:
                await sync_snapshot(board, rows, zentao_url=client._base_url)
            except (BoardError, httpx.HTTPError, OSError, ValueError) as exc:
                sync_error = str(exc)

        result = await build_daily_report_snapshot(
            client,
            assignees=assignees,
            products=products,
            status=status,
            note_author=note_author,
            snapshot_sink=publish if board is not None else None,
        )
        # Classified rows are an internal reusable projection for the
        # conversational query adapter and optional board sink. Scheduled MCP
        # callers receive only the compact report, as before.
        result.pop("rows", None)
        if result.get("ok") and sync_board:
            if sync_error:
                result["report"] += f"\n\n飞书看板同步失败：{sync_error}（禅道日报仍有效）。"
            else:
                board_url = os.getenv("FEISHU_BUG_BOARD_URL", f"https://service.example.invalid")
                result["report"] += f"\n\n协作看板：{board_url}?table={board.table_id}"
        return result
    except (ValueError, ZentaoError, RuntimeError) as exc:
        return {"ok": False, "error": str(exc)}


def main() -> None:
    """Run the server using the MCP standard-input/output transport."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
