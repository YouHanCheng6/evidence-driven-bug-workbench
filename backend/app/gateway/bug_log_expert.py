"""Bounded pre-investigation runtime-log evidence curation for Bug Workbench."""

from __future__ import annotations

import json
import re
import zipfile
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime
from itertools import zip_longest
from pathlib import Path
from typing import Any

from app.gateway.bug_attachment_evidence import MAX_ARCHIVE_MEMBERS, _is_text, _media_type_for_name, _redact_text
from app.gateway.bug_log_query_runtime import MAX_LOG_SOURCE_BYTES, MAX_LOG_TOTAL_BYTES, read_log_lines, write_log_index
from deerflow.config.app_config import AppConfig
from deerflow.utils.oneshot_llm import run_oneshot_llm_result

_RUNTIME_MARKER = re.compile(
    r"(?i)(?:\brequest\b|\bresponse\b|\breq\b|\bresp\b|\brsp\b|error|exception|timeout|failed|failure|"
    r"file(?:path)?|snapshot|decode|write|read|foreground|background|componentwill|lifecycle|pause|resume|"
    r"onerror|onsuccess|callback|bridge|navigate|bluetooth|connect|disconnect|audio|play|stop|sleep|"
    r"播放|录音|停止|断开|连接|\bble\b|https?://|/[A-Za-z0-9_.-]+/[A-Za-z0-9_./?=&%-]+)"
)
_EVENT_ROLE_PATTERNS = {
    "trigger": re.compile(r"(?i)(?:\brequest\b|\breq\b|called|invoke|click|start|begin|navigate|capture|takeSnapshot|open)"),
    "result": re.compile(r"(?i)(?:\bresponse\b|\bresp\b|\brsp\b|success|written|complete|failed|failure|error|exception|timeout)"),
    "transfer": re.compile(r"(?i)(?:callback|bridge|file(?:path)?|output|emit|resolve|promise|return|notify|dispatch|send|receive)"),
}
_GENERIC_SIGNAL = re.compile(r"(?i)^(?:\d+|object|data|desc|status|result|payload|code|error)$")
_OPEN_RELEVANCE = re.compile(r"(?:是否|能否).{0,28}(?:与本.{0,4}(?:Bug|问题)|直接相关|同一(?:操作|页面|流程|事务))", re.IGNORECASE)
_DIRECT_RELEVANCE_DISCLAIMER = re.compile(
    r"(?:未证明|尚未证明|无法确认|仍需确认).{0,180}(?:就是|来自|对应|属于).{0,40}(?:页面|操作|流程|工单|问题|Bug)",
    re.IGNORECASE,
)
_TRANSACTION_KEY = re.compile(
    r"(?i)(?:request[_ -]?id|reqid|requesttaskid|http[_ -]?log[_ -]?id|trace[_ -]?id|correlation[_ -]?id|package)"
    r"[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9][A-Za-z0-9_.:-]{2,79})"
)
_LOG_TIMESTAMP = re.compile(r"(?P<value>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[,.]\d{1,6})?)")
_STATE_MARKER = re.compile(
    r"(?i)(?P<marker>[A-Za-z][A-Za-z0-9_-]{1,80}(?:state|status)[A-Za-z0-9_-]{0,40}(?:change|changed)?)|"
    r"(?P<phrase>state\s*(?:change|changed)|状态(?:变更|变化))"
)
_STATE_VALUE = re.compile(
    r"(?i)\b(connected|disconnected|foreground|background|resumed|paused|started|stopped|active|inactive|"
    r"opened|closed|logged[_ -]?in|logged[_ -]?out|authenticated|unauthenticated|running|completed|failed|timeout)\b"
)
_STATE_OBJECT_PATTERNS = tuple(
    (
        label,
        re.compile(rf"(?i)(?:\[|\b){pattern}[\"']?\s*[:=]\s*[\"']?(?P<value>[A-Za-z0-9][A-Za-z0-9_.:@/-]{{2,127}})"),
    )
    for label, pattern in (
        ("rmac", "rmac"),
        ("device_id", "device[_ -]?id"),
        ("device", "device"),
        ("session_id", "session[_ -]?id"),
        ("task_id", "task[_ -]?id"),
        ("user_id", "user[_ -]?id"),
        ("sn", "sn"),
        ("package", "package"),
        ("hmac", "hmac"),
        ("bmac", "bmac"),
    )
)
_STATE_STARTS = frozenset({"connected", "foreground", "resumed", "started", "active", "opened", "logged_in", "authenticated", "running"})
_STATE_ENDS = frozenset({"disconnected", "background", "paused", "stopped", "inactive", "closed", "logged_out", "unauthenticated", "completed", "failed", "timeout"})
_LOG_EXPERT_TICKET_FIELDS = (
    "id",
    "title",
    "type",
    "description",
    "steps",
    "expected",
    "actual",
    "module",
    "product",
    "status",
    "resolution",
    "resolved_at",
    "closed_at",
    "severity",
)
_STATE_TRANSACTION_LIMIT = 4
_STATE_TRANSACTION_CHAR_BUDGET = 4_000
_STATE_ALIAS_LABELS = frozenset({"rmac", "hmac", "bmac", "device_id", "device", "sn"})
_PROPERTY_OBJECT = re.compile(r'\{[^{}\n]{0,800}"pid"\s*:\s*"[^"\n]{1,120}"[^{}\n]{0,800}\}')


def _signal_matcher(signal: str) -> re.Pattern[str]:
    # Search aliases only: spelling similarity never proves protocol identity.
    words = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", signal).split("_")
    return re.compile("[_ -]?".join(re.escape(word) for word in words), re.IGNORECASE)


def _focused_log_quote(lines: list[str], start: int, end: int, matchers: Sequence[re.Pattern[str]]) -> str:
    """Keep matching values, not just the prefix of a large JSON observation."""
    text = _redact_text("\n".join(lines[start:end])).strip()
    if len(text) <= 2_400:
        return text
    ranges = [(0, 300), (max(0, len(text) - 200), len(text))]
    for matcher in matchers:
        for match in list(matcher.finditer(text))[:3]:
            ranges.append((max(0, match.start() - 140), min(len(text), match.end() + 220)))
    merged: list[list[int]] = []
    for left, right in sorted(ranges):
        if merged and left <= merged[-1][1]:
            merged[-1][1] = max(right, merged[-1][1])
        else:
            merged.append([left, right])
    return "\n[日志摘录省略，非连续片段]\n".join(text[left:right] for left, right in merged)[:2_400]


def _identity_links(line: str, identifiers: Sequence[str]) -> list[dict[str, Any]]:
    """Bind an identifier to aliases inside the same parsed device record only."""
    wanted = {value.casefold() for value in identifiers if len(value) >= 8}
    if not wanted or not any(value in line.casefold() for value in wanted):
        return []
    records: list[dict[str, Any]] = []
    nodes = 0

    def visit(value: Any, depth: int = 0) -> set[str]:
        nonlocal nodes
        nodes += 1
        if nodes > 5_000 or depth > 16:
            return set()
        if isinstance(value, str):
            return {value} if value.casefold() in wanted else set()
        children = value.values() if isinstance(value, Mapping) else value if isinstance(value, list) else ()
        before = len(records)
        found: set[str] = set()
        for child in children:
            found.update(visit(child, depth + 1))
        if found and isinstance(value, Mapping) and len(records) == before:
            aliases = [alias for key, alias in value.items() if str(key).casefold() in {"uuid", "deviceid", "device_id", "rmac", "hmac", "bmac"}
                       and isinstance(alias, str) and 8 <= len(alias) <= 128]
            if aliases:
                for identifier in found:
                    fragments = []
                    for literal in [identifier, *aliases]:
                        offset = line.find(literal)
                        if offset >= 0:
                            fragments.append(line[max(0, offset - 60):offset + len(literal) + 60])
                    records.append({"identifier": identifier, "aliases": aliases[:4],
                                    "quote": _redact_text("\n[同一解析设备对象，省略中间内容]\n".join(fragments))[:1_200]})
        return found

    decoder = json.JSONDecoder()
    offset = 0
    for _ in range(16):
        offset = line.find("{", offset)
        if offset < 0:
            break
        try:
            value, end = decoder.raw_decode(line, offset)
        except ValueError:
            offset += 1
            continue
        visit(value)
        offset = end
    return records[:8]


def _coverage_order(items: Sequence[Mapping[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    """Prefer strong hits, then maximize line coverage inside one source."""
    remaining = [dict(item) for item in items]
    selected: list[dict[str, Any]] = []
    while remaining and len(selected) < limit:
        if not selected:
            best = max(remaining, key=lambda item: (int(item["_priority"]), -int(item["line_start"])))
        else:
            selected_lines = [int(item["line_start"]) for item in selected]
            best = max(
                remaining,
                key=lambda item: (
                    int(item["_priority"]),
                    min(abs(int(item["line_start"]) - line) for line in selected_lines),
                    -int(item["line_start"]),
                ),
            )
        selected.append(best)
        remaining.remove(best)
    return selected


def _round_robin_sources(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Interleave sources so archive order cannot consume the global budget."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    source_order: list[str] = []
    for raw in items:
        item = dict(raw)
        source = str(item.get("source") or "")
        if source not in grouped:
            source_order.append(source)
        grouped[source].append(item)
    ordered: list[dict[str, Any]] = []
    offset = 0
    while True:
        added = False
        for source in source_order:
            values = grouped[source]
            if offset < len(values):
                ordered.append(values[offset])
                added = True
        if not added:
            return ordered
        offset += 1


def _compact_ticket_for_log_expert(bug_snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Project only reporter-authored ticket facts into the one-shot log call."""
    compact: dict[str, Any] = {}
    remaining = 6_000
    for field in _LOG_EXPERT_TICKET_FIELDS:
        value = bug_snapshot.get(field)
        if value in (None, "") or not isinstance(value, (str, int, float, bool)):
            continue
        if isinstance(value, str):
            value = re.sub(r"\s+", " ", value).strip()
            if not value:
                continue
            value = value[:remaining]
        compact[field] = value
        remaining -= len(str(value))
        if remaining <= 0:
            break
    return compact


def _candidate_fingerprint(quote: str) -> str:
    """Collapse transport copies while retaining device IDs and business values."""
    normalized = re.sub(r"(?<=\d{2}:\d{2}:\d{2})[,.]\d{1,6}", "", quote)
    normalized = re.sub(r"(?i)\[(?:App|插件)日志\]", "[运行日志]", normalized)
    normalized = re.sub(r"(?i)\[(?:main|worker|thread):\d+\]", "[thread]", normalized)
    return re.sub(r"\s+", " ", normalized).strip().casefold()


def _candidate_for_prompt(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Hide duplicated restoration metadata from the model-visible packet."""
    return {key: value for key, value in candidate.items() if key != "endpoints"}


def _parse_log_timestamp(line: str) -> datetime | None:
    match = _LOG_TIMESTAMP.search(line)
    if match is None:
        return None
    try:
        return datetime.fromisoformat(match.group("value").replace(",", "."))
    except ValueError:
        return None


def _state_object(line: str) -> str:
    values = _state_objects(line)
    return values[0][1] if values else ""


def _state_objects(line: str) -> list[tuple[str, str]]:
    values: list[tuple[str, str]] = []
    seen: set[str] = set()
    for label, matcher in _STATE_OBJECT_PATTERNS:
        match = matcher.search(line)
        if match is not None:
            value = match.group("value").rstrip("])}>,;|").casefold()
            if value not in seen:
                seen.add(value)
                values.append((label, value))
    return values


def _canonical_state_family(marker: str) -> str:
    compact = re.sub(r"[^a-z0-9]+", "", marker.casefold())
    if "connectionstate" in compact:
        return "connection_state_change"
    return re.sub(r"[^a-z0-9]+", "_", marker.casefold()).strip("_")


def _state_event(source: str, line_number: int, line: str) -> dict[str, Any] | None:
    timestamp = _parse_log_timestamp(line)
    marker = _STATE_MARKER.search(line)
    object_id = _state_object(line)
    states = [_normalize_state(match.group(1)) for match in _STATE_VALUE.finditer(line)]
    if timestamp is None or marker is None or not object_id or not states:
        return None
    marker_value = marker.group("marker") or marker.group("phrase") or "state_change"
    family = _canonical_state_family(marker_value)
    objects = _state_objects(line)
    return {
        "source": source,
        "line_start": line_number,
        "line_end": line_number,
        "quote": _redact_text(line).strip()[:1_200],
        "timestamp": timestamp,
        "family": family,
        "object_id": object_id,
        "object_ids": [value for label, value in objects if label in _STATE_ALIAS_LABELS],
        "state": states[-1],
    }


def _normalize_state(value: str) -> str:
    return value.casefold().replace("-", "_").replace(" ", "_")


def _state_transaction_candidates(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Build bounded complete state intervals; isolated state lines stay weak."""
    ordered = sorted((dict(item) for item in events), key=lambda item: (item["timestamp"], item["source"], item["line_start"]))
    parents: dict[str, str] = {}

    def find(value: str) -> str:
        parents.setdefault(value, value)
        while parents[value] != value:
            parents[value] = parents[parents[value]]
            value = parents[value]
        return value

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parents[max(left_root, right_root)] = min(left_root, right_root)

    for event in ordered:
        object_ids = [str(value) for value in event.get("object_ids", []) if str(value)]
        for other in object_ids[1:]:
            union(object_ids[0], other)
    for event in ordered:
        event["object_id"] = find(str(event["object_id"]))

    deduplicated: list[dict[str, Any]] = []
    recent_by_signature: dict[tuple[str, str, str], datetime] = {}
    for event in ordered:
        signature = (str(event["family"]), str(event["object_id"]), str(event["state"]))
        previous_time = recent_by_signature.get(signature)
        if previous_time is not None and abs((event["timestamp"] - previous_time).total_seconds()) <= 0.25:
            continue
        recent_by_signature[signature] = event["timestamp"]
        deduplicated.append(event)

    starts: dict[tuple[str, str], dict[str, Any]] = {}
    transactions: list[dict[str, Any]] = []
    for event in deduplicated:
        key = (str(event["family"]), str(event["object_id"]))
        state = str(event["state"])
        if state in _STATE_STARTS:
            starts.setdefault(key, event)
            continue
        if state not in _STATE_ENDS:
            continue
        start = starts.pop(key, None)
        if start is None:
            continue
        duration_ms = round((event["timestamp"] - start["timestamp"]).total_seconds() * 1_000)
        if duration_ms <= 0 or duration_ms > 7 * 24 * 60 * 60 * 1_000:
            continue
        endpoints = [
            {name: start[name] for name in ("source", "line_start", "line_end", "quote")},
            {name: event[name] for name in ("source", "line_start", "line_end", "quote")},
        ]
        transactions.append(
            {
                "kind": "state_transaction",
                "source": str(start["source"]),
                "line_start": int(start["line_start"]),
                "line_end": int(event["line_end"]),
                "reasons": ["runtime_structure", "complete_state_transaction"],
                "quote": (f"[起点 {start['source']}:{start['line_start']}] {start['quote']}\n[终点 {event['source']}:{event['line_start']}] {event['quote']}"),
                "transaction_keys": [f"{key[0]}:{key[1]}"],
                "state_family": key[0],
                "state_object": key[1],
                "state_from": str(start["state"]),
                "state_to": state,
                "duration_ms": duration_ms,
                "endpoints": endpoints,
            }
        )
    transactions.sort(key=lambda item: (-int(item["duration_ms"]), int(item["line_start"])))
    return transactions[:_STATE_TRANSACTION_LIMIT]


def _indexed_asset_names(attachment_evidence: Mapping[str, Any]) -> list[str]:
    assets = attachment_evidence.get("assets")
    if not isinstance(assets, list):
        return []
    return list(dict.fromkeys(str(item.get("name") or "").strip() for item in assets if isinstance(item, Mapping) and str(item.get("status") or "") == "indexed" and str(item.get("name") or "").strip()))


def collect_log_material(
    uploads_dir: Path,
    attachment_evidence: Mapping[str, Any],
    *,
    signals: Sequence[str] = (),
    identity_signals: Sequence[str] = (),
    time_hints: Sequence[str] = (),
    max_chars: int = 80_000,
    index_path: Path | None = None,
) -> dict[str, Any]:
    """Collect broad, provenance-preserving excerpts without judging Bug relevance."""
    normalized_signals: list[str] = []
    seen_signals: set[str] = set()
    for raw_signal in signals:
        signal = str(raw_signal).strip()
        folded = signal.casefold()
        if len(signal) < 3 or _GENERIC_SIGNAL.fullmatch(signal) or folded in seen_signals:
            continue
        seen_signals.add(folded)
        normalized_signals.append(signal)
    normalized_times = list(dict.fromkeys(str(item).strip() for item in time_hints if str(item).strip()))
    date_hints = [value.replace("/", "-") for value in normalized_times if re.fullmatch(r"20\d{2}[-/]\d{1,2}[-/]\d{1,2}", value)]
    minute_hints = [value for value in normalized_times if re.fullmatch(r"\d{2}:\d{2}", value)]

    def matches_ticket_time(line: str) -> bool:
        return bool(normalized_times) and (not date_hints or any(value in line for value in date_hints)) and (not minute_hints or any(value in line for value in minute_hints))
    signal_matchers = [(value, _signal_matcher(value)) for value in normalized_signals]
    raw_candidates: list[dict[str, Any]] = []
    state_events: list[dict[str, Any]] = []
    identity_links: list[dict[str, Any]] = []
    linked_aliases: set[tuple[str, str]] = set()
    files_searched = 0
    diagnostics: list[dict[str, Any]] = []
    indexed_sources: list[tuple[str, list[str]]] = []
    bytes_read = 0

    def collect(source: str, lines: list[str]) -> None:
        nonlocal files_searched
        files_searched += 1
        hits: list[tuple[int, int, list[str]]] = []
        property_states: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        for index, line in enumerate(lines):
            if len(identity_links) < 8:
                for link in _identity_links(line, identity_signals):
                    identity = (link["identifier"], "|".join(link["aliases"]))
                    if identity not in linked_aliases:
                        identity_links.append({**link, "source": source, "line_start": index + 1, "line_end": index + 1})
                        linked_aliases.add(identity)
            event = _state_event(source, index + 1, line)
            if event is not None:
                state_events.append(event)
            # Preserve actual property objects even when deep inside a long
            # status JSON and the field was not known before source reading.
            # Value diversity is an observation, not an automatic defect.
            if not normalized_times or matches_ticket_time(line):
                for match in _PROPERTY_OBJECT.finditer(line):
                    try:
                        value = json.loads(match.group(0))
                    except ValueError:
                        continue
                    if not isinstance(value, Mapping) or not isinstance(value.get("pid"), str) or "value" not in value:
                        continue
                    pid = value["pid"]
                    if len(property_states) >= 128 and pid not in property_states:
                        continue
                    state = json.dumps(value["value"], ensure_ascii=False)
                    # First and latest for each value preserve both conflicting
                    # and recovered observations without copying the whole log.
                    rows = property_states[pid]
                    quote = _redact_text(line[:250] + "\n[属性原文，省略中间内容]\n" + match.group(0) + "\n[行尾原文]\n" + line[-180:])[:1_400]
                    matched = [signal for signal, matcher in signal_matchers if matcher.search(pid)]
                    item = {"source": source, "line_start": index + 1, "line_end": index + 1, "quote": quote,
                            "reasons": ["runtime_structure", "property_state", *(["ticket_time"] if normalized_times else []), *[f"signal:{signal}" for signal in matched]],
                            "_priority": 200 + (300 if matched else 0), "_source_order": files_searched}
                    if state in rows or len(rows) < 4:
                        rows.setdefault(state, item)
                        rows["latest"] = item
            reasons: list[str] = []
            matched_signals = [value for value, matcher in signal_matchers if matcher.search(line)]
            if matched_signals:
                reasons.extend(f"signal:{value}" for value in matched_signals[:4])
            if matches_ticket_time(line):
                reasons.append("ticket_time")
            if _RUNTIME_MARKER.search(line):
                reasons.append("runtime_structure")
            if not reasons:
                continue
            nearby = "\n".join(lines[max(0, index - 3) : min(len(lines), index + 4)])
            event_roles = [role for role, matcher in _EVENT_ROLE_PATTERNS.items() if matcher.search(nearby)]
            reasons.extend(f"event_role:{role}" for role in event_roles)
            priority = (300 if "ticket_time" in reasons else 0) + (200 if matched_signals else 0) + (100 if "runtime_structure" in reasons else 0) + max(0, len(event_roles) - 1) * 90
            hits.append((priority, index, reasons))
        source_candidates: list[dict[str, Any]] = []
        covered_centers: set[int] = set()
        for priority, index, reasons in sorted(hits, key=lambda item: (-item[0], item[1])):
            radius = 4 if sum(1 for value in reasons if value.startswith("event_role:")) >= 2 else 1
            start = max(0, index - radius)
            end = min(len(lines), index + radius + 1)
            # Partial neighborhood overlap must not erase a later result or
            # callback outside the previously transmitted interval.
            if index in covered_centers:
                continue
            quote = _focused_log_quote(lines, start, end, [matcher for _, matcher in signal_matchers])
            if not quote:
                continue
            covered_centers.update(range(start, end))
            transaction_keys = list(dict.fromkeys(match.group(1) for match in _TRANSACTION_KEY.finditer(quote)))[:4]
            source_candidates.append(
                {
                    "source": source,
                    "line_start": start + 1,
                    "line_end": end,
                    "reasons": reasons,
                    "quote": quote[:2_400],
                    **({"transaction_keys": transaction_keys} if transaction_keys else {}),
                    "_priority": priority,
                    "_source_order": files_searched,
                }
            )
        for rows in property_states.values():
            changed = len(rows) > 2
            for item in rows.values():
                item = dict(item)
                if changed:
                    item["_priority"] += 180
                source_candidates.append(item)
        transaction_counts = Counter(key for item in source_candidates for key in item.get("transaction_keys", []))
        for item in source_candidates:
            if any(transaction_counts[key] > 1 for key in item.get("transaction_keys", [])):
                item["reasons"].append("transaction_group")
                item["_priority"] = int(item["_priority"]) + 180
        signal_candidates = [item for item in source_candidates if any(str(reason).startswith("signal:") for reason in item["reasons"])]
        time_candidates = [item for item in source_candidates if "ticket_time" in item["reasons"] and item not in signal_candidates]
        runtime_candidates = [item for item in source_candidates if item not in signal_candidates and item not in time_candidates]
        raw_candidates.extend(_coverage_order(signal_candidates, limit=16))
        raw_candidates.extend(_coverage_order(time_candidates, limit=24))
        raw_candidates.extend(_coverage_order(runtime_candidates, limit=24))

    def scan(source: str, stream: Any) -> None:
        nonlocal bytes_read
        remaining = MAX_LOG_TOTAL_BYTES - bytes_read
        if remaining <= 0:
            diagnostics.append({"source": source, "status": "skipped", "reason": "total_byte_limit"})
            return
        lines, limits = read_log_lines(stream, min(MAX_LOG_SOURCE_BYTES, remaining))
        bytes_read += int(limits["bytes_read"])
        diagnostics.append({"source": source, "status": "partial" if limits["truncated"] or limits["oversized_lines"] else "scanned", **limits})
        collect(source, lines)
        if index_path is not None:
            indexed_sources.append((source, lines))

    for name in _indexed_asset_names(attachment_evidence):
        path = uploads_dir / Path(name).name
        try:
            if path.suffix.lower() == ".zip" or zipfile.is_zipfile(path):
                with zipfile.ZipFile(path) as archive:
                    members = archive.infolist()
                    if len(members) > MAX_ARCHIVE_MEMBERS:
                        diagnostics.append({"source": name, "status": "partial", "reason": "member_count_limit"})
                    for member in members[:MAX_ARCHIVE_MEMBERS]:
                        if member.is_dir() or member.file_size <= 0:
                            continue
                        member_name = member.filename.replace("\\", "/").lstrip("/")
                        if not _is_text(_media_type_for_name(member_name), member_name):
                            continue
                        source = f"{name}!/{member_name}"
                        if member.flag_bits & 0x1:
                            diagnostics.append({"source": source, "status": "skipped", "reason": "encrypted_member"})
                            continue
                        try:
                            with archive.open(member) as stream:
                                scan(source, stream)
                        except (OSError, RuntimeError, zipfile.BadZipFile, EOFError):
                            diagnostics.append({"source": source, "status": "failed", "reason": "member_read_failed"})
            elif path.is_file() and _is_text(_media_type_for_name(name), name):
                with path.open("rb") as stream:
                    scan(name, stream)
            elif not path.is_file():
                diagnostics.append({"source": name, "status": "failed", "reason": "attachment_missing"})
        except (OSError, RuntimeError, zipfile.BadZipFile, KeyError, EOFError):
            diagnostics.append({"source": name, "status": "failed", "reason": "archive_or_attachment_read_failed"})
    index_status = "not_requested"
    if index_path is not None and files_searched:
        try:
            write_log_index(index_path, indexed_sources, diagnostics, _redact_text, normalized_times)
            index_status = "ready"
        except Exception:
            # Optional acceleration cannot block analysis or lose the excerpts.
            index_status = "failed"

    signal_candidates = [item for item in raw_candidates if any(str(reason).startswith("signal:") for reason in item["reasons"])]
    time_candidates = [item for item in raw_candidates if "ticket_time" in item["reasons"] and item not in signal_candidates]
    runtime_candidates = [item for item in raw_candidates if item not in signal_candidates and item not in time_candidates]
    # Strong identifiers must not consume the entire packet before time/state
    # evidence gets a slot. All categories retain fair per-source ordering.
    ordered = [item for row in zip_longest(_round_robin_sources(signal_candidates),
                                          _round_robin_sources(time_candidates),
                                          _round_robin_sources(runtime_candidates))
               for item in row if item is not None]

    candidates: list[dict[str, Any]] = []
    used_chars = 0
    quote_fingerprints: set[str] = set()
    duplicate_candidates_removed = 0
    considered_material_chars = 0
    for item in ordered:
        public = {key: value for key, value in item.items() if not key.startswith("_")}
        cost = len(json.dumps(public, ensure_ascii=False)) + 1
        considered_material_chars += cost
        fingerprint = _candidate_fingerprint(str(public.get("quote") or ""))
        if not fingerprint or fingerprint in quote_fingerprints:
            if fingerprint in quote_fingerprints:
                duplicate_candidates_removed += 1
            continue
        if used_chars + cost > max_chars:
            continue
        public["id"] = f"L{len(candidates) + 1}"
        candidates.append(public)
        quote_fingerprints.add(fingerprint)
        used_chars += cost
        if len(candidates) >= 96 or used_chars >= max_chars:
            break
    ordinary_material_chars = used_chars
    transaction_material_chars = 0
    state_transactions = _state_transaction_candidates(state_events)
    for transaction in state_transactions:
        public = dict(transaction)
        public["id"] = f"L{len(candidates) + 1}"
        cost = len(json.dumps(_candidate_for_prompt(public), ensure_ascii=False)) + 1
        if transaction_material_chars + cost > _STATE_TRANSACTION_CHAR_BUDGET:
            continue
        candidates.append(public)
        transaction_material_chars += cost
    used_chars += transaction_material_chars
    return {
        "status": "collected" if candidates else "read_failed" if files_searched == 0 and diagnostics else "no_material",
        "scan_diagnostics": diagnostics[:64],
        "scan_partial": any(item["status"] in {"partial", "skipped", "failed"} for item in diagnostics),
        "bytes_read": bytes_read,
        "index_status": index_status,
        "identity_links": identity_links,
        "files_searched": files_searched,
        "signals": normalized_signals,
        "time_hints": normalized_times,
        "candidates": candidates,
        "material_chars": used_chars,
        "ordinary_material_chars": ordinary_material_chars,
        "considered_material_chars": considered_material_chars,
        "state_transaction_chars": transaction_material_chars,
        "state_transaction_count": sum(1 for item in candidates if item.get("kind") == "state_transaction"),
        "duplicate_candidates_removed": duplicate_candidates_removed,
    }


def _json_object(text: str) -> dict[str, Any]:
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, flags=re.DOTALL | re.IGNORECASE)
    candidate = fenced.group(1) if fenced else cleaned
    start, end = candidate.find("{"), candidate.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("日志专家没有返回 JSON 对象")
    value = json.loads(candidate[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("日志专家返回值不是 JSON 对象")
    return value


def validate_log_expert_output(payload: Mapping[str, Any], material: Mapping[str, Any], *, max_items: int) -> dict[str, Any]:
    """Restore exact excerpts by candidate ID and admit only direct evidence."""
    raw_candidates = material.get("candidates")
    candidate_map = {str(item.get("id")): dict(item) for item in raw_candidates if isinstance(item, Mapping) and str(item.get("id") or "")} if isinstance(raw_candidates, list) else {}
    raw_items = payload.get("evidence")
    selections: list[dict[str, Any]] = []
    accepted: list[dict[str, Any]] = []
    seen_groups: set[tuple[str, ...]] = set()
    for raw in raw_items if isinstance(raw_items, list) else []:
        if not isinstance(raw, Mapping):
            continue
        ids = tuple(dict.fromkeys(str(value).strip() for value in raw.get("candidate_ids", []) if str(value).strip())) if isinstance(raw.get("candidate_ids"), list) else ()
        if not ids or len(ids) > 4 or any(value not in candidate_map for value in ids) or ids in seen_groups:
            continue
        seen_groups.add(ids)
        relevance = str(raw.get("relevance") or "").strip().lower()
        if relevance not in {"direct", "supporting", "reject"}:
            continue
        unproven = str(raw.get("unproven") or "").strip()[:700]
        if relevance == "direct" and (_OPEN_RELEVANCE.search(unproven) or _DIRECT_RELEVANCE_DISCLAIMER.search(unproven)):
            relevance = "supporting"
        restored_sources: list[dict[str, Any]] = []
        for value in ids:
            candidate = candidate_map[value]
            endpoints = candidate.get("endpoints")
            if isinstance(endpoints, list) and endpoints:
                restored_sources.extend(
                    {
                        "candidate_id": value,
                        "source": endpoint["source"],
                        "line_start": endpoint["line_start"],
                        "line_end": endpoint["line_end"],
                        "quote": endpoint["quote"],
                    }
                    for endpoint in endpoints
                    if isinstance(endpoint, Mapping) and all(name in endpoint for name in ("source", "line_start", "line_end", "quote"))
                )
            else:
                restored_sources.append(
                    {
                        "candidate_id": value,
                        "source": candidate["source"],
                        "line_start": candidate["line_start"],
                        "line_end": candidate["line_end"],
                        "quote": candidate["quote"],
                    }
                )
        item = {
            "candidate_ids": list(ids),
            "relevance": relevance,
            "summary": str(raw.get("summary") or "").strip()[:500],
            "proven": str(raw.get("proven") or "").strip()[:700],
            "unproven": unproven,
            "sources": restored_sources,
        }
        selections.append(item)
        if relevance == "direct" and item["summary"] and item["proven"] and len(accepted) < max_items:
            accepted.append({**item, "evidence_id": f"R{len(accepted) + 1}"})
    return {"selections": selections, "accepted_evidence": accepted}


def _render_evidence_text(evidence: Sequence[Mapping[str, Any]]) -> str:
    chunks: list[str] = []
    for item in evidence:
        sources = "\n".join(f"[{source['candidate_id']}] {source['source']}:{source['line_start']}-{source['line_end']}\n{source['quote']}" for source in item.get("sources", []) if isinstance(source, Mapping))
        chunks.append(f"RUNTIME_EVIDENCE {item.get('evidence_id')}\n摘要：{item.get('summary')}\n已证明：{item.get('proven')}\n尚未证明：{item.get('unproven')}\n{sources}")
    return "\n\n".join(chunks)


async def run_log_expert(
    *,
    bug_id: int,
    bug_snapshot: Mapping[str, Any],
    code_signals: Sequence[str],
    material: Mapping[str, Any],
    app_config: AppConfig,
    model_name: str | None,
    max_output_tokens: int,
    max_items: int,
    thinking_enabled: bool,
    thread_id: str,
) -> dict[str, Any]:
    candidates = material.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return {"status": "no_material", "model": model_name, "model_call_count": 0, "token_usage": {}, "selections": [], "accepted_evidence": [], "text": ""}
    compact_ticket = _compact_ticket_for_log_expert(bug_snapshot)
    prompt_candidates = sorted(
        (_candidate_for_prompt(item) for item in candidates if isinstance(item, Mapping)),
        key=lambda item: 0 if item.get("kind") == "state_transaction" else 1,
    )
    prompt = "\n".join(
        (
            f"为禅道 Bug #{bug_id} 从真实日志候选中提炼调查前运行证据。你不能调用工具。",
            "充分理解工单语义和日志上下文。背景请求、普通成功流量、孤立数字和通用字段不得作为直接证据。允许返回空 evidence。",
            "最多选择 3 条证据；只有确实存在不同因果角色时可选择第 4 条。连续候选或带相同 transaction_keys 的请求、响应、字段与回调应合并为同一条证据。",
            "direct 必须直接对应工单中同一用户路径的操作、异常结果或关键传递节点；仅仅属于同一产品、设备或邻近页面不够。",
            "如果你在 unproven 中仍需确认该日志是否与本 Bug、同一操作或同一页面直接相关，就必须标为 supporting 或 reject，不能标为 direct。",
            "supporting 只作背景；reject 表示无关。优先覆盖触发、传递、失败和消费者等不同因果角色，不要选择重复背景流量。",
            "状态同步问题应保留同一对象、同一属性在设备返回、上传、云端读取、缓存中的一致或冲突值及时间顺序，不得只挑一条正常返回。上传请求不等于成功，接口成功码不等于字段满足预期。跨拼写字段只作候选，协议或源码核实后才认定同义。",
            "kind=state_transaction 的候选由后端从同一对象的两个精确状态端点确定性生成；duration_ms 只证明该状态区间长度，不证明结束原因。",
            "不要判断最终根因、责任仓库、Native/RN、修复范围或 target。必须明确已证明与尚未证明的边界。",
            '只输出 JSON：{"evidence":[{"candidate_ids":["L1"],"relevance":"direct|supporting|reject","summary":"一句话","proven":"日志直接证明的事实","unproven":"仍需源码验证的内容"}]}。',
            "工单事实：",
            json.dumps(compact_ticket, ensure_ascii=False)[:20_000],
            "历史源码线索（只作提示，可忽略）：",
            json.dumps(list(code_signals), ensure_ascii=False),
            "后端从同一解析设备对象提取的标识映射（带原始来源片段，只用于对象关联，不证明因果）：",
            json.dumps(material.get("identity_links", []), ensure_ascii=False),
            "扫描范围与读取限制：未命中不能写日志中不存在；读取失败不能写用户未提供日志。",
            json.dumps(material.get("scan_diagnostics", [])[:16], ensure_ascii=False),
            "带稳定 ID 的真实日志材料：",
            "\n".join(json.dumps(item, ensure_ascii=False) for item in prompt_candidates),
        )
    )
    response = await run_oneshot_llm_result(
        system_instruction="你是一次性的运行日志证据专家。你只从给定真实候选中提炼与工单直接相关的运行事实。",
        user_content=prompt,
        run_name="bug-runtime-log-expert",
        app_config=app_config,
        model_name=model_name,
        thread_id=thread_id,
        max_tokens=max_output_tokens,
        thinking_enabled=thinking_enabled,
    )
    validated = validate_log_expert_output(_json_object(response.text), material, max_items=max_items)
    accepted = validated["accepted_evidence"]
    text = _render_evidence_text(accepted)
    return {
        "status": "matched" if accepted else "no_match",
        "model": model_name,
        "model_call_count": 1,
        "token_usage": dict(response.usage_metadata),
        "files_searched": int(material.get("files_searched") or 0),
        "material_chars": int(material.get("material_chars") or 0),
        "selections": validated["selections"],
        "accepted_evidence": accepted,
        "text": text,
        "transactions": [
            {
                "signal": str(item.get("summary") or ""),
                "source": ", ".join(str(source.get("source") or "") for source in item.get("sources", []) if isinstance(source, Mapping)),
                "confidence": "model_selected",
                "causal_closure_allowed": False,
                "evidence_id": item.get("evidence_id"),
            }
            for item in accepted
        ],
    }
