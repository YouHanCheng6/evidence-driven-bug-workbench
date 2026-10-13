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


runtime_stub = types.ModuleType('deerflow.config.runtime_paths')
runtime_stub.runtime_home = lambda: Path('/nonexistent-public-test-runtime')
with patch.dict(sys.modules, {
    'deerflow': types.ModuleType('deerflow'),
    'deerflow.config': types.ModuleType('deerflow.config'),
    'deerflow.config.runtime_paths': runtime_stub,
}):
    investigation = load_module('public_investigation', 'backend/app/gateway/bug_codex_summary.py')
logs = load_module('public_logs', 'backend/app/gateway/bug_log_query_runtime.py')


def report(second, fourth):
    return '\n\n'.join((
        '一、分析结论\n虚构案例。',
        '二、责任端与责任层\n' + second,
        '三、根本原因及源码证据\nsample_mobile_repo/example.py:1：虚构引用。',
        '四、修改范围与其他端风险\n' + fourth,
    ))


class InvestigationContracts(unittest.TestCase):
    def test_visual_extraction_has_only_the_codex_entry(self):
        path = ROOT / 'backend/app/gateway/bug_attachment_evidence.py'
        tree = ast.parse(path.read_text())
        functions = {node.name: node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)}
        self.assertNotIn('_legacy_chat_model_visual_analyzer', functions)
        entry = functions['_default_visual_analyzer']
        calls = [node for node in ast.walk(entry) if isinstance(node, ast.Call)]
        self.assertFalse(any(isinstance(call.func, ast.Name) and call.func.id == 'create_chat_model' for call in calls))
        start = next(call for call in calls if isinstance(call.func, ast.Attribute) and call.func.attr == 'thread_start')
        self.assertTrue(next(kw.value.value for kw in start.keywords if kw.arg == 'ephemeral'))
        strings = {node.value for node in ast.walk(entry) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
        self.assertIn('features.shell_tool=false', strings)
        self.assertIn('features.apps=false', strings)
        self.assertIn('web_search="disabled"', strings)

    def test_wiki_allowlist_and_credential_boundary(self):
        wiki = investigation.ExampleWikiMcp('https://wiki.example.invalid/mcp', 'Bearer fictional-fixture')
        config = wiki.thread_config()
        server = config['mcp_servers']['examplewiki']
        self.assertEqual(len(server['enabled_tools']), 8)
        self.assertTrue(all(name.startswith('example_') for name in server['enabled_tools']))
        self.assertEqual(set(server['tools']), set(server['enabled_tools']))
        self.assertTrue(all(value == {'approval_mode': 'approve'} for value in server['tools'].values()))
        self.assertEqual(server['env_http_headers']['Authorization'], 'DEERFLOW_BUG_EXAMPLE_WIKI_AUTHORIZATION')
        self.assertNotIn(wiki.authorization, repr(wiki))
        self.assertNotIn(wiki.authorization, json.dumps(config))

    def test_missing_connection_disables_optional_wiki(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(investigation, 'runtime_home', return_value=Path(folder)):
            self.assertIsNone(investigation.load_example_wiki_mcp())

    def test_connection_loader_rejects_bad_header_without_echoing_it(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(investigation, 'runtime_home', return_value=Path(folder)):
            config = {'examplewiki': {'type': 'http', 'url': 'https://wiki.example.invalid/mcp', 'headers': {'Authorization': 'Bearer fictional\nfixture'}}}
            (Path(folder) / 'examplewiki-mcp.json').write_text(json.dumps(config))
            with self.assertRaises(RuntimeError) as raised:
                investigation.load_example_wiki_mcp()
            self.assertNotIn('fictional', str(raised.exception))

    def test_wiki_report_cannot_exclude_then_assign_embedded_responsibility(self):
        fourth = '主要涉及端：客户端。\n待确认输入：无\n本地代码修改：修正示例消费者。'
        valid = report('嵌入式判定：已排除。', fourth)
        self.assertIsNone(investigation._report_format_error(valid, embedded_wiki_used=True))
        contradictory = valid.replace('待确认输入：无', '待确认输入：嵌入式：导出记录。')
        self.assertEqual(investigation._report_format_error(contradictory, embedded_wiki_used=True), 'fourth_section_conflicts_with_embedded_exclusion')
        missing = report('责任待定。', fourth)
        self.assertEqual(investigation._report_format_error(missing, embedded_wiki_used=True), 'second_section_missing_embedded_decision')

    def test_collection_snapshot_preserves_ids_and_labels_incomplete_sets(self):
        complete = logs.collection_snapshots('Response {"itemList":[{"itemId":11},{"itemId":13}]}')['itemList']
        self.assertEqual(complete['ids'], [11, 13])
        self.assertTrue(complete['complete'])
        incomplete = logs.collection_snapshots('Response {"itemList":[{"itemId":11},{}]}')['itemList']
        self.assertEqual(incomplete['count'], 2)
        self.assertFalse(incomplete['complete'])
        self.assertEqual(incomplete['ids'], [])

    def test_streaming_logs_keep_tail_and_physical_line_numbers(self):
        lines = [f'fictional event {i}' for i in range(40)]
        stream = io.BytesIO(('\n'.join(lines) + '\n').encode())
        with patch.object(logs, 'LOG_SCAN_CHUNK_BYTES', 48):
            chunks = list(logs.iter_log_chunks(stream))
        self.assertGreater(len(chunks), 1)
        self.assertEqual([line for _, part, _, _ in chunks for line in part], lines)
        self.assertEqual(chunks[-1][0] + len(chunks[-1][1]) - 1, 40)


if __name__ == '__main__':
    unittest.main()
