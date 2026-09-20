"""Compact ZenTao list queries and durable main-agent Bug selections.

The existing private list skill owns the user-facing query semantics. This
application tool executes its verified pagination/filter rules without passing
whole product pages through the model context, then saves the exact ID snapshot
used by a later, explicitly requested Workbench batch.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal

from zentao_mcp.client import ZentaoClient, ZentaoError

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


async def _products(client: ZentaoClient, requested: str | None) -> list[tuple[int, str]]:
    if requested and requested.strip().isdigit():
        identifier = int(requested.strip())
        if identifier <= 0:
            raise ValueError("产品 ID 必须是正整数")
        return [(identifier, requested.strip())]
    products = await _paged_records(client, "/products", "products", ["name"])
    choices = [(int(item["id"]), str(item.get("name") or "")) for item in products]
    if not requested or not requested.strip():
        return choices
    name = requested.strip()
    exact = [item for item in choices if item[1] == name]
    matches = exact or [item for item in choices if name in item[1]]
    if len(matches) != 1:
        labels = "、".join(f"{title}({identifier})" for identifier, title in matches[:12])
        raise ValueError(f"产品名称未唯一定位：{labels or '无匹配'}；请指定完整名称或产品 ID")
    return matches


async def query_and_save_bug_selection(
    runtime: Any,
    *,
    assignee: str,
    match_kind: Literal["auto", "account", "realname"] = "auto",
    product: str | None = None,
    status: str = "active",
    limit: int | None = None,
    offset: int = 0,
    include_ids: bool = True,
) -> str:
    """Query until the requested selection is full, then save its exact IDs."""
    name = assignee.strip()
    if not name or len(name) > 120 or not status.strip() or offset < 0 or (limit is not None and limit <= 0):
        return "请提供指派人姓名、有效状态和正数条数；offset 不能为负数。"
    try:
        thread_id, user_id = _thread_identity(runtime)
        store = make_thread_store(get_session_factory(), runtime.store)
        thread = await store.get(thread_id, user_id=user_id)
        if thread is None:
            return "当前会话尚未建立持久化线程，不能保存待分析列表。"
        client = ZentaoClient.from_environment()
        products = await _products(client, product)
        if not products:
            return "当前禅道连接没有可读取的产品；不能把结果报告成 0 个 Bug。"
        matching: dict[int, dict[str, Any]] = {}
        observed: dict[int, tuple[str, str, str]] = {}
        matched_accounts: set[str] = set()
        required = offset + limit if limit is not None else None
        scan_complete = True
        for product_id, _title in products:
            page = 1
            product_records: set[int] = set()
            while True:
                bugs, total = _page(
                    await client.read_api(
                        f"/products/{product_id}/bugs",
                        params={"page": page, "limit": 100},
                        fields=["assignedTo.account", "assignedTo.realname", "status", "openedDate"],
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
                    account_match = match_kind != "realname" and account.lower() == name.lower()
                    realname_match = match_kind != "account" and realname == name
                    if (account_match or realname_match) and account:
                        matched_accounts.add(account.lower())
                    if (account_match or realname_match) and bug_status == status.strip():
                        matching[raw_id] = bug
                        if required is not None and len(matching) >= required:
                            scan_complete = False
                            break
                if not scan_complete:
                    break
                if len(product_records) >= total:
                    break
                if not bugs or len(product_records) == before:
                    raise ValueError(f"禅道 /products/{product_id}/bugs 第 {page} 页为空或重复，未拉全 {total} 条")
                page += 1
            if not scan_complete:
                break
        if len(matched_accounts) > 1:
            return "该姓名/账号匹配多个禅道账号：" + "、".join(sorted(matched_accounts)) + "。请核实账号并以 match_kind=account 精确重查，不能合并不同人员。"
        # ZenTao exposes the Bug creation timestamp as openedDate.  Use it for
        # the requested newest-first presentation instead of assuming ID order.
        ids = sorted(
            matching,
            key=lambda identifier: (str(matching[identifier].get("openedDate") or ""), identifier),
            reverse=True,
        )
        selected = ids[offset : offset + limit if limit is not None else None]
        selection = {
            "id": f"bug-selection-{uuid.uuid4().hex}",
            "source": "zentao_query",
            "assignee": name,
            "match_kind": match_kind,
            "product": product or "全部可见产品",
            "status": status.strip(),
            "matched_total": len(ids),
            "offset": offset,
            "limit": limit,
            "scan_complete": scan_complete,
            "sort": "openedDate_desc",
            "bug_ids": selected,
            "created_at": datetime.now(UTC).isoformat(),
        }
        await store.update_metadata(thread_id, {"zentao_bug_selection": selection}, user_id=user_id)
    except (ValueError, OSError, ZentaoError) as exc:
        return f"禅道列表未确认完整，未保存待分析集合：{exc}"
    count = len(selected)
    details = "、".join(str(identifier) for identifier in selected) if selected else "无"
    scope = f"完整匹配 {len(ids)} 个" if scan_complete else f"找到本次所需的 {count} 个后已停止继续扫描"
    return (
        f"指派人：{name}；状态：{status.strip()}；产品：{product or '全部可见产品'}。"
        f"{scope}，按创建时间从新到旧本次选中 {count} 个。\n" + (f"Bug 编号：{details}\n" if include_ids else "") + "已保存本次集合；只有用户明确要求运行这些 Bug 才启动工作台。"
    )
