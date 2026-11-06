"""Synthetic, offline contracts for operator-only Feishu Git delivery."""

import ast
import importlib.util
import re
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[2]
MANAGER = ROOT / "backend/app/channels/manager.py"
INTENT = ROOT / "backend/app/channels/feishu_operation_intent.py"


def load_dispatch():
    tree = ast.parse(MANAGER.read_text())
    selected = [
        ast.ImportFrom(
            module="__future__", names=[ast.alias(name="annotations")], level=0
        )
    ]
    names = {"_BUG_CODE_FETCH_PATTERN", "_BUG_CODE_SUBMIT_PATTERN"}
    functions = {
        "_parse_bug_code_fetch",
        "_parse_bug_code_submit",
        "_is_operator_local_git_sender",
    }
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id in names for t in node.targets
        ):
            selected.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in functions:
            selected.append(node)
        elif isinstance(node, ast.ClassDef) and node.name == "ChannelManager":
            selected.extend(
                n
                for n in node.body
                if isinstance(n, ast.AsyncFunctionDef)
                and n.name == "_try_dispatch_feishu_bug_workflow"
            )
    scope = {"re": re, "_strip_leading_feishu_mention": lambda value: value}
    exec(
        compile(
            ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[])),
            str(MANAGER),
            "exec",
        ),
        scope,
    )
    return scope


class FakeResponse:
    is_error = False

    def json(self):
        return {
            "local_test_delivery": {"report": "Synthetic fetch"},
            "git_delivery": {"report": "Synthetic submit"},
        }


class FakeClient:
    def __init__(self, requests):
        self.requests = requests

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return FakeResponse()


class FeishuOperatorContracts(unittest.IsolatedAsyncioTestCase):
    async def dispatch(self, sender, allowed, command):
        scope = load_dispatch()
        requests = []
        scope["httpx"] = types.SimpleNamespace(
            AsyncClient=lambda **kwargs: FakeClient(requests), HTTPError=OSError
        )
        manager = types.SimpleNamespace(
            _gateway_url="https://gateway.example.invalid",
            _publish_bug_workflow_message=AsyncMock(),
            _bug_workflow_headers=lambda msg: {"X-Test-Owner": "synthetic-owner"},
        )
        message = types.SimpleNamespace(
            channel_name="feishu", text=command, user_id=sender, files=[]
        )
        config = types.ModuleType("deerflow.config")
        config.get_app_config = lambda: types.SimpleNamespace(
            channels={"feishu": {"bug_workbench_server_local_open_id": allowed}}
        )
        with patch.dict(
            sys.modules,
            {"deerflow": types.ModuleType("deerflow"), "deerflow.config": config},
        ):
            handled = await scope["_try_dispatch_feishu_bug_workflow"](manager, message)
        return handled, requests, manager._publish_bug_workflow_message

    async def test_unconfigured_or_different_sender_cannot_touch_host_git(self):
        for command in ("拉取 #101", "提交拉取并推送 #101"):
            for sender, allowed in (
                ("", ""),
                ("example-other", ""),
                ("example-other", "example-operator"),
                ("example-other", 123),
            ):
                with self.subTest(command=command, sender=sender, allowed=allowed):
                    handled, requests, notices = await self.dispatch(
                        sender, allowed, command
                    )
                    self.assertTrue(handled)
                    self.assertEqual(requests, [])
                    notices.assert_awaited_once()

    async def test_only_exact_operator_calls_existing_local_endpoints(self):
        for command, suffix in (
            ("拉取 #101", "fetch"),
            ("提交拉取并推送 #101", "submit"),
        ):
            handled, requests, notices = await self.dispatch(
                "example-operator", "example-operator", command
            )
            self.assertTrue(handled)
            self.assertEqual(len(requests), 1)
            self.assertEqual(
                requests[0][0],
                f"https://gateway.example.invalid/api/bug-workflows/by-bug/101/{suffix}",
            )
            self.assertEqual(
                requests[0][1]["headers"]["X-Test-Owner"], "synthetic-owner"
            )
            notices.assert_awaited_once()

    async def test_intent_has_no_pairing_command_and_no_tool_exposure(self):
        config = types.ModuleType("deerflow.config")
        config.get_app_config = lambda: object()
        agents = types.ModuleType("deerflow.config.agents_config")
        agents.load_agent_config = lambda *args, **kwargs: None
        oneshot = types.ModuleType("deerflow.utils.oneshot_llm")
        oneshot.run_oneshot_llm = AsyncMock(return_value="绑定本机")
        spec = importlib.util.spec_from_file_location(
            "public_feishu_intent_contract", INTENT
        )
        module = importlib.util.module_from_spec(spec)
        with patch.dict(
            sys.modules,
            {
                "public_feishu_intent_contract": module,
                "deerflow": types.ModuleType("deerflow"),
                "deerflow.config": config,
                "deerflow.config.agents_config": agents,
                "deerflow.utils.oneshot_llm": oneshot,
            },
        ):
            spec.loader.exec_module(module)
            self.assertFalse(
                module.should_recognize_feishu_operation_intent("绑定本机")
            )
            self.assertTrue(
                module.should_recognize_feishu_operation_intent("请拉取 Bug 101")
            )
            with self.assertRaises(ValueError):
                await module.recognize_feishu_operation_intent(
                    "synthetic input", user_id=None, thread_id=None
                )
            oneshot.run_oneshot_llm.return_value = "拉取 #101"
            result = await module.recognize_feishu_operation_intent(
                "拉取 Bug 101", user_id=None, thread_id=None
            )
            self.assertEqual(result.command, "拉取 #101")
            self.assertNotIn("tools", oneshot.run_oneshot_llm.call_args.kwargs)

    def test_manager_has_no_remote_executor_or_binding_bypass(self):
        source = MANAGER.read_text()
        for removed in (
            "bug-devices",
            "bug_local_executor",
            "remote_delivery",
            "绑定本机",
            "is_sender_local_git_command",
        ):
            self.assertFalse(
                removed in source, f"Excluded entry is still present: {removed}"
            )


if __name__ == "__main__":
    unittest.main()
