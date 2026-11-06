"""Offline contracts using invented data; no SDK, model, or enterprise services."""

import ast
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def load_module(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


runtime_stub = types.ModuleType("deerflow.config.runtime_paths")
runtime_stub.runtime_home = lambda: Path("/nonexistent-public-test-runtime")
with patch.dict(
    sys.modules,
    {
        "deerflow": types.ModuleType("deerflow"),
        "deerflow.config": types.ModuleType("deerflow.config"),
        "deerflow.config.runtime_paths": runtime_stub,
    },
):
    investigation = load_module(
        "public_investigation", "backend/app/gateway/bug_codex_summary.py"
    )
logs = load_module("public_logs", "backend/app/gateway/bug_log_query_runtime.py")
history = load_module(
    "public_history", "backend/app/gateway/bug_git_history_runtime.py"
)
attachment_stub = types.ModuleType("app.gateway.bug_attachment_evidence")
attachment_stub._redact_text = lambda text: text
with patch.dict(
    sys.modules,
    {
        "app.gateway.bug_attachment_evidence": attachment_stub,
        "app.gateway.bug_log_query_runtime": logs,
    },
):
    preparation = load_module(
        "public_preparation", "backend/app/gateway/bug_log_expert.py"
    )


def report(second, fourth):
    return "\n\n".join(
        (
            "一、分析结论\n虚构案例。",
            "二、责任端与责任层\n" + second,
            "三、根本原因及源码证据\nsample_mobile_repo/example.py:1：虚构引用。",
            "四、修改范围与其他端风险\n" + fourth,
        )
    )


class InvestigationContracts(unittest.TestCase):
    def test_visual_extraction_has_only_the_codex_entry(self):
        path = ROOT / "backend/app/gateway/bug_attachment_evidence.py"
        tree = ast.parse(path.read_text())
        functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef)
        }
        self.assertNotIn("_legacy_chat_model_visual_analyzer", functions)
        entry = functions["_default_visual_analyzer"]
        calls = [node for node in ast.walk(entry) if isinstance(node, ast.Call)]
        self.assertFalse(
            any(
                isinstance(call.func, ast.Name) and call.func.id == "create_chat_model"
                for call in calls
            )
        )
        start = next(
            call
            for call in calls
            if isinstance(call.func, ast.Attribute) and call.func.attr == "thread_start"
        )
        self.assertTrue(
            next(kw.value.value for kw in start.keywords if kw.arg == "ephemeral")
        )
        codex_calls = [
            call.func.attr
            for call in calls
            if isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "codex"
        ]
        self.assertEqual(
            codex_calls,
            ["thread_start"],
            "Start the visual thread directly; do not add account-routing preflight calls.",
        )
        strings = {
            node.value
            for node in ast.walk(entry)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        self.assertIn("features.shell_tool=false", strings)
        self.assertIn("features.apps=false", strings)
        self.assertIn('web_search="disabled"', strings)

    def test_wiki_allowlist_and_credential_boundary(self):
        wiki = investigation.ExampleWikiMcp(
            "https://wiki.example.invalid/mcp", "Bearer fictional-fixture"
        )
        config = wiki.thread_config()
        server = config["mcp_servers"]["examplewiki"]
        self.assertEqual(len(server["enabled_tools"]), 8)
        self.assertTrue(
            all(name.startswith("example_") for name in server["enabled_tools"])
        )
        self.assertEqual(set(server["tools"]), set(server["enabled_tools"]))
        self.assertTrue(
            all(
                value == {"approval_mode": "approve"}
                for value in server["tools"].values()
            )
        )
        self.assertEqual(
            server["env_http_headers"]["Authorization"],
            "DEERFLOW_BUG_EXAMPLE_WIKI_AUTHORIZATION",
        )
        self.assertNotIn(wiki.authorization, repr(wiki))
        self.assertNotIn(wiki.authorization, json.dumps(config))

    def test_missing_connection_disables_optional_wiki(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(investigation, "runtime_home", return_value=Path(folder)),
        ):
            self.assertIsNone(investigation.load_example_wiki_mcp())

    def test_connection_loader_rejects_bad_header_without_echoing_it(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(investigation, "runtime_home", return_value=Path(folder)),
        ):
            config = {
                "examplewiki": {
                    "type": "http",
                    "url": "https://wiki.example.invalid/mcp",
                    "headers": {"Authorization": "Bearer fictional\nfixture"},
                }
            }
            (Path(folder) / "examplewiki-mcp.json").write_text(json.dumps(config))
            with self.assertRaises(RuntimeError) as raised:
                investigation.load_example_wiki_mcp()
            self.assertNotIn("fictional", str(raised.exception))

    def test_wiki_report_cannot_exclude_then_assign_embedded_responsibility(self):
        fourth = "主要涉及端：客户端。\n待确认输入：无\n本地代码修改：修正示例消费者。"
        valid = report("嵌入式判定：已排除。", fourth)
        self.assertIsNone(
            investigation._report_format_error(valid, embedded_wiki_used=True)
        )
        contradictory = valid.replace(
            "待确认输入：无", "待确认输入：嵌入式：导出记录。"
        )
        self.assertEqual(
            investigation._report_format_error(contradictory, embedded_wiki_used=True),
            "fourth_section_conflicts_with_embedded_exclusion",
        )
        missing = report("责任待定。", fourth)
        self.assertEqual(
            investigation._report_format_error(missing, embedded_wiki_used=True),
            "second_section_missing_embedded_decision",
        )

    def test_streaming_logs_preserve_long_utf8_lines_and_unterminated_tail(self):
        lines = ["fictional " + "示例记录" * 20, "", "tail without newline"]
        raw = "\n".join(lines).encode()
        with patch.object(logs, "SEGMENT_BYTES", 7):
            segments = list(logs.iter_log_segments(io.BytesIO(raw)))
        self.assertEqual(sum(size for _, _, _, size in segments), len(raw))
        self.assertGreater(sum(line == 1 for line, _, _, _ in segments), 1)
        for number, expected in enumerate(lines, 1):
            parts = [(part, text) for line, part, text, _ in segments if line == number]
            self.assertEqual([part for part, _ in parts], list(range(len(parts))))
            self.assertEqual("".join(text for _, text in parts), expected)

    def test_index_search_returns_whole_lines_across_segments_once(self):
        lines = ["prefix event_name=RefreshFinished suffix", "second record", "tail"]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "logs.sqlite"
            writer = logs.LogIndexWriter(path, redact=lambda text: text)
            with patch.object(logs, "SEGMENT_BYTES", 24):
                for line, part, text, _ in logs.iter_log_segments(
                    io.BytesIO("\n".join(lines).encode())
                ):
                    writer.add("synthetic.log.1", line, part, text)
            writer.finish()
            expected = {"source": "synthetic.log.1", "line": 1, "raw": lines[0]}
            self.assertEqual(logs.read_record(path, "synthetic.log.1", 1), expected)
            self.assertEqual(
                list(logs.search_records(path, "event-name = refreshfinished")),
                [expected],
            )
            self.assertEqual(
                list(logs.search_records(path, r"prefix.*suffix$", regex=True)),
                [expected],
            )
            self.assertEqual(
                [
                    record["raw"]
                    for record in logs.read_range(path, "synthetic.log.1", 1, 3)
                ],
                lines,
            )
            self.assertEqual(next(logs.list_sources(path))["last_line"], 3)

    def test_paged_search_is_bounded_and_original_records_remain_complete(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "logs.sqlite"
            writer = logs.LogIndexWriter(path, redact=lambda text: text)
            long_record = "matching event " + "x" * (logs.SEARCH_PREVIEW_CHARS + 100)
            for number, raw in enumerate(
                (long_record, "matching second", "matching third"), 1
            ):
                writer.add("synthetic.log", number, 0, raw)
            writer.add("other.log", 1, 0, "matching elsewhere")
            writer.finish()
            meta, first = logs.search_page(
                path, "matching", source="synthetic.log", limit=2
            )
            self.assertEqual(
                (meta["total"], meta["returned"], meta["next_offset"]), (3, 2, 2)
            )
            self.assertEqual([item["line"] for item in first], [1, 2])
            self.assertTrue(first[0]["truncated"])
            self.assertEqual(len(first[0]["preview"]), logs.SEARCH_PREVIEW_CHARS)
            self.assertEqual(
                logs.read_record(path, first[0]["source"], first[0]["line"])["raw"],
                long_record,
            )
            meta, second = logs.search_page(
                path, "matching", source="synthetic.log", offset=2, limit=2
            )
            self.assertEqual([item["line"] for item in second], [3])
            self.assertIsNone(meta["next_offset"])
            _, narrowed = logs.search_page(
                path, "matching", source="synthetic.log", first=2, last=2
            )
            self.assertEqual([item["line"] for item in narrowed], [2])
            for kwargs in (
                {"offset": -1},
                {"limit": 0},
                {"limit": logs.MAX_SEARCH_LIMIT + 1},
            ):
                with self.assertRaises(ValueError):
                    logs.search_page(path, "matching", **kwargs)
            self.assertFalse(hasattr(logs, "discover_symbols"))

    def test_missing_attachment_evidence_still_creates_a_valid_empty_index(self):
        for evidence in (None, {}):
            with (
                self.subTest(evidence=evidence),
                tempfile.TemporaryDirectory() as folder,
            ):
                root = Path(folder)
                path = root / "logs.sqlite"
                result = preparation.collect_log_material(
                    root, evidence, index_path=path
                )
                self.assertEqual(result["status"], "no_material")
                self.assertEqual(result["index_status"], "ready")
                self.assertEqual(result["indexed_parts"], 0)
                self.assertEqual(list(logs.list_sources(path)), [])
                meta, records = logs.search_page(path, "fictional")
                self.assertEqual(meta["total"], 0)
                self.assertIsNone(meta["next_offset"])
                self.assertEqual(records, [])

    def test_history_caps_at_eight_and_keeps_dates_outside_truncated_patches(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, view = root / "source", root / "view"
            for directory in (source, view):
                directory.mkdir()
                (directory / "example.py").write_text("example = True\n")
            (source / ".git").mkdir()
            date = "2026-01-01T12:00:00+00:00"
            calls = []

            def fake_git(repository, *args):
                calls.append(args)
                output = (
                    "".join(
                        f"{number:040x}\x1f{date}\x1fSynthetic fix {number}\x1e"
                        for number in range(1, 9)
                    )
                    if args[0] == "log"
                    else "long fictional patch text"
                )
                return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

            output = io.StringIO()
            with (
                patch.object(history, "__file__", str(view / "history.py")),
                patch.object(history, "_run_git", side_effect=fake_git),
                patch.object(history, "MAX_PATCH_CHARS", 4),
                patch.object(history, "MAX_TOTAL_PATCH_CHARS", 32),
                redirect_stdout(output),
            ):
                self.assertEqual(history._query(str(source), "example.py", 100), 0)
            self.assertIn("--max-count=8", calls[0])
            result, metadata = output.getvalue().splitlines()
            payload = json.loads(result.removeprefix(history.RESULT_PREFIX))
            dates = json.loads(metadata.removeprefix(history.METADATA_PREFIX))
            self.assertEqual(len(dates), 8)
            self.assertTrue(all(item["patch_truncated"] for item in payload["commits"]))
            self.assertTrue(all(item["committed_at"] == date for item in dates))
            self.assertEqual(dates[-1]["subject"], "Synthetic fix 8")

    def test_index_redaction_discard_and_missing_parts_fail_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "logs.sqlite"
            writer = logs.LogIndexWriter(
                path,
                redact=lambda text: text.replace(
                    "fictional-private-value", "[REDACTED]"
                ),
            )
            writer.add("synthetic.log", 1, 0, "value=fictional-private-value")
            writer.add("synthetic.log", 2, 0, "discarded marker")
            writer.add("synthetic.log", 2, 1, " continued")
            writer.add("synthetic.log", 3, 1, "missing initial segment")
            self.assertEqual(writer.discard_line("synthetic.log", 2), 2)
            writer.finish()
            self.assertEqual(
                logs.read_record(path, "synthetic.log", 1)["raw"], "value=[REDACTED]"
            )
            self.assertEqual(
                list(logs.search_records(path, "fictional-private-value")), []
            )
            self.assertEqual(list(logs.search_records(path, "discarded")), [])
            self.assertIsNone(logs.read_record(path, "synthetic.log", 3))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                logs.LogIndexWriter(path, redact=lambda text: text)


if __name__ == "__main__":
    unittest.main()
