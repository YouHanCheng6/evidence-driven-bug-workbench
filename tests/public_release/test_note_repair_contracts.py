"""Offline repair contracts using a disposable Git repo and invented source.

The production workspace, rollback helpers and result renderer are executed.
Only optional model/config imports and Pydantic's data-container protocol are
shimmed, so these tests do not validate Pydantic schemas or model integration.
"""

import copy
import importlib.util
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
REPOSITORY = "sample_mobile_repo"


def load_module(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def module_stub(name, **attributes):
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    return module


def no_runtime(*args, **kwargs):
    raise AssertionError(
        "Offline repair contracts must not call a model or live configuration."
    )


class DataOnlyModel:
    """Well-typed test inputs only; deliberately not a schema validator."""

    def __init__(self, **values):
        self.instruction = values["instruction"]
        self.files = values["files"]
        self.outcome = values.get("outcome", "pending")
        self.reason = values.get("reason", "")

    def model_copy(self, deep=False):
        return copy.deepcopy(self) if deep else copy.copy(self)

    def model_dump(self):
        return copy.deepcopy(vars(self))


with patch.dict(
    sys.modules,
    {
        "deerflow": module_stub("deerflow"),
        "deerflow.config": module_stub("deerflow.config", get_app_config=no_runtime),
        "deerflow.config.app_config": module_stub(
            "deerflow.config.app_config", get_app_config=no_runtime
        ),
        "deerflow.models": module_stub("deerflow.models", create_chat_model=no_runtime),
        "langchain": module_stub("langchain"),
        "langchain.agents": module_stub("langchain.agents", create_agent=no_runtime),
        "langchain_core": module_stub("langchain_core"),
        "langchain_core.messages": module_stub(
            "langchain_core.messages",
            AIMessage=type("AIMessage", (), {}),
            HumanMessage=type("HumanMessage", (), {}),
        ),
        "langchain_core.tools": module_stub("langchain_core.tools", tool=no_runtime),
        "pydantic": module_stub(
            "pydantic", BaseModel=DataOnlyModel, Field=lambda **kwargs: None
        ),
    },
):
    rollback = load_module(
        "public_note_rollback", "backend/app/gateway/bug_rollback.py"
    )
    with patch.dict(sys.modules, {"app.gateway.bug_rollback": rollback}):
        repair = load_module(
            "public_note_repair", "backend/app/gateway/bug_note_repair.py"
        )


class NoteRepairContracts(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = (Path(self.folder.name) / "repository").resolve()
        self.view = (Path(self.folder.name) / "source-view").resolve()
        self.path = "src/page.py"
        self.original = "label = 'before'\n"
        for directory in (self.root, self.view):
            (directory / "src").mkdir(parents=True)
            (directory / self.path).write_text(self.original)
        self.git("init", "-q")
        self.git("add", "--", self.path)
        self.git(
            "-c",
            "user.name=Example",
            "-c",
            "user.email=example@example.invalid",
            "commit",
            "-qm",
            "Synthetic baseline",
        )
        mount = types.SimpleNamespace(
            host_path=str(self.root),
            container_path=f"/mnt/repos/{REPOSITORY}",
            read_only=False,
        )
        config = types.SimpleNamespace(sandbox=types.SimpleNamespace(mounts=[mount]))
        self.config_patch = patch.object(
            rollback, "get_app_config", return_value=config
        )
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.workspace = repair.NoteRepairWorkspace(
            repository=REPOSITORY,
            root=self.root,
            source_view=self.view,
            readable_paths=frozenset({self.path}),
            editable_paths=frozenset({self.path}),
        )
        self.instruction = "Change the example label."
        self.note = "待确认输入：无\n本地代码修改：" + self.instruction

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.root), *args], check=True, capture_output=True
        ).stdout

    def item(self, **overrides):
        return repair.NoteRepairItem(
            **{"instruction": self.instruction, "files": [self.path], **overrides}
        )

    def stage(self):
        self.workspace.track([self.item()], self.note)
        self.workspace.read(self.path)
        self.workspace.replace_item(1, self.path, "'before'", "'after'")

    def test_paths_require_exact_scope_source_view_and_no_escape(self):
        outside = Path(self.folder.name) / "outside.py"
        outside.write_text("fictional outside source\n")
        (self.root / "src/link.py").symlink_to(outside)
        (self.view / "src/link.py").write_text("placeholder\n")
        for path in (
            "../outside.py",
            str(outside),
            "src/./page.py",
            "src/link.py",
            ".env",
            "docs/notes.md",
        ):
            with self.subTest(path=path):
                self.workspace.readable_paths = frozenset({path})
                self.workspace.editable_paths = frozenset({path})
                with self.assertRaises(ValueError):
                    self.workspace.read(path)
        self.workspace.readable_paths = frozenset({self.path})
        self.workspace.editable_paths = frozenset({self.path})
        (self.view / self.path).unlink()
        with self.assertRaises(ValueError):
            self.workspace.read(self.path)

    def test_requirement_list_must_cover_scope_and_cannot_drop_requirements(self):
        self.workspace.editable_paths = frozenset({self.path, "src/check.py"})
        with self.assertRaises(ValueError):
            self.workspace.track([self.item()], self.note)
        self.workspace.editable_paths = frozenset({self.path})
        second = "Keep the example callback."
        note = self.note + "\n" + second
        self.workspace.track([self.item(), self.item(instruction=second)], note)
        with self.assertRaises(ValueError):
            self.workspace.track([self.item()], note)
        with self.assertRaises(ValueError):
            self.workspace.track([self.item(), self.item()], note)
        self.assertEqual(len(self.workspace.items), 2)

    def test_replacement_requires_read_unique_text_and_clean_working_file(self):
        self.workspace.track([self.item()], self.note)
        with self.assertRaises(ValueError):
            self.workspace.replace_item(1, self.path, "'before'", "'after'")
        self.workspace.read(self.path)
        with self.assertRaises(ValueError):
            self.workspace.replace_item(1, self.path, "not present", "replacement")
        (self.root / self.path).write_text("label = 'user edit'\n")
        with self.assertRaises(ValueError):
            self.workspace.replace_item(1, self.path, "'before'", "'after'")
        self.assertEqual(self.workspace.results()[0]["outcome"], "blocked")
        self.assertEqual((self.root / self.path).read_text(), "label = 'user edit'\n")

    def test_result_tracking_requires_actual_staged_changes_and_reasons(self):
        self.workspace.track([self.item()], self.note)
        self.workspace.read(self.path)
        with self.assertRaises(ValueError):
            self.workspace.track(
                [self.item(outcome="applied", reason="Claimed complete.")], self.note
            )
        self.workspace.replace_item(1, self.path, "'before'", "'after'")
        with self.assertRaises(ValueError):
            self.workspace.track(
                [self.item(outcome="already_satisfied", reason="Claimed unchanged.")],
                self.note,
            )
        with self.assertRaises(ValueError):
            self.workspace.track([self.item(outcome="applied")], self.note)
        self.workspace.track(
            [
                self.item(
                    outcome="applied", reason="Staged the requested example label."
                )
            ],
            self.note,
        )
        self.assertEqual(self.workspace.results()[0]["outcome"], "applied")
        self.assertEqual(
            (self.root / self.path).read_text(),
            self.original,
            "Staging must not write live source.",
        )

    def test_snapshot_apply_records_only_actual_repository_qualified_changes(self):
        self.stage()
        snapshot = self.workspace.snapshot()
        self.assertTrue(snapshot["available"])
        self.assertEqual(self.workspace.apply(snapshot), [f"{REPOSITORY}/{self.path}"])
        self.assertEqual((self.root / self.path).read_text(), "label = 'after'\n")
        self.assertEqual(
            self.workspace.applied_snapshot(snapshot)["files"], snapshot["files"]
        )

    def test_apply_refuses_later_edits_without_overwriting_them(self):
        self.stage()
        snapshot = self.workspace.snapshot()
        later = "label = 'later user edit'\n"
        (self.root / self.path).write_text(later)
        with self.assertRaises(RuntimeError):
            self.workspace.apply(snapshot)
        self.assertEqual((self.root / self.path).read_text(), later)
        self.assertFalse(self.workspace.applied_snapshot(snapshot)["available"])

    def test_pending_items_are_not_reported_as_completed(self):
        self.workspace.track([self.item()], self.note)
        result = self.workspace.results("Synthetic executor interruption.")
        self.assertEqual(result[0]["outcome"], "blocked")
        self.assertEqual(result[0]["reason"], "Synthetic executor interruption.")

    def test_result_renderer_uses_item_outcomes_and_actual_written_files(self):
        applied = self.item(outcome="applied", reason="Example change.").model_dump()
        blocked = self.item(
            instruction="Other example requirement.",
            outcome="blocked",
            reason="Input missing.",
        ).model_dump()
        satisfied = self.item(
            outcome="already_satisfied", reason="Confirmed existing behavior."
        ).model_dump()
        not_required = self.item(
            outcome="not_required", reason="Conditional advice only."
        ).model_dump()
        changed = [f"{REPOSITORY}/{self.path}"]
        for items, files, expected in (
            ([applied], changed, "completed"),
            ([applied, blocked], changed, "partial"),
            ([blocked], [], "incomplete"),
            ([not_required], [], "not_required"),
            ([satisfied], [], "completed"),
            ([applied], [], "incomplete"),
        ):
            with self.subTest(expected=expected, written=len(files)):
                result = repair.note_repair_result(REPOSITORY, items, files)
                self.assertEqual(result["repair_outcome"], expected)
                self.assertIn("未运行测试", result["completion_report"])
                self.assertIn(f"{REPOSITORY}/{self.path}", result["repair_report"])
        self.assertEqual(
            applied["outcome"],
            "applied",
            "Rendering must not mutate input result dictionaries.",
        )


if __name__ == "__main__":
    unittest.main()
