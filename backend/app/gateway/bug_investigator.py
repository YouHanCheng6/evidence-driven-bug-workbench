"""Deterministic ticket and visual-fact preparation for Bug Workbench."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

_EMPTY_TICKET_FIELD_PATTERN = re.compile(
    r"[\[【]?\s*(?:手机型号与系统|测试设备\s*sn|测试时间|预置条件|测试步骤|复现概率|重现概率|实测结果|实际结果|预期结果|恢复方法|问题恢复方法)\s*[\]】]?\s*[:：]?",
    re.IGNORECASE,
)
_CLIENTS = frozenset({"android", "ios", "harmony"})
_RNSDK_CONTAINER_ROOT = "/mnt/repos/sample_mobile_repo"
_HARMONY_CONTAINER_ROOT = "/mnt/repos/sample_platform_repo"

@dataclass(frozen=True)
class InvestigationKnowledgeContext:
    """Deterministic ticket facts and repository scope supplied to Codex."""

    query_facts: dict[str, Any]
    allowed_repository_roots: tuple[str, ...] = (_RNSDK_CONTAINER_ROOT,)
    conditional_repository_roots: tuple[str, ...] = ()
    diagnostics: tuple[dict[str, Any], ...] = ()
    error: str | None = None
    business_knowledge: dict[str, Any] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        return {
            "investigation_mode": self.investigation_mode(),
            "query_facts": self.query_facts,
            "allowed_repository_roots": list(self.allowed_repository_roots),
            "conditional_repository_roots": list(self.conditional_repository_roots),
            "diagnostics": list(self.diagnostics),
            "business_knowledge": self.business_knowledge,
            **({"error": self.error} if self.error else {}),
        }

    def investigation_mode(self) -> str:
        """Resolve one mutually exclusive platform investigation mode."""
        explicit_mode = str(self.query_facts.get("investigation_mode") or "").strip()
        if explicit_mode in {"android", "ios", "android_ios_shared", "classic_shared_unknown", "harmony"}:
            return explicit_mode
        clients = _investigation_clients(
            self.query_facts.get("observed_clients", []),
            self.query_facts.get("reported_clients", []),
        )
        client_set = set(clients)
        if "harmony" in client_set:
            return "harmony"
        if {"android", "ios"}.issubset(client_set):
            return "android_ios_shared"
        if "android" in client_set:
            return "android"
        if "ios" in client_set:
            return "ios"
        return "shared"

    def compact_payload(self, *, active_target: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Return only live deterministic facts; Tabby owns source navigation."""
        del active_target
        return {
            "investigation_mode": self.investigation_mode(),
            "query_facts": {
                key: value
                for key, value in self.query_facts.items()
                if key not in {"ticket_texts", "visual_descriptions"}
            },
            "recommended_starting_points": [],
            "business_knowledge": self.business_knowledge,
        }


def _bounded_text(value: Any, *, limit: int = 500) -> str:
    return str(value or "").strip()[:limit]


def _visual_items(attachment_evidence: Mapping[str, Any] | None) -> tuple[Mapping[str, Any], ...]:
    visual = attachment_evidence.get("visual_evidence") if isinstance(attachment_evidence, Mapping) else None
    items = visual.get("items") if isinstance(visual, Mapping) else None
    if not isinstance(items, list):
        return ()
    return tuple(item for item in items if isinstance(item, Mapping))


def compact_visual_evidence(attachment_evidence: Mapping[str, Any] | None) -> dict[str, Any]:
    """Project bounded visual facts without dropping non-textual anomalies."""
    compact_items: list[dict[str, Any]] = []
    visual_items = _visual_items(attachment_evidence)
    if not visual_items:
        # Preserve the historical no-image prompt exactly; the richer contract
        # is relevant only when at least one visual fact exists.
        return {"items": []}
    for index, item in enumerate(visual_items[:12], start=1):
        visual_item_id = _bounded_text(item.get("visual_item_id"), limit=40) or f"visual_{index}"
        compact_item: dict[str, Any] = {
            key: _bounded_text(item.get(key), limit=360)
            for key in (
                "visual_item_id",
                "asset_role",
                "client",
                "client_evidence",
                "product_variant",
                "user_path",
                "page",
                "page_state",
                "region_or_control",
                "event_or_action",
                "event_id",
                "resource_key",
                "actual_text",
                "expected_text",
                "highlighted_content",
                "actual_visual",
                "expected_visual",
                "visual_difference",
                "mismatch_summary",
                "element_type",
                "confidence",
            )
            if item.get(key) not in (None, "", [])
        }
        compact_item["visual_item_id"] = visual_item_id
        observed_clients = _normalized_clients(item.get("observed_clients", [])) if isinstance(item.get("observed_clients"), Sequence) and not isinstance(item.get("observed_clients"), str) else []
        if observed_clients:
            compact_item["observed_clients"] = observed_clients
        refs: list[dict[str, str]] = []
        for reference in item.get("evidence_refs", []) if isinstance(item.get("evidence_refs"), list) else []:
            if not isinstance(reference, Mapping):
                continue
            bounded_ref = {
                key: _bounded_text(reference.get(key), limit=160)
                for key in ("asset", "role", "annotation")
                if reference.get(key) not in (None, "")
            }
            if bounded_ref:
                refs.append(bounded_ref)
            if len(refs) >= 4:
                break
        if refs:
            compact_item["evidence_refs"] = refs
        # Keep field semantics while avoiding verbatim duplicates produced by
        # analyzers that mirror visual_difference into mismatch_summary.
        earlier_values: set[str] = set()
        for key in ("actual_text", "expected_text", "highlighted_content", "actual_visual", "expected_visual", "visual_difference", "mismatch_summary"):
            value = str(compact_item.get(key) or "")
            if not value:
                continue
            if value in earlier_values:
                compact_item.pop(key, None)
                continue
            earlier_values.add(value)
        fact_keys = {
            "actual_text",
            "expected_text",
            "highlighted_content",
            "actual_visual",
            "expected_visual",
            "visual_difference",
            "mismatch_summary",
        }
        if fact_keys.intersection(compact_item):
            compact_item["copy_item_id"] = _bounded_text(item.get("copy_item_id"), limit=40) or f"copy_{index}"
            compact_items.append(compact_item)

    visual = attachment_evidence.get("visual_evidence") if isinstance(attachment_evidence, Mapping) else None
    raw_comparisons = visual.get("comparisons") if isinstance(visual, Mapping) else None
    visible_ids = {str(item["visual_item_id"]) for item in compact_items}
    comparisons: list[dict[str, Any]] = []
    for comparison in raw_comparisons if isinstance(raw_comparisons, list) else []:
        if not isinstance(comparison, Mapping):
            continue
        actual_ids = [str(value) for value in comparison.get("actual_item_ids", []) if str(value) in visible_ids]
        expected_ids = [str(value) for value in comparison.get("expected_item_ids", []) if str(value) in visible_ids]
        if not actual_ids or not expected_ids:
            continue
        comparisons.append(
            {
                "comparison_id": _bounded_text(comparison.get("comparison_id"), limit=40),
                "actual_item_ids": actual_ids,
                "expected_item_ids": expected_ids,
                "anchor": _bounded_text(comparison.get("anchor"), limit=240),
                "basis": _bounded_text(comparison.get("basis"), limit=80),
            }
        )
        if len(comparisons) >= 8:
            break
    return {"schema_version": 2, "items": compact_items, "comparisons": comparisons}


def _unique_texts(values: Sequence[Any], *, limit: int = 24, item_limit: int = 500) -> list[str]:
    result: list[str] = []
    for value in values:
        text = _bounded_text(value, limit=item_limit)
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def _normalized_clients(values: Sequence[Any]) -> list[str]:
    aliases = {"安卓": "android", "苹果": "ios", "鸿蒙": "harmony", "harmonyos": "harmony"}
    clients: list[str] = []
    for value in values:
        lowered = aliases.get(str(value).strip().lower(), str(value).strip().lower())
        if lowered in _CLIENTS and lowered not in clients:
            clients.append(lowered)
    return clients


def _investigation_clients(observed_clients: Sequence[Any], reported_clients: Sequence[Any]) -> list[str]:
    reported = _normalized_clients(reported_clients)
    if "harmony" in reported:
        return ["harmony"]
    return _normalized_clients(observed_clients) or reported


def _structured_identifiers(values: Sequence[Any]) -> tuple[list[str], list[str]]:
    text = "\n".join(str(value or "") for value in values)
    event_ids = _unique_texts(tuple(re.findall(r"(?<![A-Za-z0-9_])[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+(?![A-Za-z0-9_])", text)))
    resource_keys = _unique_texts(
        tuple(
            match.group(1)
            for match in re.finditer(
                r"(?:文案|资源|resource|string|copy)?\s*(?:key|Key|键)\s*[:：=]?\s*([A-Za-z][A-Za-z0-9_.-]{2,})",
                text,
                re.IGNORECASE,
            )
        )
    )
    return event_ids, resource_keys


def _confirmed_copy_targets(confirmed_product_scope: str | None) -> list[str]:
    if not confirmed_product_scope:
        return []
    return _unique_texts(
        tuple(
            match.group(1).strip()
            for match in re.finditer(
                r"(?:→|改为|修改为)\s*([^；;\n]+)",
                confirmed_product_scope,
            )
        )
    )


def _meaningful_ticket_text(value: Any) -> str:
    """Drop empty ZenTao template labels without discarding real values."""
    text = _bounded_text(value, limit=2_000).strip()
    if not text:
        return ""
    remainder = re.sub(r"(?m)^\s*\d+[.、)）]\s*$", "", text)
    remainder = _EMPTY_TICKET_FIELD_PATTERN.sub("", remainder)
    remainder = re.sub(r"[\s:：,，;；。.!！?？\-_/\\|()[\]【】]+", "", remainder)
    return text if remainder else ""


def _clean_investigation_focus(raw_focus: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(raw_focus, Mapping):
        return {}
    textual_lists = {"acceptance_details", "evidence_focuses", "open_edges"}
    cleaned: dict[str, Any] = {}
    for key, value in raw_focus.items():
        if isinstance(value, list) and key in textual_lists:
            retained = [text for item in value if (text := _meaningful_ticket_text(item))]
            if retained:
                cleaned[key] = retained
        elif isinstance(value, list):
            if value:
                cleaned[key] = value
        elif isinstance(value, str):
            retained = _meaningful_ticket_text(value)
            if retained:
                cleaned[key] = retained
        elif value not in (None, "", []):
            cleaned[key] = value
    return cleaned


_TICKET_ACTION_VERBS = (
    "点击", "长按", "滑动", "进入", "打开", "返回", "连接", "选择", "开启", "关闭",
    "提交", "保存", "绑定", "解绑", "登录", "退出", "添加", "删除", "修改", "播放",
    "暂停", "解锁", "开锁",
)


def _ticket_navigation_facts(values: Sequence[Any]) -> dict[str, list[str]]:
    """Extract page/action locators for Tabby ranking, never causal proof."""
    pages: list[str] = []
    actions: list[str] = []
    paths: list[str] = []
    for raw_value in values:
        text = _meaningful_ticket_text(raw_value)
        if not text:
            continue
        for match in re.finditer(r"(?:进入|打开|返回(?:到)?|位于|在)\s*([^，。；;\n]{2,32}?(?:页|页面|界面|列表|详情|设置))", text):
            pages.append(match.group(1).strip())
        for fragment in re.split(r"(?:\[|【)?(?:测试步骤|操作步骤|复现步骤)(?:\]|】)?|\d+[.、)）]|[；;。\n]", text):
            compact = fragment.strip(" ：:,，")
            if 2 <= len(compact) <= 80 and any(verb in compact for verb in _TICKET_ACTION_VERBS):
                actions.append(compact)
        if pages and actions:
            paths.append(" → ".join((*pages[-2:], *actions[-2:])))
    return {
        "pages": _unique_texts(pages, limit=8, item_limit=80),
        "actions": _unique_texts(actions, limit=10, item_limit=120),
        "paths": _unique_texts(paths, limit=4, item_limit=240),
    }


def _knowledge_query_facts(
    bug_snapshot: Mapping[str, Any],
    attachment_evidence: Mapping[str, Any] | None,
    observed_clients: Sequence[str] = (),
    reported_clients: Sequence[str] = (),
    investigation_mode: str = "",
    client_scope_status: str = "",
    confirmed_product_scope: str | None = None,
) -> dict[str, Any]:
    items = _visual_items(attachment_evidence)
    preliminary_triage = bug_snapshot.get("preliminary_triage")
    raw_focus = preliminary_triage.get("investigation_focus") if isinstance(preliminary_triage, Mapping) else None
    investigation_focus = _clean_investigation_focus(raw_focus if isinstance(raw_focus, Mapping) else None)
    acceptance_details = tuple(investigation_focus.get("acceptance_details", ())) if isinstance(investigation_focus.get("acceptance_details"), list) else ()
    focus_evidence = tuple(investigation_focus.get("evidence_focuses", ())) if isinstance(investigation_focus.get("evidence_focuses"), list) else ()
    focus_edges = tuple(investigation_focus.get("open_edges", ())) if isinstance(investigation_focus.get("open_edges"), list) else ()
    reported = _normalized_clients(reported_clients)
    visual_clients: list[Any] = []
    for item in items:
        clients = item.get("observed_clients")
        if isinstance(clients, list):
            visual_clients.extend(clients)
        visual_clients.append(item.get("client"))
    # The platform-resolution result is authoritative for reproduced clients.
    # Visual analysis may describe UI state, but must not silently widen that
    # set after the dedicated device/system judgment has completed.
    observed = ["harmony"] if "harmony" in reported else (_normalized_clients(observed_clients) or _normalized_clients(visual_clients))
    identifier_sources = (
        *(bug_snapshot.get(key) for key in ("title", "description", "steps", "actual", "expected")),
        investigation_focus.get("observed_behavior"),
        investigation_focus.get("expected_behavior"),
        *acceptance_details,
        *focus_evidence,
        *(value for item in items for value in item.values() if isinstance(value, (str, int))),
    )
    derived_event_ids, derived_resource_keys = _structured_identifiers(identifier_sources)
    ticket_navigation = _ticket_navigation_facts(identifier_sources)

    facts: dict[str, Any] = {
        "reported_clients": reported,
        "observed_clients": observed,
        "investigation_mode": investigation_mode,
        "client_scope_status": client_scope_status,
        "investigation_focus": investigation_focus,
        "product_variants": _unique_texts((bug_snapshot.get("product"), *(item.get("product_variant") or item.get("product") for item in items))),
        "user_paths": _unique_texts((*ticket_navigation["paths"], *(item.get("user_path") for item in items))),
        "entry_actions": _unique_texts((*ticket_navigation["actions"], *(item.get("entry_action") for item in items))),
        "pages_or_regions": _unique_texts((*ticket_navigation["pages"], *(item.get("page") or item.get("page_or_region") for item in items))),
        "visual_states": _unique_texts(tuple(item.get("page_state") or item.get("visual_state") for item in items)),
        "visible_controls": _unique_texts(tuple(item.get("region_or_control") or item.get("visible_control") for item in items)),
        "event_or_actions": _unique_texts((*ticket_navigation["actions"], *(item.get("event_or_action") or item.get("event_type") or item.get("action") for item in items))),
        "event_ids": _unique_texts((*derived_event_ids, *(item.get("event_id") for item in items))),
        "resource_keys": _unique_texts((*derived_resource_keys, *(item.get("resource_key") or item.get("text_key") or item.get("copy_key") for item in items))),
        "actual_texts": _unique_texts((_meaningful_ticket_text(bug_snapshot.get("actual")), investigation_focus.get("observed_behavior"), *(item.get("actual_text") or item.get("actual") for item in items))),
        "expected_texts": _unique_texts(
            (
                *_confirmed_copy_targets(confirmed_product_scope),
                _meaningful_ticket_text(bug_snapshot.get("expected")),
                investigation_focus.get("expected_behavior"),
                *acceptance_details,
                *(item.get("expected_text") or item.get("expected") for item in items),
            )
        ),
        "ticket_texts": _unique_texts(
            (*tuple(_meaningful_ticket_text(bug_snapshot.get(key)) for key in ("title", "description", "steps")), *acceptance_details, *focus_evidence, *focus_edges),
            limit=20,
            item_limit=2_000,
        ),
        "visual_descriptions": _unique_texts(
            tuple(item.get(key) for item in items for key in ("highlighted_content", "actual_visual", "expected_visual", "visual_difference", "mismatch_summary", "element_type")),
            limit=32,
            item_limit=1_000,
        ),
    }
    return {key: value for key, value in facts.items() if value}



def build_investigation_knowledge_context(
    *,
    bug_snapshot: Mapping[str, Any],
    attachment_evidence: Mapping[str, Any] | None,
    repository_root: Path | None,
    shared_rn_repository_root: Path | None = None,
    observed_clients: Sequence[str] = (),
    reported_clients: Sequence[str] = (),
    investigation_mode: str = "",
    client_scope_status: str = "",
    implementation_lookup_script: Path | None = None,
    confirmed_product_scope: str | None = None,
) -> InvestigationKnowledgeContext:
    """Build current ticket facts and repository scope for Tabby retrieval."""
    del shared_rn_repository_root, implementation_lookup_script
    facts = _knowledge_query_facts(
        bug_snapshot,
        attachment_evidence,
        observed_clients,
        reported_clients,
        investigation_mode,
        client_scope_status,
        confirmed_product_scope,
    )
    clients = _investigation_clients(facts.get("observed_clients", []), facts.get("reported_clients", []))
    allowed_roots = (_HARMONY_CONTAINER_ROOT,) if investigation_mode == "harmony" or "harmony" in clients else (_RNSDK_CONTAINER_ROOT,)
    if repository_root is None:
        return InvestigationKnowledgeContext(
            facts,
            allowed_repository_roots=allowed_roots,
            error=f"{PurePosixPath(allowed_roots[0]).name} host mount is unavailable",
        )
    return InvestigationKnowledgeContext(
        facts,
        allowed_repository_roots=allowed_roots,
    )
