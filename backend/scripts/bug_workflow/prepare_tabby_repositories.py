#!/usr/bin/env python3
"""Build private composite Git repositories for Tabby source retrieval.

The sample_mobile_repo checkout contains ignored Android and iOS Git repositories.
Tabby's repository APIs read a commit tree, so a sparse parent clone cannot
expose those native sources. This script creates a private synthetic commit
from tracked source files in all three checkouts while preserving their paths.
Developer checkouts are never modified.
"""

from __future__ import annotations

import argparse
import json
import runpy
import shutil
import subprocess
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_RUNTIME_SOURCE_POLICY = runpy.run_path(str(_BACKEND_ROOT / "app/gateway/bug_runtime_source_policy.py"))
discover_tracked_runtime_dist_roots = _RUNTIME_SOURCE_POLICY["discover_tracked_runtime_dist_roots"]
path_is_in_runtime_dist = _RUNTIME_SOURCE_POLICY["path_is_in_runtime_dist"]

_SOURCE_DIRS = frozenset({"modules", "projects", "resources", "ydsdk", "rnlibs", "chargerPlugin", "moduleControl"})
_EXCLUDED_DIRS = frozenset(
    {
        ".git", ".gradle", ".idea", ".kotlin", ".run", ".vscode", "DerivedData", "Pods",
        "build", "dist", "docs", "jniLibs", "maven-repo", "memory-bank", "node_modules",
        "openspec", "specs", "vosk-model",
    }
)
_EXCLUDED_SUFFIXES = frozenset(
    {
        ".a", ".aar", ".apk", ".class", ".docx", ".dylib", ".gif", ".ipa", ".jar",
        ".jpeg", ".jpg", ".jks", ".keystore", ".log", ".md", ".mp3", ".mp4", ".png",
        ".snap", ".so", ".tgz", ".wav", ".webp", ".zip",
    }
)
_EXCLUDED_NAMES = frozenset({"api-secrets.properties", "local.properties", "package-lock.json", "proguardMapping.txt"})


def _run(*args: str, cwd: Path | None = None, timeout: int = 300) -> str:
    completed = subprocess.run(
        list(args), cwd=cwd, check=True, capture_output=True, text=True, timeout=timeout
    )
    return completed.stdout.strip()


def _tracked_files(root: Path) -> list[Path]:
    completed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True, timeout=300
    )
    return [
        Path(value.decode("utf-8", errors="surrogateescape"))
        for value in completed.stdout.split(b"\0")
        if value
    ]


def _allowed(path: Path, *, parent: bool, runtime_dist_roots: frozenset[Path]) -> bool:
    parts = path.parts
    if not parts or (parent and len(parts) > 1 and parts[0] not in _SOURCE_DIRS):
        return False
    if any(part.startswith(".") or part.endswith(".framework") for part in parts):
        return False
    excluded = {part for part in parts if part in _EXCLUDED_DIRS}
    if excluded and not (excluded == {"dist"} and path_is_in_runtime_dist(path, runtime_dist_roots)):
        return False
    name = path.name
    lowered = name.casefold()
    if name in _EXCLUDED_NAMES or ("secret" in lowered and path.suffix == ".properties"):
        return False
    return path.suffix.casefold() not in _EXCLUDED_SUFFIXES


def _reset_generated_tree(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for child in destination.iterdir():
        if child.name == ".git":
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    if not (destination / ".git").exists():
        _run("git", "init", "--quiet", str(destination))


def _copy_tracked(source: Path, destination: Path, *, prefix: Path | None, parent: bool) -> int:
    copied = 0
    tracked_files = _tracked_files(source)
    runtime_dist_roots = discover_tracked_runtime_dist_roots(source, tracked_files=tracked_files)
    for relative in tracked_files:
        if not _allowed(relative, parent=parent, runtime_dist_roots=runtime_dist_roots):
            continue
        source_file = source / relative
        if not source_file.is_file() or source_file.is_symlink():
            continue
        target = destination / (prefix / relative if prefix else relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, target)
        copied += 1
    return copied


def _prepare(source: Path, destination: Path, *, include_native: bool) -> tuple[int, dict[str, str]]:
    _reset_generated_tree(destination)
    revisions = {"main": _run("git", "rev-parse", "HEAD", cwd=source)}
    count = _copy_tracked(source, destination, prefix=None, parent=True)
    if include_native:
        for name in ("android", "ios"):
            nested = source / name
            if not (nested / ".git").exists():
                continue
            revisions[name] = _run("git", "rev-parse", "HEAD", cwd=nested)
            count += _copy_tracked(nested, destination, prefix=Path(name), parent=False)
    (destination / ".source-revisions.json").write_text(
        json.dumps(revisions, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    _run("git", "add", "-A", cwd=destination)
    _run(
        "git", "-c", "user.name=DeerFlow Runtime", "-c", "user.email=runtime@localhost",
        "commit", "--quiet", "--allow-empty", "-m", "Refresh private Tabby source corpus", cwd=destination,
    )
    return count, revisions


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", type=Path, default=Path(".deer-flow/tabby/composite-repositories"))
    parser.add_argument("--sample_mobile_repo", type=Path, default=Path("/home/example_user/projects/sample_mobile_repo"))
    parser.add_argument("--harmony-rn", type=Path, default=Path("/home/example_user/sample_platform_repo"))
    args = parser.parse_args()
    output_root = args.output_root.expanduser().resolve()
    for name, source, include_native in (
        ("sample_mobile_repo", args.sample_mobile_repo, True), ("sample_platform_repo", args.sample_platform_repo, False)
    ):
        source = source.expanduser().resolve()
        if not (source / ".git").exists():
            raise SystemExit(f"repository checkout missing: {source}")
        count, revisions = _prepare(source, output_root / name, include_native=include_native)
        print(f"prepared {name}: files={count} revisions={len(revisions)} path={output_root / name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
