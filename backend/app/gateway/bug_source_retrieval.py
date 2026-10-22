"""Bounded external repository-context retrieval for Bug Workbench.

Tabby is used only as an index and snippet retriever.  This module never asks
Tabby (or another model) to diagnose the bug.  Every returned path and snippet
is revalidated against the selected local checkout before it can enter the
Codex preparation packet; stale, missing, generated, vendor and out-of-root
results are audit-only rejections.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
import websockets
from websockets.exceptions import WebSocketException

from app.gateway.bug_runtime_source_policy import discover_tracked_runtime_dist_roots, path_is_in_runtime_dist

_IGNORED_PARTS = frozenset({".git", ".gradle", ".next", "build", "dist", "node_modules", "out", "target", "vendor"})
_GENERIC_TERMS = frozenset(
    {
        "android",
        "ios",
        "bug",
        "app",
        "页面",
        "异常",
        "错误",
        "问题",
        "失败",
        "显示",
        "测试",
        "预期结果",
        "实际结果",
        "测试步骤",
        "手机型号与系统",
    }
)
_IDENTIFIER = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z_][A-Za-z0-9_]{3,}(?![A-Za-z0-9_])")
_API_PATH = re.compile(r"(?<![A-Za-z0-9_])/(?:[A-Za-z0-9_~.-]+/)+[A-Za-z0-9_~.-]+")
_ABSOLUTE_URL = re.compile(r"https?://[^\s<>()\[\]{}]+", re.IGNORECASE)
_DOCUMENT_LINK_PATH = re.compile(r"^/(?:wiki|docx?|docs?|sheets?|base|drive|file|pages?)(?:/|$)", re.IGNORECASE)
_REGEX_META = re.compile(r"([\\.^$|?*+()\[\]{}])")
_NOISY_IDENTIFIER = re.compile(r"^(?:APP|L)[_-]?\d+$", re.IGNORECASE)
_GENERIC_IDENTIFIERS = frozenset(
    {
        "data_type",
        "iPhone",
        "keyId",
        "keyObj",
        "Logger_I",
        "example_plugin",
        "PageIndex",
        "params",
        "SERIAL",
        "uuid",
    }
)
_HAN_RUN = re.compile(r"[\u3400-\u9fff]{2,}")
_ASCII_CONCEPT = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9.+-]{2,15}(?![A-Za-z0-9_])")
_FACT_LABEL = re.compile(
    r"(?:手机型号与系统|测试设备(?:sn)?|测试时间|预置条件|操作步骤|测试步骤|步骤|"
    r"实际结果|实测结果|预期结果|重现概率|问题恢复方法|恢复方法)",
    re.IGNORECASE,
)
_CONCEPT_BREAK = re.compile(
    r"(?:过程中|页面|点击|长按|查看|观察|打开|进入|返回|应该|应当|需要|相关|出现|"
    r"一直|仍在|进行|成功|失败|详情请见|附件视频|是否|然后|以后|以及|并且|但是|的|下)",
)
_CONCEPT_STOPWORDS = _GENERIC_TERMS | frozenset(
    {
        "手机",
        "型号",
        "系统",
        "设备",
        "详情",
        "结果",
        "操作",
        "步骤",
        "概率",
        "附件",
        "视频",
        "截图",
        "日志",
        "修改",
        "说明",
        "如下",
        "整个",
        "系列",
        "版本",
        "环境",
        "实际",
        "预期",
        "恢复",
        "问题恢复",
        "测试时间",
        "测试设备",
        "release",
        "debug",
        "log",
        "redmi",
        "xiaomi",
    }
)
_PRODUCT_TOKEN = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z]\d{2,}[A-Za-z]?|\d{3}[A-Za-z]?)(?:-\d+)?(?![A-Za-z0-9])")
_EXPLICIT_PATH_PRODUCT = re.compile(
    r"(?:(?:modules/config/|exampleModuleConfig_)(?P<token>[A-Za-z]?\d{2,}[A-Za-z]?)|"
    r"projects/com\.example\.(?P<project>[A-Za-z0-9_-]+))",
    re.IGNORECASE,
)
_SOURCE_SUFFIXES = frozenset({".java", ".kt", ".m", ".mm", ".swift", ".js", ".jsx", ".ts", ".tsx"})
_RESOURCE_SUFFIXES = frozenset({".strings", ".xml", ".json"})
_RESOURCE_PATH_PARTS = frozenset({"assets", "images", "lang", "langs", "locales", "resources", "res"})
_IMPLEMENTATION_LAYERS = frozenset({"android_native", "ios_native", "shared_rn", "harmony_native", "sample_platform_repo"})

# Small, repository-independent translations from user-visible behavior to
# common source responsibilities.  These are programming concepts, not product
# or Bug mappings; the ticket facts remain the source of the business nouns.
_CODE_DUTY_HINTS = (
    (("海外", "美国", "地区", "国家"), "Sea overseas country region"),
    (("注册",), "register signup"),
    (("编辑",), "edit editing selected"),
    (("日期",), "date header section"),
    (("门铃",), "doorbell ring event"),
    (("标签", "文案", "不一致"), "label title description mapping resource"),
    (("钥匙",), "key keyId key management"),
    (("密码",), "password pwd"),
    (("长按",), "long press onLongPress gesture disabled"),
    (("置灰", "灰色", "不可用"), "disabled enabled interaction state"),
    (("批量",), "batch selected list"),
    (("删除",), "delete remove callback"),
    (("取消",), "cancel abort cleanup reset"),
    (("断网", "无网络", "网络异常"), "network error failure callback"),
    (("无提示", "提示", "toast"), "toast error message callback"),
    (("进入", "打开"), "page entry initialize load fetch"),
    (("计时", "时长"), "timer duration format hour minute second"),
    (("崩溃", "闪退"), "lifecycle cleanup callback crash"),
)
_CODE_ACTION_WORDS = {
    "删除": ("delete", "remove"),
    "取消": ("cancel", "reset"),
    "开锁": ("unlock",),
    "下载": ("download",),
    "录屏": ("record",),
}
_CODE_ENTITY_WORDS = {
    "钥匙": ("key",),
    "密码": ("pwd", "password"),
    "按钮": ("button",),
    "视频": ("video",),
    "任务": ("task",),
    "设备": ("device",),
}


def _revision(root: Path) -> str:
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, timeout=10, check=False)
    value = result.stdout.strip().lower()
    return value if result.returncode == 0 and re.fullmatch(r"[0-9a-f]{7,64}", value) else "unknown"


def _path_revision(path: Path, fallback: str) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path.parent,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    value = result.stdout.strip().lower()
    return value if result.returncode == 0 and re.fullmatch(r"[0-9a-f]{7,64}", value) else fallback


def _iter_text(value: Any):
    if isinstance(value, str):
        text = re.sub(r"\s+", " ", value).strip()
        if text:
            yield text
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _iter_text(item)
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        for item in value:
            yield from _iter_text(item)


def _iter_api_paths(value: Any):
    for text in _iter_text(value):
        seen: set[str] = set()
        # Keep a real request path even when the ticket writes its absolute URL,
        # while excluding common document/navigation links from source anchors.
        for url_match in _ABSOLUTE_URL.finditer(text):
            url = url_match.group(0).rstrip(".,;:，。；")
            endpoint = urlsplit(url).path.rstrip("/")
            if endpoint and len(endpoint) <= 80 and _API_PATH.fullmatch(endpoint) and not _DOCUMENT_LINK_PATH.match(endpoint):
                seen.add(endpoint)
                yield endpoint
        without_urls = _ABSOLUTE_URL.sub(" ", text)
        for match in _API_PATH.finditer(without_urls):
            endpoint = match.group(0).rstrip(".,;:")
            if len(endpoint) <= 80 and endpoint not in seen:
                yield endpoint


def _without_runtime_identities(value: Any, identifiers: Sequence[str]) -> Any:
    """Project the upstream identity classification into a source-only view."""
    if not identifiers:
        return value
    if isinstance(value, str):
        cleaned = value
        for identifier in sorted(identifiers, key=len, reverse=True):
            cleaned = re.sub(
                rf"(?<![A-Za-z0-9-]){re.escape(identifier)}(?![A-Za-z0-9-])",
                " ",
                cleaned,
                flags=re.IGNORECASE,
            )
        return re.sub(r"[ \t]+", " ", cleaned).strip()
    if isinstance(value, Mapping):
        # Explicit source contracts outrank the same token's incidental
        # appearance in a device/precondition line. They remain available to
        # exact source navigation instead of being reclassified as a serial.
        return {key: item if str(key) in {"resource_keys", "event_ids", "api_endpoints"} else _without_runtime_identities(item, identifiers) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return [_without_runtime_identities(item, identifiers) for item in value]
    return value


def build_retrieval_query(
    *,
    bug_snapshot: Mapping[str, Any],
    query_facts: Mapping[str, Any],
    runtime_evidence: Mapping[str, Any],
    max_chars: int,
    retrieval_aliases: Sequence[str] = (),
) -> str:
    """Build one deterministic, fact-only natural-language retrieval query."""
    ranked: list[tuple[int, str]] = []

    def add(score: int, value: Any) -> None:
        for raw in _iter_text(value):
            text = raw.strip(" \t\r\n，。；：、|[]【】")
            if len(text) < 3 or text.casefold() in _GENERIC_TERMS:
                continue
            if len(text) > 240:
                text = text[:240]
            if not any(existing == text for _existing_score, existing in ranked):
                ranked.append((score, text))

    # ``query_facts`` is the only interpreted view of the flattened ZenTao
    # body.  Do not re-read description/steps/actual/expected here: doing so
    # reintroduces the same template prose with conflicting roles after the
    # preparation layer has already separated it.
    add(160, list(_iter_api_paths(bug_snapshot.get("title"))))
    for key in (
        "actual_texts",
        "expected_texts",
        "resource_keys",
        "event_ids",
        "pages_or_regions",
        "user_paths",
        "visible_controls",
        "event_or_actions",
    ):
        add(160, list(_iter_api_paths(query_facts.get(key))))
    add(140, retrieval_aliases)
    # The ticket states the behavior being investigated. Keep that semantic
    # center ahead of visual scene descriptions, which usually describe where
    # something was seen rather than the code path that produced the failure.
    add(135, bug_snapshot.get("title"))
    for key, score in (
        ("actual_texts", 125),
        ("expected_texts", 125),
        ("entry_actions", 110),
        ("resource_keys", 110),
        ("event_ids", 110),
        ("ticket_texts", 95),
    ):
        add(score, list(_iter_text(query_facts.get(key)))[:4])
    for key in ("pages_or_regions", "user_paths", "visible_controls", "event_or_actions"):
        add(55, list(_iter_text(query_facts.get(key)))[:2])

    parts: list[str] = []
    used = 0
    for _score, text in sorted(ranked, key=lambda item: (-item[0], len(item[1]), item[1])):
        cost = len(text) + (1 if parts else 0)
        if used + cost > max_chars:
            continue
        parts.append(text)
        used += cost
    return "\n".join(parts)


def build_code_duty_query(
    *,
    bug_snapshot: Mapping[str, Any],
    query_facts: Mapping[str, Any],
    max_chars: int = 640,
) -> str:
    """Build a compact behavior-to-source query alongside the literal query.

    The literal query preserves exact copy, identifiers and ticket wording.
    This second view keeps only page/action/state/expectation facts and adds a
    bounded set of generic programming concepts.  It never names a product
    file, class, module or Bug-specific destination.
    """
    duty_triggers = tuple(trigger for triggers, _hint in _CODE_DUTY_HINTS for trigger in triggers)
    action_triggers = ("点击", "长按", "进入", "打开", "返回", "选择", "开启", "关闭", "添加", "删除", "取消", "播放", "录制", "开锁")

    def fragments(value: Any) -> list[str]:
        candidates: list[str] = []
        for raw in _iter_text(value):
            cleaned = _FACT_LABEL.sub("|", re.sub(r"\s+", " ", raw))
            for fragment in re.split(r"[|\[\]【】；;。\n]|(?:^|\s)\d+[.、)）]", cleaned):
                text = fragment.strip(" \t\r\n，。；：、|[]【】")
                if 2 <= len(text) <= 80 and text not in candidates and not re.fullmatch(r"(?:必现|偶现|问题恢复|复现概率|时间|见视频|预置条件|无)", text) and not re.fullmatch(r"[\d\s:：./-]+", text):
                    candidates.append(text)
        return sorted(
            dict.fromkeys(candidates),
            key=lambda text: (
                -int(any(trigger in text for trigger in duty_triggers)),
                -int(any(trigger in text for trigger in action_triggers)),
                -int(bool(re.search(r"(?:页|界面|列表|按钮|管理|设置|功能)", text))),
                len(text),
                text,
            ),
        )

    parts: list[str] = []
    seen: set[str] = set()
    used = 0

    def add(value: Any, *, limit: int) -> None:
        nonlocal used
        added = 0
        for text in fragments(value):
            folded = text.casefold()
            if len(text) < 2 or folded in seen or folded in _GENERIC_TERMS or _NOISY_IDENTIFIER.fullmatch(text):
                continue
            cost = len(text) + (1 if parts else 0)
            if used + cost > max_chars:
                continue
            parts.append(text)
            seen.add(folded)
            used += cost
            added += 1
            if added >= limit:
                break

    add(bug_snapshot.get("title"), limit=1)
    add(query_facts.get("pages_or_regions"), limit=1)
    add(query_facts.get("entry_actions") or query_facts.get("event_or_actions"), limit=1)
    hint_corpus = "\n".join(
        _iter_text(
            (
                query_facts.get("entry_actions"),
                query_facts.get("actual_texts"),
                query_facts.get("expected_texts"),
                bug_snapshot.get("title"),
            )
        )
    )
    hint_corpus = re.sub(r"(?:见|查看)?(?:附件)?(?:视频|截图)", " ", hint_corpus)
    hints = [hint for triggers, hint in _CODE_DUTY_HINTS if any(trigger.casefold() in hint_corpus.casefold() for trigger in triggers)]
    composed = [
        f"{verb}{entity[:1].upper()}{entity[1:]}" for action, verbs in _CODE_ACTION_WORDS.items() if action in hint_corpus for noun, entities in _CODE_ENTITY_WORDS.items() if noun in hint_corpus for verb in verbs for entity in entities
    ][:8]
    if composed:
        add(composed, limit=8)
    if hints:
        add(hints, limit=5)
    return "\n".join(parts)


def _code_duty_anchor_terms(*, bug_snapshot: Mapping[str, Any], query_facts: Mapping[str, Any]) -> list[str]:
    """Translate visible behavior into bounded, repository-independent code terms."""
    corpus = "\n".join(
        _iter_text(
            (
                bug_snapshot.get("title"),
                query_facts.get("pages_or_regions"),
                query_facts.get("entry_actions"),
                query_facts.get("actual_texts"),
                query_facts.get("expected_texts"),
            )
        )
    ).casefold()
    terms: list[str] = []
    for triggers, hint in _CODE_DUTY_HINTS:
        if not any(trigger.casefold() in corpus for trigger in triggers):
            continue
        for term in hint.split():
            if len(term) >= 3 and term.casefold() not in {value.casefold() for value in terms}:
                terms.append(term)
    return terms[:12]


def build_retrieval_anchors(
    *,
    query: str,
    query_facts: Mapping[str, Any],
    max_anchors: int,
    retrieval_aliases: Sequence[str] = (),
) -> list[str]:
    """Choose bounded exact anchors for Tabby's repository grep.

    Complete visible/action facts precede supporting identifiers. Long
    reproduction paragraphs are never sent as a grep expression.
    """
    anchors: list[str] = []

    def add(value: str) -> None:
        candidate = re.sub(r"\s+", " ", value).strip(" \t\r\n，。；：、|[]【】`'\"")
        if len(candidate) < 3 or len(candidate) > 80 or candidate.casefold() in _GENERIC_TERMS or _NOISY_IDENTIFIER.fullmatch(candidate) or candidate in _GENERIC_IDENTIFIERS or candidate in anchors:
            return
        anchors.append(candidate)

    # Reuse preparation fields that already distinguish ticket-led actions
    # from visual scene descriptions. This changes priority inside the existing
    # bounded grep plan; it does not add another retrieval stage.
    prioritized_facts = (
        ("entry_actions", 3),
        ("ticket_texts", 2),
        ("actual_texts", 3),
        ("expected_texts", 2),
        ("user_paths", 2),
    )
    for key, limit in prioritized_facts:
        for text in list(_iter_text(query_facts.get(key)))[:limit]:
            add(text)
            if len(anchors) >= max_anchors:
                return anchors[:max_anchors]
    for key in ("resource_keys", "event_ids"):
        for text in _iter_text(query_facts.get(key)):
            for identifier in _IDENTIFIER.findall(text):
                if "_" in identifier or any(char.isupper() for char in identifier[1:]):
                    add(identifier)
            add(text)
    for endpoint in _iter_api_paths(query):
        add(endpoint)
    for alias in retrieval_aliases:
        add(alias)
    # Identifiers remain useful, but they are supporting navigation. Keep
    # complete action/copy facts ahead of serials and incidental code-shaped
    # words so those identifiers cannot evict the behavior being investigated.
    for key, limit in prioritized_facts:
        for text in list(_iter_text(query_facts.get(key)))[:limit]:
            for identifier in _IDENTIFIER.findall(text):
                if len(identifier) >= 6 and ("_" in identifier or any(char.isupper() for char in identifier[1:])):
                    add(identifier)
                    if len(anchors) >= max_anchors:
                        return anchors[:max_anchors]
    for line in query.splitlines():
        for identifier in _IDENTIFIER.findall(line):
            if "_" in identifier and len(identifier) >= 8:
                add(identifier)
        if len(line) <= 48:
            add(line)
        if len(anchors) >= max_anchors:
            break
    return anchors[:max_anchors]


def _normalized_anchor(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip(" \t\r\n，。；：、|[]【】`'\"")


def _is_complete_literal_anchor(value: str) -> bool:
    """Distinguish a visible/action fact from a generic code-shaped token."""
    han_count = len(re.findall(r"[\u3400-\u9fff]", value))
    return han_count >= 4 or (len(value) >= 12 and bool(re.search(r"[\s，。；：,:;!?！？]", value)))


def _is_specific_source_identifier(value: str) -> bool:
    """Keep generic English words usable without treating them as exact facts."""
    if _API_PATH.fullmatch(value):
        return True
    if "_" in value or re.search(r"[a-z][A-Z]", value):
        return True
    return bool(value.isascii() and re.fullmatch(r"[A-Za-z][A-Za-z0-9.-]+", value) and len(value) >= 10)


def _ticket_anchor_stage(anchor: str, query_facts: Mapping[str, Any]) -> str:
    normalized = _normalized_anchor(anchor)
    complete_facts = {
        _normalized_anchor(value) for key in ("entry_actions", "ticket_texts", "actual_texts", "expected_texts", "user_paths") for value in _iter_text(query_facts.get(key)) if _is_complete_literal_anchor(_normalized_anchor(value))
    }
    if normalized in complete_facts or _is_complete_literal_anchor(normalized):
        return "exact_ticket"
    if _is_specific_source_identifier(normalized):
        return "exact_identifier"
    return "exact_supporting"


def build_fallback_concepts(
    *,
    bug_snapshot: Mapping[str, Any],
    query_facts: Mapping[str, Any],
    runtime_evidence: Mapping[str, Any],
    exact_anchors: Sequence[str],
    max_concepts: int,
) -> list[str]:
    """Extract bounded short concepts when exact-anchor grep returns nothing.

    This is deliberately deterministic and ticket-led. Runtime logs do not
    contribute source-navigation terms.
    """
    fragments: list[tuple[int, str]] = []
    seen_fragments: set[str] = set()

    def add(weight: int, value: Any) -> None:
        for raw in _iter_text(value):
            text = re.sub(r"\s+", " ", raw).strip()
            if len(text) < 2 or text in seen_fragments:
                continue
            seen_fragments.add(text)
            fragments.append((weight, text))

    add(160, list(_iter_text(query_facts.get("entry_actions")))[:5])
    for key in ("actual_texts", "expected_texts", "resource_keys", "event_ids"):
        add(135, query_facts.get(key))
    add(105, query_facts.get("user_paths"))
    for key in ("pages_or_regions", "visible_controls"):
        add(55, query_facts.get(key))
    add(145, bug_snapshot.get("title"))
    add(80, query_facts.get("ticket_texts"))
    # Screenshot-derived paths often repeat the same sentence at increasing
    # nesting depths. Remove only the repeated span from lower-priority copies;
    # preserve their unique suffix/prefix so screenshot-only facts are not lost.
    compacted: list[tuple[int, str]] = []
    for fragment in sorted(fragments, key=lambda item: (-item[0], len(item[1]), item[1])):
        weight, text = fragment
        remainder = text
        for existing in compacted:
            if len(existing[1]) >= 6 and existing[1] in remainder:
                remainder = remainder.replace(existing[1], " ")
        remainder = re.sub(r"(?:\s*[→>|]+\s*)+", " ", remainder)
        remainder = re.sub(r"\s+", " ", remainder).strip()
        if len(remainder) < 2:
            continue
        compacted.append((weight, remainder))
    fragments = compacted

    exact = {str(value).casefold() for value in exact_anchors}
    scores: dict[str, float] = {}
    document_hits: dict[str, set[int]] = {}
    whole_hits: dict[str, set[int]] = {}

    def record(concept: str, *, weight: int, fragment_index: int, whole: bool = False) -> None:
        candidate = concept.strip(" -_.，。；：、|[]【】()（）`'\"")
        folded = candidate.casefold()
        if (
            len(candidate) < 2
            or len(candidate) > 16
            or (candidate.isascii() and any(char.isdigit() for char in candidate))
            or folded in exact
            or folded in _CONCEPT_STOPWORDS
            or _NOISY_IDENTIFIER.fullmatch(candidate)
            or candidate in _GENERIC_IDENTIFIERS
        ):
            return
        hits = document_hits.setdefault(candidate, set())
        if fragment_index in hits:
            return
        hits.add(fragment_index)
        if whole:
            whole_hits.setdefault(candidate, set()).add(fragment_index)
        contribution = weight + min(len(candidate), 8) * 3 + (90 if whole else 0)
        scores[candidate] = max(scores.get(candidate, 0.0), contribution)

    for fragment_index, (weight, raw) in enumerate(fragments):
        text = _ABSOLUTE_URL.sub(" ", _FACT_LABEL.sub(" ", raw))
        for token in _ASCII_CONCEPT.findall(text):
            record(token, weight=weight, fragment_index=fragment_index, whole=True)
        for run in _HAN_RUN.findall(text):
            pieces = [piece for piece in _CONCEPT_BREAK.split(run) if len(piece) >= 2]
            for piece in pieces:
                if len(piece) <= 12:
                    record(piece, weight=weight, fragment_index=fragment_index, whole=True)
                # Repeated character n-grams recover concise Chinese concepts
                # without maintaining product- or Bug-specific dictionaries.
                for size in range(min(4, len(piece)), 1, -1):
                    for offset in range(0, len(piece) - size + 1):
                        record(
                            piece[offset : offset + size],
                            weight=min(weight, 35),
                            fragment_index=fragment_index,
                        )

    ranked: list[tuple[float, str]] = []
    complete_concepts = {concept for concept in scores if whole_hits.get(concept)}
    for concept, score in scores.items():
        hit_count = len(document_hits.get(concept, ()))
        is_whole = bool(whole_hits.get(concept))
        # A generated sliding fragment must never outrank the complete concept
        # it came from. This removes malformed partial anchors generically,
        # without maintaining Bug- or phrase-specific blacklists.
        if not is_whole and any(concept in complete and concept != complete for complete in complete_concepts):
            continue
        # Two-character substrings are too noisy; genuine short ticket terms
        # such as 配网/直播 remain eligible when they are a complete fragment.
        if len(concept) == 2 and hit_count < 2 and not is_whole:
            continue
        specificity = min(len(concept), 8) * 16
        if len(concept) == 2:
            specificity -= 90
        if not is_whole:
            specificity -= 70
        ranked.append((score + min(hit_count, 3) * 30 + specificity, concept))
    ranked.sort(key=lambda item: (-item[0], -len(item[1]), item[1]))

    selected: list[str] = []
    cluster_counts: dict[str, int] = {}
    for _score, concept in ranked:
        cluster = next((item for item in selected if concept in item or item in concept), "")
        if cluster:
            key = min(cluster, concept, key=len)
            count = cluster_counts.get(key, 1)
            if count >= 2 or abs(len(cluster) - len(concept)) <= 1:
                continue
            cluster_counts[key] = count + 1
        selected.append(concept)
        if len(selected) >= max(1, max_concepts):
            break
    explicit_products: list[str] = []
    for text in _iter_text(bug_snapshot.get("product")):
        for token in _PRODUCT_TOKEN.findall(text):
            normalized = token.casefold().split("-", 1)[0]
            if normalized not in explicit_products:
                explicit_products.append(normalized)
    for product in explicit_products:
        if product in exact or product in selected:
            continue
        if len(selected) >= max(1, max_concepts):
            selected[-1] = product
        else:
            selected.append(product)
    return selected


def build_product_scope_tokens(bug_snapshot: Mapping[str, Any], *, retrieval_aliases: Sequence[str] = ()) -> list[str]:
    """Return normalized ticket products plus operator-declared retrieval aliases."""
    tokens: list[str] = []
    for key in ("product", "title", "module"):
        for text in _iter_text(bug_snapshot.get(key)):
            for raw in _PRODUCT_TOKEN.findall(text):
                folded = raw.casefold()
                if folded.startswith("app_") or folded not in tokens:
                    if not folded.startswith("app_"):
                        tokens.append(folded)
                base = folded.split("-", 1)[0]
                if base not in tokens:
                    tokens.append(base)
    for text in _iter_text(retrieval_aliases):
        for raw in _PRODUCT_TOKEN.findall(text):
            folded = raw.casefold()
            if folded not in tokens:
                tokens.append(folded)
            base = folded.split("-", 1)[0]
            if base not in tokens:
                tokens.append(base)
    return tokens[:4]


def _module_config_bindings(root: Path) -> dict[str, str]:
    """Read the repository's own model-to-config switch without product rules."""
    manager = root / "moduleControl" / "moduleControl.js"
    if not manager.is_file():
        return {}
    bindings: dict[str, str] = {}
    pending_models: list[str] = []
    for line in manager.read_text(encoding="utf-8", errors="replace").splitlines():
        case = re.search(r"\bcase\s+['\"](?P<model>[^'\"]+)['\"]\s*:", line)
        if case:
            pending_models.append(case.group("model"))
            continue
        required = re.search(r"require\(['\"]\./(?P<config>exampleModuleConfig_[^'\"]+\.json)['\"]\)", line)
        if required and pending_models:
            config = f"moduleControl/{required.group('config')}"
            for model in pending_models:
                bindings[model] = config
            pending_models = []
        elif re.search(r"\b(?:break|default)\b", line):
            pending_models = []
    return bindings


def _resolve_relative_source(root: Path, owner: Path, import_path: str) -> Path | None:
    """Resolve one checkout-local JS/TS import without a package resolver."""
    if not import_path.startswith("."):
        return None
    unresolved = (owner.parent / import_path).resolve(strict=False)
    candidates = [
        unresolved,
        *(unresolved.with_suffix(suffix) for suffix in (".js", ".jsx", ".ts", ".tsx")),
        *(unresolved / f"index{suffix}" for suffix in (".js", ".jsx", ".ts", ".tsx")),
    ]
    for candidate in candidates:
        if candidate.is_relative_to(root) and candidate.is_file():
            return candidate
    return None


def _runtime_route_bindings(root: Path, runtime_config: Path, source: str) -> list[dict[str, Any]]:
    """Read route-name -> actual component bindings from one product config."""
    imports: dict[str, Path] = {}
    for match in re.finditer(
        r"^\s*import\s+(?P<symbol>[A-Za-z_$][\w$]*)\s+from\s+['\"](?P<path>[^'\"]+)['\"]\s*;?",
        source,
        re.MULTILINE,
    ):
        target = _resolve_relative_source(root, runtime_config, match.group("path"))
        if target is not None:
            imports[match.group("symbol")] = target

    bindings: list[dict[str, Any]] = []
    route = re.compile(
        r"\bname\s*:\s*['\"](?P<name>[^'\"]+)['\"]\s*,"
        r"(?P<body>[^{}]{0,600}?)\bcomponent\s*:\s*(?P<component>[A-Za-z_$][\w$]*)"
        r"(?P<tail>[^{}]{0,300})",
        re.DOTALL,
    )
    for match in route.finditer(source):
        component = match.group("component")
        target = imports.get(component)
        if target is None:
            continue
        title_match = re.search(r"\btitle\s*:\s*['\"](?P<title>[^'\"]+)['\"]", match.group("tail"))
        bindings.append(
            {
                "name": match.group("name"),
                "component": component,
                "implementation_path": target.relative_to(root).as_posix(),
                "line": source[: match.start()].count("\n") + 1,
                "title": title_match.group("title") if title_match else "",
            }
        )
    return bindings


def _runtime_config_for_model(root: Path, model: str) -> dict[str, Any] | None:
    """Follow the checkout's model-to-runtime-config import, when present."""
    loader = root / "modules" / "config" / "Config.js"
    try:
        lines = loader.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    binding = re.compile(r"^\s*['\"](?P<model>[^'\"]+)['\"]\s*:\s*.*?\bimport\(\s*['\"](?P<path>\.[^'\"]+\.js)['\"]\s*\)")
    for line_number, line in enumerate(lines, start=1):
        match = binding.search(line)
        if not match or match.group("model") != model:
            continue
        target = (loader.parent / match.group("path")).resolve(strict=False)
        if not target.is_relative_to(root) or not target.is_file():
            return None
        assignments: list[dict[str, Any]] = []
        try:
            target_source = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        target_lines = target_source.splitlines()
        for index, source_line in enumerate(target_lines, start=1):
            if not re.match(r"^\s*[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+\s*=(?!=)", source_line):
                continue
            assignments.append({"line": index, "source": source_line.strip()[:160]})
            if len(assignments) >= 6:
                break
        return {
            "mapping_path": "modules/config/Config.js",
            "mapping_line": line_number,
            "path": target.relative_to(root).as_posix(),
            "assignment_clues": assignments,
            "route_bindings": _runtime_route_bindings(root, target, target_source),
        }
    return None


def _module_config_overrides(
    payload: Mapping[str, Any],
    *,
    implementation_index: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, str]:
    """Read only resolvable logical-page -> component bindings.

    ``examplePageConfig.data`` also carries booleans, indexes and other feature
    values.  Those values describe runtime behaviour; they are not page
    overrides and must not make otherwise-equivalent product-family configs
    look structurally different.
    """
    overrides: dict[str, str] = {}
    modules = payload.get("modules")
    if not isinstance(modules, Sequence) or isinstance(modules, (str, bytes, bytearray)):
        return overrides
    for module in modules:
        if not isinstance(module, Mapping):
            continue
        config_infos = module.get("examplePageConfig")
        if not isinstance(config_infos, Sequence) or isinstance(config_infos, (str, bytes, bytearray)):
            continue
        for config_info in config_infos:
            data = config_info.get("data") if isinstance(config_info, Mapping) else None
            if not isinstance(data, Mapping):
                continue
            for logical, concrete in data.items():
                if not isinstance(logical, str) or not isinstance(concrete, str):
                    continue
                logical_name = logical.strip()
                concrete_name = concrete.strip()
                if not re.fullmatch(r"[A-Za-z_$][\w$]*", logical_name) or not re.fullmatch(r"[A-Za-z_$][\w$]*", concrete_name):
                    continue
                if implementation_index is not None and concrete_name.casefold() not in implementation_index:
                    continue
                overrides[logical_name] = concrete_name
    return overrides


def _module_config_signature(
    root: Path,
    *,
    model: str,
    payload: Mapping[str, Any],
    implementation_index: Mapping[str, Sequence[str]],
) -> tuple[Any, ...]:
    """Return only current-runtime facts that must agree for family consensus."""
    modules = payload.get("modules")
    module_names = (
        tuple(sorted(str(module.get("name") or "") for module in modules if isinstance(module, Mapping) and str(module.get("name") or "").strip()))
        if isinstance(modules, Sequence) and not isinstance(modules, (str, bytes, bytearray))
        else ()
    )
    runtime = _runtime_config_for_model(root, model)
    return (
        module_names,
        tuple(sorted(_module_config_overrides(payload, implementation_index=implementation_index).items())),
        str((runtime or {}).get("path") or ""),
    )


def _legacy_project_bindings(root: Path) -> dict[str, str]:
    """Read the legacy device-model to RN-project map from current source."""
    path = root / "examplePlatformMap.js"
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    block = re.search(r"\bMODEL_TO_PROJECT\s*=\s*\{(?P<body>.*?)\}\s*;", source, re.DOTALL)
    if not block:
        return {}
    return {
        match.group("model"): match.group("project")
        for match in re.finditer(
            r"['\"](?P<model>[A-Za-z0-9_]+)['\"]\s*:\s*['\"](?P<project>[A-Za-z0-9_-]+)['\"]",
            block.group("body"),
        )
    }


def _legacy_product_repositories(root: Path) -> dict[str, tuple[str, str]]:
    """Read product aliases from current nested-project source metadata.

    A Codex source view intentionally contains no ``.git`` directories. The
    checked-in package model and project directory therefore remain the
    source-only identity, while a real checkout's remote contributes any
    additional public product alias (for example V03 -> sampleproduct).
    """
    projects = root / "projects"
    if not projects.is_dir():
        return {}
    aliases: dict[str, tuple[str, str]] = {}
    for project_path in sorted(projects.glob("com.example.*")):
        if not project_path.is_dir():
            continue
        project = project_path.name.removeprefix("com.example.")
        if not project:
            continue
        aliases[project.casefold()] = (project, project)
        try:
            package = json.loads((project_path / "package.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            package = {}
        package_model = str(package.get("model") or "").strip() if isinstance(package, Mapping) else ""
        if package_model:
            aliases[package_model.casefold()] = (package_model, project)
        try:
            completed = subprocess.run(
                ["git", "config", "--get", "remote.origin.url"],
                cwd=project_path,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        remote = completed.stdout.strip()
        if completed.returncode != 0 or not remote:
            continue
        repository_name = re.split(r"[/:]", remote.rstrip("/"))[-1]
        alias = re.sub(r"\.git$", "", repository_name, flags=re.IGNORECASE).strip()
        if alias and project:
            aliases[alias.casefold()] = (alias, project)
    return aliases


def _declared_product_tokens(snapshot: Mapping[str, Any]) -> list[str]:
    """Return explicit product labels, without mining prose for incidental names."""
    values: list[str] = []
    for key in ("product", "module"):
        for text in _iter_text(snapshot.get(key)):
            values.extend(_PRODUCT_TOKEN.findall(text))
    title = " ".join(_iter_text(snapshot.get("title")))
    for value in re.findall(r"【\s*([^】]{1,40})\s*】", title)[:4]:
        values.extend(_PRODUCT_TOKEN.findall(value))
    return list(dict.fromkeys(value.casefold() for value in values))


def _recognized_declared_product_tokens(root: Path, snapshot: Mapping[str, Any]) -> set[str]:
    """Keep only explicit ticket products represented by current source metadata."""
    project_aliases = _legacy_product_repositories(root)
    config_tokens = {Path(relative).stem.removeprefix("exampleModuleConfig_").casefold() for relative in _module_config_bindings(root).values()}
    recognized: set[str] = set()
    for token in _declared_product_tokens(snapshot):
        if token in project_aliases or token in config_tokens:
            recognized.add(token)
            continue
        if any(candidate.startswith(token) and len(candidate) > len(token) and candidate[len(token)].isalpha() for candidate in config_tokens):
            recognized.add(token)
    return recognized


def _project_override_conflicts_with_declared_product(
    root: Path,
    *,
    path: str,
    declared_products: set[str],
) -> bool:
    """Reject an explicitly different product override, never shared source."""
    match = re.match(r"projects/com\.example\.(?P<project>[^/]+)/", path, re.IGNORECASE)
    if not match or not declared_products:
        return False
    project = match.group("project").casefold()
    identities = {project}
    identities.update(alias for alias, (_display, mapped_project) in _legacy_product_repositories(root).items() if mapped_project.casefold() == project)
    return identities.isdisjoint(declared_products)


def _resolve_legacy_project(root: Path, *, snapshot: Mapping[str, Any], corpus: str) -> dict[str, Any] | None:
    bindings = _legacy_project_bindings(root)
    repositories = _legacy_product_repositories(root)
    matched_models = [model for model in bindings if re.search(r"(?<![A-Za-z0-9])" + re.escape(model) + r"(?![A-Za-z0-9])", corpus, re.IGNORECASE)]
    declared_products = _declared_product_tokens(snapshot)
    matched_aliases = [repositories[token] for token in declared_products if token in repositories]
    if matched_models:
        projects = list(dict.fromkeys(bindings[model] for model in matched_models))
        basis = "current_checkout_legacy_device_mapping"
        aliases_by_project: dict[str, str] = {}
    elif matched_aliases:
        projects = list(dict.fromkeys(project for _alias, project in matched_aliases))
        basis = "current_checkout_nested_project_identity"
        aliases_by_project = {project: alias for alias, project in matched_aliases}
    else:
        return None
    models_by_project: dict[str, list[str]] = {}
    for model, project in bindings.items():
        models_by_project.setdefault(project, []).append(model)
    candidates = [
        {
            "project": project,
            "path": f"projects/com.example.{project}",
            "product_alias": aliases_by_project.get(project, ""),
            "device_models": sorted(models_by_project.get(project, [])),
        }
        for project in projects
    ]
    resolved = projects[0] if len(projects) == 1 else ""
    anchors = [*matched_models, *(alias for alias, _project in matched_aliases), *projects]
    return {
        "status": "ready",
        "architecture_kind": "legacy_project",
        "resolved_project": resolved,
        "match_basis": basis,
        "project_candidates": candidates,
        "modules": [],
        "anchors": list(dict.fromkeys(value for value in anchors if value)),
    }


def _module_search_corpus(*, bug_snapshot: Mapping[str, Any], query_facts: Mapping[str, Any], query: str) -> str:
    values: list[str] = [query]
    for key in ("product", "module", "title", "device_model", "model"):
        values.extend(_iter_text(bug_snapshot.get(key)))
    for key in ("entry_actions", "pages_or_regions", "user_paths", "visible_controls", "event_or_actions", "resource_keys", "actual_texts", "expected_texts", "ticket_texts"):
        values.extend(_iter_text(query_facts.get(key)))
    return "\n".join(values).casefold()


def _module_implementation_index(root: Path) -> dict[str, list[str]]:
    """Index shared module implementations once for one architecture resolution."""
    index: dict[str, list[str]] = {}
    modules_root = root / "modules"
    for path in modules_root.rglob("*"):
        if not path.is_file() or path.suffix.casefold() not in _SOURCE_SUFFIXES:
            continue
        index.setdefault(path.stem.casefold(), []).append(path.relative_to(root).as_posix())
    for paths in index.values():
        paths.sort()
    return index


def _source_dependencies(root: Path, relative_path: str) -> list[str]:
    """Resolve checkout-local static imports used for conservative reachability."""
    owner = (root / relative_path).resolve(strict=False)
    try:
        source = owner.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    imports = re.findall(
        r"(?:\bfrom\s+|\brequire\s*\(\s*|\bimport\s*\(\s*)['\"](?P<path>[^'\"]+)['\"]",
        source,
    )
    dependencies: list[str] = []
    for import_path in imports:
        target = _resolve_relative_source(root, owner, import_path)
        if target is None and not import_path.startswith("."):
            target = _resolve_relative_source(root, root / "__root__.js", f"./{import_path}")
        if target is not None:
            relative = target.relative_to(root).as_posix()
            if relative not in dependencies:
                dependencies.append(relative)
    return dependencies


def _reachable_sources(root: Path, roots: Sequence[str], *, blocked_roots: Sequence[str] = ()) -> set[str]:
    """Follow only literal checkout-local imports; unresolved edges stay unknown."""
    reachable: set[str] = set()
    blocked = {str(path) for path in blocked_roots if str(path).strip()}
    pending = list(dict.fromkeys(str(path) for path in roots if str(path).strip()))
    while pending:
        relative = pending.pop()
        if relative in reachable or relative in blocked:
            continue
        path = root / relative
        if not path.is_file() or path.suffix.casefold() not in _SOURCE_SUFFIXES:
            continue
        reachable.add(relative)
        pending.extend(dependency for dependency in _source_dependencies(root, relative) if dependency not in reachable)
    return reachable


def _implementation_bindings(
    root: Path,
    *,
    model: str,
    payload: Mapping[str, Any],
    implementation_index: Mapping[str, Sequence[str]],
) -> dict[str, set[str]]:
    """Return exact logical-slot bindings declared by current checkout source."""
    bindings: dict[str, set[str]] = {}
    runtime = _runtime_config_for_model(root, model)
    for binding in (runtime or {}).get("route_bindings", []):
        if not isinstance(binding, Mapping):
            continue
        logical = str(binding.get("name") or "").strip()
        path = str(binding.get("implementation_path") or "").strip()
        if logical and path:
            bindings.setdefault(logical, set()).add(path)
    for logical, concrete in _module_config_overrides(payload, implementation_index=implementation_index).items():
        if logical in bindings:
            continue
        paths = {str(path) for path in implementation_index.get(concrete.casefold(), []) if str(path).strip()}
        if paths:
            bindings[logical] = paths
    return bindings


def _product_reachability(
    root: Path,
    *,
    selected_model: str,
    selected_payload: Mapping[str, Any],
    available_configs: Sequence[tuple[str, str, Mapping[str, Any]]],
    implementation_index: Mapping[str, Sequence[str]],
) -> dict[str, list[str]]:
    """Separate selected and provably competing implementations without guessing."""
    selected_bindings = _implementation_bindings(
        root,
        model=selected_model,
        payload=selected_payload,
        implementation_index=implementation_index,
    )
    selected_roots = {path for paths in selected_bindings.values() for path in paths}
    unselected_roots: set[str] = set()
    for model, _config_path, payload in available_configs:
        if model == selected_model:
            continue
        bindings = _implementation_bindings(root, model=model, payload=payload, implementation_index=implementation_index)
        for logical, paths in bindings.items():
            if logical in selected_bindings:
                unselected_roots.update(paths - selected_bindings[logical])
    # Global route registries import mutually exclusive product pages together.
    # A selected implementation may depend on that registry for navigation, but
    # this must not make every registered alternative reachable for the product.
    selected_reachable = _reachable_sources(root, sorted(selected_roots), blocked_roots=sorted(unselected_roots))
    unselected_reachable = _reachable_sources(root, sorted(unselected_roots))
    return {
        "selected_implementation_roots": sorted(selected_roots),
        "unselected_implementation_roots": sorted(unselected_roots),
        "selected_reachable_paths": sorted(selected_reachable),
        "unselected_only_paths": sorted(unselected_reachable - selected_reachable),
    }


def resolve_module_architecture(
    *,
    repository_root: str | Path,
    repository: str,
    bug_snapshot: Mapping[str, Any],
    query_facts: Mapping[str, Any],
    query: str,
    config: Any | None = None,
) -> dict[str, Any]:
    """Resolve model -> module config -> shared implementation from current source.

    This is intentionally repository-derived.  It does not assume that a
    product-specific ``projects/com.example.*`` directory should exist.
    """
    root = Path(repository_root).expanduser().resolve(strict=False)
    if repository not in {"sample_mobile_repo", "sample_platform_repo"}:
        return {"status": "not_applicable", "reason": "module_architecture_not_present", "modules": [], "anchors": []}
    resolution_corpus = _module_search_corpus(bug_snapshot=bug_snapshot, query_facts=query_facts, query=query)
    legacy_resolution = _resolve_legacy_project(root, snapshot=bug_snapshot, corpus=resolution_corpus)
    if not (root / "moduleControl").is_dir() or not (root / "modules").is_dir():
        return legacy_resolution or {"status": "unresolved", "reason": "module_architecture_not_present", "modules": [], "anchors": []}
    bindings = _module_config_bindings(root)
    if not bindings:
        return legacy_resolution or {"status": "unresolved", "reason": "model_config_bindings_not_found", "modules": [], "anchors": []}
    implementation_index = _module_implementation_index(root)

    product_tokens = set(build_product_scope_tokens(bug_snapshot))
    available_configs: list[tuple[str, str, Mapping[str, Any]]] = []
    ranked_configs: list[tuple[int, str, str, Mapping[str, Any]]] = []
    for model, relative in bindings.items():
        path = root / relative
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        available_configs.append((model, relative, payload))
        score = 0
        if model.casefold() in resolution_corpus:
            score += 400
        suffix = Path(relative).stem.removeprefix("exampleModuleConfig_").casefold()
        if suffix in product_tokens:
            score += 260
        device_name = str(payload.get("deviceName") or "").strip()
        if device_name and device_name.casefold() in resolution_corpus:
            score += 320
        if score:
            ranked_configs.append((score, model, relative, payload))
    match_basis = "current_checkout_module_manager_and_config"
    reachability = "confirmed"
    if not ranked_configs:
        family_configs = [
            (model, relative, payload)
            for model, relative, payload in available_configs
            for token in product_tokens
            if len(token) >= 3 and (suffix := Path(relative).stem.removeprefix("exampleModuleConfig_").casefold()).startswith(token) and len(suffix) > len(token) and suffix[len(token)].isalpha()
        ]
        family_configs = list({relative: (model, relative, payload) for model, relative, payload in family_configs}.values())
        signatures = {_module_config_signature(root, model=model, payload=payload, implementation_index=implementation_index) for model, _relative, payload in family_configs}
        if family_configs and len(signatures) == 1:
            ranked_configs = [(180, model, relative, payload) for model, relative, payload in family_configs]
            match_basis = "current_checkout_product_family_consensus"
            reachability = "family_consensus"
        else:
            reason = "product_family_configs_diverge" if family_configs else "model_not_identified_from_ticket"
            return legacy_resolution or {"status": "unresolved", "reason": reason, "modules": [], "anchors": []}
    ranked_configs.sort(key=lambda item: (-item[0], item[1]))
    _score, model, config_path, payload = ranked_configs[0]
    matched_models = [item[1] for item in ranked_configs]
    matched_config_paths = list(dict.fromkeys(item[2] for item in ranked_configs))

    runtime_config = _runtime_config_for_model(root, model)
    module_overrides = _module_config_overrides(payload, implementation_index=implementation_index)
    product_reachability = _product_reachability(
        root,
        selected_model=model,
        selected_payload=payload,
        available_configs=available_configs,
        implementation_index=implementation_index,
    )
    raw_modules = payload.get("modules", []) if isinstance(payload, Mapping) else []
    configured_module_names: list[str] = []
    enabled_module_paths: list[str] = []
    for raw_module in raw_modules:
        if not isinstance(raw_module, Mapping):
            continue
        name = str(raw_module.get("name") or "").strip()
        if not name:
            continue
        configured_module_names.append(name)
        for path in implementation_index.get(name.casefold(), []):
            if path not in enabled_module_paths:
                enabled_module_paths.append(path)
    for concrete_name in module_overrides.values():
        for path in implementation_index.get(concrete_name.casefold(), []):
            if path not in enabled_module_paths:
                enabled_module_paths.append(path)
    return {
        "status": "ready",
        "architecture_kind": "module_config",
        "model": model,
        "models": matched_models,
        "config_path": config_path,
        "config_paths": matched_config_paths,
        "match_basis": match_basis,
        "reachability": reachability,
        "configured_module_count": len(configured_module_names),
        "configured_module_names": configured_module_names,
        "enabled_module_paths": enabled_module_paths,
        **product_reachability,
        **({"runtime_config": runtime_config} if runtime_config else {}),
        "modules": [],
        "anchors": [],
    }


def _product_scope_adjustment(path: str, requested: Sequence[str]) -> tuple[float, str]:
    if not requested:
        return 0.0, "unspecified"
    matched = _EXPLICIT_PATH_PRODUCT.search(path)
    if not matched:
        return 0.0, "shared_or_unlabeled"
    path_token = str(matched.group("token") or matched.group("project") or "").casefold()
    if path_token in requested:
        return 0.08, "matched"
    same_family = any(token.isdigit() == path_token.isdigit() for token in requested)
    # A differently named product override is not a shared implementation.
    # Keep it discoverable, but do not let an unrelated project outrank the
    # current checkout's shared source merely because its copy is similar.
    return (-0.12, "conflicting") if same_family else (-0.18, "unmapped")


def _resolve_secret(value: str | None, file_value: str | None = None) -> str:
    text = str(value or "").strip()
    if text.startswith("$") and len(text) > 1:
        return os.environ.get(text[1:], "")
    if not text and file_value:
        try:
            return Path(file_value).expanduser().read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return text


def _literal_regex(value: str) -> str:
    # Tabby's query grammar tokenizes unquoted spaces and ORs the tokens. Keep
    # one anchor as one regex, and double regex escapes so its tokenizer emits
    # the single backslash required by the Rust regex engine.
    literal = _REGEX_META.sub(r"\\\1", value)
    encoded = literal.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{encoded}"'


def _refresh_tabby_access(*, endpoint: str, access_file: str | None, refresh_file: str, timeout: float) -> str:
    refresh_path = Path(refresh_file).expanduser()
    refresh_token = refresh_path.read_text(encoding="utf-8").strip()
    if not refresh_token:
        raise ValueError("empty Tabby refresh token")
    response = httpx.post(
        endpoint,
        json={
            "query": """
                mutation RefreshToken($refreshToken: String!) {
                  refreshToken(refreshToken: $refreshToken) { accessToken refreshToken }
                }
            """,
            "variables": {"refreshToken": refresh_token},
        },
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    refreshed = payload.get("data", {}).get("refreshToken", {}) if isinstance(payload, Mapping) else {}
    access_token = refreshed.get("accessToken") if isinstance(refreshed, Mapping) else None
    next_refresh = refreshed.get("refreshToken") if isinstance(refreshed, Mapping) else None
    if not isinstance(access_token, str) or not access_token or not isinstance(next_refresh, str) or not next_refresh:
        raise ValueError("Tabby session refresh failed")
    refresh_path.write_text(next_refresh + "\n", encoding="utf-8")
    refresh_path.chmod(0o600)
    if access_file:
        access_path = Path(access_file).expanduser()
        access_path.write_text(access_token + "\n", encoding="utf-8")
        access_path.chmod(0o600)
    return access_token


def _graphql(*, endpoint: str, headers: Mapping[str, str], query: str, variables: Mapping[str, Any], timeout: float) -> Mapping[str, Any]:
    response = httpx.post(
        endpoint,
        json={"query": query, "variables": dict(variables)},
        headers=dict(headers),
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, Mapping) or payload.get("errors"):
        raise ValueError("invalid Tabby GraphQL response")
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise ValueError("missing Tabby GraphQL data")
    return data


def _subscription_url(base_url: str) -> str:
    parsed = urlsplit(base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunsplit((scheme, parsed.netloc, "/subscriptions", "", ""))


async def _collect_tabby_hybrid_items_async(
    *,
    base_url: str,
    authorization: str,
    source_id: str,
    query_text: str,
    timeout: float,
    limit: int,
) -> tuple[list[dict[str, Any]], str | None]:
    """Collect Tabby's own BM25/embedding/RRF hits without asking it to answer.

    Tabby 0.32 exposes ``RetrievalService.collect_relevant_code`` only through
    its Answer Engine subscription.  The code-attachment event is emitted
    before answer generation; closing the subscription at that event reuses
    the retriever while keeping Bug Workbench's Codex-only investigation path.
    """
    operation_id = "deerflow-code-retrieval"
    subscription = """
        subscription DeerFlowCodeRetrieval($input: CreateThreadAndRunInput!) {
          createThreadAndRun(input: $input) {
            __typename
            ... on ThreadCreated { id }
            ... on ThreadAssistantMessageReadingCode { snippet fileList }
            ... on ThreadAssistantMessageAttachmentsCode {
              hits {
                code { gitUrl commit filepath language content startLine }
                scores { rrf bm25 embedding }
              }
            }
          }
        }
    """
    variables = {
        "input": {
            "thread": {"userMessage": {"content": query_text}},
            "options": {
                "generateRelevantQuestions": False,
                "codeQuery": {"sourceId": source_id, "content": query_text},
                "debugOptions": {
                    "returnChatCompletionRequest": False,
                    "codeSearchParamsOverride": {
                        "minEmbeddingScore": 0.0,
                        # Tabby applies each channel floor independently.  A
                        # zero BM25 score is normal for a semantic-only match,
                        # so admitting it requires a floor below zero.
                        "minBm25Score": -1.0,
                        "minRrfScore": 0.0,
                        "numToReturn": limit,
                        "numToScore": max(limit * 2, 24),
                    },
                },
            },
        }
    }
    connection_payload = {"authorization": authorization} if authorization else {}
    items: list[dict[str, Any]] = []
    thread_id: str | None = None
    async with websockets.connect(
        _subscription_url(base_url),
        subprotocols=["graphql-transport-ws"],
        open_timeout=timeout,
        close_timeout=min(timeout, 3.0),
    ) as websocket:
        await websocket.send(json.dumps({"type": "connection_init", "payload": connection_payload}))
        acknowledgement = json.loads(await asyncio.wait_for(websocket.recv(), timeout=timeout))
        if acknowledgement.get("type") != "connection_ack":
            raise ValueError("Tabby subscription authentication failed")
        await websocket.send(
            json.dumps(
                {
                    "id": operation_id,
                    "type": "subscribe",
                    "payload": {"query": subscription, "variables": variables},
                }
            )
        )
        while True:
            message = json.loads(await asyncio.wait_for(websocket.recv(), timeout=timeout))
            message_type = message.get("type")
            if message_type == "next":
                payload = message.get("payload")
                if not isinstance(payload, Mapping) or payload.get("errors"):
                    raise ValueError("invalid Tabby retrieval subscription response")
                data = payload.get("data")
                event = data.get("createThreadAndRun") if isinstance(data, Mapping) else None
                if not isinstance(event, Mapping):
                    continue
                if event.get("__typename") == "ThreadCreated":
                    thread_id = str(event.get("id") or "") or None
                    continue
                if event.get("__typename") == "ThreadAssistantMessageReadingCode" and not event.get("snippet"):
                    await websocket.send(json.dumps({"id": operation_id, "type": "complete"}))
                    return [], thread_id
                if event.get("__typename") != "ThreadAssistantMessageAttachmentsCode":
                    continue
                hits = event.get("hits")
                if isinstance(hits, Sequence) and not isinstance(hits, (str, bytes, bytearray)):
                    for rank, hit in enumerate(hits[:limit]):
                        if not isinstance(hit, Mapping):
                            continue
                        code = hit.get("code")
                        scores = hit.get("scores")
                        if not isinstance(code, Mapping):
                            continue
                        score_map = scores if isinstance(scores, Mapping) else {}
                        try:
                            rrf = float(score_map.get("rrf") or 0.0)
                        except (TypeError, ValueError):
                            rrf = 0.0
                        items.append(
                            {
                                "filepath": code.get("filepath"),
                                "start_line": code.get("startLine") or 1,
                                "content": code.get("content"),
                                "score": rrf,
                                "matched_anchor": query_text[:160],
                                "retrieval_stage": "tabby_hybrid",
                                "tabby_rank": rank,
                                "tabby_scores": dict(score_map),
                            }
                        )
                await websocket.send(json.dumps({"id": operation_id, "type": "complete"}))
                return items, thread_id
            if message_type == "error":
                raise ValueError("Tabby retrieval subscription failed")
            if message_type == "complete":
                return items, thread_id


def _collect_tabby_hybrid_items(
    *,
    base_url: str,
    authorization: str,
    source_id: str,
    query_text: str,
    timeout: float,
    limit: int,
) -> tuple[list[dict[str, Any]], str | None]:
    return asyncio.run(
        _collect_tabby_hybrid_items_async(
            base_url=base_url,
            authorization=authorization,
            source_id=source_id,
            query_text=query_text,
            timeout=timeout,
            limit=limit,
        )
    )


def _line_text(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    text = value.get("text")
    if isinstance(text, str):
        return text
    encoded = value.get("base64")
    if isinstance(encoded, str) and encoded:
        try:
            return base64.b64decode(encoded, validate=True).decode("utf-8", errors="replace")
        except (ValueError, OSError):
            return ""
    return ""


def _grep_items(files: Any, *, anchor: str, anchor_rank: int, retrieval_stage: str = "exact_anchor") -> list[dict[str, Any]]:
    """Convert Tabby grep context into exact, contiguous source snippets."""
    items: list[dict[str, Any]] = []
    if not isinstance(files, Sequence) or isinstance(files, (str, bytes, bytearray)):
        return items
    for file in files:
        if not isinstance(file, Mapping):
            continue
        path = str(file.get("path") or "").strip()
        raw_lines = file.get("lines")
        if not path or not isinstance(raw_lines, Sequence):
            continue
        groups: list[list[tuple[int, str]]] = []
        for raw_line in raw_lines:
            if not isinstance(raw_line, Mapping):
                continue
            try:
                number = int(raw_line.get("lineNumber"))
            except (TypeError, ValueError):
                continue
            text = _line_text(raw_line.get("line"))
            if not text:
                continue
            if not groups or number != groups[-1][-1][0] + 1:
                groups.append([])
            groups[-1].append((number, text))
        for group in groups:
            snippet = "".join(text for _number, text in group).strip("\n")
            if not snippet:
                continue
            items.append(
                {
                    "filepath": path,
                    "start_line": group[0][0],
                    "content": snippet,
                    "score": float(max(1, 100 - anchor_rank)),
                    "matched_anchor": anchor,
                    "retrieval_stage": retrieval_stage,
                }
            )
    return items


def _implementation_layer(path: str) -> str:
    """Classify repository structure without inferring responsibility."""
    candidate = Path(path)
    parts = tuple(part.casefold() for part in candidate.parts)
    part_set = set(parts)
    if "android" in part_set:
        return "android_native"
    if "ios" in part_set:
        return "ios_native"
    if part_set & {"harmony", "harmonyos", "ohos"} or "ets" in part_set:
        return "harmony_native"
    if candidate.suffix.casefold() in {".js", ".jsx", ".ts", ".tsx"} or part_set & {"modules", "projects", "lang", "locales", "resources"}:
        return "shared_rn"
    return "unknown"


def _code_path_adjustment(path: str) -> tuple[float, str]:
    candidate = Path(path)
    parts = {part.casefold() for part in candidate.parts}
    if "rawfile" in parts and "bundle" in parts and candidate.name.casefold().startswith("bundle."):
        return -0.12, "generated_bundle"
    is_localized_resource = (candidate.suffix.casefold() in _RESOURCE_SUFFIXES or (candidate.suffix.casefold() in {".js", ".ts"} and bool(parts & {"lang", "langs", "locales"}))) and (
        bool(parts & _RESOURCE_PATH_PARTS) or any(part.startswith("values") or part.endswith(".lproj") for part in parts) or candidate.name.casefold() == "localizable.strings"
    )
    if is_localized_resource:
        return 0.0, "resource"
    if candidate.suffix.casefold() in _SOURCE_SUFFIXES:
        if path.startswith("modules/config/") and candidate.name == "Config.js":
            return 0.0, "configuration"
        return (0.12 if parts & {"page", "pages"} else 0.08), "implementation"
    return 0.0, "other"


def _requested_implementation_layers(query_facts: Mapping[str, Any]) -> tuple[str, ...]:
    values = query_facts.get("candidate_implementation_layers")
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        return ()
    return tuple(dict.fromkeys(str(value) for value in values if str(value) in _IMPLEMENTATION_LAYERS))


def _missing_implementation_layers(items: Sequence[Mapping[str, Any]], requested: Sequence[str]) -> list[str]:
    covered = {_implementation_layer(str(item.get("filepath") or item.get("path") or "")) for item in items}
    return [layer for layer in requested if layer not in covered]


def _layer_balanced_candidates(candidates: Sequence[dict[str, Any]], requested: Sequence[str], *, limit: int) -> list[dict[str, Any]]:
    """Select stronger evidence first and diversify layers only within a tier."""
    ordered = sorted(candidates, key=_candidate_sort_key)
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(item: dict[str, Any]) -> None:
        path = str(item.get("path") or "")
        if path and path not in seen and len(selected) < limit:
            seen.add(path)
            selected.append(item)

    strengths = sorted({int(item.get("evidence_strength") or _retrieval_evidence_strength(item)) for item in ordered}, reverse=True)
    for strength in strengths:
        tier = [item for item in ordered if int(item.get("evidence_strength") or _retrieval_evidence_strength(item)) == strength]
        for layer in requested:
            if any(item.get("implementation_layer") == layer for item in selected):
                continue
            match = next(
                (item for item in tier if item.get("implementation_layer") == layer and item.get("evidence_role") == "implementation"),
                None,
            ) or next((item for item in tier if item.get("implementation_layer") == layer), None)
            if match is not None:
                add(match)
        # Pair consumers and resources only when doing so cannot cross an
        # evidence boundary and displace a stronger candidate.
        for layer in requested:
            present_roles = {item.get("evidence_role") for item in selected if item.get("implementation_layer") == layer}
            complement = next(
                (item for item in tier if item.get("implementation_layer") == layer and item.get("evidence_role") in {"implementation", "resource"} and item.get("evidence_role") not in present_roles),
                None,
            )
            if complement is not None:
                add(complement)
        for item in tier:
            add(item)
        if len(selected) >= limit:
            break
    return selected


def _fuse_candidate_pools(
    *,
    pools: Sequence[Sequence[dict[str, Any]]],
    limit: int,
) -> list[dict[str, Any]]:
    """Fuse independent ranked views once, at file granularity.

    Provider scores are intentionally incomparable (Tabby RRF, repository grep
    rank and architecture relevance).  Reciprocal-rank fusion uses only each
    view's ordering, rewards agreement between views, and prevents multiple
    snippets from one file consuming the bounded candidate budget.
    """
    fused: dict[str, dict[str, Any]] = {}
    for pool in pools:
        seen_in_pool: set[str] = set()
        for rank, raw_item in enumerate(pool):
            path = str(raw_item.get("filepath") or "")
            if not path or path in seen_in_pool:
                continue
            seen_in_pool.add(path)
            contribution = 1.0 / (60.0 + rank + 1.0)
            raw_stage = str(raw_item.get("retrieval_stage") or "unknown")
            raw_strength = _retrieval_evidence_strength(raw_item)
            existing = fused.get(path)
            if existing is None:
                item = dict(raw_item)
                item["provider_score"] = raw_item.get("score")
                item["retrieval_stages"] = [raw_stage]
                item["evidence_strength"] = raw_strength
                item["score"] = contribution
                fused[path] = item
                continue
            fused_score = float(existing.get("score") or 0.0) + contribution
            stages = list(existing.get("retrieval_stages") or ())
            current_strength = int(existing.get("evidence_strength") or 0)
            if raw_strength > current_strength:
                existing.update(raw_item)
            if raw_stage not in stages:
                stages.append(raw_stage)
            existing["score"] = fused_score
            existing["retrieval_stages"] = stages
            existing["evidence_strength"] = max(current_strength, raw_strength)
    # Apply the same evidence-first ordering before every bounded cut. Multiple
    # weak views may improve order inside their tier, never overtake one exact
    # literal fact merely by agreeing with each other.
    ordered = sorted(fused.values(), key=_candidate_sort_key)[:limit]
    for rank, item in enumerate(ordered):
        item["fusion_score"] = item.get("score")
        item["score"] = max(0.0, 1.0 - rank * 0.02)
    return ordered


def _retrieval_evidence_strength(item: Mapping[str, Any]) -> int:
    """Return one stable evidence tier; numeric similarity never changes it."""
    raw_stages = item.get("retrieval_stages")
    stages = [str(value) for value in raw_stages] if isinstance(raw_stages, Sequence) and not isinstance(raw_stages, (str, bytes, bytearray)) else []
    stages.append(str(item.get("retrieval_stage") or "unknown"))
    strengths = {
        "exact_ticket": 5,
        "exact_identifier": 4,
        "tabby_hybrid": 3,
        "concept_expansion": 2,
        "exact_supporting": 2,
        "exact_anchor": 2,
        "exact_code_duty": 1,
        "tabby_code_duty": 1,
    }
    return max((strengths.get(stage, 2) for stage in stages), default=2)


def _candidate_sort_key(item: Mapping[str, Any]) -> tuple[int, float, str]:
    """One ordering key shared by fusion, semantic ranking and selection."""
    return (
        -int(item.get("evidence_strength") or _retrieval_evidence_strength(item)),
        -float(item.get("score") or 0.0),
        str(item.get("filepath") or item.get("path") or ""),
    )


def _allocate_anchor_queries(*, groups: Sequence[Sequence[str]], limit: int) -> list[str]:
    """Share the exact-query budget across ticket, navigation and structure.

    A flat concatenation allowed the first group to consume the whole budget.
    Round-robin allocation keeps every available evidence class represented
    without assigning fixed slots to any product or Bug.
    """
    selected: list[str] = []
    seen: set[str] = set()
    remaining = [list(dict.fromkeys(str(value).strip() for value in group if str(value).strip())) for group in groups if group]
    while len(selected) < limit and any(remaining):
        progressed = False
        for group in remaining:
            while group:
                value = group.pop(0)
                folded = value.casefold()
                if folded in seen:
                    continue
                seen.add(folded)
                selected.append(value)
                progressed = True
                break
            if len(selected) >= limit:
                break
        if not progressed:
            break
    return selected


def _allocate_source_anchor_queries(
    *,
    ticket_anchors: Sequence[str],
    duty_anchors: Sequence[str],
    query_facts: Mapping[str, Any],
    limit: int,
) -> tuple[list[str], dict[str, str]]:
    """Allocate the grep budget with the same evidence ordering as selection."""
    candidates: dict[str, dict[str, Any]] = {}
    order = 0

    def add(values: Sequence[str], stage_for: Any) -> None:
        nonlocal order
        for raw in values:
            value = str(raw).strip()
            folded = value.casefold()
            if not value:
                continue
            stage = stage_for(value) if callable(stage_for) else str(stage_for)
            strength = _retrieval_evidence_strength({"retrieval_stage": stage})
            existing = candidates.get(folded)
            if existing is None:
                candidates[folded] = {"value": value, "stage": stage, "strength": strength, "order": order}
                order += 1
            elif strength > int(existing["strength"]):
                existing["stage"] = stage
                existing["strength"] = strength

    add(ticket_anchors, lambda value: _ticket_anchor_stage(value, query_facts))
    add(duty_anchors, "exact_code_duty")
    ordered = sorted(candidates.values(), key=lambda item: (-int(item["strength"]), int(item["order"])))[:limit]
    anchors = [str(item["value"]) for item in ordered]
    stages = {str(item["value"]).casefold(): str(item["stage"]) for item in ordered}
    return anchors, stages


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=False))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    return dot / (left_norm * right_norm) if left_norm and right_norm else 0.0


def _embed(*, endpoint: str, model: str, texts: Sequence[str], headers: Mapping[str, str], timeout: float) -> list[list[float]]:
    vectors: list[list[float]] = []
    for offset in range(0, len(texts), 8):
        response = httpx.post(
            endpoint,
            json={"model": model, "input": list(texts[offset : offset + 8])},
            headers=dict(headers),
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(data, list):
            raise ValueError("invalid embedding response")
        ordered = sorted((item for item in data if isinstance(item, Mapping)), key=lambda item: int(item.get("index", 0)))
        batch = [item.get("embedding") for item in ordered]
        if len(batch) != len(texts[offset : offset + 8]) or not all(isinstance(item, list) for item in batch):
            raise ValueError("incomplete embedding response")
        vectors.extend(batch)
    return vectors


def _semantic_rank(
    *,
    query: str,
    items: list[dict[str, Any]],
    config: Any,
    timeout: float,
) -> tuple[list[dict[str, Any]], str]:
    endpoint = str(getattr(config, "embedding_base_url", "http://127.0.0.1:18082/v1")).rstrip("/") + "/embeddings"
    model = str(getattr(config, "embedding_model", "example-embedding-model"))
    headers: dict[str, str] = {"Accept": "application/json", "Content-Type": "application/json"}
    key = _resolve_secret(getattr(config, "embedding_api_key", None), getattr(config, "embedding_api_key_file", None))
    if key:
        headers["Authorization"] = f"Bearer {key}"
    bounded = items[: int(getattr(config, "max_raw_candidates", 24))]
    if not bounded:
        return [], "no_lexical_candidate"
    texts = [query] + [f"{item['filepath']}\n{item['content']}"[:2400] for item in bounded]
    try:
        vectors = _embed(endpoint=endpoint, model=model, texts=texts, headers=headers, timeout=timeout)
    except (httpx.HTTPError, ValueError):
        return bounded, "embedding_unavailable"
    query_vector = vectors[0]
    for item, vector in zip(bounded, vectors[1:], strict=False):
        semantic_score = _cosine(query_vector, vector)
        item["semantic_score"] = semantic_score
        lexical_score = float(item.get("score") or 0.0)
        item["lexical_score"] = lexical_score
        # Lexical retrieval is the evidence-producing step. Embeddings may
        # break close ties inside that bounded set, but must not replace the
        # exact/concept rank or erase a literal match that embeds poorly
        # against a long source snippet.
        item["score"] = lexical_score + semantic_score * 0.04
    bounded.sort(key=_candidate_sort_key)
    return bounded, "ready"


def _first(item: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _candidate_from_item(
    *,
    root: Path,
    revision: str,
    item: Mapping[str, Any],
    max_snippet_chars: int,
    require_current_revision: bool,
    runtime_dist_roots: frozenset[Path] = frozenset(),
) -> tuple[dict[str, Any] | None, str]:
    raw_path = str(_first(item, "filepath", "file_path", "path", "file") or "").strip()
    if not raw_path:
        return None, "missing_path"
    path = Path(raw_path)
    if path.is_absolute():
        try:
            relative = path.resolve(strict=False).relative_to(root).as_posix()
        except ValueError:
            return None, "outside_repository"
    else:
        relative = raw_path.lstrip("/")
        path = (root / relative).resolve(strict=False)
        try:
            path.relative_to(root)
        except ValueError:
            return None, "outside_repository"
    ignored_parts = set(Path(relative).parts) & _IGNORED_PARTS
    if not path.is_file() or path.is_symlink() or (ignored_parts and not (ignored_parts == {"dist"} and path_is_in_runtime_dist(Path(relative), runtime_dist_roots))):
        return None, "missing_or_ignored_file"

    source = path.read_text(encoding="utf-8", errors="replace")
    raw_snippet = str(_first(item, "body", "content", "text", "snippet") or "").strip()
    try:
        line = int(_first(item, "start_line", "line", "line_start") or 1)
    except (TypeError, ValueError):
        line = 1
    line = max(1, line)
    matched_snippet = ""
    if raw_snippet:
        normalized = raw_snippet.replace("\r\n", "\n")
        if normalized in source:
            matched_snippet = normalized
            line = source[: source.index(normalized)].count("\n") + 1
        else:
            compact = "\n".join(part.rstrip() for part in normalized.splitlines()).strip()
            if compact and compact in source:
                matched_snippet = compact
                line = source[: source.index(compact)].count("\n") + 1
    if not matched_snippet and require_current_revision:
        # Tabby's raw search response does not consistently expose an indexed
        # commit.  Exact snippet membership in the selected checkout is the
        # revision gate: a stale or path-only hit never enters the prompt.
        return None, "stale_or_unverifiable_snippet"
    if not matched_snippet:
        lines = source.splitlines()
        if line > len(lines):
            return None, "stale_snippet"
        start = max(0, line - 4)
        matched_snippet = "\n".join(lines[start : min(len(lines), start + 12)]).strip()
        line = start + 1
    if not matched_snippet:
        return None, "empty_current_source"

    score_value = _first(item, "score", "relevance", "rank_score")
    try:
        score = float(score_value) if score_value is not None else None
    except (TypeError, ValueError):
        score = None
    snippet = matched_snippet[:max_snippet_chars]
    return (
        {
            "path": relative,
            "entry_line": line,
            "view_range": [max(1, line - 20), line + 59],
            "symbol": str(_first(item, "name", "symbol", "title", "matched_anchor") or "")[:160],
            "snippet": snippet,
            "score": score,
            "source": "tabby_repository_context",
            "engine": "tabby",
            "confidence": "candidate",
            "provenance": "hybrid repository retrieval; causality unverified",
            "source_revision": _path_revision(path, revision),
            "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "current_source_verified": True,
            "retrieval_stage": str(item.get("retrieval_stage") or "exact_anchor"),
            "retrieval_stages": [str(value) for value in item.get("retrieval_stages", []) if str(value).strip()],
            "evidence_strength": int(item.get("evidence_strength") or _retrieval_evidence_strength(item)),
            "candidate_kind": str(item.get("candidate_kind") or "other"),
            "implementation_layer": str(item.get("implementation_layer") or _implementation_layer(relative)),
            "evidence_role": str(item.get("candidate_kind") or "other"),
            "product_scope": str(item.get("product_scope") or "unspecified"),
            "verified_edges": [],
            "reachability": str(item.get("reachability") or "unresolved"),
        },
        "",
    )


def build_source_retrieval(
    *,
    repository_root: Path,
    repository: str,
    bug_snapshot: Mapping[str, Any],
    query_facts: Mapping[str, Any],
    runtime_evidence: Mapping[str, Any],
    config: Any,
    retrieval_aliases: Sequence[str] = (),
) -> dict[str, Any]:
    """Query Tabby and return a bounded, current-checkout candidate packet."""
    root = repository_root.expanduser().resolve()
    revision = _revision(root)
    enabled = bool(getattr(config, "enabled", False))
    runtime_pre_scan = runtime_evidence.get("runtime_pre_scan")
    device_identifiers = tuple(str(value).strip() for value in (runtime_pre_scan.get("ticket_device_identities", ()) if isinstance(runtime_pre_scan, Mapping) else ()) if str(value).strip())
    source_bug_snapshot = _without_runtime_identities(bug_snapshot, device_identifiers)
    source_query_facts = _without_runtime_identities(query_facts, device_identifiers)
    query = build_retrieval_query(
        bug_snapshot=source_bug_snapshot,
        query_facts=source_query_facts,
        runtime_evidence=runtime_evidence,
        max_chars=int(getattr(config, "max_query_chars", 1600)),
        retrieval_aliases=retrieval_aliases,
    )
    duty_query = build_code_duty_query(
        bug_snapshot=source_bug_snapshot,
        query_facts=source_query_facts,
        max_chars=min(640, int(getattr(config, "max_query_chars", 1600))),
    )
    architecture_resolution = resolve_module_architecture(
        repository_root=root,
        repository=repository,
        bug_snapshot=source_bug_snapshot,
        query_facts=source_query_facts,
        query=query,
        config=config,
    )
    unselected_only_paths = {str(path) for path in architecture_resolution.get("unselected_only_paths", []) if str(path).strip()}
    architecture_kind = str(architecture_resolution.get("architecture_kind") or "")
    declared_products = _recognized_declared_product_tokens(root, source_bug_snapshot)
    enabled_module_paths = {str(path) for path in architecture_resolution.get("enabled_module_paths", []) if str(path).strip()}
    resolved_project = str(architecture_resolution.get("resolved_project") or "").strip()
    selected_project_prefix = f"projects/com.example.{resolved_project}/" if architecture_kind == "legacy_project" and resolved_project else ""
    scope_rejections: list[dict[str, str]] = []

    def product_scope_rejection_reason(path: str, *, include_project_overrides: bool) -> str:
        if architecture_kind == "module_config" and path in unselected_only_paths:
            return "unselected_product_implementation"
        if not include_project_overrides or not path.startswith("projects/com.example."):
            return ""
        unselected_override = (
            (architecture_kind == "module_config" and path not in enabled_module_paths)
            or (architecture_kind == "legacy_project" and bool(selected_project_prefix) and not path.startswith(selected_project_prefix))
            or _project_override_conflicts_with_declared_product(
                root,
                path=path,
                declared_products=declared_products,
            )
        )
        return "unselected_product_override" if unselected_override else ""

    def retain_product_scope(
        items: Sequence[dict[str, Any]],
        *,
        include_project_overrides: bool = False,
        rejections: list[dict[str, str]] | None = None,
    ) -> list[dict[str, Any]]:
        rejection_sink = scope_rejections if rejections is None else rejections
        retained: list[dict[str, Any]] = []
        for item in items:
            path = str(item.get("filepath") or "")
            reason = product_scope_rejection_reason(
                path,
                include_project_overrides=include_project_overrides,
            )
            if reason:
                if len(rejection_sink) < 12 and not any(entry["path"] == path for entry in rejection_sink):
                    rejection_sink.append({"path": path[:300], "reason": reason})
                continue
            retained.append(item)
        return retained

    base = {
        "schema_version": 4,
        "provider": "tabby",
        "repository": repository,
        "source_revision": revision,
        "query": query,
        "duty_query": duty_query,
        "entries": [],
        "relations": [],
        "resource_coverage": [],
        "architecture_resolution": architecture_resolution,
        "rejections": [],
        "metrics": {
            "query_chars": len(query),
            "duty_query_chars": len(duty_query),
            "source_excluded_device_identifier_count": len(device_identifiers),
            "raw_candidate_count": 0,
            "entry_count": 0,
        },
    }
    if not enabled:
        return {**base, "status": "disabled", "reason": "source_retrieval_disabled"}
    if not query:
        return {**base, "status": "no_match", "reason": "no_retrieval_query"}

    endpoint = str(getattr(config, "base_url", "http://127.0.0.1:8080")).rstrip("/") + "/" + str(getattr(config, "search_path", "/graphql")).lstrip("/")
    headers: dict[str, str] = {"Accept": "application/json", "Content-Type": "application/json"}
    repository_urls = getattr(config, "repository_urls", {})
    git_url = repository_urls.get(repository) if isinstance(repository_urls, Mapping) else None
    timeout = float(getattr(config, "timeout_seconds", 12.0))
    access_file = getattr(config, "api_key_file", None)
    refresh_file = getattr(config, "refresh_token_file", None)
    try:
        token = (
            _refresh_tabby_access(
                endpoint=endpoint,
                access_file=str(access_file) if access_file else None,
                refresh_file=str(refresh_file),
                timeout=timeout,
            )
            if refresh_file
            else _resolve_secret(getattr(config, "api_key", None), access_file)
        )
    except (httpx.HTTPError, OSError, ValueError) as exc:
        return {**base, "status": "unavailable", "reason": type(exc).__name__}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    max_anchor_queries = int(getattr(config, "max_anchor_queries", 8))
    ticket_anchors = build_retrieval_anchors(
        query=query,
        query_facts=source_query_facts,
        max_anchors=max_anchor_queries,
        retrieval_aliases=retrieval_aliases,
    )
    duty_anchors = _code_duty_anchor_terms(bug_snapshot=source_bug_snapshot, query_facts=source_query_facts)
    anchors, anchor_stages = _allocate_source_anchor_queries(
        ticket_anchors=ticket_anchors,
        duty_anchors=duty_anchors,
        query_facts=source_query_facts,
        limit=max_anchor_queries,
    )
    fallback_concepts: list[str] = []
    requested_layers = _requested_implementation_layers(source_query_facts)
    product_scope_tokens = build_product_scope_tokens(
        source_bug_snapshot,
        retrieval_aliases=retrieval_aliases,
    )
    for candidate in architecture_resolution.get("project_candidates", []):
        if not isinstance(candidate, Mapping):
            continue
        project = str(candidate.get("project") or "").strip().casefold()
        if project and project not in product_scope_tokens:
            product_scope_tokens.append(project)
    max_raw_candidates = int(getattr(config, "max_raw_candidates", 24))
    retrieval_candidate_limit = max_raw_candidates + min(len(unselected_only_paths), max_raw_candidates)
    try:
        repositories = _graphql(
            endpoint=endpoint,
            headers=headers,
            query="query RepositoryList { repositoryList { id sourceId kind gitUrl } }",
            variables={},
            timeout=timeout,
        )
        repository_list = repositories.get("repositoryList")
        selected = (
            next(
                (item for item in repository_list if isinstance(item, Mapping) and str(item.get("gitUrl") or "") == str(git_url or "")),
                None,
            )
            if isinstance(repository_list, Sequence)
            else None
        )
        if not isinstance(selected, Mapping):
            return {**base, "status": "unavailable", "reason": "tabby_repository_not_registered"}
        hybrid_items: list[dict[str, Any]] = []
        duty_items: list[dict[str, Any]] = []
        hybrid_status = "unavailable"
        duty_status = "not_run"
        source_id = str(selected.get("sourceId") or "")

        def collect_hybrid(query_text: str, *, stage: str) -> tuple[list[dict[str, Any]], str]:
            if not source_id or not query_text.strip():
                return [], "no_match"
            thread_id: str | None = None
            try:
                items, thread_id = _collect_tabby_hybrid_items(
                    base_url=str(getattr(config, "base_url", "http://127.0.0.1:8080")),
                    authorization=headers.get("Authorization", ""),
                    source_id=source_id,
                    query_text=query_text,
                    timeout=timeout,
                    limit=retrieval_candidate_limit,
                )
                for item in items:
                    item["retrieval_stage"] = stage
                items = retain_product_scope(items)
                return items, "ready" if items else "no_match"
            except (OSError, TimeoutError, ValueError, WebSocketException):
                return [], "unavailable"
            finally:
                if thread_id:
                    try:
                        _graphql(
                            endpoint=endpoint,
                            headers=headers,
                            query="mutation DeleteRetrievalThread($id: ID!) { deleteThread(id: $id) }",
                            variables={"id": thread_id},
                            timeout=timeout,
                        )
                    except (httpx.HTTPError, ValueError):
                        pass

        if source_id:
            hybrid_items, hybrid_status = collect_hybrid(query, stage="tabby_hybrid")
            if duty_query and duty_query != query:
                duty_items, duty_status = collect_hybrid(duty_query, stage="tabby_code_duty")
        exact_items: list[dict[str, Any]] = []
        concept_items: list[dict[str, Any]] = []
        grep_query = """
            query RepositoryGrep($kind: RepositoryKind!, $id: ID!, $query: String!) {
              repositoryGrep(kind: $kind, id: $id, query: $query) {
                files { path lines { line { text base64 } lineNumber } }
                elapsedMs
              }
            }
        """

        def retrieve(
            terms: Sequence[str],
            *,
            stage: str,
            destination: list[dict[str, Any]],
            stages_by_term: Mapping[str, str] | None = None,
        ) -> None:
            term_pools: list[list[dict[str, Any]]] = []
            for anchor_rank, anchor in enumerate(terms):
                data = _graphql(
                    endpoint=endpoint,
                    headers=headers,
                    query=grep_query,
                    variables={
                        "kind": str(selected.get("kind") or "GIT_CONFIG"),
                        "id": str(selected.get("id") or ""),
                        "query": _literal_regex(anchor),
                    },
                    timeout=timeout,
                )
                grep = data.get("repositoryGrep")
                if isinstance(grep, Mapping):
                    anchor_stage = (stages_by_term or {}).get(anchor.casefold(), stage)
                    anchor_items = retain_product_scope(_grep_items(grep.get("files"), anchor=anchor, anchor_rank=anchor_rank, retrieval_stage=anchor_stage))
                    anchor_items.sort(
                        key=lambda item: (
                            _product_scope_adjustment(str(item.get("filepath") or ""), product_scope_tokens)[0],
                            str(item.get("filepath") or "").startswith("modules/"),
                        ),
                        reverse=True,
                    )
                    term_items: list[dict[str, Any]] = []
                    seen_anchor_paths: set[str] = set()
                    for item in anchor_items:
                        path = str(item.get("filepath") or "")
                        if not path or path in seen_anchor_paths:
                            continue
                        seen_anchor_paths.add(path)
                        term_items.append(item)
                    # Fuse every returned file before the shared candidate cap:
                    # per-term slicing discarded consumers before agreement
                    # across independent anchors could affect their rank.
                    if term_items:
                        term_pools.append(term_items)
            destination.extend(
                _fuse_candidate_pools(
                    pools=term_pools,
                    limit=max_raw_candidates,
                )
            )

        retrieve(anchors, stage="exact_anchor", destination=exact_items, stages_by_term=anchor_stages)
        pre_fallback_items = [*hybrid_items, *duty_items, *exact_items]
        missing_layers_before_fallback = _missing_implementation_layers(pre_fallback_items, requested_layers)
        if not hybrid_items or not duty_items or missing_layers_before_fallback:
            fallback_concepts = build_fallback_concepts(
                bug_snapshot=source_bug_snapshot,
                query_facts=source_query_facts,
                runtime_evidence=runtime_evidence,
                exact_anchors=anchors,
                max_concepts=int(getattr(config, "max_fallback_concepts", 8)),
            )
            retrieve(fallback_concepts, stage="concept_expansion", destination=concept_items)
        lexical_items = _fuse_candidate_pools(
            pools=(exact_items, concept_items),
            limit=max_raw_candidates,
        )
        ranking_query = "\n".join(part for part in (duty_query, query) if part).strip()[:2_400]
        lexical_items, semantic_status = _semantic_rank(
            query=ranking_query,
            items=lexical_items,
            config=config,
            timeout=timeout,
        )
        raw_items = _fuse_candidate_pools(
            pools=(hybrid_items, duty_items, lexical_items),
            limit=max_raw_candidates,
        )
    except (httpx.HTTPError, ValueError) as exc:
        return {**base, "status": "unavailable", "reason": type(exc).__name__}
    raw_candidate_count = len(raw_items)
    for item in raw_items:
        adjustment, scope = _product_scope_adjustment(str(item.get("filepath") or ""), product_scope_tokens)
        code_adjustment, candidate_kind = _code_path_adjustment(str(item.get("filepath") or ""))
        item["product_scope"] = scope
        item["candidate_kind"] = candidate_kind
        item["implementation_layer"] = _implementation_layer(str(item.get("filepath") or ""))
        item["evidence_strength"] = _retrieval_evidence_strength(item)
        item["score"] = float(item.get("score") or 0.0) + adjustment + code_adjustment
    raw_items.sort(key=_candidate_sort_key)
    validated_candidates: list[dict[str, Any]] = []
    rejections: list[dict[str, str]] = list(scope_rejections)
    if architecture_kind in {"module_config", "legacy_project"} or declared_products:
        raw_items = retain_product_scope(
            raw_items,
            include_project_overrides=True,
            rejections=rejections,
        )
    seen: set[str] = set()
    max_candidates = int(getattr(config, "max_candidates", 5))
    max_snippet_chars = int(getattr(config, "max_snippet_chars", 1800))
    max_packet_chars = int(getattr(config, "max_packet_chars", 10000))
    require_current_revision = bool(getattr(config, "require_current_revision", True))
    runtime_dist_roots = discover_tracked_runtime_dist_roots(root)
    for item in raw_items:
        candidate, reason = _candidate_from_item(
            root=root,
            revision=revision,
            item=item,
            max_snippet_chars=max_snippet_chars,
            require_current_revision=require_current_revision,
            runtime_dist_roots=runtime_dist_roots,
        )
        if candidate is None:
            if len(rejections) < 12:
                rejections.append(
                    {
                        "path": str(_first(item, "filepath", "file_path", "path", "file") or "")[:300],
                        "reason": reason,
                    }
                )
            continue
        if candidate["path"] in seen:
            continue
        seen.add(candidate["path"])
        validated_candidates.append(candidate)
    entries: list[dict[str, Any]] = []
    packet_chars = 0
    for candidate in _layer_balanced_candidates(validated_candidates, requested_layers, limit=max_candidates):
        cost = len(candidate.get("snippet") or "") + len(candidate["path"])
        if packet_chars + cost > max_packet_chars:
            rejections.append({"path": candidate["path"], "reason": "packet_budget"})
            continue
        entries.append(candidate)
        packet_chars += cost
    return {
        **base,
        "status": "ready" if entries else "no_match",
        "reason": "" if entries else "no_current_checkout_candidate",
        "entries": entries,
        "rejections": rejections,
        "metrics": {
            "query_chars": len(query),
            "duty_query_chars": len(duty_query),
            "source_excluded_device_identifier_count": len(device_identifiers),
            "raw_candidate_count": raw_candidate_count,
            "ranked_candidate_count": len(raw_items),
            "entry_count": len(entries),
            "packet_chars": packet_chars,
            "anchor_count": len(anchors),
            "exact_anchors": anchors,
            "fallback_concept_count": len(fallback_concepts),
            "fallback_concepts": fallback_concepts,
            "requested_implementation_layers": list(requested_layers),
            "missing_layers_before_fallback": missing_layers_before_fallback,
            "missing_layers_after_selection": _missing_implementation_layers(entries, requested_layers),
            "architecture_status": architecture_resolution.get("status"),
            "architecture_model": architecture_resolution.get("model"),
            "architecture_module_count": int(architecture_resolution.get("configured_module_count") or 0),
            "product_scope_tokens": product_scope_tokens,
            "business_alias_count": len(tuple(retrieval_aliases)),
            "retrieval_mode": ("tabby_hybrid" if hybrid_items else "layered" if exact_items and concept_items else "concept_fallback" if concept_items else "exact_anchor" if exact_items else "none"),
            "tabby_hybrid_status": hybrid_status,
            "tabby_hybrid_candidate_count": len(hybrid_items),
            "tabby_code_duty_status": duty_status,
            "tabby_code_duty_candidate_count": len(duty_items),
            "semantic_status": semantic_status,
        },
    }
