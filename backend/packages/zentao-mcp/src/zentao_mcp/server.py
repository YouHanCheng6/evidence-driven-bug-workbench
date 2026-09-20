"""MCP server entry point for read-only ZenTao resources."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from .client import ZentaoClient, ZentaoError
from .daily_report import build_daily_report_snapshot

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
    note_author: str = "徐昊鹍",
) -> dict[str, Any]:
    """Build one compact, read-only daily Bug report snapshot.

    This tool resolves products exactly with a unique optional ``家用`` prefix
    canonicalized back to the real ZenTao name, paginates each product once,
    filters assignees and status deterministically, and reads matching Bug histories
    with bounded concurrency. It returns the complete compact report and never
    exposes note bodies to the model. Return ``report`` unchanged and do not
    follow this tool with another tool call when ``ok`` is true.
    """
    try:
        return await build_daily_report_snapshot(
            ZentaoClient.from_environment(),
            assignees=assignees,
            products=products,
            status=status,
            note_author=note_author,
        )
    except (ValueError, ZentaoError, RuntimeError) as exc:
        return {"ok": False, "error": str(exc)}


def main() -> None:
    """Run the server using the MCP standard-input/output transport."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
