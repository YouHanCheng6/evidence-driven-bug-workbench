"""Conditional ZenTao attachment evidence side-path for Bug Workbench."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import mimetypes
import re
import shutil
import zipfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from imageio_ffmpeg import get_ffmpeg_exe
from langchain_core.messages import HumanMessage, SystemMessage
from zentao_mcp.client import ZentaoError

from deerflow.config import get_app_config
from deerflow.models import create_chat_model
from deerflow.uploads.manager import claim_unique_filename, normalize_filename
from deerflow.utils.llm_text import extract_response_text

MAX_EVIDENCE_FILES = 10
MAX_EVIDENCE_FILE_BYTES = 50 * 1024 * 1024
MAX_EVIDENCE_TOTAL_BYTES = 100 * 1024 * 1024
MAX_LOG_EVIDENCE_CHARS = 4_000
MAX_AGGREGATE_LOG_EVIDENCE_CHARS = 8_000
MAX_VISUAL_FILES = 6
MAX_VISUAL_FILE_BYTES = 8 * 1024 * 1024
MAX_VISUAL_EVIDENCE_ITEMS = 24
MAX_VISUAL_COMPARISONS = 12
VISUAL_EVIDENCE_SCHEMA_VERSION = 2
VISUAL_ANALYZER_MODEL = "volc-doubao-seed-2-1-pro"
VISUAL_ANALYZER_TEMPERATURE = 0.0
MAX_ARCHIVE_MEMBERS = 20
MAX_ARCHIVE_MEMBER_BYTES = 5 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 20 * 1024 * 1024
VISUAL_FAST_TIMEOUT_SECONDS = 180.0
VISUAL_THINKING_TIMEOUT_SECONDS = 300.0

_SENSITIVE_TEXT_PATTERN = re.compile(
    r"""(?imx)
    (
        ["']?
        \b(?:authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|token|cookie|password|passwd|secret)\b
        ["']?\s*[:=]\s*["']?
        (?:(?:bearer|basic)\s+)?
    )
    ([^\s,;"'&]+)
    """,
)
_EXPECTED_TIME_PLACEHOLDER = re.compile(r"^#time#\s*", re.IGNORECASE)

ProgressCallback = Callable[[list[dict[str, Any]]], Awaitable[None]]
VisualAnalyzer = Callable[[list[Path]], Awaitable[str | dict[str, Any] | list[dict[str, Any]]]]
logger = logging.getLogger(__name__)


def _public_asset(asset: dict[str, Any], *, status: str, name: str | None = None, path: str | None = None, error: str | None = None) -> dict[str, Any]:
    result = {
        "id": str(asset.get("id") or ""),
        "name": name or str(asset.get("name") or "附件"),
        "source": str(asset.get("source") or "attachment"),
        "media_type": str(asset.get("media_type") or "application/octet-stream"),
        "size": asset.get("size") if isinstance(asset.get("size"), int) else None,
        "status": status,
    }
    if path:
        result["path"] = path
    if error:
        result["error"] = error
    return result


def _is_text(media_type: str, name: str) -> bool:
    return media_type.startswith("text/") or Path(name).suffix.lower() in {".csv", ".json", ".log", ".md", ".txt", ".xml", ".yaml", ".yml"}


def _redact_text(value: str) -> str:
    """Keep log evidence useful without forwarding common credentials."""
    return _SENSITIVE_TEXT_PATTERN.sub(r"\1[REDACTED]", value)


def _comparison_anchor(value: Any) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").lower())


def _visual_fact(item: dict[str, Any], *keys: str) -> str:
    return next((str(item.get(key) or "").strip() for key in keys if str(item.get(key) or "").strip()), "")


def _derive_visual_comparisons(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add conservative actual/expected links without replacing either source fact."""
    comparisons: list[dict[str, Any]] = []

    def add(actual_ids: list[str], expected_ids: list[str], *, anchor: str, basis: str) -> None:
        if len(comparisons) >= MAX_VISUAL_COMPARISONS:
            return
        identity = (tuple(actual_ids), tuple(expected_ids))
        if any((tuple(item["actual_item_ids"]), tuple(item["expected_item_ids"])) == identity for item in comparisons):
            return
        comparisons.append(
            {
                "comparison_id": f"comparison_{len(comparisons) + 1}",
                "actual_item_ids": actual_ids,
                "expected_item_ids": expected_ids,
                "anchor": anchor,
                "basis": basis,
            }
        )

    actual_items: list[dict[str, Any]] = []
    expected_items: list[dict[str, Any]] = []
    for item in items:
        actual_fact = _visual_fact(item, "actual_visible_text", "actual_visual")
        expected_fact = _visual_fact(item, "expected_visible_text", "expected_visual")
        if actual_fact:
            actual_items.append(item)
        if expected_fact:
            expected_items.append(item)
        if actual_fact and expected_fact:
            anchor = _visual_fact(item, "event_id", "resource_key", "region_or_control", "event_or_action", "page")
            add([item["visual_item_id"]], [item["visual_item_id"]], anchor=anchor, basis="same_item_actual_expected_facts")

    # Separate facts are linked only by an exact, unique business anchor. A
    # fuzzy visual match could silently turn unrelated screenshots into a
    # requirement and is therefore left for Codex to evaluate.
    anchor_fields = ("event_id", "resource_key", "region_or_control", "event_or_action")
    for actual in actual_items:
        if _visual_fact(actual, "expected_visible_text", "expected_visual"):
            continue
        for field in anchor_fields:
            anchor = _comparison_anchor(actual.get(field))
            if not anchor:
                continue
            matches = [
                expected
                for expected in expected_items
                if expected["visual_item_id"] != actual["visual_item_id"] and _comparison_anchor(expected.get(field)) == anchor
            ]
            if len(matches) == 1:
                add(
                    [actual["visual_item_id"]],
                    [matches[0]["visual_item_id"]],
                    anchor=str(actual.get(field) or ""),
                    basis=f"exact_{field}",
                )
                break
    return comparisons


def _minimal_visual_evidence(value: Any, *, allowed_asset_names: set[str] | None = None) -> dict[str, Any]:
    """Restore rich visual facts while retaining the four legacy aliases."""
    if isinstance(value, dict):
        raw_items = value.get("items")
        items = raw_items if isinstance(raw_items, list) else [value]
    else:
        items = value if isinstance(value, list) else []
    result: list[dict[str, Any]] = []

    def text(item: dict[str, Any], *keys: str, limit: int = 600) -> str:
        for key in keys:
            candidate = item.get(key)
            if candidate not in (None, ""):
                return re.sub(r"\s+", " ", str(candidate)).strip()[:limit]
        return ""

    for item in (items if isinstance(items, list) else [])[:MAX_VISUAL_EVIDENCE_ITEMS]:
        if not isinstance(item, dict):
            continue
        asset_role = text(item, "asset_role", limit=60).lower()
        client_evidence = text(item, "client_evidence", limit=240)
        raw_clients = item.get("observed_clients")
        clients = [str(candidate).lower() for candidate in raw_clients if str(candidate).lower() in {"android", "ios", "harmony"}] if isinstance(raw_clients, list) else []
        explicit_client = str(item.get("client") or "").lower()
        if explicit_client in {"android", "ios", "harmony"} and explicit_client not in clients:
            clients.insert(0, explicit_client)
        if not clients:
            evidence_clients = [
                client
                for client, pattern in (
                    ("android", r"android"),
                    ("ios", r"ios"),
                    ("harmony", r"harmony|鸿蒙"),
                )
                if re.search(pattern, client_evidence, re.IGNORECASE)
            ]
            if len(evidence_clients) == 1:
                clients = evidence_clients
        # Tables, desktop document viewers and expected/reference material do
        # not prove which mobile client reproduced the Bug. Older analyzer
        # payloads have no asset_role, so preserve their clients for checkpoint
        # compatibility while enforcing the role contract for all new runs.
        if asset_role in {"reference_table", "expected_reference", "supplementary_material", "desktop_document"}:
            clients = []
        client = clients[0] if clients else "unknown"
        page = text(item, "page", "page_or_region")
        region = text(item, "region_or_control", "page_or_region")
        actual_text = text(item, "actual_visible_text", "actual_text")
        expected_text = _EXPECTED_TIME_PLACEHOLDER.sub(
            "",
            text(item, "expected_visible_text", "expected_text"),
        )
        confidence = text(item, "confidence", limit=20).lower()
        if confidence not in {"high", "medium", "low"}:
            confidence = "unknown"
        evidence_refs: list[dict[str, str]] = []
        raw_refs = item.get("evidence_refs")
        for reference in raw_refs if isinstance(raw_refs, list) else []:
            if not isinstance(reference, dict):
                continue
            normalized_ref = {key: text(reference, key, limit=160) for key in ("asset", "role", "annotation") if text(reference, key, limit=160)}
            if allowed_asset_names is not None and normalized_ref.get("asset") not in allowed_asset_names:
                continue
            if normalized_ref:
                evidence_refs.append(normalized_ref)
            if len(evidence_refs) >= MAX_VISUAL_FILES:
                break
        evidence_refs = [
            dict(values)
            for _identity, values in sorted(
                {
                    (reference.get("asset", ""), reference.get("role", ""), reference.get("annotation", "")): reference
                    for reference in evidence_refs
                }.items()
            )
        ]
        result.append(
            {
                "visual_item_id": f"visual_{len(result) + 1}",
                "asset_role": asset_role or "unknown",
                "observed_clients": clients,
                "client_evidence": client_evidence,
                "product_variant": text(item, "product_variant"),
                "user_path": text(item, "user_path"),
                "page": page,
                "region_or_control": region,
                "page_state": text(item, "page_state"),
                "actual_visible_text": actual_text,
                "expected_visible_text": expected_text,
                "highlighted_content": text(item, "highlighted_content"),
                "actual_visual": text(item, "actual_visual"),
                "expected_visual": text(item, "expected_visual"),
                "visual_difference": text(item, "visual_difference", "mismatch_summary"),
                "element_type": text(item, "element_type", limit=80),
                "mismatch_summary": text(item, "mismatch_summary", "visual_difference"),
                "event_or_action": text(item, "event_or_action", "event_type", "action", limit=160),
                "event_id": text(item, "event_id", limit=160),
                "resource_key": text(item, "resource_key", "text_key", "copy_key", limit=160),
                "confidence": confidence,
                "evidence_refs": evidence_refs,
                # Compatibility aliases consumed by the existing map/resource tools.
                "client": client,
                "page_or_region": region or page,
                "actual_text": actual_text,
                "expected_text": expected_text,
            }
        )
    deduplicated: list[dict[str, Any]] = []
    seen_items: set[str] = set()
    for item in result:
        identity_payload = {key: value for key, value in item.items() if key not in {"visual_item_id", "confidence"}}
        identity = json.dumps(identity_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if identity in seen_items:
            continue
        seen_items.add(identity)
        item["visual_item_id"] = f"visual_{len(deduplicated) + 1}"
        deduplicated.append(item)
    return {
        "schema_version": VISUAL_EVIDENCE_SCHEMA_VERSION,
        "items": deduplicated,
        "comparisons": _derive_visual_comparisons(deduplicated),
    }


def _archive_index(payload: bytes, archive_name: str) -> list[dict[str, Any]]:
    """Return metadata only: archive contents are never first-round evidence."""
    if not zipfile.is_zipfile(io.BytesIO(payload)):
        return []
    members_index: list[dict[str, Any]] = []
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            members = [item for item in archive.infolist() if not item.is_dir()][:MAX_ARCHIVE_MEMBERS]
            for member in members:
                if member.flag_bits & 0x1 or member.file_size <= 0:
                    continue
                member_name = member.filename.replace("\\", "/").lstrip("/")
                members_index.append({"archive": archive_name, "name": member_name, "media_type": _media_type_for_name(member_name), "size": member.file_size})
    except (OSError, RuntimeError, zipfile.BadZipFile):
        return []
    return members_index


def extract_targeted_log_evidence(path: Path, *, signals: list[str], purpose: str, time_window: str | None = None) -> dict[str, Any]:
    """Return bounded, redacted matching log lines; never fall back to a file head."""
    normalized = [item.strip() for item in signals if item and item.strip()]
    if not normalized:
        return {"purpose": purpose, "time_window": time_window, "status": "no_target", "text": ""}
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"purpose": purpose, "time_window": time_window, "status": "unreadable", "text": ""}
    matcher = re.compile("|".join(re.escape(item) for item in normalized), re.IGNORECASE)
    lines = content.splitlines()
    selected: list[str] = []
    seen: set[int] = set()
    for index, line in enumerate(lines):
        if matcher.search(line):
            for nearby in range(max(0, index - 1), min(len(lines), index + 2)):
                if nearby not in seen:
                    selected.append(lines[nearby])
                    seen.add(nearby)
    text = _redact_text("\n".join(selected))[:MAX_LOG_EVIDENCE_CHARS]
    return {"source": str(path), "purpose": purpose, "time_window": time_window, "signals": normalized, "status": "matched" if text else "no_match", "text": text}


def extract_aggregate_log_evidence(
    uploads_dir: Path,
    attachment_evidence: dict[str, Any],
    *,
    signals: list[str],
    time_hints: list[str] | None = None,
) -> dict[str, Any]:
    """Search all downloaded logs as one bounded runtime-evidence operation."""
    normalized_signals = list(dict.fromkeys(item.strip() for item in signals if item and item.strip()))
    normalized_time_hints = list(dict.fromkeys(item.strip() for item in (time_hints or []) if item and item.strip()))
    if not normalized_signals:
        return {"status": "no_target", "signals": [], "time_hints": normalized_time_hints, "files_searched": 0, "text": ""}
    signal_matchers = [re.compile(re.escape(item), re.IGNORECASE) for item in normalized_signals]
    assets = attachment_evidence.get("assets")
    names = list(dict.fromkeys(str(item.get("name") or "").strip() for item in assets if isinstance(item, dict) and str(item.get("status") or "") == "indexed" and str(item.get("name") or "").strip())) if isinstance(assets, list) else []
    groups: list[tuple[int, int, str, list[tuple[int, int, str]]]] = []
    signal_counts = [{"matches": 0, "request": 0, "response": 0, "error": 0, "cooccurrence": 0} for _signal in normalized_signals]
    files_searched = 0

    def collect(source: str, content: str) -> None:
        nonlocal files_searched
        files_searched += 1
        source_order = files_searched
        lines = content.splitlines()
        indexes_by_signal: list[list[int]] = [[] for _signal in normalized_signals]
        overlap_by_line: dict[int, int] = {}
        for index, line in enumerate(lines):
            matched_indexes: list[int] = []
            for signal_index, matcher in enumerate(signal_matchers):
                if matcher.search(line):
                    indexes_by_signal[signal_index].append(index)
                    matched_indexes.append(signal_index)
            if not matched_indexes:
                continue
            overlap_by_line[index] = len(matched_indexes)
            lowered = line.casefold()
            for signal_index in matched_indexes:
                signal_counts[signal_index]["matches"] += 1
                if len(matched_indexes) > 1:
                    signal_counts[signal_index]["cooccurrence"] += 1
                if "request" in lowered or " req" in lowered:
                    signal_counts[signal_index]["request"] += 1
                if "response" in lowered or " rsp" in lowered or " resp" in lowered:
                    signal_counts[signal_index]["response"] += 1
                if re.search(r"\b(?:error|err(?:or)?code|fail(?:ed|ure)?|timeout|exception|reject|status)\b", lowered):
                    signal_counts[signal_index]["error"] += 1
        for signal_index, matching_indexes in enumerate(indexes_by_signal):
            if not matching_indexes:
                continue
            time_matching_indexes = [index for index in matching_indexes if all(hint in lines[index] for hint in normalized_time_hints)] if normalized_time_hints else []
            chosen_indexes = time_matching_indexes or matching_indexes
            chosen_set = set(chosen_indexes)
            matching_set = set(matching_indexes)
            selected_indexes: set[int] = set()
            selected: list[tuple[int, int, str]] = []
            for index in chosen_indexes:
                for nearby in range(max(0, index - 1), min(len(lines), index + 2)):
                    if nearby in selected_indexes:
                        continue
                    if nearby in matching_set and nearby not in chosen_set:
                        continue
                    selected_indexes.add(nearby)
                    selected.append((overlap_by_line.get(index, 1), int(nearby == index), _redact_text(lines[nearby])))
            groups.append((signal_index, source_order, source, selected))

    for name in names:
        path = uploads_dir / Path(name).name
        try:
            if zipfile.is_zipfile(path):
                with zipfile.ZipFile(path) as archive:
                    for member in archive.infolist()[:MAX_ARCHIVE_MEMBERS]:
                        if member.is_dir() or member.flag_bits & 0x1 or member.file_size <= 0 or member.file_size > MAX_ARCHIVE_MEMBER_BYTES:
                            continue
                        member_name = member.filename.replace("\\", "/").lstrip("/")
                        if not _is_text(_media_type_for_name(member_name), member_name):
                            continue
                        collect(f"{name}!/{member_name}", archive.read(member).decode("utf-8", errors="replace"))
            elif path.is_file() and _is_text(_media_type_for_name(name), name):
                collect(name, path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, RuntimeError, zipfile.BadZipFile, KeyError):
            continue

    def structure_rank(signal: str) -> int:
        if signal.startswith("/") and signal.count("/") >= 2:
            return 4
        if re.fullmatch(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]{2,})+|[A-Z]{1,8}-?\d{3,}", signal):
            return 4
        if signal.isdigit() and len(signal) >= 3:
            return 3
        if "." in signal and re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)+", signal):
            return 3
        if re.search(r"\b(?:request|response|error|fail(?:ed|ure)?|timeout|exception|status|code)\b", signal, re.IGNORECASE):
            return 3
        if len(signal) >= 6 and re.fullmatch(r"[A-Za-z][A-Za-z0-9]*(?:[_-][A-Za-z0-9]+)+", signal):
            return 2
        if len(signal) >= 6 and re.fullmatch(r"[A-Za-z][A-Za-z0-9]+", signal) and re.search(r"[a-z][A-Z]", signal):
            return 2
        return 0

    relevant_indexes: set[int] = set()
    relevance_scores: dict[int, int] = {}
    for signal_index, (signal, counts) in enumerate(zip(normalized_signals, signal_counts, strict=True)):
        if not counts["matches"]:
            continue
        structure = structure_rank(signal)
        runtime_markers = counts["request"] + counts["response"] + counts["error"]
        relevant = structure == 4 or (structure >= 2 and (runtime_markers > 0 or counts["cooccurrence"] > 0 or counts["matches"] <= 8))
        if not relevant:
            continue
        relevant_indexes.add(signal_index)
        relevance_scores[signal_index] = structure * 100 + min(runtime_markers, 20) * 10 + min(counts["cooccurrence"], 10) * 5 + max(0, 20 - min(counts["matches"], 20))

    if not relevant_indexes:
        return {
            "status": "no_match",
            "signals": normalized_signals,
            "matched_signals": [],
            "time_hints": normalized_time_hints,
            "files_searched": files_searched,
            "text": "",
        }

    summary_lines = [
        (f"{signal}: matches={counts['matches']}, request={counts['request']}, response={counts['response']}, error={counts['error']}, cooccurrence={counts['cooccurrence']}")
        for signal_index, (signal, counts) in enumerate(zip(normalized_signals, signal_counts, strict=True))
        if signal_index in relevant_indexes
    ]
    chunks: list[str] = ["LOG_SIGNAL_SUMMARY\n" + "\n".join(summary_lines)]
    seen_lines: set[str] = set()
    signal_weights: dict[int, int] = {}
    for signal_index, (signal, counts) in enumerate(zip(normalized_signals, signal_counts, strict=True)):
        if signal_index not in relevant_indexes:
            continue
        signal_weights[signal_index] = 3 if structure_rank(signal) >= 4 else 2 if counts["matches"] <= 8 else 1
    available_chars = max(0, MAX_AGGREGATE_LOG_EVIDENCE_CHARS - sum(len(chunk) for chunk in chunks) - 300)
    unit_chars = available_chars // max(1, sum(signal_weights.values()))
    per_signal_limits = {signal_index: max(350, unit_chars * weight) for signal_index, weight in signal_weights.items()}
    used_by_signal: dict[int, int] = {}
    snippet_count = 0
    for signal_index, _source_order, source, selected in sorted(
        groups,
        key=lambda item: (-relevance_scores.get(item[0], -1), item[1], item[0]),
    ):
        per_signal_limit = per_signal_limits.get(signal_index, 0)
        if not per_signal_limit:
            continue
        signal_used = used_by_signal.get(signal_index, 0)
        if signal_used >= per_signal_limit:
            continue
        rendered_lines: list[str] = []
        for _overlap, _is_hit, line in sorted(selected, key=lambda item: (-item[0], -item[1])):
            identity = f"{source}\0{line}"
            if identity in seen_lines:
                continue
            remaining = per_signal_limit - signal_used - sum(len(item) + 1 for item in rendered_lines)
            if remaining <= 0:
                break
            seen_lines.add(identity)
            rendered_lines.append(line[:remaining])
        if rendered_lines:
            chunk = f"LOG_SIGNAL {normalized_signals[signal_index]}\nLOG_SOURCE {source}\n" + "\n".join(rendered_lines)
            chunks.append(chunk)
            snippet_count += 1
            used_by_signal[signal_index] = signal_used + len(chunk)
    text = "\n\n".join(chunks)[:MAX_AGGREGATE_LOG_EVIDENCE_CHARS] if snippet_count else ""
    transactions = _runtime_transactions_from_evidence_text(text, normalized_signals)
    return {
        "status": "matched" if text else "no_match",
        "signals": normalized_signals,
        "matched_signals": [normalized_signals[index] for index in sorted(relevant_indexes, key=lambda index: -relevance_scores[index])],
        "time_hints": normalized_time_hints,
        "files_searched": files_searched,
        "text": text,
        "transactions": transactions,
    }


_TRANSACTION_ID_RE = re.compile(
    r"\b(?P<key>requestId|request_id|traceId|trace_id|spanId|span_id|transactionId|transaction_id|sessionId|session_id)\s*[:=]\s*[\"']?(?P<value>[A-Za-z0-9_.:@-]{3,128})",
    re.IGNORECASE,
)


def _runtime_transactions_from_evidence_text(text: str, signals: list[str]) -> list[dict[str, Any]]:
    """Build bounded transaction records from emitted runtime excerpts.

    Exact correlation identifiers are authoritative. Without one, request and
    response/error lines may form only a strong same-excerpt relation; a lone
    keyword remains weak and cannot close a causal chain.
    """
    if not text:
        return []
    fragments: list[dict[str, Any]] = []
    for chunk in text.split("\n\n"):
        if not chunk.startswith("LOG_SIGNAL "):
            continue
        lines = chunk.splitlines()
        signal = lines[0].removeprefix("LOG_SIGNAL ").strip()
        source = lines[1].removeprefix("LOG_SOURCE ").strip() if len(lines) > 1 and lines[1].startswith("LOG_SOURCE ") else ""
        evidence_lines = lines[2:] if source else lines[1:]
        joined = "\n".join(evidence_lines)
        identifiers = [f"{match.group('key')}={match.group('value')}" for match in _TRANSACTION_ID_RE.finditer(joined)]
        has_request = bool(re.search(r"\b(?:request|req)\b", joined, re.IGNORECASE))
        has_response = bool(re.search(r"\b(?:response|resp|rsp)\b", joined, re.IGNORECASE))
        has_error = bool(re.search(r"\b(?:error|err(?:or)?code|fail(?:ed|ure)?|timeout|exception|reject)\b", joined, re.IGNORECASE))
        fields = tuple(dict.fromkeys(match.group(0) for match in re.finditer(r"\b(?:response|resp|result|data|payload)\.[A-Za-z_][A-Za-z0-9_.]*", joined, re.IGNORECASE)))
        confidence = "exact" if identifiers else "strong" if has_request and (has_response or has_error) else "weak"
        fragments.append(
            {
                "signal": signal,
                "source": source,
                "correlation_ids": list(dict.fromkeys(identifiers))[:8],
                "request_observed": has_request,
                "response_observed": has_response,
                "error_observed": has_error,
                "response_fields": list(fields)[:16],
                "confidence": confidence,
                "causal_closure_allowed": confidence in {"exact", "strong"},
            }
        )
    merged: list[dict[str, Any]] = []
    fragment_indexes_by_identifier: dict[str, list[int]] = {}
    for index, fragment in enumerate(fragments):
        for identifier in fragment["correlation_ids"]:
            fragment_indexes_by_identifier.setdefault(identifier.casefold(), []).append(index)

    consumed: set[int] = set()
    for index, fragment in enumerate(fragments):
        if index in consumed:
            continue
        related = {index}
        pending = list(fragment["correlation_ids"])
        seen_identifiers: set[str] = set()
        while pending:
            identifier = pending.pop().casefold()
            if identifier in seen_identifiers:
                continue
            seen_identifiers.add(identifier)
            for related_index in fragment_indexes_by_identifier.get(identifier, []):
                if related_index not in related:
                    related.add(related_index)
                    pending.extend(fragments[related_index]["correlation_ids"])
        if len(related) == 1:
            merged.append(fragment)
            consumed.add(index)
            continue
        ordered = [fragments[item] for item in sorted(related)]
        consumed.update(related)
        merged.append(
            {
                "signal": " | ".join(dict.fromkeys(str(item["signal"]) for item in ordered)),
                "source": " | ".join(dict.fromkeys(str(item["source"]) for item in ordered if item["source"])),
                "correlation_ids": list(dict.fromkeys(identifier for item in ordered for identifier in item["correlation_ids"]))[:8],
                "request_observed": any(bool(item["request_observed"]) for item in ordered),
                "response_observed": any(bool(item["response_observed"]) for item in ordered),
                "error_observed": any(bool(item["error_observed"]) for item in ordered),
                "response_fields": list(dict.fromkeys(field for item in ordered for field in item["response_fields"]))[:16],
                "confidence": "exact",
                "causal_closure_allowed": True,
            }
        )
    return merged[:24]


def _media_type_for_name(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def _expected_signature_type_for_name(name: str) -> str | None:
    """Limit mismatch checks to formats whose container signature is definitive."""
    return {
        ".zip": "application/zip",
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".pdf": "application/pdf",
    }.get(Path(name).suffix.lower())


def _sniff_media_type(payload: bytes) -> str | None:
    """Return a type only when the file signature is strong enough to trust."""
    if zipfile.is_zipfile(io.BytesIO(payload)):
        return "application/zip"
    if len(payload) >= 12 and payload[4:8] == b"ftyp":
        return "video/quicktime" if payload[8:12] == b"qt  " else "video/mp4"
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if payload.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if payload.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if payload.startswith(b"%PDF-"):
        return "application/pdf"
    return None


def _media_type_label(media_type: str) -> str:
    labels = {
        "application/zip": "ZIP",
        "video/mp4": "MP4",
        "video/quicktime": "MOV",
        "image/png": "PNG",
        "image/jpeg": "JPEG",
        "image/gif": "GIF",
        "application/pdf": "PDF",
    }
    return labels.get(media_type, media_type)


def _signature_types_compatible(filename_type: str, signature_type: str) -> bool:
    """Treat MP4 and QuickTime brands as one decodable video-container family."""
    if filename_type == signature_type:
        return True
    iso_video_family = {"video/mp4", "video/quicktime"}
    return {filename_type, signature_type}.issubset(iso_video_family)


def _ffmpeg_executable() -> str:
    """Resolve the host decoder first and the pinned bundled decoder second."""
    return shutil.which("ffmpeg") or get_ffmpeg_exe()


async def _extract_video_frames(video: Path) -> list[Path]:
    """Extract a bounded visual sample with system or bundled ffmpeg."""
    pattern = video.with_name(f".{video.stem}-frame-%02d.jpg")
    executable = _ffmpeg_executable()
    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(video),
            "-vf",
            "fps=1/10,scale='min(1280,iw)':-2",
            "-frames:v",
            "6",
            "-y",
            str(pattern),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        if await process.wait() != 0:
            return []
    except (FileNotFoundError, OSError):
        return []
    return sorted(video.parent.glob(f".{video.stem}-frame-*.jpg"))[:6]


async def _default_visual_analyzer(paths: list[Path], *, thinking_enabled: bool = False) -> str:
    blocks: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                "这些图片来自同一个禅道 UI Bug。必须逐张判断 asset_role：actual_app_screenshot、cross_client_app_screenshot、"
                "reference_table、expected_reference、desktop_document 或 supplementary_material。"
                "不要识别、猜测或输出设备型号、操作系统、客户端平台、Native/RN 或仓库；这些由工单文字和源码调查的独立链路确认。"
                "红框、箭头、高亮、下划线和批注是最高优先级证据：单独识别其中的文字、图片、图标、控件和视觉状态，"
                "并保留少量页面、Tab、产品、横竖屏和操作路径上下文。每一个被标注的独立文案差异必须单独输出一个 item，"
                "禁止把三条事件文案用分号拼成一个 actual/expected。无论是否存在预期材料，都必须先完整输出实际截图中的事实；"
                "没有预期材料时不得猜测预期，也不得因为无法配对而省略实际事实。仅当实际与预期属于同一页面、控件或业务事件时，"
                "才将它们放入同一个 item，并在 evidence_refs 中同时引用实际与预期文件；无法确定时分别输出，不能强行配对。"
                "实际或预期是图片/布局而非文案时，必须分别写入 actual_visual、expected_visual 和 visual_difference，不能留空后只描述页面。"
                "visual_difference 必须写清可见异常本身，例如遮挡、白边、缺值、裁切、顺序、状态或布局差异，不能只重复区域名称。"
                "输出 JSON：{items:[{asset_role,product_variant,user_path,page,region_or_control,page_state,"
                "actual_visible_text,expected_visible_text,highlighted_content,actual_visual,expected_visual,visual_difference,"
                "event_or_action,event_id,resource_key,"
                "element_type,mismatch_summary,confidence,evidence_refs:[{asset,role,annotation}]}]}。"
                "evidence_refs 必须使用下方提供的真实文件名；annotation 写 red_box、arrow、highlight、underline、note 或 none。"
                "预期材料开头的 #time# 是时间占位符，输出 expected_visible_text 时删除它。"
                "不要输出源码、实现归属、根因或修复建议，也不要把未标注的大量页面内容当成重点。"
                "不要复述 Token、Cookie、密码、密钥或个人隐私，统一写成 [REDACTED]。"
            ),
        }
    ]
    accepted = 0
    for path in paths:
        try:
            payload = path.read_bytes()
        except OSError:
            continue
        if not payload or len(payload) > MAX_VISUAL_FILE_BYTES:
            continue
        media_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        blocks.append({"type": "text", "text": f"文件：{path.name}"})
        blocks.append({"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{base64.b64encode(payload).decode('ascii')}"}})
        accepted += 1
        if accepted >= MAX_VISUAL_FILES:
            break
    if not accepted:
        return ""
    model = create_chat_model(
        name=VISUAL_ANALYZER_MODEL,
        thinking_enabled=thinking_enabled,
        app_config=get_app_config(),
        model_overrides={"temperature": VISUAL_ANALYZER_TEMPERATURE},
    )
    response = await model.ainvoke(
        [
            SystemMessage(content="你是只读视觉证据提取器。输出事实，不做修复决策。"),
            HumanMessage(content=blocks),
        ],
        config={"run_name": "bug-attachment-visual-evidence"},
    )
    return extract_response_text(response.content).strip()


def _normalize_visual_analysis(value: Any, *, allowed_asset_names: set[str] | None = None) -> tuple[dict[str, Any], bool]:
    """Return normalized evidence and whether the provider response was valid JSON-like data."""
    parsed = value
    valid = isinstance(parsed, (dict, list))
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
            valid = isinstance(parsed, (dict, list))
        except json.JSONDecodeError:
            parsed = {}
            valid = False
    return _minimal_visual_evidence(parsed, allowed_asset_names=allowed_asset_names), valid


def _visual_quality_summary(evidence: dict[str, Any]) -> dict[str, Any]:
    items = evidence.get("items") if isinstance(evidence.get("items"), list) else []
    meaningful = [
        item
        for item in items
        if isinstance(item, dict)
        and _visual_fact(
            item,
            "actual_visible_text",
            "expected_visible_text",
            "highlighted_content",
            "actual_visual",
            "expected_visual",
            "visual_difference",
            "mismatch_summary",
        )
    ]
    referenced = [item for item in meaningful if item.get("evidence_refs")]
    comparisons = evidence.get("comparisons") if isinstance(evidence.get("comparisons"), list) else []
    return {
        "meaningful_items": len(meaningful),
        "referenced_items": len(referenced),
        "comparison_count": len(comparisons),
        "weak_items": max(0, len(items) - len(meaningful)),
        "usable": bool(meaningful and referenced),
    }


def _visual_input_provenance(paths: list[Path]) -> list[dict[str, Any]]:
    provenance: list[dict[str, Any]] = []
    for path in paths[:MAX_VISUAL_FILES]:
        try:
            payload = path.read_bytes()
        except OSError:
            continue
        provenance.append({"asset": path.name, "sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)})
    return provenance


async def collect_bug_attachment_evidence(
    client: Any,
    bug_id: int,
    assets: list[dict[str, Any]],
    uploads_dir: Path,
    *,
    on_progress: ProgressCallback | None = None,
    visual_analyzer: VisualAnalyzer | None = None,
) -> dict[str, Any] | None:
    """Download and summarize Bug media without participating when none exists."""
    if not assets:
        return None
    uploads_dir.mkdir(parents=True, exist_ok=True)
    candidates = [item for item in assets if isinstance(item, dict)]
    selected = candidates[:MAX_EVIDENCE_FILES]
    states = [_public_asset(item, status="queued") for item in selected]
    states.extend(_public_asset(item, status="failed", error="超过附件数量限制") for item in candidates[MAX_EVIDENCE_FILES:])
    if on_progress:
        await on_progress([dict(item) for item in states])

    seen: set[str] = set()
    total_bytes = 0
    attachment_index: list[dict[str, Any]] = []
    visual_paths: list[Path] = []
    visual_asset_indexes: set[int] = set()

    for index, asset in enumerate(selected):
        states[index] = _public_asset(asset, status="downloading")
        if on_progress:
            await on_progress([dict(item) for item in states])
        declared_size = asset.get("size")
        if isinstance(declared_size, int) and (declared_size > MAX_EVIDENCE_FILE_BYTES or total_bytes + declared_size > MAX_EVIDENCE_TOTAL_BYTES):
            states[index] = _public_asset(asset, status="failed", error="文件超过下载限制")
            if on_progress:
                await on_progress([dict(item) for item in states])
            continue
        try:
            payload, response_type = await client.download_evidence_asset(bug_id, asset, max_bytes=min(MAX_EVIDENCE_FILE_BYTES, MAX_EVIDENCE_TOTAL_BYTES - total_bytes))
            if total_bytes + len(payload) > MAX_EVIDENCE_TOTAL_BYTES:
                raise ValueError("total limit")
            safe_name = claim_unique_filename(normalize_filename(str(asset.get("name") or f"attachment-{index + 1}")), seen)
            target = uploads_dir / safe_name
            target.write_bytes(payload)
            total_bytes += len(payload)
            declared_type = str(asset.get("media_type") or "application/octet-stream")
            signature_type = _sniff_media_type(payload)
            media_type = signature_type or (response_type if response_type and response_type != "application/octet-stream" else declared_type)
            public_path = f"/mnt/user-data/uploads/{safe_name}"
            normalized_asset = {**asset, "media_type": media_type, "size": len(payload)}
            states[index] = _public_asset(normalized_asset, status="downloaded", name=safe_name, path=public_path)
            filename_type = _expected_signature_type_for_name(safe_name)
            if signature_type and filename_type and not _signature_types_compatible(filename_type, signature_type):
                states[index] = _public_asset(
                    normalized_asset,
                    status="type_mismatch",
                    name=safe_name,
                    path=public_path,
                    error=(f"文件名声明为 {_media_type_label(filename_type)}，实际内容为 {_media_type_label(signature_type)}，疑似附件上传或关联错误"),
                )
                if on_progress:
                    await on_progress([dict(item) for item in states])
                continue
            if _is_text(media_type, safe_name):
                attachment_index.append({"name": safe_name, "path": public_path, "media_type": media_type, "kind": "text_or_log", "status": "available", "size": len(payload)})
                states[index]["status"] = "indexed"
            elif media_type in {"application/zip", "application/x-zip-compressed"} or Path(safe_name).suffix.lower() == ".zip":
                attachment_index.extend(_archive_index(payload, safe_name))
                states[index]["status"] = "indexed"
            elif media_type.startswith("image/"):
                visual_paths.append(target)
                visual_asset_indexes.add(index)
            elif media_type.startswith("video/"):
                frames = await _extract_video_frames(target)
                visual_paths.extend(frames)
                if frames:
                    visual_asset_indexes.add(index)
        except (OSError, ValueError, ZentaoError):
            states[index] = _public_asset(asset, status="failed", error="下载失败")
        if on_progress:
            await on_progress([dict(item) for item in states])

    visual_evidence: Any = {"items": [], "status": "not_run"}
    if visual_paths:
        visual_error = ""
        normalized_evidence: dict[str, Any] = {"items": []}
        normalized_valid = False
        quality_summary: dict[str, Any] = {}
        attempt_count = 0
        selected_thinking_enabled = False
        response_sha256 = ""
        allowed_asset_names = {path.name for path in visual_paths}
        attempts = (
            (False, VISUAL_FAST_TIMEOUT_SECONDS, "visual_fast_processing"),
            (True, VISUAL_THINKING_TIMEOUT_SECONDS, "visual_thinking_retry"),
        )
        for thinking_enabled, timeout_seconds, progress_status in attempts:
            attempt_count += 1
            for index in visual_asset_indexes:
                states[index]["status"] = progress_status
                states[index].pop("error", None)
            if on_progress:
                await on_progress([dict(item) for item in states])
            try:
                if visual_analyzer is None:
                    raw_evidence = await asyncio.wait_for(
                        _default_visual_analyzer(visual_paths, thinking_enabled=thinking_enabled),
                        timeout=timeout_seconds,
                    )
                else:
                    raw_evidence = await asyncio.wait_for(visual_analyzer(visual_paths), timeout=timeout_seconds)
                normalized_evidence, normalized_valid = _normalize_visual_analysis(raw_evidence, allowed_asset_names=allowed_asset_names)
                quality_summary = _visual_quality_summary(normalized_evidence)
                response_sha256 = hashlib.sha256(
                    (raw_evidence if isinstance(raw_evidence, str) else json.dumps(raw_evidence, ensure_ascii=False, sort_keys=True)).encode("utf-8")
                ).hexdigest()
                selected_thinking_enabled = thinking_enabled
                if normalized_valid and normalized_evidence.get("items") and quality_summary.get("usable"):
                    break
                visual_error = "视觉识别未返回带真实附件引用的有效证据"
            except TimeoutError:
                visual_error = "视觉识别超时"
                logger.warning("Bug attachment visual analysis timed out: thinking_enabled=%s", thinking_enabled)
            except Exception:
                visual_error = "视觉分析失败"
                logger.exception("Bug attachment visual analysis failed: thinking_enabled=%s", thinking_enabled)
        visual_evidence = normalized_evidence
        visual_evidence["extractor"] = {
            "model": VISUAL_ANALYZER_MODEL,
            "temperature": VISUAL_ANALYZER_TEMPERATURE,
            "schema_version": VISUAL_EVIDENCE_SCHEMA_VERSION,
            "attempt_count": attempt_count,
            "thinking_enabled": selected_thinking_enabled,
            "response_sha256": response_sha256,
            "input_assets": _visual_input_provenance(visual_paths),
        }
        visual_evidence["quality"] = quality_summary
        visual_items = visual_evidence.get("items") if isinstance(visual_evidence, dict) else []
        if not normalized_valid:
            visual_evidence["status"] = "failed"
            for index in visual_asset_indexes:
                states[index]["status"] = "visual_analysis_failed"
                states[index]["error"] = visual_error or "视觉分析失败"
        elif visual_items:
            visual_evidence["status"] = "processed"
            for index in visual_asset_indexes:
                states[index]["status"] = "processed"
        else:
            visual_evidence["status"] = "no_evidence"
            for index in visual_asset_indexes:
                states[index]["status"] = "visual_no_evidence"
        if on_progress:
            await on_progress([dict(item) for item in states])

    return {
        "assets": states,
        "attachment_index": attachment_index,
        "visual_evidence": visual_evidence,
    }
