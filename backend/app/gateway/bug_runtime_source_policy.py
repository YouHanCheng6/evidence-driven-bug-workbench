"""Shared policy for tracked generated directories that are runtime source.

Most ``dist`` directories are disposable build output and stay excluded from Bug
investigations. A vendored package is different when its tracked package manifest
declares a runtime entry inside ``dist``: in that case the generated JavaScript is
the only implementation the application actually loads and must remain visible to
Tabby and Codex.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

_RUNTIME_MANIFEST_FIELDS = ("main", "module", "browser", "react-native", "exports")
_GENERATED_RUNTIME_DIRECTORY = "dist"


def git_tracked_files(root: Path) -> frozenset[Path]:
    """Return repository-relative tracked paths, or an empty set off Git."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=root,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return frozenset()
    if result.returncode != 0:
        return frozenset()
    return frozenset(
        Path(value.decode("utf-8", errors="surrogateescape"))
        for value in result.stdout.split(b"\0")
        if value
    )


def _manifest_entry_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for nested in value.values():
            yield from _manifest_entry_strings(nested)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for nested in value:
            yield from _manifest_entry_strings(nested)


def _runtime_dist_root(package_directory: Path, entry: str) -> Path | None:
    value = entry.strip().replace("\\", "/")
    if not value or value.startswith(("http://", "https://", "node:", "#")):
        return None
    path = PurePosixPath(value.removeprefix("./"))
    if path.is_absolute() or ".." in path.parts:
        return None
    try:
        dist_index = tuple(part.casefold() for part in path.parts).index(_GENERATED_RUNTIME_DIRECTORY)
    except ValueError:
        return None
    return package_directory.joinpath(*path.parts[: dist_index + 1])


def discover_tracked_runtime_dist_roots(
    root: Path,
    *,
    tracked_files: Iterable[Path] | None = None,
) -> frozenset[Path]:
    """Find tracked package ``dist`` roots proven by a tracked runtime manifest."""
    root = root.resolve()
    tracked = frozenset(Path(path) for path in (tracked_files if tracked_files is not None else git_tracked_files(root)))
    if not tracked:
        return frozenset()
    roots: set[Path] = set()
    for manifest_relative in sorted(path for path in tracked if path.name == "package.json"):
        manifest = root / manifest_relative
        if not manifest.is_file() or manifest.is_symlink():
            continue
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, Mapping):
            continue
        for field in _RUNTIME_MANIFEST_FIELDS:
            for entry in _manifest_entry_strings(payload.get(field)):
                candidate = _runtime_dist_root(manifest_relative.parent, entry)
                if candidate is None:
                    continue
                if any(path != candidate and candidate in path.parents for path in tracked):
                    roots.add(candidate)
    return frozenset(roots)


def path_is_in_runtime_dist(path: Path, runtime_dist_roots: Iterable[Path]) -> bool:
    relative = Path(path)
    return any(relative == root or root in relative.parents for root in runtime_dist_roots)


def directory_is_required_for_runtime_dist(directory: Path, runtime_dist_roots: Iterable[Path]) -> bool:
    relative = Path(directory)
    return any(relative == root or relative in root.parents for root in runtime_dist_roots)
