"""Safe, per-workflow rollback snapshots for automated Bug repairs."""

from __future__ import annotations

import base64
import hashlib
import os
import stat
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from deerflow.config import get_app_config

_MAX_ROLLBACK_FILE_BYTES = 4 * 1024 * 1024
_MAX_ROLLBACK_TOTAL_BYTES = 16 * 1024 * 1024
_MAX_REPAIR_SNAPSHOT_FILE_BYTES = 4 * 1024 * 1024
_MAX_REPAIR_SNAPSHOT_TOTAL_BYTES = 256 * 1024 * 1024
_MAX_REPAIR_SNAPSHOT_FILES = 4_000
_MAX_REPAIR_CHANGED_FILES = 96
_MAX_REPAIR_IMPORT_TOTAL_BYTES = 32 * 1024 * 1024
_REPAIR_EXCLUDED_DIRECTORIES = frozenset(
    {
        ".agents",
        ".claude",
        ".codex",
        ".cursor",
        ".git",
        ".gradle",
        ".idea",
        ".mcp",
        ".next",
        ".openhands",
        ".trae",
        ".turbo",
        ".vscode",
        "build",
        "deriveddata",
        "dist",
        "docs",
        "documentation",
        "intermediates",
        "man",
        "memory-bank",
        "node_modules",
        "out",
        "target",
        "third_party",
        "thirdparty",
        "vendor",
    }
)
_REPAIR_EXCLUDED_FILENAMES = frozenset(
    {
        ".cursorrules",
        ".feishu-webhook",
        ".npmrc",
        ".pypirc",
        ".yarnrc.yml",
        "credentials",
        "credentials.json",
        "id_rsa",
        "id_ed25519",
        "agents.md",
        "claude.md",
    }
)
_REPAIR_EXCLUDED_SUFFIXES = frozenset(
    {
        ".7z",
        ".aab",
        ".apk",
        ".cer",
        ".crt",
        ".der",
        ".dylib",
        ".framework",
        ".gif",
        ".gz",
        ".ipa",
        ".jar",
        ".jks",
        ".jpeg",
        ".jpg",
        ".keystore",
        ".key",
        ".mobileprovision",
        ".mp3",
        ".mp4",
        ".p12",
        ".pem",
        ".png",
        ".rar",
        ".so",
        ".tar",
        ".tgz",
        ".ttf",
        ".war",
        ".webp",
        ".woff",
        ".woff2",
        ".xcarchive",
        ".xz",
        ".zip",
        ".bak",
        ".log",
        ".orig",
        ".rej",
    }
)


def _selected_mount_root(repository: str) -> Path:
    """Resolve one configured writable repository-family mount."""
    if not repository or PurePosixPath(repository).name != repository:
        raise RuntimeError(f"Invalid repair repository: {repository!r}")
    selected_container_root = f"/mnt/repos/{repository}"
    matches = {
        Path(mount.host_path).expanduser().resolve()
        for mount in get_app_config().sandbox.mounts
        if not mount.read_only
        and str(mount.container_path).rstrip("/") == selected_container_root
        and Path(mount.host_path).expanduser().resolve().is_dir()
    }
    if len(matches) != 1:
        raise RuntimeError(f"Repair repository {repository} does not resolve uniquely to one writable mount")
    return next(iter(matches))


def _git(repo_root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=True,
        capture_output=True,
    ).stdout


def _git_optional(repo_root: Path, *args: str) -> str | None:
    try:
        value = _git(repo_root, *args).decode("utf-8", "replace").strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return value or None


def _safe_relative_path(value: str) -> Path:
    path = PurePosixPath(value)
    if path.is_absolute() or not value or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"Unsafe repository-relative path: {value!r}")
    return Path(*path.parts)


def _porcelain_paths(repo_root: Path) -> set[str]:
    """Return every currently dirty path, including untracked files and renames."""
    entries = _git(repo_root, "status", "--porcelain=v1", "-z", "--untracked-files=all").split(b"\0")
    paths: set[str] = set()
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        if len(entry) < 4:
            raise ValueError("Unexpected git status output")
        status = entry[:2].decode("ascii", "strict")
        paths.add(entry[3:].decode("utf-8", "surrogateescape"))
        if "R" in status or "C" in status:
            if index >= len(entries) or not entries[index]:
                raise ValueError("Unexpected git rename status output")
            paths.add(entries[index].decode("utf-8", "surrogateescape"))
            index += 1
    return paths


def _sha256(path: Path) -> str | None:
    if not path.is_file() or path.is_symlink():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _repair_workspace_path_allowed(value: str) -> bool:
    """Keep repair workspaces source-focused and free of credentials/control files."""
    try:
        relative = _safe_relative_path(value)
    except ValueError:
        return False
    lowered_parts = tuple(part.lower() for part in relative.parts)
    if any(part in _REPAIR_EXCLUDED_DIRECTORIES for part in lowered_parts):
        return False
    name = relative.name.lower()
    if name in _REPAIR_EXCLUDED_FILENAMES or name == ".env" or name.startswith(".env."):
        return False
    return not any(name.endswith(suffix) for suffix in _REPAIR_EXCLUDED_SUFFIXES)


def _writable_repo_root() -> Path:
    config = get_app_config()
    for mount in config.sandbox.mounts:
        if mount.read_only:
            continue
        candidate = Path(mount.host_path).expanduser().resolve()
        if not candidate.is_dir():
            continue
        try:
            root = Path(_git(candidate, "rev-parse", "--show-toplevel").decode().strip()).resolve()
        except (OSError, subprocess.CalledProcessError):
            continue
        if root == candidate:
            return root
    raise RuntimeError("No writable Git repository is configured as a sandbox mount")


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _nested_repo_for_authorized_files(
    allowed_scopes: tuple[str, ...],
    repository: str | None = None,
) -> tuple[Path, str] | None:
    """Resolve exact authorized files to one shared closest configured Git root."""
    if any(scope.endswith("/") for scope in allowed_scopes):
        return None

    if repository is not None and (not repository or PurePosixPath(repository).name != repository):
        raise RuntimeError(f"Invalid repair repository: {repository!r}")

    config = get_app_config()
    selected_container_root = f"/mnt/repos/{repository}" if repository is not None else None
    if not allowed_scopes:
        if selected_container_root is None:
            return None
        matches: set[tuple[Path, str]] = set()
        for mount in config.sandbox.mounts:
            if mount.read_only or str(mount.container_path).rstrip("/") != selected_container_root:
                continue
            mount_root = Path(mount.host_path).expanduser().resolve()
            root_text = _git_optional(mount_root, "rev-parse", "--show-toplevel")
            if not root_text:
                continue
            repo_root = Path(root_text).resolve()
            if not _path_is_within(repo_root, mount_root):
                continue
            prefix = repo_root.relative_to(mount_root).as_posix()
            matches.add((repo_root, "" if prefix == "." else prefix))
        if len(matches) != 1:
            raise RuntimeError(f"Repair repository {repository} does not resolve uniquely to one writable Git repository")
        return next(iter(matches))

    shared_matches: set[tuple[Path, str]] | None = None
    for scope in dict.fromkeys(allowed_scopes):
        try:
            relative_scope = _safe_relative_path(scope)
        except ValueError:
            return None
        matches: set[tuple[Path, str]] = set()
        for mount in config.sandbox.mounts:
            if mount.read_only:
                continue
            if selected_container_root is not None and str(mount.container_path).rstrip("/") != selected_container_root:
                continue
            mount_root = Path(mount.host_path).expanduser().resolve()
            target = mount_root / relative_scope
            if not target.is_file() or target.is_symlink():
                continue
            root_text = _git_optional(target.parent, "rev-parse", "--show-toplevel")
            if not root_text:
                continue
            repo_root = Path(root_text).resolve()
            if not _path_is_within(repo_root, mount_root):
                continue
            prefix = repo_root.relative_to(mount_root).as_posix()
            matches.add((repo_root, "" if prefix == "." else prefix))
        shared_matches = matches if shared_matches is None else shared_matches & matches
        if not shared_matches:
            if repository is not None:
                raise RuntimeError(f"Authorized files do not resolve uniquely inside repair repository {repository}")
            return None
    if shared_matches is None or len(shared_matches) != 1:
        if repository is not None:
            raise RuntimeError(f"Authorized files do not resolve uniquely inside repair repository {repository}")
        return None
    return next(iter(shared_matches))


def _discover_git_roots(mount_root: Path) -> list[tuple[Path, str]]:
    """Find every real Git worktree inside one configured repository family."""
    roots: set[tuple[Path, str]] = set()
    for current, directories, _filenames in os.walk(mount_root, followlinks=False):
        current_path = Path(current).resolve()
        directories[:] = sorted(
            name
            for name in directories
            if name != ".git"
            and name.lower() not in _REPAIR_EXCLUDED_DIRECTORIES
            and not (current_path / name).is_symlink()
        )
        if not (current_path / ".git").exists():
            continue
        resolved = _git_optional(current_path, "rev-parse", "--show-toplevel")
        if not resolved or Path(resolved).resolve() != current_path:
            continue
        prefix = current_path.relative_to(mount_root).as_posix()
        roots.add((current_path, "" if prefix == "." else prefix))
    if not roots:
        raise RuntimeError("Selected repair repository contains no Git worktree")
    return sorted(roots, key=lambda item: (len(PurePosixPath(item[1]).parts), item[1]))


def _select_navigation_git_roots(
    discovered: list[tuple[Path, str]],
    navigation_paths: tuple[str, ...],
) -> list[tuple[Path, str]]:
    """Select only Git roots reached by verified repair navigation.

    A repository-family mount may contain many independent third-party worktrees.
    Repair needs the deepest worktree owning each verified path, not every nested
    worktree that happens to exist below the mount.  With no usable path, retain
    only the shallow primary worktree rather than expanding speculatively.
    """
    selected: set[tuple[Path, str]] = set()
    for value in navigation_paths:
        try:
            relative = PurePosixPath(_safe_relative_path(value).as_posix())
        except ValueError:
            continue
        matches: list[tuple[int, Path, str]] = []
        for repo_root, prefix in discovered:
            if prefix:
                try:
                    relative.relative_to(PurePosixPath(prefix))
                except ValueError:
                    continue
            matches.append((len(PurePosixPath(prefix).parts) if prefix else 0, repo_root, prefix))
        if matches:
            _depth, repo_root, prefix = max(matches, key=lambda item: item[0])
            selected.add((repo_root, prefix))
    if selected:
        return sorted(selected, key=lambda item: (len(PurePosixPath(item[1]).parts), item[1]))
    return [min(discovered, key=lambda item: (len(PurePosixPath(item[1]).parts), item[1]))]


def _git_root_record(repo_root: Path, path_prefix: str) -> dict[str, Any]:
    branch = _git_optional(repo_root, "symbolic-ref", "--short", "HEAD") or "DETACHED"
    head = _git_optional(repo_root, "rev-parse", "HEAD") or "unknown"
    upstream = _git_optional(repo_root, "rev-parse", "--abbrev-ref", "@{upstream}")
    ahead: int | None = None
    behind: int | None = None
    if upstream:
        counts = _git_optional(repo_root, "rev-list", "--left-right", "--count", f"HEAD...{upstream}")
        if counts:
            left, _, right = counts.partition("\t")
            if left.isdigit() and right.isdigit():
                ahead, behind = int(left), int(right)
    return {
        "repo_root": str(repo_root),
        "path_prefix": path_prefix,
        "branch": branch,
        "head": head,
        "upstream": upstream,
        "ahead": ahead,
        "behind": behind,
        "dirty_before": sorted(_workflow_path(path_prefix, path) for path in _porcelain_paths(repo_root)),
    }


def _baseline_git_roots(baseline: dict[str, Any]) -> list[dict[str, Any]]:
    roots = baseline.get("git_roots")
    if isinstance(roots, list) and roots and all(isinstance(item, dict) for item in roots):
        return [dict(item) for item in roots]
    return [
        {
            "repo_root": str(baseline["repo_root"]),
            "path_prefix": str(baseline.get("path_prefix") or ""),
            "branch": baseline.get("branch"),
            "head": baseline.get("head"),
            "dirty_before": list(baseline.get("dirty_before", [])),
        }
    ]


def _root_for_workflow_path(baseline: dict[str, Any], workflow_path: str) -> tuple[Path, str, dict[str, Any]]:
    relative = PurePosixPath(_safe_relative_path(workflow_path).as_posix())
    matches: list[tuple[int, Path, str, dict[str, Any]]] = []
    for root in _baseline_git_roots(baseline):
        prefix = str(root.get("path_prefix") or "")
        if prefix:
            try:
                relative.relative_to(PurePosixPath(prefix))
            except ValueError:
                continue
        matches.append((len(PurePosixPath(prefix).parts) if prefix else 0, Path(str(root["repo_root"])).resolve(), prefix, root))
    if not matches:
        raise ValueError(f"Workflow path is outside every repair Git root: {workflow_path!r}")
    _depth, repo_root, prefix, root = max(matches, key=lambda item: item[0])
    return repo_root, prefix, root


def _safe_baseline_target(baseline: dict[str, Any], workflow_path: str) -> Path:
    repo_root, path_prefix, _root = _root_for_workflow_path(baseline, workflow_path)
    return _safe_repo_target(repo_root, path_prefix, workflow_path)


def _workflow_path(path_prefix: str, repo_path: str) -> str:
    relative_path = _safe_relative_path(repo_path).as_posix()
    return f"{path_prefix}/{relative_path}" if path_prefix else relative_path


def _repo_path(path_prefix: str, workflow_path: str) -> Path:
    relative_path = PurePosixPath(_safe_relative_path(workflow_path).as_posix())
    if not path_prefix:
        return Path(*relative_path.parts)
    prefix = PurePosixPath(_safe_relative_path(path_prefix).as_posix())
    try:
        repo_relative = relative_path.relative_to(prefix)
    except ValueError as exc:
        raise ValueError(f"Workflow path is outside the repair repository: {workflow_path!r}") from exc
    return Path(*repo_relative.parts)


def _safe_repo_target(repo_root: Path, path_prefix: str, workflow_path: str) -> Path:
    target = repo_root / _repo_path(path_prefix, workflow_path)
    if not _path_is_within(target.resolve(strict=False), repo_root):
        raise ValueError(f"Repair path escapes the selected repository through a symlink: {workflow_path!r}")
    return target


def _nested_repo_is_configured(repo_root: Path) -> bool:
    config = get_app_config()
    for mount in config.sandbox.mounts:
        if mount.read_only:
            continue
        mount_root = Path(mount.host_path).expanduser().resolve()
        if not _path_is_within(repo_root, mount_root):
            continue
        resolved = _git_optional(repo_root, "rev-parse", "--show-toplevel")
        if resolved and Path(resolved).resolve() == repo_root:
            return True
    return False


def rollback_repair(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Restore only the exact clean files captured for this workflow."""
    if not snapshot.get("available") or snapshot.get("completed"):
        raise ValueError("This repair has no pending rollback")
    repository = snapshot.get("repository")
    if repository:
        mount_root = _selected_mount_root(str(repository))
        for root in _baseline_git_roots(snapshot):
            repo_root = Path(str(root["repo_root"])).resolve()
            resolved_root = _git_optional(repo_root, "rev-parse", "--show-toplevel")
            if not _path_is_within(repo_root, mount_root) or not resolved_root or Path(resolved_root).resolve() != repo_root:
                raise ValueError("The configured repair repository has changed")
    elif len(_baseline_git_roots(snapshot)) == 1:
        repo_root = Path(str(snapshot["repo_root"])).resolve()
        path_prefix = str(snapshot.get("path_prefix") or "")
        if (not path_prefix and _writable_repo_root() != repo_root) or (path_prefix and not _nested_repo_is_configured(repo_root)):
            raise ValueError("The configured repair repository has changed")
    files = snapshot.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("Rollback snapshot is invalid")

    conflicts: list[str] = []
    resolved: list[tuple[dict[str, Any], Path]] = []
    for entry in files:
        if not isinstance(entry, dict):
            raise ValueError("Rollback snapshot is invalid")
        path = _safe_baseline_target(snapshot, str(entry.get("path", "")))
        expected_hash = entry.get("after_sha256")
        if _sha256(path) != expected_hash:
            conflicts.append(str(entry.get("path", "")))
        resolved.append((entry, path))
    if conflicts:
        raise RuntimeError(f"文件已在修复后被改动，拒绝覆盖：{', '.join(conflicts)}")

    for entry, path in resolved:
        if entry.get("before_exists"):
            original = base64.b64decode(str(entry.get("before_content", "")), validate=True)
            path.parent.mkdir(parents=True, exist_ok=True)
            file_descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.rollback-", dir=path.parent)
            try:
                with os.fdopen(file_descriptor, "wb") as temporary:
                    temporary.write(original)
                os.replace(temporary_name, path)
                before_mode = entry.get("before_mode")
                if isinstance(before_mode, int):
                    os.chmod(path, before_mode)
            finally:
                Path(temporary_name).unlink(missing_ok=True)
        else:
            path.unlink(missing_ok=True)

    return {**snapshot, "completed": True}
