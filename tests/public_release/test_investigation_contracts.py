"""Offline contracts using invented data; no SDK, model, or enterprise services."""

import ast
import importlib.util
import io
import json
import sys
import tempfile
import types
import unittest
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

    def test_symbol_discovery_only_summarizes_actual_matching_names(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "logs.sqlite"
            writer = logs.LogIndexWriter(path, redact=lambda text: text)
            writer.add(
                "synthetic.log",
                1,
                0,
                '{"eventName":"RefreshFinished","otherField":"UnrelatedValue"}',
            )
            writer.add("synthetic.log", 2, 0, "eventName=RefreshFinished")
            writer.add("synthetic.log", 3, 0, "a prose-only marker with no fields")
            writer.finish()
            symbols = list(logs.discover_symbols(path, "refresh"))
            self.assertEqual(
                {(item["kind"], item["name"]) for item in symbols},
                {("field", "eventName"), ("value", "RefreshFinished")},
            )
            self.assertTrue(all(item["record_count"] == 2 for item in symbols))
            self.assertEqual(list(logs.discover_symbols(path, "prose-only")), [])
            self.assertEqual(
                len(list(logs.search_records(path, "prose-only"))),
                1,
                "Empty symbol discovery does not establish absent records.",
            )

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
