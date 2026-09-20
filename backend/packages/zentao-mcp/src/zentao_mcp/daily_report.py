"""Deterministic, compact input preparation for recurring ZenTao Bug reports."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from .client import ZentaoClient, ZentaoError

_SECTION_FOUR = re.compile(r"(?:^|\n)\s*四[、.]\s*修改范围与其他端风险\s*(.*)", re.S)
_SECTION_END = re.compile(r"\n\s*(?:五[、.]|最终目标|开放边\s*/\s*尚缺证据|代码执行状态|禅道备注)\b")
_COOPERATION_LINE = re.compile(r"(?:^|\n)\s*(?:需|需要)配合(?:端)?\s*[：:]\s*([^\n]+)", re.I)
_NAMED_COOPERATION_LINE = re.compile(r"(?:^|\n)\s*([^\n：:]{1,30}?)(?:需|需要)?配合\s*[：:]", re.I)
_NO_COOPERATION = re.compile(r"^(?:无|无需|不需要|无其他端|无跨端配合)(?:[。；;，,（(]|$)")
_ENDPOINT_ALIASES = (
    ("嵌入式", ("嵌入式", "固件", "设备端")),
    ("后端", ("后端", "服务端", "云端")),
    ("Android", ("Android", "安卓")),
    ("iOS", ("iOS",)),
    ("Harmony", ("Harmony", "鸿蒙")),
    ("Web", ("Web", "网页端", "前端")),
)


def _records(result: dict[str, Any], key: str) -> list[dict[str, Any]]:
    if not result.get("ok"):
        raise RuntimeError(str(result.get("message") or result.get("error") or "禅道读取失败"))
    data = result.get("data")
    value = data.get(key) if isinstance(data, dict) else None
    return [item for item in value or [] if isinstance(item, dict)]


def _total(result: dict[str, Any], fallback: int) -> int:
    data = result.get("data")
    raw = data.get("total") if isinstance(data, dict) else None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return fallback


def _product_key(value: Any) -> str:
    key = re.sub(r"\s+", "", str(value or "")).casefold()
    return key[2:] if key.startswith("家用") and len(key) > 2 else key


def _resolve_product(rows: list[dict[str, Any]], requested: str) -> dict[str, Any]:
    requested_name = requested.strip()
    exact = [row for row in rows if str(row.get("name") or "").strip() == requested_name]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise RuntimeError(f"产品“{requested}”完整等值匹配得到 {len(exact)} 项，日报未生成。")

    # The lead model may helpfully add/drop the business-facing "家用" prefix
    # even when the scheduled prompt carries the canonical ZenTao label. Accept
    # only a unique equality after removing that one prefix. The report always
    # uses the matched ZenTao row's real name, never the model-supplied alias.
    normalized = [row for row in rows if _product_key(row.get("name")) == _product_key(requested_name)]
    if len(normalized) == 1:
        return normalized[0]
    raise RuntimeError(f"产品“{requested}”完整等值/家用前缀匹配得到 {len(normalized)} 项，日报未生成。")


async def _all_products(client: ZentaoClient) -> list[dict[str, Any]]:
    found: dict[int, dict[str, Any]] = {}
    page = 1
    while page <= 100:
        result = await client.read_api("/products", params={"page": page, "limit": 100}, fields=["name"])
        items = _records(result, "products")
        for item in items:
            try:
                found[int(item["id"])] = item
            except (KeyError, TypeError, ValueError):
                continue
        if len(found) >= _total(result, len(found)):
            return list(found.values())
        if not items:
            break
        page += 1
    raise RuntimeError("禅道产品分页未完整返回，日报未生成。")


async def _product_bugs(client: ZentaoClient, product_id: int) -> list[dict[str, Any]]:
    found: dict[int, dict[str, Any]] = {}
    page = 1
    fields = ["assignedTo.account", "assignedTo.realname", "status"]
    while page <= 200:
        result = await client.read_api(
            f"/products/{product_id}/bugs",
            params={"page": page, "limit": 100},
            fields=fields,
        )
        items = _records(result, "bugs")
        for item in items:
            try:
                found[int(item["id"])] = item
            except (KeyError, TypeError, ValueError):
                continue
        if len(found) >= _total(result, len(found)):
            return list(found.values())
        if not items:
            break
        page += 1
    raise RuntimeError(f"禅道产品 {product_id} 的 Bug 分页未完整返回，日报未生成。")


def _assignee_label(item: dict[str, Any], requested: list[str]) -> str | None:
    account = str(item.get("assignedTo.account") or "").strip()
    realname = str(item.get("assignedTo.realname") or "").strip()
    for label in requested:
        candidate = label.strip()
        if candidate == realname or candidate.casefold() == account.casefold():
            return label
    return None


def _latest_note(history: list[dict[str, str]], author: str) -> str | None:
    author_key = author.strip().casefold()
    ordered = sorted(
        history,
        key=lambda item: (str(item.get("date") or ""), str(item.get("id") or "")),
        reverse=True,
    )
    for item in ordered:
        actor = str(item.get("actor") or "").strip()
        comment = str(item.get("comment") or "").strip()
        if actor.casefold() == author_key and comment:
            return comment
    return None


def _section_four(note: str) -> tuple[str, bool]:
    match = _SECTION_FOUR.search(note)
    if match is None:
        return note[:600], False
    section = match.group(1).strip()
    end = _SECTION_END.search(section)
    if end is not None:
        section = section[: end.start()].strip()
    return section[:1200], True


def _cooperation_endpoints(section: str) -> list[str] | None:
    line = _COOPERATION_LINE.search(section)
    values: list[str] = []
    if line is not None:
        value = line.group(1).strip()
        if _NO_COOPERATION.search(value):
            return []
        values.append(value)
    for match in _NAMED_COOPERATION_LINE.finditer(section):
        label = match.group(1).strip()
        if label in {"无", "无需", "不需要", "无其他端", "无跨端"}:
            continue
        values.append(label)
    if not values:
        return None
    endpoints: list[str] = []
    for canonical, aliases in _ENDPOINT_ALIASES:
        if any(re.search(rf"(?<![A-Za-z]){re.escape(alias)}(?![A-Za-z])", value, re.I) for value in values for alias in aliases):
            endpoints.append(canonical)
    return endpoints or [values[0][:80]]


def _empty_group() -> dict[str, Any]:
    return {
        "还没有解决": [],
        "已有修改建议": [],
        "需要其他端配合": {},
    }


def _format_ids(ids: list[int]) -> str:
    return "、".join(f"#{bug_id}" for bug_id in ids) if ids else "无"


def _render_report(
    grouped: dict[str, dict[str, dict[str, Any]]],
    *,
    assignees: list[str],
    products: list[str],
    total: int,
) -> str:
    lines = [f"今日未解决 Bug 共 {total} 个。"]
    for owner in assignees:
        lines.extend(("", f"## {owner}"))
        for product in products:
            bucket = grouped[owner][product]
            unresolved = bucket["还没有解决"]
            advised = bucket["已有修改建议"]
            cooperation = bucket["需要其他端配合"]
            cooperation_ids = sorted({bug_id for ids in cooperation.values() for bug_id in ids}, reverse=True)
            lines.extend(
                (
                    f"### {product}",
                    f"- 还没有解决：{len(unresolved)} 个（{_format_ids(unresolved)}）",
                    f"- 已有修改建议：{len(advised)} 个（{_format_ids(advised)}）",
                    f"- 需要其他端配合：{len(cooperation_ids)} 个（{_format_ids(cooperation_ids)}）",
                )
            )
            for endpoint, ids in cooperation.items():
                lines.append(f"  - {endpoint}：{len(ids)} 个（{_format_ids(ids)}）")
    return "\n".join(lines)


async def build_daily_report_snapshot(
    client: ZentaoClient,
    *,
    assignees: list[str],
    products: list[str],
    status: str,
    note_author: str,
    detail_concurrency: int = 8,
) -> dict[str, Any]:
    """Return compact report facts without exposing full Bug payloads to the model."""
    if not assignees or not products or not note_author.strip() or not status.strip():
        raise ValueError("assignees、products、status 和 note_author 均不能为空")
    if len(assignees) > 20 or len(products) > 20:
        raise ValueError("单次日报最多支持 20 个负责人和 20 个产品")

    product_rows = await _all_products(client)
    resolved_products: list[tuple[str, int]] = []
    for requested in products:
        resolved = _resolve_product(product_rows, requested)
        resolved_products.append((str(resolved["name"]).strip(), int(resolved["id"])))
    resolved_product_ids = [product_id for _, product_id in resolved_products]
    if len(set(resolved_product_ids)) != len(resolved_product_ids):
        raise RuntimeError("多个产品参数解析到了同一个禅道产品，日报未生成。")
    canonical_products = [name for name, _ in resolved_products]

    selected: dict[int, dict[str, Any]] = {}
    for product_name, product_id in resolved_products:
        for item in await _product_bugs(client, product_id):
            if str(item.get("status") or "") != status:
                continue
            owner = _assignee_label(item, assignees)
            if owner is None:
                continue
            bug_id = int(item["id"])
            selected[bug_id] = {"bug_id": bug_id, "owner": owner, "product": product_name}

    semaphore = asyncio.Semaphore(max(1, min(detail_concurrency, 16)))

    async def read_detail(item: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | Exception]:
        async with semaphore:
            try:
                return item, await client._get_bug_for_note(item["bug_id"], stage="daily_report")
            except (RuntimeError, ZentaoError) as exc:
                return item, exc

    details = await asyncio.gather(*(read_detail(item) for item in selected.values()))
    failures = [{"bug_id": item["bug_id"], "error": str(result)} for item, result in details if isinstance(result, Exception)]
    if failures:
        return {"ok": False, "message": "部分 Bug 详情读取失败，未生成不完整日报。", "failed": failures[:20]}

    grouped: dict[str, dict[str, dict[str, Any]]] = {owner: {product: _empty_group() for product in canonical_products} for owner in assignees}
    for item, result in details:
        assert isinstance(result, dict)
        bucket = grouped[item["owner"]][item["product"]]
        note = _latest_note(result.get("history") or [], note_author)
        if note is None:
            bucket["还没有解决"].append(item["bug_id"])
            continue
        section, found = _section_four(note)
        endpoints = _cooperation_endpoints(section if found else note)
        if endpoints:
            for endpoint in endpoints:
                bucket["需要其他端配合"].setdefault(endpoint, []).append(item["bug_id"])
        else:
            # The report is author-presence based. A Bug with the configured
            # author's note is handled unless that note explicitly asks another
            # endpoint to cooperate. Legacy notes may predate the four-part
            # heading; never send their full prose back into model context.
            bucket["已有修改建议"].append(item["bug_id"])

    for owner_groups in grouped.values():
        for bucket in owner_groups.values():
            bucket["还没有解决"].sort(reverse=True)
            bucket["已有修改建议"].sort(reverse=True)
            for ids in bucket["需要其他端配合"].values():
                ids.sort(reverse=True)

    return {
        "ok": True,
        "total": len(selected),
        "report": _render_report(grouped, assignees=assignees, products=canonical_products, total=len(selected)),
    }


__all__ = ["build_daily_report_snapshot"]
