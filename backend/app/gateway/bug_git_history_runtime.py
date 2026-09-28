"""Bounded, read-only Git history query for a verified current source file.

The Bug Workbench source view intentionally omits ``.git``.  This helper is
copied into that view and invoked through a generated launcher that supplies
the matching real repository root.  It accepts only one file that exists in
both trees and returns a small number of file-local diffs; it is not a general
Git shell.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

RUNTIME_NAME = ".deerflow-git-history.py"
RESULT_PREFIX = "GIT_HISTORY_RESULT="
MAX_COMMITS = 4
MAX_PATCH_CHARS = 2_400
MAX_TOTAL_PATCH_CHARS = 6_000


def _emit(payload: dict[str, object]) -> None:
    print(RESULT_PREFIX + json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def _run_git(repository_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    command = ["git", "-C", str(repository_root), *args]
    try:
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(command, 127, stdout="", stderr=f"{type(exc).__name__}: {exc}")


def _relative_view_path(raw_path: str, view_root: Path) -> Path | None:
    candidate = Path(raw_path).expanduser()
    if candidate.is_absolute():
        try:
            candidate = candidate.resolve(strict=True).relative_to(view_root)
        except (OSError, ValueError):
            return None
    if not candidate.parts or candidate.is_absolute() or ".." in candidate.parts:
        return None
    view_file = (view_root / candidate).resolve(strict=False)
    try:
        view_file.relative_to(view_root)
    except ValueError:
        return None
    return candidate


def _nearest_git_root(source_file: Path, source_root: Path) -> Path | None:
    current = source_file.parent
    while current == source_root or source_root in current.parents:
        if (current / ".git").exists():
            return current
        if current == source_root:
            break
        current = current.parent
    return None


def _query(source_root_value: str, path_value: str, max_commits_value: int) -> int:
    view_root = Path(__file__).resolve().parent
    try:
        source_root = Path(source_root_value).expanduser().resolve(strict=True)
    except OSError:
        _emit({"engine": "git_history", "status": "unavailable", "diagnostics": ["source_root_unavailable"], "commits": []})
        return 0

    relative = _relative_view_path(path_value, view_root)
    if relative is None:
        _emit({"engine": "git_history", "status": "invalid_query", "diagnostics": ["path_outside_source_view"], "commits": []})
        return 2
    view_file = view_root / relative
    source_file = source_root / relative
    if not view_file.is_file() or not source_file.is_file():
        _emit(
            {
                "engine": "git_history",
                "status": "invalid_query",
                "path": relative.as_posix(),
                "diagnostics": ["current_file_required"],
                "commits": [],
            }
        )
        return 2
    repository_root = _nearest_git_root(source_file, source_root)
    if repository_root is None:
        _emit(
            {
                "engine": "git_history",
                "status": "unavailable",
                "path": relative.as_posix(),
                "diagnostics": ["git_repository_not_found"],
                "commits": [],
            }
        )
        return 0

    repository_path = source_file.relative_to(repository_root).as_posix()
    max_commits = max(1, min(int(max_commits_value), MAX_COMMITS))
    log = _run_git(
        repository_root,
        "log",
        "--follow",
        f"--max-count={max_commits}",
        "--format=%H%x1f%aI%x1f%s%x1e",
        "--",
        repository_path,
    )
    if log.returncode != 0:
        _emit(
            {
                "engine": "git_history",
                "status": "unavailable",
                "path": relative.as_posix(),
                "diagnostics": ["git_log_failed"],
                "commits": [],
            }
        )
        return 0

    remaining_patch_chars = MAX_TOTAL_PATCH_CHARS
    commits: list[dict[str, object]] = []
    for record in log.stdout.split("\x1e"):
        fields = record.strip().split("\x1f", 2)
        if len(fields) != 3:
            continue
        commit_sha, committed_at, subject = fields
        patch = _run_git(
            repository_root,
            "show",
            "--no-color",
            "--format=",
            "--unified=3",
            commit_sha,
            "--",
            repository_path,
        )
        patch_text = patch.stdout if patch.returncode == 0 else ""
        patch_limit = min(MAX_PATCH_CHARS, remaining_patch_chars)
        patch_text = patch_text[:patch_limit]
        remaining_patch_chars -= len(patch_text)
        commits.append(
            {
                "sha": commit_sha,
                "committed_at": committed_at,
                "subject": subject[:300],
                "patch": patch_text,
                "patch_truncated": patch.returncode == 0 and len(patch.stdout) > len(patch_text),
            }
        )
        if remaining_patch_chars <= 0:
            break

    _emit(
        {
            "engine": "git_history",
            "status": "matched" if commits else "no_match",
            "path": relative.as_posix(),
            "diagnostics": [],
            "commits": commits,
        }
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="DeerFlow bounded file-local Git history query")
    parser.add_argument("--source-root", required=True, help=argparse.SUPPRESS)
    parser.add_argument("--path", required=True)
    parser.add_argument("--max-commits", type=int, default=MAX_COMMITS)
    args = parser.parse_args()
    return _query(args.source_root, args.path, args.max_commits)


if __name__ == "__main__":
    raise SystemExit(main())
