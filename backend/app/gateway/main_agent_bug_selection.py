"""Compact ZenTao list queries and durable main-agent Bug selections.

The existing private list skill owns the user-facing query semantics. This
application tool executes its verified pagination/filter rules without passing
whole product pages through the model context, then saves the exact ID snapshot
used by a later, explicitly requested Workbench batch.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from zentao_mcp.client import ZentaoClient, ZentaoError
from zentao_mcp.daily_report import build_daily_report_snapshot, resolve_product

from deerflow.persistence.engine import get_session_factory
from deerflow.persistence.thread_meta import make_thread_store
from deerflow.runtime.user_context import resolve_runtime_user_id


def _thread_identity(runtime: Any) -> tuple[str, str]:
    context = runtime.context if isinstance(runtime.context, Mapping) else {}
    config = runtime.config if isinstance(runtime.config, Mapping) else {}
    configurable = config.get("configurable") if isinstance(config.get("configurable"), Mapping) else {}
    thread_id = context.get("thread_id") or configurable.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id:
        raise ValueError("当前会话没有可保存查询结果的线程")
    return thread_id, resolve_runtime_user_id(runtime)


def _page(result: Mapping[str, Any], collection: str) -> tuple[list[dict[str, Any]], int]:
    if result.get("ok") is not True:
        raise ValueError(str(result.get("message") or result.get("error") or "禅道列表读取失败"))
    data = result.get("data")
    if not isinstance(data, Mapping):
        raise ValueError("禅道列表缺少分页数据")
    records = data.get(collection)
    total = data.get("total")
    if not isinstance(records, list) or not isinstance(total, int) or total < 0:
        raise ValueError("禅道列表分页结构不完整，不能宣称已查全")
    return [item for item in records if isinstance(item, dict)], total


async def _paged_records(client: ZentaoClient, path: str, collection: str, fields: list[str]) -> list[dict[str, Any]]:
    records: dict[int, dict[str, Any]] = {}
    page = 1
    while True:
        items, total = _page(
            await client.read_api(path, params={"page": page, "limit": 100}, fields=fields),
            collection,
        )
        before = len(records)
        for item in items:
            raw_id = item.get("id")
            if isinstance(raw_id, int) and raw_id > 0:
                records[raw_id] = item
            elif isinstance(raw_id, str) and raw_id.isdigit() and int(raw_id) > 0:
                records[int(raw_id)] = item
        if len(records) >= total:
            return list(records.values())
        if not items or len(records) == before:
            raise ValueError(f"禅道 {path} 第 {page} 页为空或重复，未拉全 {total} 条")
        page += 1


_LOCAL_TIMEZONE = ZoneInfo("Asia/Shanghai")


async def _products(client: ZentaoClient, requested: list[str] | None) -> list[tuple[int, str]]:
    products = await _paged_records(client, "/products", "products", ["name"])
    choices = [(int(item["id"]), str(item.get("name") or "")) for item in products]
    if requested is None:
        return choices
    values = list(requested)
    if not values or any(not str(value).strip() for value in values):
        raise ValueError("产品名称不能为空")
    by_id = {identifier: title for identifier, title in choices}
    resolved: list[tuple[int, str]] = []
    seen: set[int] = set()
    for value in values:
        name = str(value).strip()
        if name.isdigit():
            identifier = int(name)
            if identifier not in by_id:
                raise ValueError(f"产品 ID {identifier} 不在当前可见产品中")
            item = (identifier, by_id[identifier])
        else:
            try:
                row = resolve_product(products, name)
            except RuntimeError as exc:
                raise ValueError(str(exc)) from exc
            item = (int(row["id"]), str(row.get("name") or "").strip())
        if item[0] not in seen:
            seen.add(item[0])
            resolved.append(item)
    return resolved


def _assignee_names(value: list[str]) -> list[str]:
    values = list(value)
    names: list[str] = []
    seen: set[str] = set()
    for item in values:
        name = str(item).strip()
        key = name.casefold()
        if name and key not in seen:
            seen.add(key)
            names.append(name)
    return names


def _opened_date_range(opened_from: str | None, opened_to: str | None) -> tuple[date | None, date | None]:
    try:
        lower = date.fromisoformat(opened_from.strip()) if opened_from is not None else None
        upper = date.fromisoformat(opened_to.strip()) if opened_to is not None else None
    except ValueError as exc:
        raise ValueError("创建时间范围必须使用 YYYY-MM-DD 格式") from exc
    if lower is not None and upper is not None and lower > upper:
        raise ValueError("创建时间起始日期不能晚于结束日期")
    return lower, upper


def _opened_at(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_LOCAL_TIMEZONE)
    return parsed


def _opened_on_local_date(value: Any) -> date | None:
    parsed = _opened_at(value)
    return parsed.astimezone(_LOCAL_TIMEZONE).date() if parsed is not None else None


def _opened_sort_key(value: Any) -> datetime:
    parsed = _opened_at(value)
    return parsed.astimezone(UTC) if parsed is not None else datetime.min.replace(tzinfo=UTC)


def _format_opened_time(value: Any) -> str:
    parsed = _opened_at(value)
    if parsed is None:
        return "未知"
    local = parsed.astimezone(_LOCAL_TIMEZONE)
    return f"{local.strftime('%Y-%m-%d %H:%M:%S')}（Asia/Shanghai）"


def _opened_in_range(value: Any, lower: date | None, upper: date | None) -> bool:
    if lower is None and upper is None:
        return True
    opened = _opened_on_local_date(value)
    return opened is not None and (lower is None or opened >= lower) and (upper is None or opened <= upper)


def _date_scope_label(lower: date | None, upper: date | None) -> str:
    if lower is None and upper is None:
        return ""
    if lower == upper:
        return f"；创建日期：{lower.isoformat()}"
    return f"；创建日期：{lower.isoformat() if lower else '不限'} 至 {upper.isoformat() if upper else '不限'}"


def _render_bug_facts(rows: list[dict[str, Any]], *, include_details: bool) -> str:
    if not include_details or not rows:
        return ""
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        grouped[str(row["query_assignee"])][str(row["product"])].append(row)
    lines: list[str] = []
    for assignee, products in grouped.items():
        lines.append(f"\n## {assignee}")
        for product, items in products.items():
            lines.append(f"### {product}（{len(items)} 个）")
            for item in items:
                owner = str(item.get("assignee_realname") or item.get("assignee_account") or "未指派")
                account = str(item.get("assignee_account") or "")
                if account and account.casefold() != owner.casefold():
                    owner = f"{owner}（{account}）"
                lines.append(f"- #{item['id']}｜{item.get('title') or '无标题'}｜创建时间：{_format_opened_time(item.get('opened_date'))}｜负责人：{owner}")
    return "\n".join(lines)


async def query_and_save_bug_selection(
    runtime: Any,
    *,
    assignees: list[str],
    match_kind: Literal["auto", "account", "realname"] = "auto",
    product: list[str] | None = None,
    status: str = "active",
    handling_stage: Literal["all_active", "pending_analysis", "pending_input", "local_pending", "external_pending"] = "all_active",
    note_author: str = "示例负责人",
    opened_from: str | None = None,
    opened_to: str | None = None,
    limit: int | None = None,
    offset: int = 0,
    include_ids: bool = True,
    save_selection: bool = False,
) -> str:
    """Query standard Bug facts and optionally save the exact selected IDs."""
    names = _assignee_names(assignees)
    if not names or len(names) > 20 or any(len(name) > 120 for name in names) or not status.strip() or not note_author.strip() or offset < 0 or (limit is not None and limit <= 0):
        return "请提供指派人姓名、有效状态和正数条数；offset 不能为负数。"
    try:
        opened_lower, opened_upper = _opened_date_range(opened_from, opened_to)
        client = ZentaoClient.from_environment()
        products = await _products(client, product)
        if not products:
            return "当前禅道连接没有可读取的产品；不能把结果报告成 0 个 Bug。"
        if handling_stage != "all_active":
            classified_by_id: dict[int, dict[str, Any]] = {}
            product_rows = [{"id": identifier, "name": title} for identifier, title in products]
            for start in range(0, len(products), 20):
                product_chunk = products[start : start + 20]
                snapshot = await build_daily_report_snapshot(
                    client,
                    assignees=names,
                    products=[title for _, title in product_chunk],
                    status=status.strip(),
                    note_author=note_author.strip(),
                    product_rows=product_rows,
                )
                if snapshot.get("ok") is not True:
                    return str(snapshot.get("message") or snapshot.get("error") or "禅道处理阶段查询失败")
                for row in snapshot.get("rows") or []:
                    if not isinstance(row, dict):
                        continue
                    bug_id = int(row["bug_id"])
                    previous = classified_by_id.get(bug_id)
                    if previous is not None and previous.get("category") != row.get("category"):
                        raise ValueError(f"Bug #{bug_id} 跨产品处理阶段不一致，不能确认查询结果")
                    classified_by_id.setdefault(bug_id, row)
            category = {
                "pending_analysis": "待分析",
                "pending_input": "待确认输入",
                "local_pending": "本地待处理",
                "external_pending": "待其他端配合",
            }[handling_stage]
            classified = [
                row
                for row in classified_by_id.values()
                if row.get("category") == category and _opened_in_range(row.get("opened_date"), opened_lower, opened_upper)
            ]
            classified.sort(key=lambda row: (_opened_sort_key(row.get("opened_date")), int(row.get("bug_id") or 0)), reverse=True)
            matched_total = len(classified)
            sliced = classified[offset : offset + limit if limit is not None else None]
            selected = [int(row["bug_id"]) for row in sliced]
            rows = [
                {
                    "id": int(row["bug_id"]),
                    "title": row.get("title"),
                    "opened_date": row.get("opened_date"),
                    "assignee_account": row.get("assignee_account"),
                    "assignee_realname": row.get("assignee_realname"),
                    "product": row.get("product"),
                    "status": row.get("status"),
                    "query_assignee": row.get("owner"),
                }
                for row in sliced
            ]
            if save_selection:
                await _save_selection(
                    runtime,
                    names=names,
                    match_kind=match_kind,
                    product=product,
                    status=status,
                    handling_stage=handling_stage,
                    opened_from=opened_from,
                    opened_to=opened_to,
                    matched_total=matched_total,
                    offset=offset,
                    limit=limit,
                    selected=selected,
                )
            product_label = "、".join(title for _, title in products)
            facts = _render_bug_facts(rows, include_details=include_ids)
            ids_text = "、".join(str(identifier) for identifier in selected) if selected else "无"
            saved = "\n已保存本次集合；只有用户明确要求运行这些 Bug 才启动工作台。" if save_selection else "\n本次为只读查询，未保存待运行集合，也未启动 Bug 工作台。"
            date_scope = _date_scope_label(opened_lower, opened_upper)
            header = f"指派人：{'、'.join(names)}；禅道状态：{status.strip()}；处理阶段：{category}；产品：{product_label}{date_scope}。完整匹配 {matched_total} 个，本次查询到 {len(selected)} 个。"
            return header + (facts if facts else (f"\nBug 编号：{ids_text}" if include_ids else "")) + saved
        matching: dict[int, dict[str, Any]] = {}
        matched_by: dict[int, str] = {}
        product_membership: dict[int, list[str]] = defaultdict(list)
        observed: dict[int, tuple[str, str, str]] = {}
        matched_accounts: dict[str, set[str]] = {name: set() for name in names}
        for product_id, product_title in products:
            page = 1
            product_records: set[int] = set()
            while True:
                bugs, total = _page(
                    await client.read_api(
                        f"/products/{product_id}/bugs",
                        params={"page": page, "limit": 100},
                        fields=["title", "assignedTo.account", "assignedTo.realname", "status", "openedDate"],
                    ),
                    "bugs",
                )
                before = len(product_records)
                for bug in bugs:
                    raw_id = bug.get("id")
                    if isinstance(raw_id, str) and raw_id.isdigit():
                        raw_id = int(raw_id)
                    if not isinstance(raw_id, int) or raw_id <= 0:
                        continue
                    product_records.add(raw_id)
                    account = str(bug.get("assignedTo.account") or "").strip()
                    realname = str(bug.get("assignedTo.realname") or "").strip()
                    bug_status = str(bug.get("status") or "")
                    facts = (account.lower(), realname, bug_status)
                    if raw_id in observed and observed[raw_id] != facts:
                        raise ValueError(f"Bug #{raw_id} 跨产品列表字段不一致，不能确认查询结果")
                    observed[raw_id] = facts
                    matching_name = next(
                        (
                            name
                            for name in names
                            if (match_kind != "realname" and account.casefold() == name.casefold()) or (match_kind != "account" and realname == name)
                        ),
                        None,
                    )
                    if matching_name is not None and account:
                        matched_accounts[matching_name].add(account.casefold())
                    if matching_name is not None and bug_status == status.strip() and _opened_in_range(bug.get("openedDate"), opened_lower, opened_upper):
                        matching[raw_id] = bug
                        matched_by.setdefault(raw_id, matching_name)
                        if product_title not in product_membership[raw_id]:
                            product_membership[raw_id].append(product_title)
                if len(product_records) >= total:
                    break
                if not bugs or len(product_records) == before:
                    raise ValueError(f"禅道 /products/{product_id}/bugs 第 {page} 页为空或重复，未拉全 {total} 条")
                page += 1
        ambiguous = {name: accounts for name, accounts in matched_accounts.items() if len(accounts) > 1}
        if ambiguous:
            details = "；".join(f"{name}：{'、'.join(sorted(accounts))}" for name, accounts in ambiguous.items())
            return f"以下姓名/账号匹配多个禅道账号：{details}。请核实账号并以 match_kind=account 精确重查，不能合并不同人员。"
        # ZenTao exposes the Bug creation timestamp as openedDate.  Use it for
        # the requested newest-first presentation instead of assuming ID order.
        ids = sorted(
            matching,
            key=lambda identifier: (_opened_sort_key(matching[identifier].get("openedDate")), identifier),
            reverse=True,
        )
        selected = ids[offset : offset + limit if limit is not None else None]
        rows = [
            {
                "id": identifier,
                "title": str(matching[identifier].get("title") or "").strip(),
                "opened_date": str(matching[identifier].get("openedDate") or "").strip(),
                "assignee_account": str(matching[identifier].get("assignedTo.account") or "").strip(),
                "assignee_realname": str(matching[identifier].get("assignedTo.realname") or "").strip(),
                "product": "、".join(product_membership[identifier]) or "未知产品",
                "status": str(matching[identifier].get("status") or "").strip(),
                "query_assignee": matched_by[identifier],
            }
            for identifier in selected
        ]
        if save_selection:
            await _save_selection(
                runtime,
                names=names,
                match_kind=match_kind,
                product=product,
                status=status,
                handling_stage=handling_stage,
                opened_from=opened_from,
                opened_to=opened_to,
                matched_total=len(ids),
                offset=offset,
                limit=limit,
                selected=selected,
            )
    except (ValueError, OSError, ZentaoError) as exc:
        return f"禅道列表未确认完整：{exc}"
    count = len(selected)
    details = "、".join(str(identifier) for identifier in selected) if selected else "无"
    product_label = "、".join(product) if product is not None else None
    date_scope = _date_scope_label(opened_lower, opened_upper)
    header = f"指派人：{'、'.join(names)}；禅道状态：{status.strip()}；产品：{product_label or '全部可见产品'}{date_scope}。完整匹配 {len(ids)} 个，按创建时间从新到旧本次查询到 {count} 个。"
    facts = _render_bug_facts(rows, include_details=include_ids)
    saved = "\n已保存本次集合；只有用户明确要求运行这些 Bug 才启动工作台。" if save_selection else "\n本次为只读查询，未保存待运行集合，也未启动 Bug 工作台。"
    return header + (facts if facts else (f"\nBug 编号：{details}" if include_ids else "")) + saved


async def _save_selection(
    runtime: Any,
    *,
    names: list[str],
    match_kind: str,
    product: list[str] | None,
    status: str,
    handling_stage: str,
    opened_from: str | None,
    opened_to: str | None,
    matched_total: int,
    offset: int,
    limit: int | None,
    selected: list[int],
) -> None:
    thread_id, user_id = _thread_identity(runtime)
    store = make_thread_store(get_session_factory(), runtime.store)
    thread = await store.get(thread_id, user_id=user_id)
    if thread is None:
        raise ValueError("当前会话尚未建立持久化线程，不能保存待分析列表。")
    product_value = "、".join(product) if product is not None else None
    selection = {
        "id": f"bug-selection-{uuid.uuid4().hex}",
        "source": "zentao_query",
        "assignee": names[0] if len(names) == 1 else "、".join(names),
        "assignees": names,
        "match_kind": match_kind,
        "product": product_value or "全部可见产品",
        "status": status.strip(),
        "handling_stage": handling_stage,
        "opened_from": opened_from,
        "opened_to": opened_to,
        "matched_total": matched_total,
        "offset": offset,
        "limit": limit,
        "scan_complete": True,
        "sort": "openedDate_desc",
        "bug_ids": selected,
        "created_at": datetime.now(UTC).isoformat(),
    }
    await store.update_metadata(thread_id, {"zentao_bug_selection": selection}, user_id=user_id)
