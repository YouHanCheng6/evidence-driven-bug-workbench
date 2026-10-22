"""Private lossless index for accepted runtime text logs."""

from __future__ import annotations

import argparse
import codecs
import json
import re
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import BinaryIO

MAX_LOG_TOTAL_BYTES = 512 * 1024 * 1024
SEGMENT_BYTES = 64 * 1024
LOG_QUERY_SCRIPT = ".deerflow-log-index.py"
_JSON_FIELD = re.compile(r'"([A-Za-z][A-Za-z0-9_]{1,63})"\s*:')
_PLAIN_FIELD = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z][A-Za-z0-9_]{1,63})\s*=")
_JSON_VALUE = re.compile(r'"(?P<field>[A-Za-z][A-Za-z0-9_]{1,63})"\s*:\s*"(?P<value>[A-Za-z][A-Za-z0-9_]{2,127})"')
_PLAIN_VALUE = re.compile(r'(?<![A-Za-z0-9_])(?P<field>[A-Za-z][A-Za-z0-9_]{1,63})\s*=\s*["\']?(?P<value>[A-Za-z][A-Za-z0-9_]{2,127})')


def search_key(value: str) -> str:
    return re.sub(r"[_ -]", "", value.casefold())


def iter_log_segments(stream: BinaryIO) -> Iterator[tuple[int, int, str, int]]:
    """Read physical lines in bounded pieces without dropping long lines."""
    line = 1
    part = 0
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    while raw := stream.readline(SEGMENT_BYTES):
        ended = raw.endswith(b"\n")
        quote = decoder.decode(raw, final=ended)
        yield line, part, quote.rstrip("\r\n") if ended else quote, len(raw)
        if ended:
            line += 1
            part = 0
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
        else:
            part += 1
    remaining = decoder.decode(b"", final=True)
    if remaining:
        yield line, part, remaining, 0


def is_text_source(name: str, header: bytes) -> bool:
    if not header or b"\x00" in header:
        return False
    suffix = Path(name).suffix.lower()
    if suffix in {".log", ".txt", ".json", ".csv", ".xml", ".yaml", ".yml", ".md"} or re.search(r"\.(?:log|txt|json|csv|xml|yaml|yml|md)\.\d+$", name, re.I):
        return True
    sample = header[:4096]
    return sum(byte in b"\t\n\r" or 32 <= byte <= 126 or byte >= 128 for byte in sample) / len(sample) >= 0.95


class LogIndexWriter:
    def __init__(self, path: Path, redact: Callable[[str], str]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.exists():
            raise FileExistsError("runtime log index already exists")
        self.path = path
        self.redact = redact
        self._previous_record: tuple[str, int] | None = None
        self._previous_tail = ""
        self.connection = sqlite3.connect(path)
        path.chmod(0o600)
        self.connection.execute("CREATE TABLE logs(source TEXT, line INTEGER, part INTEGER, quote TEXT, PRIMARY KEY(source,line,part))")
        self.connection.execute("CREATE VIRTUAL TABLE log_search USING fts5(normalized, tokenize='trigram')")

    def add(self, source: str, line: int, part: int, text: str) -> None:
        quote = self.redact(text)
        previous = self._previous_tail if self._previous_record == (source, line) else ""
        row = self.connection.execute("INSERT INTO logs VALUES(?,?,?,?)", (source, line, part, quote))
        self.connection.execute("INSERT INTO log_search(rowid,normalized) VALUES(?,?)", (row.lastrowid, search_key(previous + quote)))
        self._previous_record = (source, line)
        self._previous_tail = quote[-256:]

    def discard_line(self, source: str, line: int) -> int:
        self.connection.execute("DELETE FROM log_search WHERE rowid IN (SELECT rowid FROM logs WHERE source=? AND line=?)", (source, line))
        return self.connection.execute("DELETE FROM logs WHERE source=? AND line=?", (source, line)).rowcount

    def finish(self) -> None:
        self.connection.commit()
        self.connection.close()

    def abort(self) -> None:
        self.connection.close()
        self.path.unlink(missing_ok=True)


def read_record(path: Path, source: str, line: int) -> dict | None:
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        rows = connection.execute("SELECT part,quote FROM logs WHERE source=? AND line=? ORDER BY part", (source, line)).fetchall()
    if not rows or [row[0] for row in rows] != list(range(len(rows))):
        return None
    return {"source": source, "line": line, "raw": "".join(row[1] for row in rows)}


def _iter_lines(rows: Iterator[tuple[str, int, int, str]]) -> Iterator[dict]:
    current: tuple[str, int] | None = None
    parts: list[str] = []
    for source, line, _part, quote in rows:
        key = (source, line)
        if current is not None and key != current:
            yield {"source": current[0], "line": current[1], "raw": "".join(parts)}
            parts = []
        current = key
        parts.append(quote)
    if current is not None:
        yield {"source": current[0], "line": current[1], "raw": "".join(parts)}


def search_records(path: Path, text: str, *, regex: bool = False) -> Iterator[dict]:
    """Search the indexed text and return complete physical lines."""
    if not text:
        raise ValueError("search text cannot be empty")
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        if regex:
            matcher = re.compile(text, re.IGNORECASE)
            rows = connection.execute("SELECT source,line,part,quote FROM logs ORDER BY source,line,part")
            yield from (record for record in _iter_lines(rows) if matcher.search(record["raw"]))
            return
        normalized = search_key(text)
        if not normalized:
            raise ValueError("search text cannot be empty after normalization")
        if len(normalized) >= 3:
            escaped = normalized.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            hits = connection.execute("SELECT logs.source,logs.line FROM log_search JOIN logs ON logs.rowid=log_search.rowid WHERE log_search.normalized LIKE ? ESCAPE '\\'", (f"%{escaped}%",))
        else:
            hits = connection.execute("SELECT logs.source,logs.line FROM log_search JOIN logs ON logs.rowid=log_search.rowid WHERE instr(log_search.normalized, ?) > 0", (normalized,))
        seen: set[tuple[str, int]] = set()
        for source, line in hits:
            key = (source, line)
            if key in seen:
                continue
            seen.add(key)
            record = read_record(path, source, line)
            if record is not None:
                yield record


def discover_symbols(path: Path, text: str) -> Iterator[dict]:
    """Summarize fields and symbolic values that directly contain the query."""
    found: dict[tuple[str, str, str], dict] = {}
    query = search_key(text)
    for record in search_records(path, text):
        raw = record["raw"]
        names = {("field", "", match.group(1)) for pattern in (_JSON_FIELD, _PLAIN_FIELD) for match in pattern.finditer(raw) if query in search_key(match.group(1))}
        for pattern in (_JSON_VALUE, _PLAIN_VALUE):
            for match in pattern.finditer(raw):
                field = match.group("field")
                value = match.group("value")
                if query not in search_key(field) and query not in search_key(value):
                    continue
                names.add(("field", "", field))
                names.add(("value", field, value))
        for key in names:
            item = found.get(key)
            if item is None:
                found[key] = {
                    "kind": key[0],
                    "field": key[1],
                    "name": key[2],
                    "record_count": 1,
                    "source": record["source"],
                    "line": record["line"],
                }
            else:
                item["record_count"] += 1
    yield from sorted(
        found.values(),
        key=lambda item: (
            query not in search_key(item["name"]),
            item["kind"] != "value",
            -item["record_count"],
            item["name"],
            item["field"],
        ),
    )


def read_range(path: Path, source: str, first: int, last: int) -> Iterator[dict]:
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        rows = connection.execute("SELECT source,line,part,quote FROM logs WHERE source=? AND line BETWEEN ? AND ? ORDER BY line,part", (source, first, last))
        yield from _iter_lines(rows)


def list_sources(path: Path) -> Iterator[dict]:
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        sources = connection.execute("SELECT source,MIN(line),MAX(line) FROM logs GROUP BY source ORDER BY source").fetchall()
    for source, first, last in sources:
        first_record = read_record(path, source, first)
        last_record = read_record(path, source, last)
        yield {
            "source": source,
            "first_line": first,
            "last_line": last,
            "first_timestamp": (first_record["raw"][:24] if first_record else ""),
            "last_timestamp": (last_record["raw"][:24] if last_record else ""),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Search the read-only runtime log index")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("sources")
    search = commands.add_parser("search")
    search.add_argument("text", help="literal text to find across indexed log segments")
    search.add_argument("--regex", action="store_true", help="search complete lines with a regular expression")
    symbols = commands.add_parser("symbols")
    symbols.add_argument("text", help="literal text whose matching lines should reveal their actual field and event names")
    read = commands.add_parser("read")
    read.add_argument("source")
    read.add_argument("line", type=int)
    around = commands.add_parser("range")
    around.add_argument("source")
    around.add_argument("first", type=int)
    around.add_argument("last", type=int)
    args = parser.parse_args()
    index = Path(__file__).with_name("logs.sqlite")
    if args.command == "sources":
        for record in list_sources(index):
            print(json.dumps(record, ensure_ascii=False))
    elif args.command == "search":
        for record in search_records(index, args.text, regex=args.regex):
            print(json.dumps(record, ensure_ascii=False))
    elif args.command == "symbols":
        for record in discover_symbols(index, args.text):
            print(json.dumps(record, ensure_ascii=False))
    elif args.command == "read":
        record = read_record(index, args.source, args.line)
        if record is None:
            parser.error("source and line do not exist")
        print(json.dumps(record, ensure_ascii=False))
    else:
        for record in read_range(index, args.source, args.first, args.last):
            print(json.dumps(record, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
