"""Private, bounded runtime-log index and read-only query (stdlib only).

Copied into the source view for Terminal use; output is never source proof.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

LOG_CACHE_DIRECTORY = ".deerflow-runtime-evidence"
LOG_QUERY_RUNTIME = ".deerflow-log-query.py"
MAX_LOG_SOURCE_BYTES = 16 * 1024 * 1024
MAX_LOG_TOTAL_BYTES = 64 * 1024 * 1024
MAX_LOG_LINE_BYTES = 256 * 1024


def search_key(value: str) -> str:
    # Search spelling aliases, NOT protocol equivalence.
    return re.sub(r"[_ -]", "", value.casefold())


def read_log_lines(stream: BinaryIO, limit: int) -> tuple[list[str], dict[str, int | bool]]:
    """Bound decompression and memory; retain physical line numbering.

    Oversized physical lines are skipped, not truncated into false observations.
    A source-byte boundary also drops its incomplete last line.
    """
    lines: list[str] = []
    used = 0
    oversized = 0
    discarding = False
    truncated = False
    while used < limit and len(lines) < 100_000:
        raw = stream.readline(min(MAX_LOG_LINE_BYTES + 1, limit - used))
        if not raw:
            break
        used += len(raw)
        terminated = raw.endswith(b"\n")
        if discarding or len(raw) > MAX_LOG_LINE_BYTES:
            if not discarding:
                lines.append("")
                oversized += 1
            discarding = not terminated
            continue
        if used == limit and not terminated:
            truncated = True
            break
        lines.append(raw.decode("utf-8", errors="replace").rstrip("\r\n"))
    if used == limit or len(lines) == 100_000:
        truncated = bool(stream.read(1)) or truncated
    return lines, {"bytes_read": used, "oversized_lines": oversized, "truncated": truncated}


def write_log_index(path: Path, sources: list[tuple[str, list[str]]], diagnostics: list[dict], redact: Callable[[str], str], time_hints: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Caller supplies a fresh per-run UUID path. Never overwrite another run.
    if path.exists():
        raise FileExistsError("runtime log index already exists")
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE logs(source TEXT, line INTEGER, quote TEXT, normalized TEXT, PRIMARY KEY(source,line))")
        connection.execute("CREATE TABLE metadata(value TEXT)")
        connection.execute("CREATE TABLE properties(source TEXT,line INTEGER,pid TEXT,value TEXT)")
        for source, lines in sources:
            connection.executemany("INSERT INTO logs VALUES(?,?,?,?)", ((source, line, quote, search_key(quote))
                                   for line, text in enumerate(lines, 1) if text
                                   for quote in [redact(text)]))
            for line, text in enumerate(lines, 1):
                for match in re.finditer(r'\{[^{}\n]{0,800}"pid"\s*:\s*"[^"\n]{1,120}"[^{}\n]{0,800}\}', text):
                    try:
                        value = json.loads(match.group(0))
                    except ValueError:
                        continue
                    if isinstance(value.get("pid"), str) and "value" in value:
                        digest = hashlib.sha256(json.dumps(value["value"], ensure_ascii=False).encode()).hexdigest()
                        connection.execute("INSERT INTO properties VALUES(?,?,?,?)", (source, line, search_key(value["pid"]), digest))
        connection.execute("INSERT INTO metadata VALUES(?)", (json.dumps({"diagnostics": diagnostics, "time_hints": time_hints or []}, ensure_ascii=False),))
    path.chmod(0o600)


def query_log_index(path: Path, signals: list[str], *, max_chars: int = 8_000) -> dict:
    """Keep first and last hits per source/field with their adjacent lines."""
    packet: dict = {"policy": "runtime_observation_not_source_or_causal_proof", "sources": [],
                    "limits": "字段拼写近似仅用于检索；先核对设备、事务、时间及值；命中不证明责任，未命中不证明不存在。"}
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        connection.execute("PRAGMA query_only=ON")
        metadata = json.loads(connection.execute("SELECT value FROM metadata").fetchone()[0])
        diagnostics = metadata["diagnostics"]
        packet["diagnostics"] = diagnostics[:16]
        packet["time_hints"] = metadata.get("time_hints", [])
        packet["scan_partial"] = any(item.get("status") != "scanned" for item in diagnostics)
        used = len(json.dumps(packet, ensure_ascii=False))
        seen: set[tuple[str, int]] = set()
        for signal in signals[:16]:
            if not isinstance(signal, str) or not 3 <= len(signal) <= 160 or signal.isdigit():
                continue
            key = search_key(signal)
            rows = []
            clauses = []
            time_params: list[str] = []
            for hints in ([value for value in packet["time_hints"] if "-" in value], [value for value in packet["time_hints"] if ":" in value]):
                if hints:
                    clauses.append("(" + " OR ".join("instr(quote,?)>0" for _ in hints) + ")")
                    time_params.extend(hints)
            if clauses:
                rows.extend(connection.execute("SELECT source,MIN(line),MAX(line),COUNT(*) FROM logs WHERE instr(normalized,?)>0 AND " + " AND ".join(clauses) + " GROUP BY source", (key, *time_params)).fetchall())
            # Time coincidence is ranking only, never transaction identity;
            # retain untimed alternatives instead of silently excluding them.
            rows.extend(connection.execute("SELECT source,MIN(line),MAX(line),COUNT(*) FROM logs WHERE instr(normalized,?)>0 GROUP BY source", (key,)).fetchall())
            # Preserve returned property-value diversity, even when the first
            # and last values are equal. Diversity alone is not a defect.
            rows.extend(connection.execute("SELECT source,MIN(line),MAX(line),COUNT(*) FROM properties WHERE instr(pid,?)>0 GROUP BY source,pid,value LIMIT 32", (key,)).fetchall())
            for source, first, last, count in rows:
                for hit in dict.fromkeys((first, last)):
                    if (source, hit) in seen:
                        continue
                    nearby = connection.execute("SELECT line,quote FROM logs WHERE source=? AND line BETWEEN ? AND ? ORDER BY line", (source, max(1, hit - 1), hit + 1)).fetchall()
                    # Keep the actual match inside a huge JSON, never silently
                    # crop it to a prefix. Noncontinuous fragments are labelled.
                    quotes = []
                    for line, quote in nearby:
                        if len(quote) > 1_600:
                            # Normalization changes offsets, so use the literal
                            # alias regex in the original quote for restoration.
                            matcher = re.compile("[_ -]?".join(re.escape(part) for part in re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", signal).split("_")), re.I)
                            match = matcher.search(quote)
                            offset = match.start() if match else 0
                            quote = "[非连续摘录]\n" + quote[:180] + "\n[省略]\n" + quote[max(0, offset - 180):offset + 1_000]
                        quotes.append(f"{line}\t{quote}")
                    item = {"source": source, "line_start": nearby[0][0], "line_end": nearby[-1][0],
                            "quote": "\n".join(quotes), "signal": signal, "hit_count": count}
                    cost = len(json.dumps(item, ensure_ascii=False))
                    if used + cost > max_chars:
                        packet["projection_truncated"] = True
                        continue
                    packet["sources"].append(item)
                    seen.add((source, hit))
                    used += cost
        packet["status"] = "matched" if packet["sources"] else "no_match_in_scanned_material"
    return packet


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only private runtime-log recall")
    parser.add_argument("--run", required=True)
    parser.add_argument("--signal", action="append", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-f0-9]{32}", args.run):
        parser.error("invalid run identifier")
    root = Path(__file__).resolve().parent
    path = root / LOG_CACHE_DIRECTORY / args.run / "logs.sqlite"
    if path.resolve().parent != root / LOG_CACHE_DIRECTORY / args.run:
        parser.error("runtime evidence path escapes private run directory")
    try:
        result = query_log_index(path, args.signal)
    except (OSError, sqlite3.Error, ValueError):
        result = {"status": "index_unavailable", "policy": "runtime_observation_not_source_or_causal_proof", "sources": []}
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
