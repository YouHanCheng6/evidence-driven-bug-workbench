"""Synthetic local Git contracts; transport, config and real workspaces are excluded.

These checks never fetch, pull or push a remote. Local Git commands run only in
disposable repositories; relay transport and host checkout discovery are mocked.
"""

import importlib.util
import subprocess
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


def no_runtime():
    raise AssertionError("No live configuration is permitted in public tests.")


config_stub = types.ModuleType("deerflow.config")
config_stub.get_app_config = no_runtime
with patch.dict(
    sys.modules,
    {"deerflow": types.ModuleType("deerflow"), "deerflow.config": config_stub},
):
    rollback = load_module(
        "public_delivery_rollback", "backend/app/gateway/bug_rollback.py"
    )
    with patch.dict(sys.modules, {"app.gateway.bug_rollback": rollback}):
        delivery = load_module(
            "public_delivery", "backend/app/gateway/bug_git_delivery.py"
        )


def example_config(
    enabled=True, remote_url="https://relay.example.invalid/project.git"
):
    relay = types.SimpleNamespace(
        enabled=enabled,
        remote_url=remote_url,
        remote_name="example",
        branch_prefix="bug",
    )
    return types.SimpleNamespace(bug_analysis=types.SimpleNamespace(git_relay=relay))


class DeliveryBoundaryContracts(unittest.TestCase):
    def test_disabled_delivery_blocks_every_entry_before_process_or_workspace_access(
        self,
    ):
        entries = (
            lambda: delivery.publish_bug_repair({}),
            lambda: delivery.fetch_bug_repair_for_test({}),
            lambda: delivery.sync_bug_relay({"status": "published"}, restore=False),
            lambda: delivery.sync_bug_relay({"status": "withdrawn"}, restore=True),
            lambda: delivery.deliver_bug_repair(
                {"git_delivery": {"status": "submitted"}}
            ),
            lambda: delivery.deliver_bug_repair(
                {"git_delivery": {"status": "push_failed", "commit": "fictional"}}
            ),
        )
        for config in (example_config(enabled=False), example_config(remote_url="")):
            for entry in entries:
                with (
                    self.subTest(
                        enabled=config.bug_analysis.git_relay.enabled,
                        entry=entries.index(entry),
                    ),
                    patch.object(delivery, "get_app_config", return_value=config),
                    patch.object(delivery, "_run") as run,
                    patch.object(delivery, "_repair_context") as context,
                    patch.object(delivery, "_personal_repository") as personal,
                    patch.object(delivery, "_retry_push") as retry,
                ):
                    with self.assertRaises(delivery.BugGitDeliveryError):
                        entry()
                    for call in (run, context, personal, retry):
                        call.assert_not_called()

    def test_submit_requires_published_relay_and_applied_local_handoff(self):
        with (
            patch.object(delivery, "get_app_config", return_value=example_config()),
            patch.object(delivery, "_run") as run,
            patch.object(delivery, "_commit_current_target_files") as commit,
        ):
            for workflow in ({}, {"relay_delivery": {"status": "published"}}):
                with self.assertRaises(delivery.BugGitDeliveryError):
                    delivery.deliver_bug_repair(workflow)
            run.assert_not_called()
            commit.assert_not_called()

    def test_submit_identity_is_an_example_not_inherited_from_host(self):
        original = {
            "GIT_AUTHOR_NAME": "fictional host",
            "GIT_AUTHOR_EMAIL": "host@example.invalid",
            "KEEP": "yes",
        }
        result = delivery._formal_submit_env(original)
        self.assertEqual(result["GIT_AUTHOR_EMAIL"], "example@example.invalid")
        self.assertEqual(result["GIT_COMMITTER_EMAIL"], "example@example.invalid")
        self.assertEqual(result["GIT_AUTHOR_NAME"], result["GIT_COMMITTER_NAME"])
        self.assertEqual(result["KEEP"], "yes")
        self.assertEqual(original["GIT_AUTHOR_NAME"], "fictional host")


class LocalGitDeliveryContracts(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.repo = Path(folder.name).resolve()
        (self.repo / "src").mkdir()
        self.target = "src/page.py"
        (self.repo / self.target).write_text("label = 'before'\n")
        (self.repo / "unrelated.txt").write_text("unrelated baseline\n")
        self.git("init", "-q", "--initial-branch=main")
        self.git("config", "core.hooksPath", "/dev/null")
        self.git("config", "user.name", "Example")
        self.git("config", "user.email", "example@example.invalid")
        self.git("add", "--", self.target, "unrelated.txt")
        self.git("commit", "-qm", "Synthetic baseline")
        self.base = self.git("rev-parse", "HEAD").decode().strip()
        self.current = "label = 'after'\nmanual_followup = True\n"
        (self.repo / self.target).write_text(self.current)
        (self.repo / "unrelated.txt").write_text("unrelated local edit\n")
        self.git("add", "--", "unrelated.txt")

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.repo), *args], check=True, capture_output=True
        ).stdout

    def test_relay_uses_temporary_index_without_moving_head_or_real_index(self):
        context = delivery.BugRepairGitContext(
            bug_id=101,
            workflow_id="synthetic-run",
            title="Synthetic repair",
            repo=self.repo,
            paths=(self.target,),
            branch="main",
            official_remote="origin",
            official_remote_url="https://upstream.example.invalid/project.git",
            official_branch="main",
            base_commit=self.base,
            subject="fix(ZENTAO):101 Synthetic repair",
        )
        original_index = (self.repo / ".git/index").read_bytes()
        real_run = delivery._run
        pushed = []

        def local_only(repo, *args, **kwargs):
            if args[0] == "push":
                pushed.append(args)
                return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")
            self.assertIn(
                args[0], {"read-tree", "add", "diff", "write-tree", "commit-tree"}
            )
            return real_run(repo, *args, **kwargs)

        with (
            patch.object(delivery, "get_app_config", return_value=example_config()),
            patch.object(delivery, "_repair_context", return_value=context),
            patch.object(delivery, "_remote_ref", return_value=None),
            patch.object(delivery, "_run", side_effect=local_only),
        ):
            result = delivery.publish_bug_repair({})
        self.assertEqual(result["status"], "published")
        self.assertEqual(self.git("rev-parse", "HEAD").decode().strip(), self.base)
        self.assertEqual((self.repo / ".git/index").read_bytes(), original_index)
        self.assertEqual(
            self.git("show", result["test_commit"] + ":" + self.target).decode(),
            self.current,
        )
        self.assertEqual(
            self.git("show", result["test_commit"] + ":unrelated.txt"),
            b"unrelated baseline\n",
        )
        self.assertEqual(
            pushed[0][1], example_config().bug_analysis.git_relay.remote_url
        )
        self.assertEqual(len(pushed), 1)

    def test_submit_commit_contains_current_target_file_and_preserves_other_staged_edits(
        self,
    ):
        old_head, commit = delivery._commit_current_target_files(
            self.repo,
            branch="main",
            paths=[self.target],
            subject="fix(ZENTAO):101 Synthetic repair",
        )
        self.assertEqual(old_head, self.base)
        self.assertEqual(self.git("symbolic-ref", "--short", "HEAD").strip(), b"main")
        self.assertEqual(
            self.git("show", commit + ":" + self.target).decode(), self.current
        )
        self.assertEqual(
            self.git("show", commit + ":unrelated.txt"), b"unrelated baseline\n"
        )
        self.assertEqual(
            self.git("diff", "--cached", "--name-only").strip(), b"unrelated.txt"
        )
        self.assertEqual(
            (self.repo / "unrelated.txt").read_text(), "unrelated local edit\n"
        )
        self.assertEqual(
            self.git("show", "-s", "--format=%ae|%ce", commit).strip(),
            b"example@example.invalid|example@example.invalid",
        )


if __name__ == "__main__":
    unittest.main()
