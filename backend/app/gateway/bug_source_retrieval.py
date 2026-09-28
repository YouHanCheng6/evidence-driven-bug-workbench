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
    r"projects/com\.loock\.(?P<project>[A-Za-z0-9_-]+))",
    re.IGNORECASE,
)
_SOURCE_SUFFIXES = frozenset({".java", ".kt", ".m", ".mm", ".swift", ".js", ".jsx", ".ts", ".tsx"})
_RESOURCE_SUFFIXES = frozenset({".strings", ".xml", ".json"})
_RESOURCE_PATH_PARTS = frozenset({"assets", "images", "lang", "locales", "resources", "res"})
_IMPLEMENTATION_LAYERS = frozenset({"android_native", "ios_native", "shared_rn", "harmony_native", "sample_platform_repo"})


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
        for match in _API_PATH.finditer(text):
            endpoint = match.group(0).rstrip(".,;:")
            if len(endpoint) <= 80:
                yield endpoint


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

    for key in ("title", "description", "steps", "actual", "expected"):
        add(160, list(_iter_api_paths(bug_snapshot.get(key))))
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
    log_evidence = runtime_evidence.get("log_evidence")
    if isinstance(log_evidence, Mapping):
        for item in log_evidence.get("accepted_evidence", []) if isinstance(log_evidence.get("accepted_evidence"), Sequence) else ():
            if isinstance(item, Mapping):
                add(160, list(_iter_api_paths(item.get("summary"))))

    add(140, retrieval_aliases)
    # The ticket states the behavior being investigated. Keep that semantic
    # center ahead of visual scene descriptions, which usually describe where
    # something was seen rather than the code path that produced the failure.
    for key, score in (("title", 135), ("actual", 125), ("expected", 125), ("description", 95), ("steps", 85)):
        add(score, bug_snapshot.get(key))
    for key in ("actual_texts", "expected_texts", "resource_keys", "event_ids"):
        add(110, list(_iter_text(query_facts.get(key)))[:4])
    for key in ("pages_or_regions", "user_paths", "visible_controls", "event_or_actions"):
        add(55, list(_iter_text(query_facts.get(key)))[:2])

    log_evidence = runtime_evidence.get("log_evidence")
    if isinstance(log_evidence, Mapping):
        for item in log_evidence.get("accepted_evidence", []) if isinstance(log_evidence.get("accepted_evidence"), Sequence) else ():
            if isinstance(item, Mapping):
                add(110, item.get("summary"))
                add(105, item.get("sources"))

    parts: list[str] = []
    used = 0
    for _score, text in sorted(ranked, key=lambda item: (-item[0], len(item[1]), item[1])):
        cost = len(text) + (1 if parts else 0)
        if used + cost > max_chars:
            continue
        parts.append(text)
        used += cost
    return "\n".join(parts)


def build_module_intent_query(
    *,
    bug_snapshot: Mapping[str, Any],
    query_facts: Mapping[str, Any],
    max_chars: int = 1_600,
) -> str:
    """Build the ticket-led query used only to rank product modules.

    Runtime logs remain available to repository retrieval and model/config
    resolution, but they must not choose the primary UI/business module. A log
    commonly describes a downstream native consumer rather than the surface
    where the reported action starts.
    """
    parts: list[str] = []
    seen: set[str] = set()
    used = 0

    def add(label: str, value: Any, *, limit: int) -> None:
        nonlocal used
        added = 0
        for raw in _iter_text(value):
            text = raw.strip(" \t\r\n，。；：、|[]【】")
            folded = text.casefold()
            if len(text) < 2 or folded in _GENERIC_TERMS or folded in seen:
                continue
            text = text[:240]
            rendered = f"{label}：{text}"
            cost = len(rendered) + (1 if parts else 0)
            if used + cost > max_chars:
                continue
            seen.add(folded)
            parts.append(rendered)
            used += cost
            added += 1
            if added >= limit:
                break

    add("工单标题", bug_snapshot.get("title"), limit=1)
    for key, label, limit in (
        ("entry_actions", "入口操作", 4),
        ("pages_or_regions", "页面区域", 3),
        ("user_paths", "用户路径", 3),
        ("visible_controls", "可见控件", 3),
        ("event_or_actions", "页面动作", 3),
        ("actual_texts", "实际表现", 3),
        ("expected_texts", "预期表现", 3),
        ("resource_keys", "资源字段", 3),
        ("event_ids", "事件字段", 3),
    ):
        add(label, query_facts.get(key), limit=limit)
    for key, label, limit in (
        ("actual", "实际结果", 2),
        ("expected", "预期结果", 2),
        ("steps", "复现步骤", 3),
        ("description", "问题描述", 2),
    ):
        add(label, bug_snapshot.get(key), limit=limit)
    return "\n".join(parts)


def build_retrieval_anchors(*, query: str, query_facts: Mapping[str, Any], max_anchors: int, retrieval_aliases: Sequence[str] = ()) -> list[str]:
    """Choose bounded exact anchors for Tabby's repository grep.

    Resource keys and event identifiers outrank prose.  Visible UI text is
    still useful for locating localization resources, but long reproduction
    paragraphs are never sent as a grep expression.
    """
    anchors: list[str] = []

    def add(value: str) -> None:
        candidate = re.sub(r"\s+", " ", value).strip(" \t\r\n，。；：、|[]【】`'\"")
        if len(candidate) < 3 or len(candidate) > 80 or candidate.casefold() in _GENERIC_TERMS or _NOISY_IDENTIFIER.fullmatch(candidate) or candidate in _GENERIC_IDENTIFIERS or candidate in anchors:
            return
        anchors.append(candidate)

    for alias in retrieval_aliases:
        add(alias)
    for key in ("resource_keys", "event_ids"):
        for text in _iter_text(query_facts.get(key)):
            for identifier in _IDENTIFIER.findall(text):
                if "_" in identifier or any(char.isupper() for char in identifier[1:]):
                    add(identifier)
            add(text)
    for endpoint in _iter_api_paths(query):
        add(endpoint)
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
            for identifier in _IDENTIFIER.findall(text):
                if len(identifier) >= 6 and ("_" in identifier or any(char.isupper() for char in identifier[1:])):
                    add(identifier)
    for key, limit in prioritized_facts:
        for text in list(_iter_text(query_facts.get(key)))[:limit]:
            add(text)
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


def build_fallback_concepts(
    *,
    bug_snapshot: Mapping[str, Any],
    query_facts: Mapping[str, Any],
    runtime_evidence: Mapping[str, Any],
    exact_anchors: Sequence[str],
    max_concepts: int,
) -> list[str]:
    """Extract bounded short concepts when exact-anchor grep returns nothing.

    This is deliberately deterministic and ticket-led. Runtime-log prose has a
    much lower weight and cannot contribute identifier-shaped terms, preventing
    a firmware symbol from taking over navigation for a client repository.
    """
    fragments: list[tuple[int, bool, str]] = []
    seen_fragments: set[str] = set()

    def add(weight: int, value: Any, *, from_log: bool = False) -> None:
        for raw in _iter_text(value):
            text = re.sub(r"\s+", " ", raw).strip()
            if len(text) < 2 or text in seen_fragments:
                continue
            seen_fragments.add(text)
            fragments.append((weight, from_log, text))

    add(160, list(_iter_text(query_facts.get("entry_actions")))[:5])
    for key in ("actual_texts", "expected_texts", "resource_keys", "event_ids"):
        add(135, query_facts.get(key))
    add(105, query_facts.get("user_paths"))
    for key in ("pages_or_regions", "visible_controls"):
        add(55, query_facts.get(key))
    for key, weight in (("title", 145), ("actual", 120), ("expected", 120), ("steps", 100), ("description", 80)):
        add(weight, bug_snapshot.get(key))
    log_evidence = runtime_evidence.get("log_evidence")
    if isinstance(log_evidence, Mapping):
        accepted = log_evidence.get("accepted_evidence")
        if isinstance(accepted, Sequence) and not isinstance(accepted, (str, bytes, bytearray)):
            for item in accepted:
                if isinstance(item, Mapping):
                    add(25, item.get("summary"), from_log=True)

    # Screenshot-derived paths often repeat the same sentence at increasing
    # nesting depths. Remove only the repeated span from lower-priority copies;
    # preserve their unique suffix/prefix so screenshot-only facts are not lost.
    compacted: list[tuple[int, bool, str]] = []
    for fragment in sorted(fragments, key=lambda item: (-item[0], len(item[2]), item[2])):
        weight, from_log, text = fragment
        remainder = text
        for existing in compacted:
            if len(existing[2]) >= 6 and existing[2] in remainder:
                remainder = remainder.replace(existing[2], " ")
        remainder = re.sub(r"(?:\s*[→>|]+\s*)+", " ", remainder)
        remainder = re.sub(r"\s+", " ", remainder).strip()
        if len(remainder) < 2:
            continue
        compacted.append((weight, from_log, remainder))
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

    for fragment_index, (weight, from_log, raw) in enumerate(fragments):
        text = _FACT_LABEL.sub(" ", raw)
        if not from_log:
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


def _runtime_config_for_model(root: Path, model: str) -> dict[str, Any] | None:
    """Follow the checkout's model-to-runtime-config import, when present."""
    loader = root / "modules" / "config" / "Config.js"
    try:
        lines = loader.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    binding = re.compile(
        r"^\s*['\"](?P<model>[^'\"]+)['\"]\s*:\s*.*?\bimport\(\s*['\"](?P<path>\.[^'\"]+\.js)['\"]\s*\)"
    )
    for line_number, line in enumerate(lines, start=1):
        match = binding.search(line)
        if not match or match.group("model") != model:
            continue
        target = (loader.parent / match.group("path")).resolve(strict=False)
        if not target.is_relative_to(root) or not target.is_file():
            return None
        assignments: list[dict[str, Any]] = []
        try:
            target_lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return None
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
        }
    return None


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
    """Read product aliases from the nested projects' existing Git remotes."""
    projects = root / "projects"
    if not projects.is_dir():
        return {}
    aliases: dict[str, tuple[str, str]] = {}
    for project_path in sorted(projects.glob("com.example.*")):
        if not project_path.is_dir():
            continue
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
        project = project_path.name.removeprefix("com.example.")
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


def _resolve_legacy_project(root: Path, *, snapshot: Mapping[str, Any], corpus: str) -> dict[str, Any] | None:
    bindings = _legacy_project_bindings(root)
    repositories = _legacy_product_repositories(root)
    matched_models = [
        model
        for model in bindings
        if re.search(r"(?<![A-Za-z0-9])" + re.escape(model) + r"(?![A-Za-z0-9])", corpus, re.IGNORECASE)
    ]
    declared_products = _declared_product_tokens(snapshot)
    matched_aliases = [repositories[token] for token in declared_products if token in repositories]
    if matched_models:
        projects = list(dict.fromkeys(bindings[model] for model in matched_models))
        basis = "current_checkout_legacy_device_mapping"
        aliases_by_project: dict[str, str] = {}
    elif matched_aliases:
        projects = list(dict.fromkeys(project for _alias, project in matched_aliases))
        basis = "current_checkout_nested_repository_remote"
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
    for key in ("product", "module", "title", "description", "steps", "actual", "expected", "device_model", "model"):
        values.extend(_iter_text(bug_snapshot.get(key)))
    for key in ("pages_or_regions", "visible_controls", "event_or_actions", "resource_keys", "actual_texts", "expected_texts"):
        values.extend(_iter_text(query_facts.get(key)))
    return "\n".join(values).casefold()


def _module_relevance(name: str, nickname: str, corpus: str) -> int:
    score = 0
    if name and name.casefold() in corpus:
        score += 240
    if nickname and nickname.casefold() in corpus:
        score += 220
    compact = re.sub(r"(?:设置|管理|功能|首页|页面|页)$", "", nickname).strip()
    if len(compact) >= 2 and compact.casefold() in corpus:
        score += 160
    for concept in _HAN_RUN.findall(nickname):
        if len(concept) >= 2 and concept.casefold() in corpus:
            score += min(100, len(concept) * 18)
    return score


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


def _module_profile(*, root: Path, name: str, nickname: str, paths: Sequence[str], concepts: Sequence[str] = ()) -> str:
    """Build a bounded current-source description used only to rank enabled modules."""
    parts = [f"模块名 {name}", f"模块昵称 {nickname}"]
    for relative in list(paths)[:2]:
        path = root / relative
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        parts.append(f"源码路径 {relative}")
        excerpts = [source[:3_000]]
        seen_offsets: list[int] = []
        folded = source.casefold()
        for concept in concepts:
            offset = folded.find(concept.casefold())
            if offset < 0 or any(abs(offset - previous) < 800 for previous in seen_offsets):
                continue
            seen_offsets.append(offset)
            excerpts.append(source[max(0, offset - 700) : offset + 1_100])
            if sum(len(excerpt) for excerpt in excerpts) >= 10_000:
                break
        parts.extend(excerpts)
    return re.sub(r"[ \t]+", " ", "\n".join(parts)).strip()[:12_000]


def _module_profile_overlap(profile: str, concepts: Sequence[str]) -> int:
    folded = profile.casefold()
    return sum(45 + min(len(concept), 8) * 8 for concept in concepts if concept.casefold() in folded)


def _module_intent_concepts(*, bug_snapshot: Mapping[str, Any], query_facts: Mapping[str, Any]) -> list[str]:
    """Extract bounded Chinese page/action phrases for module-profile matching."""
    values: list[str] = []
    for key in ("title", "actual", "expected", "steps", "description"):
        values.extend(_iter_text(bug_snapshot.get(key)))
    for key in (
        "entry_actions",
        "pages_or_regions",
        "user_paths",
        "visible_controls",
        "event_or_actions",
        "actual_texts",
        "expected_texts",
    ):
        values.extend(_iter_text(query_facts.get(key)))
    concepts: list[str] = []
    for value in values:
        for run in _HAN_RUN.findall(value):
            for size in range(min(8, len(run)), 3, -1):
                for offset in range(0, len(run) - size + 1):
                    concept = run[offset : offset + size]
                    if concept not in concepts:
                        concepts.append(concept)
                    if len(concepts) >= 96:
                        return concepts
    return concepts


def _module_intent_overlap(profile: str, concepts: Sequence[str]) -> int:
    folded = profile.casefold()
    matches = [concept for concept in concepts if concept.casefold() in folded]
    matches.sort(key=lambda value: (-len(value), value))
    selected: list[str] = []
    for concept in matches:
        if any(concept in existing for existing in selected):
            continue
        selected.append(concept)
        if len(selected) >= 8:
            break
    return min(320, sum((len(concept) - 3) * 30 for concept in selected))


def _matched_module_intents(profile: str, concepts: Sequence[str], *, limit: int = 3) -> list[str]:
    folded = profile.casefold()
    matches = sorted((concept for concept in concepts if concept.casefold() in folded), key=lambda value: (-len(value), value))
    selected: list[str] = []
    for concept in matches:
        if any(concept in existing for existing in selected):
            continue
        selected.append(concept)
        if len(selected) >= limit:
            break
    return selected


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
    module_intent_query = build_module_intent_query(bug_snapshot=bug_snapshot, query_facts=query_facts)
    module_intent_corpus = module_intent_query.casefold()
    legacy_resolution = _resolve_legacy_project(root, snapshot=bug_snapshot, corpus=resolution_corpus)
    if not (root / "moduleControl").is_dir() or not (root / "modules").is_dir():
        return legacy_resolution or {"status": "unresolved", "reason": "module_architecture_not_present", "modules": [], "anchors": []}
    bindings = _module_config_bindings(root)
    if not bindings:
        return legacy_resolution or {"status": "unresolved", "reason": "model_config_bindings_not_found", "modules": [], "anchors": []}

    product_tokens = set(build_product_scope_tokens(bug_snapshot))
    ranked_configs: list[tuple[int, str, str, Mapping[str, Any]]] = []
    for model, relative in bindings.items():
        path = root / relative
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
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
    if not ranked_configs:
        return legacy_resolution or {"status": "unresolved", "reason": "model_not_identified_from_ticket", "modules": [], "anchors": []}
    ranked_configs.sort(key=lambda item: (-item[0], item[1]))
    _score, model, config_path, payload = ranked_configs[0]

    implementation_index = _module_implementation_index(root)
    concepts = build_fallback_concepts(
        bug_snapshot=bug_snapshot,
        query_facts=query_facts,
        runtime_evidence={},
        exact_anchors=[],
        max_concepts=8,
    )
    intent_concepts = _module_intent_concepts(bug_snapshot=bug_snapshot, query_facts=query_facts)
    module_candidates: list[dict[str, Any]] = []
    for raw_module in payload.get("modules", []) if isinstance(payload, Mapping) else []:
        if not isinstance(raw_module, Mapping):
            continue
        name = str(raw_module.get("name") or "").strip()
        nickname = str(raw_module.get("nickname") or "").strip()
        if not name:
            continue
        paths = implementation_index.get(name.casefold(), [])
        profile = _module_profile(root=root, name=name, nickname=nickname, paths=paths, concepts=(*concepts, *intent_concepts))
        identity_relevance = _module_relevance(name, nickname, module_intent_corpus)
        support_relevance = _module_profile_overlap(profile, concepts)
        business_relevance = identity_relevance + _module_intent_overlap(profile, intent_concepts)
        matched_intents = _matched_module_intents(profile, intent_concepts)
        profile_relevance = support_relevance + business_relevance
        module_candidates.append(
            {
                "filepath": paths[0] if paths else config_path,
                "content": profile,
                "score": float(identity_relevance + profile_relevance),
                "module_name": name,
                "module_nickname": nickname,
                "implementation_paths": paths[:4],
                "intent_score": float(business_relevance),
                "intent_anchors": matched_intents,
                "selection_basis": "module_identity" if identity_relevance else "source_profile_overlap" if profile_relevance else "product_config",
                "retrieval_stage": "module_profile",
            }
        )

    configured_module_count = len(module_candidates)
    selection_status = "lexical_fallback"
    if config is not None and module_candidates:
        module_candidates, selection_status = _semantic_rank(
            query=module_intent_query[:2_400] or query[:2_400],
            items=module_candidates,
            config=config,
            timeout=float(getattr(config, "timeout_seconds", 12.0)),
        )
    else:
        module_candidates.sort(key=lambda item: (-float(item.get("score") or 0.0), str(item.get("module_name") or "")))

    modules = []
    for candidate in module_candidates[:4]:
        semantic_score = candidate.get("semantic_score")
        intent_score = candidate.get("intent_score", 0.0)
        modules.append(
            {
                "name": str(candidate.get("module_name") or ""),
                "nickname": str(candidate.get("module_nickname") or ""),
                "implementation_paths": list(candidate.get("implementation_paths") or []),
                "selection_basis": "semantic_module_profile" if isinstance(semantic_score, (int, float)) else str(candidate.get("selection_basis") or "product_config"),
                "semantic_score": float(semantic_score) if isinstance(semantic_score, (int, float)) else None,
                "intent_score": float(intent_score) if isinstance(intent_score, (int, float)) else 0.0,
                "intent_anchors": list(candidate.get("intent_anchors") or []),
                "profile_excerpt": str(candidate.get("content") or "")[:600],
            }
        )
    anchors = [
        *(module["name"] for module in modules),
        *(anchor for module in modules for anchor in module.get("intent_anchors", [])),
        model,
        Path(config_path).stem,
    ]
    runtime_config = _runtime_config_for_model(root, model)
    return {
        "status": "ready",
        "architecture_kind": "module_config",
        "model": model,
        "config_path": config_path,
        "match_basis": "current_checkout_module_manager_and_config",
        "selection_status": selection_status,
        "configured_module_count": configured_module_count,
        "module_intent_query": module_intent_query,
        "modules": modules,
        **({"runtime_config": runtime_config} if runtime_config else {}),
        "anchors": list(dict.fromkeys(anchors)),
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
    return (-0.12, "conflicting" if same_family else "unmapped") if same_family else (0.0, "unmapped")


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
                        "minBm25Score": 0.0,
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


def _architecture_items(root: Path, resolution: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Create exact current-source candidates for resolved shared modules."""
    items: list[dict[str, Any]] = []
    modules = resolution.get("modules")
    if not isinstance(modules, Sequence) or isinstance(modules, (str, bytes, bytearray)):
        return items
    for module_rank, module in enumerate(modules):
        if not isinstance(module, Mapping):
            continue
        name = str(module.get("name") or "").strip()
        intent_anchors = [str(value) for value in module.get("intent_anchors", []) if str(value).strip()]
        try:
            intent_score = float(module.get("intent_score") or 0.0)
        except (TypeError, ValueError):
            intent_score = 0.0
        paths = module.get("implementation_paths")
        if not name or not isinstance(paths, Sequence) or isinstance(paths, (str, bytes, bytearray)):
            continue
        for relative in paths:
            path = root / str(relative)
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            except OSError:
                continue
            search_terms = [*intent_anchors, name]
            index = next((index for index, line in enumerate(lines) if any(term in line for term in search_terms)), 0)
            start = max(0, index - 6)
            snippet = "".join(lines[start : min(len(lines), index + 13)]).strip("\n")
            if not snippet:
                continue
            items.append(
                {
                    "filepath": str(relative),
                    "start_line": start + 1,
                    "content": snippet,
                    "score": float(140 - module_rank),
                    "matched_anchor": next((term for term in search_terms if term in lines[index]), name),
                    "retrieval_stage": "module_architecture",
                    "business_seed": module_rank == 0 and intent_score > 0,
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
    is_localized_resource = (
        candidate.suffix.casefold() in _RESOURCE_SUFFIXES
        and (
            bool(parts & _RESOURCE_PATH_PARTS)
            or any(part.startswith("values") or part.endswith(".lproj") for part in parts)
            or candidate.name.casefold() == "localizable.strings"
        )
    )
    if candidate.suffix.casefold() in _SOURCE_SUFFIXES and not is_localized_resource:
        return 0.08, "implementation"
    if is_localized_resource:
        return 0.0, "resource"
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
    """Keep the primary business entry, then represent plausible layers."""
    ordered = sorted(candidates, key=lambda item: float(item.get("score") or -1.0), reverse=True)
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(item: dict[str, Any]) -> None:
        path = str(item.get("path") or "")
        if path and path not in seen and len(selected) < limit:
            seen.add(path)
            selected.append(item)

    # Product-module resolution has already ranked current source against the
    # ticket, screenshot, page, action and field facts. Preserve that primary
    # business entry before a downstream native log triggers layer balancing.
    seed = next((item for item in ordered if item.get("business_seed") is True), None)
    if seed is not None:
        add(seed)

    for layer in requested:
        if any(item.get("implementation_layer") == layer for item in selected):
            continue
        match = next((item for item in ordered if item.get("implementation_layer") == layer), None)
        if match is not None:
            add(match)
    # When space permits, pair a layer's consumer with its localization resource.
    for layer in requested:
        present_roles = {item.get("evidence_role") for item in selected if item.get("implementation_layer") == layer}
        complement = next(
            (
                item
                for item in ordered
                if item.get("implementation_layer") == layer
                and item.get("evidence_role") in {"implementation", "resource"}
                and item.get("evidence_role") not in present_roles
            ),
            None,
        )
        if complement is not None:
            add(complement)
    for item in ordered:
        add(item)
    return selected


def _balanced_raw_candidates(*, architecture: Sequence[dict[str, Any]], exact: Sequence[dict[str, Any]], concepts: Sequence[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Keep every retrieval layer represented before semantic reranking."""
    selected: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    pools = [list(architecture), list(exact), list(concepts)]
    while len(selected) < limit and any(pools):
        progressed = False
        for pool in pools:
            while pool:
                item = pool.pop(0)
                identity = (str(item.get("filepath") or ""), str(item.get("content") or ""))
                if not identity[0] or identity in seen:
                    continue
                seen.add(identity)
                selected.append(item)
                progressed = True
                break
            if len(selected) >= limit:
                break
        if not progressed:
            break
    return selected


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
    product_scope_tokens: Sequence[str] = (),
    requested_layers: Sequence[str] = (),
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
        for item in bounded:
            adjustment, scope = _product_scope_adjustment(str(item.get("filepath") or ""), product_scope_tokens)
            code_adjustment, candidate_kind = _code_path_adjustment(str(item.get("filepath") or ""))
            item["product_scope"] = scope
            item["candidate_kind"] = candidate_kind
            item["implementation_layer"] = _implementation_layer(str(item.get("filepath") or ""))
            item["score"] = float(item.get("score", 0)) + adjustment + code_adjustment
        bounded.sort(key=lambda item: float(item.get("score", -1)), reverse=True)
        return bounded, "embedding_unavailable"
    query_vector = vectors[0]
    module_intent_max = max(
        (float(item.get("intent_score") or 0.0) for item in bounded if item.get("retrieval_stage") == "module_profile"),
        default=0.0,
    )
    for item, vector in zip(bounded, vectors[1:], strict=False):
        semantic_score = _cosine(query_vector, vector)
        adjustment, scope = _product_scope_adjustment(str(item.get("filepath") or ""), product_scope_tokens)
        code_adjustment, candidate_kind = _code_path_adjustment(str(item.get("filepath") or ""))
        item["semantic_score"] = semantic_score
        item["product_scope"] = scope
        item["candidate_kind"] = candidate_kind
        item["implementation_layer"] = _implementation_layer(str(item.get("filepath") or ""))
        lexical_score = float(item.get("score") or 0.0)
        item["lexical_score"] = lexical_score
        module_intent_score = float(item.get("intent_score") or 0.0)
        intent_adjustment = (
            min(0.20, module_intent_score / module_intent_max * 0.20)
            if item.get("retrieval_stage") == "module_profile" and module_intent_max > 0 and module_intent_score > 0
            else 0.0
        )
        item["score"] = semantic_score + adjustment + code_adjustment + intent_adjustment
    bounded.sort(key=lambda item: float(item.get("score", -1)), reverse=True)
    best = float(bounded[0].get("score", 0)) if bounded else 0.0
    floor = max(0.35, best - 0.12)
    selected = [item for item in bounded if float(item.get("score", -1)) >= floor]
    selected_paths = {str(item.get("filepath") or "") for item in selected}
    for layer in requested_layers:
        layer_best = next((item for item in bounded if item.get("implementation_layer") == layer), None)
        if layer_best is not None and str(layer_best.get("filepath") or "") not in selected_paths:
            selected.append(layer_best)
            selected_paths.add(str(layer_best.get("filepath") or ""))
    selected.sort(key=lambda item: float(item.get("score", -1)), reverse=True)
    return selected, "ready"


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
            "candidate_kind": str(item.get("candidate_kind") or "other"),
            "implementation_layer": str(item.get("implementation_layer") or _implementation_layer(relative)),
            "evidence_role": str(item.get("candidate_kind") or "other"),
            "product_scope": str(item.get("product_scope") or "unspecified"),
            "verified_edges": [],
            "business_seed": item.get("business_seed") is True,
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
    query = build_retrieval_query(
        bug_snapshot=bug_snapshot,
        query_facts=query_facts,
        runtime_evidence=runtime_evidence,
        max_chars=int(getattr(config, "max_query_chars", 1600)),
        retrieval_aliases=retrieval_aliases,
    )
    architecture_resolution = resolve_module_architecture(
        repository_root=root,
        repository=repository,
        bug_snapshot=bug_snapshot,
        query_facts=query_facts,
        query=query,
        config=config,
    )
    base = {
        "schema_version": 3,
        "provider": "tabby",
        "repository": repository,
        "source_revision": revision,
        "query": query,
        "entries": [],
        "relations": [],
        "resource_coverage": [],
        "architecture_resolution": architecture_resolution,
        "rejections": [],
        "metrics": {"query_chars": len(query), "raw_candidate_count": 0, "entry_count": 0},
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
    ticket_anchor_budget = max(1, max_anchor_queries - 3)
    ticket_anchors = build_retrieval_anchors(
        query=query,
        query_facts=query_facts,
        max_anchors=ticket_anchor_budget,
        retrieval_aliases=retrieval_aliases,
    )
    module_anchors = [str(item.get("name") or "") for item in architecture_resolution.get("modules", []) if isinstance(item, Mapping) and item.get("name")]
    architecture_anchors = [str(value) for value in architecture_resolution.get("anchors", []) if str(value).strip()]
    # Ticket/action anchors express the reported behavior. Module and model
    # anchors supplement them but may not consume the whole bounded grep plan.
    anchors = list(dict.fromkeys([*ticket_anchors, *module_anchors, *architecture_anchors]))[:max_anchor_queries]
    fallback_concepts: list[str] = []
    requested_layers = _requested_implementation_layers(query_facts)
    product_scope_tokens = build_product_scope_tokens(
        bug_snapshot,
        retrieval_aliases=retrieval_aliases,
    )
    for candidate in architecture_resolution.get("project_candidates", []):
        if not isinstance(candidate, Mapping):
            continue
        project = str(candidate.get("project") or "").strip().casefold()
        if project and project not in product_scope_tokens:
            product_scope_tokens.append(project)
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
        architecture_items = _architecture_items(root, architecture_resolution)
        hybrid_items: list[dict[str, Any]] = []
        hybrid_status = "unavailable"
        hybrid_thread_id: str | None = None
        source_id = str(selected.get("sourceId") or "")
        if source_id:
            try:
                hybrid_items, hybrid_thread_id = _collect_tabby_hybrid_items(
                    base_url=str(getattr(config, "base_url", "http://127.0.0.1:8080")),
                    authorization=headers.get("Authorization", ""),
                    source_id=source_id,
                    query_text=query,
                    timeout=timeout,
                    limit=int(getattr(config, "max_raw_candidates", 24)),
                )
                hybrid_status = "ready" if hybrid_items else "no_match"
            except (OSError, TimeoutError, ValueError, WebSocketException):
                hybrid_status = "unavailable"
            finally:
                if hybrid_thread_id:
                    try:
                        _graphql(
                            endpoint=endpoint,
                            headers=headers,
                            query="mutation DeleteRetrievalThread($id: ID!) { deleteThread(id: $id) }",
                            variables={"id": hybrid_thread_id},
                            timeout=timeout,
                        )
                    except (httpx.HTTPError, ValueError):
                        pass
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
        max_raw_candidates = int(getattr(config, "max_raw_candidates", 24))

        def retrieve(terms: Sequence[str], *, stage: str, destination: list[dict[str, Any]]) -> None:
            seen_items = {(str(item.get("filepath") or ""), str(item.get("content") or "")) for item in destination}
            for anchor_rank, anchor in enumerate(terms):
                per_term_limit = 8 if len(anchor) >= 3 else 4
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
                    anchor_items = _grep_items(grep.get("files"), anchor=anchor, anchor_rank=anchor_rank, retrieval_stage=stage)
                    seen_anchor_paths: set[str] = set()
                    for item in anchor_items:
                        path = str(item.get("filepath") or "")
                        identity = (path, str(item.get("content") or ""))
                        if not path or path in seen_anchor_paths or identity in seen_items:
                            continue
                        seen_anchor_paths.add(path)
                        seen_items.add(identity)
                        destination.append(item)
                        if len(seen_anchor_paths) >= per_term_limit or len(destination) >= max_raw_candidates:
                            break
                if len(destination) >= max_raw_candidates:
                    break

        retrieve(anchors, stage="exact_anchor", destination=exact_items)
        pre_fallback_items = [*architecture_items, *hybrid_items, *exact_items]
        missing_layers_before_fallback = _missing_implementation_layers(pre_fallback_items, requested_layers)
        if not hybrid_items or missing_layers_before_fallback:
            fallback_concepts = build_fallback_concepts(
                bug_snapshot=bug_snapshot,
                query_facts=query_facts,
                runtime_evidence=runtime_evidence,
                exact_anchors=anchors,
                max_concepts=int(getattr(config, "max_fallback_concepts", 8)),
            )
            retrieve(fallback_concepts, stage="concept_expansion", destination=concept_items)
        if hybrid_items:
            raw_items = []
            seen_hybrid: set[tuple[str, str]] = set()
            for item in [*architecture_items, *hybrid_items, *exact_items, *concept_items]:
                identity = (str(item.get("filepath") or ""), str(item.get("content") or ""))
                if not identity[0] or identity in seen_hybrid:
                    continue
                seen_hybrid.add(identity)
                raw_items.append(item)
                if len(raw_items) >= max_raw_candidates:
                    break
        else:
            raw_items = _balanced_raw_candidates(
                architecture=architecture_items,
                exact=exact_items,
                concepts=concept_items,
                limit=max_raw_candidates,
            )
    except (httpx.HTTPError, ValueError) as exc:
        return {**base, "status": "unavailable", "reason": type(exc).__name__}
    raw_candidate_count = len(raw_items)
    business_seed_paths = {
        str(item.get("filepath") or "")
        for item in architecture_items
        if item.get("business_seed") is True and str(item.get("filepath") or "")
    }
    for item in raw_items:
        if str(item.get("filepath") or "") in business_seed_paths:
            item["business_seed"] = True
    if hybrid_items:
        for item in raw_items:
            adjustment, scope = _product_scope_adjustment(str(item.get("filepath") or ""), product_scope_tokens)
            code_adjustment, candidate_kind = _code_path_adjustment(str(item.get("filepath") or ""))
            item["product_scope"] = scope
            item["candidate_kind"] = candidate_kind
            item["implementation_layer"] = _implementation_layer(str(item.get("filepath") or ""))
            item["score"] = float(item.get("score") or 0.0) + adjustment + code_adjustment
        semantic_status = "tabby_hybrid"
    else:
        ranking_query = str(architecture_resolution.get("module_intent_query") or "").strip() or query
        raw_items, semantic_status = _semantic_rank(
            query=ranking_query,
            items=raw_items,
            config=config,
            timeout=timeout,
            product_scope_tokens=product_scope_tokens,
            requested_layers=requested_layers,
        )
    validated_candidates: list[dict[str, Any]] = []
    rejections: list[dict[str, str]] = []
    if architecture_resolution.get("architecture_kind") == "module_config":
        enabled_module_paths = {
            str(path)
            for module in architecture_resolution.get("modules", [])
            if isinstance(module, Mapping)
            for path in module.get("implementation_paths", [])
        }
        retained_items: list[dict[str, Any]] = []
        for item in raw_items:
            path = str(item.get("filepath") or "")
            if path.startswith("projects/com.example.") and path not in enabled_module_paths:
                if len(rejections) < 12:
                    rejections.append({"path": path[:300], "reason": "unselected_product_override"})
                continue
            retained_items.append(item)
        raw_items = retained_items
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
            "architecture_module_count": len(architecture_resolution.get("modules") or []),
            "business_seed_path": next((entry["path"] for entry in entries if entry.get("business_seed") is True), ""),
            "product_scope_tokens": product_scope_tokens,
            "business_alias_count": len(tuple(retrieval_aliases)),
            "retrieval_mode": (
                "tabby_hybrid" if hybrid_items else "layered" if exact_items and concept_items else "concept_fallback" if concept_items else "module_architecture" if architecture_items else "exact_anchor" if exact_items else "none"
            ),
            "tabby_hybrid_status": hybrid_status,
            "tabby_hybrid_candidate_count": len(hybrid_items),
            "semantic_status": semantic_status,
        },
    }
