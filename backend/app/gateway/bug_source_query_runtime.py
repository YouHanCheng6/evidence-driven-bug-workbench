"""Stateless bounded source navigation copied into Codex source views.

The runtime is intentionally read-only: it never writes candidate files or
indexes. It tries Tree-sitter, then exact text and emits one bounded JSON result
that the caller can audit and use for a narrow source read.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

RUNTIME_NAME = ".deerflow-source-query.py"
RESULT_PREFIX = "CODE_INTELLIGENCE_RESULT="
ALLOWED_PURPOSES = frozenset({"definition", "references", "type", "consumer", "resource", "existing-implementation"})
SEMANTIC_PURPOSES = frozenset({"definition", "references", "type"})
MAX_FALLBACK_SCOPES = 2
MAX_SCOPE_SOURCE_FILES = 5_000
MAX_EXACT_HIT_VIEW_LINES = 120
GENERIC_ANCHORS = frozenset({"class", "component", "config", "data", "error", "event", "function", "handler", "import", "page", "resource", "resourcestrings", "return", "string", "text", "view"})
# The materialized view admits only manifest-proven, Git-tracked runtime
# ``dist`` trees, so the in-view query must not hide those files again.
EXCLUDED_DIRECTORIES = frozenset({".git", ".deerflow-runtime-evidence", ".gradle", ".next", ".nuxt", ".output", ".turbo", ".cxx", "build", "deriveddata", "intermediates", "node_modules", "out", "target"})
EXCLUDED_SUFFIXES = (".7z", ".aab", ".apk", ".bundle", ".bz2", ".gz", ".ipa", ".jar", ".jsbundle", ".map", ".min.js", ".rar", ".tar", ".tgz", ".war", ".xcarchive", ".xz", ".zip")

_TREE_SITTER_LANGUAGES = {
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".swift": "swift",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".h": "c",
    ".hpp": "cpp",
    ".m": "objc",
    ".mm": "objc",
    ".py": "python",
}
_DEFINITION_ANCESTORS = frozenset(
    {
        "class_declaration",
        "class_definition",
        "function_declaration",
        "function_definition",
        "generator_function_declaration",
        "interface_declaration",
        "lexical_declaration",
        "method_definition",
        "property_declaration",
        "type_alias_declaration",
        "variable_declaration",
    }
)


def _is_generated_path(path: Path) -> bool:
    return any(part.lower() in EXCLUDED_DIRECTORIES for part in path.parts) or path.name.lower().endswith(EXCLUDED_SUFFIXES)


def _is_within(path: Path, roots: Sequence[Path]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _source_files(scope: Path):
    if scope.is_file():
        yield scope
        return
    for current_root, directory_names, file_names in os.walk(scope, followlinks=False):
        current = Path(current_root)
        directory_names[:] = sorted(name for name in directory_names if name.lower() not in EXCLUDED_DIRECTORIES and not (current / name).is_symlink())
        for name in sorted(file_names):
            path = current / name
            if path.is_symlink() or path.name == RUNTIME_NAME or _is_generated_path(path):
                continue
            try:
                if path.stat().st_size > 2_000_000:
                    continue
            except OSError:
                continue
            yield path


def _scope_exceeds_source_file_limit(scope: Path) -> bool:
    return any(index > MAX_SCOPE_SOURCE_FILES for index, _path in enumerate(_source_files(scope), start=1))


def _safe_scope(root: Path, value: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve(strict=False)
    if not _is_within(candidate, (root,)) or not candidate.exists() or candidate.is_symlink() or _is_generated_path(candidate):
        raise ValueError("scope 必须是当前源码视图中的可读文件或目录")
    if candidate.is_dir() and _scope_exceeds_source_file_limit(candidate):
        raise ValueError(f"scope 源码文件超过 {MAX_SCOPE_SOURCE_FILES} 个，必须缩小到当前模块")
    return candidate


def _parsed_origin(root: Path, value: str) -> tuple[Path, int]:
    path_value, separator, line_value = value.rpartition(":")
    if not separator or not line_value.isdigit() or int(line_value) < 1:
        raise ValueError("origin 必须是已读源码的绝对路径:行号")
    path = Path(path_value).resolve(strict=False)
    if not _is_within(path, (root,)) or not path.is_file() or path.is_symlink() or _is_generated_path(path):
        raise ValueError("origin 必须位于当前源码视图")
    return path, int(line_value)


def _tree_sitter_syntax_matches(scope: Path, anchor: str, purpose: str, max_results: int) -> tuple[list[tuple[str, int, str, tuple[int, int]]], int, bool]:
    if purpose not in SEMANTIC_PURPOSES:
        return [], 0, False
    try:
        from tree_sitter_language_pack import get_parser
    except ImportError:
        return [], 0, False
    matches: list[tuple[str, int, str, tuple[int, int]]] = []
    total = 0
    parser_available = False
    anchor_bytes = anchor.encode()
    for path in _source_files(scope):
        language = _TREE_SITTER_LANGUAGES.get(path.suffix.lower())
        if language is None:
            continue
        try:
            parser = get_parser(language)
            content = path.read_bytes()
            lines = content.decode("utf-8", errors="ignore").splitlines()
        except (LookupError, OSError, RuntimeError, ValueError):
            continue
        parser_available = True
        stack = [parser.parse(content).root_node]
        while stack:
            node = stack.pop()
            stack.extend(reversed(node.children))
            if not (node.type == "identifier" or node.type.endswith("_identifier")) or content[node.start_byte : node.end_byte] != anchor_bytes:
                continue
            if purpose in {"definition", "type"}:
                ancestor = node.parent
                is_definition = False
                for _depth in range(4):
                    if ancestor is None:
                        break
                    if ancestor.type in _DEFINITION_ANCESTORS:
                        is_definition = True
                        break
                    if ancestor.type in {"call_expression", "member_expression", "navigation_expression"}:
                        break
                    ancestor = ancestor.parent
                if not is_definition:
                    continue
            line_number = node.start_point[0] + 1
            total += 1
            if len(matches) >= max_results or line_number > len(lines):
                continue
            start = max(1, line_number - 40)
            end = min(len(lines), start + MAX_EXACT_HIT_VIEW_LINES - 1)
            matches.append((path.as_posix(), line_number, lines[line_number - 1].strip()[:300], (start, end)))
    return matches, total, parser_available


def _scope_matches(scope: Path, anchor: str, max_results: int) -> tuple[list[tuple[str, int, str, tuple[int, int]]], int]:
    matches: list[tuple[str, int, str, tuple[int, int]]] = []
    total = 0
    for path in _source_files(scope):
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for line_number, line in enumerate(lines, start=1):
            if anchor not in line:
                continue
            total += 1
            if len(matches) < max_results:
                start = max(1, line_number - 40)
                end = min(len(lines), start + MAX_EXACT_HIT_VIEW_LINES - 1)
                matches.append((path.as_posix(), line_number, line.strip()[:300], (start, end)))
    return matches, total


def _candidate_payload(root: Path, match: tuple[str, int, str, tuple[int, int]]) -> dict[str, Any]:
    path, line, source, view_range = match
    return {"path": Path(path).resolve(strict=False).relative_to(root).as_posix(), "line": line, "view_start": view_range[0], "view_end": view_range[1], "source": source}


def _emit(payload: Mapping[str, Any]) -> None:
    print(RESULT_PREFIX + json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")))


def _query(anchor: str, scope_value: str, purpose: str, max_results: int, *, fallback_scope_values: tuple[str, ...] = (), origin_value: str = "") -> int:
    root = Path(__file__).resolve().parent
    anchor = anchor.strip()
    if purpose not in ALLOWED_PURPOSES or len(anchor) < 2 or anchor.lower() in GENERIC_ANCHORS or "\n" in anchor:
        _emit({"engine": "none", "status": "invalid_query", "relation": purpose, "anchor": anchor, "diagnostics": ["exact_anchor_and_supported_relation_required"], "candidates": []})
        return 2
    try:
        scopes = [_safe_scope(root, scope_value)]
        for value in fallback_scope_values[:MAX_FALLBACK_SCOPES]:
            candidate = _safe_scope(root, value)
            if candidate not in scopes:
                scopes.append(candidate)
        origin = _parsed_origin(root, origin_value) if origin_value else None
    except ValueError as exc:
        _emit({"engine": "none", "status": "invalid_query", "relation": purpose, "anchor": anchor, "diagnostics": [str(exc)], "candidates": []})
        return 2
    if purpose in SEMANTIC_PURPOSES and origin is None:
        _emit({"engine": "none", "status": "invalid_query", "relation": purpose, "anchor": anchor, "diagnostics": ["origin_required_for_semantic_relation"], "candidates": []})
        return 2
    diagnostics: list[str] = []
    for scope in scopes:
        syntax_matches, syntax_total, syntax_available = _tree_sitter_syntax_matches(scope, anchor, purpose, max_results)
        diagnostics.append("tree_sitter:matched" if syntax_matches else "tree_sitter:no_matching_node" if syntax_available else "tree_sitter:unavailable")
        if syntax_total > max_results:
            _emit(
                {
                    "engine": "tree_sitter",
                    "status": "too_broad",
                    "semantic": False,
                    "relation": purpose,
                    "anchor": anchor,
                    "scope": scope.relative_to(root).as_posix(),
                    "diagnostics": diagnostics,
                    "candidate_count": syntax_total,
                    "candidates": [],
                }
            )
            return 0
        if syntax_matches:
            _emit(
                {
                    "engine": "tree_sitter",
                    "status": "matched",
                    "semantic": False,
                    "relation": purpose,
                    "anchor": anchor,
                    "scope": scope.relative_to(root).as_posix(),
                    "diagnostics": diagnostics,
                    "candidates": [_candidate_payload(root, item) for item in syntax_matches],
                }
            )
            return 0
        text_matches, text_total = _scope_matches(scope, anchor, max_results)
        diagnostics.append("text:matched" if text_matches else "text:no_match")
        if text_total > max_results:
            _emit({"engine": "text", "status": "too_broad", "semantic": False, "relation": purpose, "anchor": anchor, "scope": scope.relative_to(root).as_posix(), "diagnostics": diagnostics, "candidate_count": text_total, "candidates": []})
            return 0
        if text_matches:
            _emit(
                {
                    "engine": "text",
                    "status": "matched",
                    "semantic": False,
                    "relation": purpose,
                    "anchor": anchor,
                    "scope": scope.relative_to(root).as_posix(),
                    "diagnostics": diagnostics,
                    "candidates": [_candidate_payload(root, item) for item in text_matches],
                }
            )
            return 0
    _emit({"engine": "text", "status": "no_match", "semantic": False, "relation": purpose, "anchor": anchor, "diagnostics": diagnostics, "candidates": []})
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="DeerFlow stateless bounded source-relation query")
    parser.add_argument("--anchor")
    parser.add_argument("--scope", default=".")
    parser.add_argument("--fallback-scope", action="append", default=[])
    parser.add_argument("--purpose", choices=sorted(ALLOWED_PURPOSES))
    parser.add_argument("--origin", default="")
    parser.add_argument("--max-results", type=int, default=12)
    args = parser.parse_args()
    if not isinstance(args.anchor, str) or not isinstance(args.purpose, str):
        parser.error("--anchor and --purpose are required")
    return _query(args.anchor, args.scope, args.purpose, max(1, min(args.max_results, 12)), fallback_scope_values=tuple(args.fallback_scope), origin_value=args.origin)


if __name__ == "__main__":
    raise SystemExit(main())
