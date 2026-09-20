"""Deterministic source relationships and exact-anchor tracing for UI analysis."""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass

_IDENTIFIER = r"[A-Za-z_$][A-Za-z0-9_$]*"
_RELATIVE_PATH_PATTERN = re.compile(r"['\"](?P<path>\.\.?/[^'\"\r\n]+)['\"]")
_DECLARATION_PATTERNS = (
    re.compile(rf"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(?P<name>{_IDENTIFIER})\s*\("),
    re.compile(rf"^\s*(?:async\s+)?(?P<name>{_IDENTIFIER})\s*\([^;]*\)\s*\{{"),
    re.compile(rf"^\s*(?:const|let|var)\s+(?P<name>{_IDENTIFIER})\s*=.*=>"),
    re.compile(rf"^\s*(?:def|func|fun)\s+(?P<name>{_IDENTIFIER})\s*\("),
    re.compile(rf"^\s*[-+]\s*\([^)]*\)\s*(?P<name>{_IDENTIFIER})"),
)
_NON_DECLARATION_NAMES = frozenset({"if", "for", "while", "switch", "catch", "return"})
_SEARCH_NOISE_IDENTIFIERS = frozenset({"false", "null", "this", "true", "undefined"})
_STYLE_PATTERN = re.compile(rf"\bstyles\.(?P<name>{_IDENTIFIER})\b")
_RESOURCE_IDENTIFIER_PATTERN = re.compile(rf"\b(?:resourceStrings|CameraCfg|ICONS)\.(?P<name>{_IDENTIFIER})\b")
_CALL_IDENTIFIER_PATTERN = re.compile(rf"(?<![A-Za-z0-9_$])(?P<name>{_IDENTIFIER})\s*\(")
_SOURCE_SUFFIXES = (".js", ".jsx", ".ts", ".tsx", ".java", ".kt", ".m", ".mm", ".swift", ".xml", ".json")


@dataclass(frozen=True)
class SourceScope:
    start_line: int
    end_line: int
    label: str


@dataclass(frozen=True)
class SourceAnchorTrace:
    identifier: str
    reference_lines: tuple[int, ...]
    scopes: tuple[SourceScope, ...]


def explicit_relative_source_paths(source_path: str, content: str) -> tuple[str, ...]:
    """Resolve only literal relative paths that are visibly present in source."""
    base_dir = posixpath.dirname(source_path.replace("\\", "/"))
    paths: list[str] = []
    for match in _RELATIVE_PATH_PATTERN.finditer(content):
        resolved = posixpath.normpath(posixpath.join(base_dir, match.group("path")))
        variants = [resolved]
        if not posixpath.splitext(resolved)[1]:
            variants.extend(f"{resolved}{suffix}" for suffix in _SOURCE_SUFFIXES)
            variants.extend(posixpath.join(resolved, f"index{suffix}") for suffix in _SOURCE_SUFFIXES[:4])
        for path in variants:
            if path not in paths:
                paths.append(path)
    return tuple(paths)


def source_anchor_candidates(content: str) -> tuple[str, ...]:
    """Return structural source anchors from one bounded source read."""
    declarations: list[str] = []
    for line in content.splitlines():
        for pattern in _DECLARATION_PATTERNS:
            match = pattern.search(line)
            if match:
                name = match.group("name")
                if name not in _NON_DECLARATION_NAMES and name not in declarations:
                    declarations.append(name)
                break
    if declarations:
        return tuple(declarations)

    fallback: list[str] = []
    for pattern in (_STYLE_PATTERN, _RESOURCE_IDENTIFIER_PATTERN):
        for match in pattern.finditer(content):
            name = match.group("name")
            if name not in fallback:
                fallback.append(name)
    for path in _RELATIVE_PATH_PATTERN.finditer(content):
        basename = posixpath.basename(path.group("path"))
        if basename and basename not in fallback:
            fallback.append(basename)
    return tuple(fallback)


def source_anchor_candidates_for_search(content: str, pattern: str, matched_lines: tuple[str, ...]) -> tuple[str, ...]:
    """Find exact source anchors related to one successful entry-scoped search."""
    terms = tuple(dict.fromkeys(term.lower() for term in re.findall(_IDENTIFIER, pattern) if len(term) >= 3))
    if not terms:
        return ()

    def related(identifier: str) -> bool:
        lowered = identifier.lower()
        return identifier not in _SEARCH_NOISE_IDENTIFIERS and any(term in lowered or lowered in term for term in terms)

    candidates = [identifier for identifier in source_anchor_candidates(content) if related(identifier)]
    for line in matched_lines:
        for identifier in re.findall(_IDENTIFIER, line):
            if related(identifier) and identifier not in candidates:
                candidates.append(identifier)
    return tuple(candidates)


def source_anchor_followup_candidates(content: str, current_identifier: str) -> tuple[str, ...]:
    """Return exact call identifiers visible inside the just-traced bounded scopes."""
    declarations = set(source_anchor_candidates(content))
    excluded = (
        declarations
        | _NON_DECLARATION_NAMES
        | _SEARCH_NOISE_IDENTIFIERS
        | {
            current_identifier,
            "require",
            "render",
        }
    )
    candidates: list[str] = []
    for match in _CALL_IDENTIFIER_PATTERN.finditer(content):
        identifier = match.group("name")
        if identifier not in excluded and identifier not in candidates:
            candidates.append(identifier)
    for pattern in (_STYLE_PATTERN, _RESOURCE_IDENTIFIER_PATTERN):
        for match in pattern.finditer(content):
            identifier = match.group("name")
            if identifier not in excluded and identifier not in candidates:
                candidates.append(identifier)
    return tuple(candidates)


def _declaration_name(line: str) -> str | None:
    for pattern in _DECLARATION_PATTERNS:
        match = pattern.search(line)
        if match:
            name = match.group("name")
            return None if name in _NON_DECLARATION_NAMES else name
    return None


def _brace_scope(lines: list[str], start_index: int) -> SourceScope:
    label = _declaration_name(lines[start_index]) or f"line_{start_index + 1}"
    balance = 0
    opened = False
    for index in range(start_index, len(lines)):
        balance += lines[index].count("{")
        if lines[index].count("{"):
            opened = True
        balance -= lines[index].count("}")
        if opened and balance <= 0:
            return SourceScope(start_index + 1, index + 1, label)
    return SourceScope(start_index + 1, start_index + 1, label)


def trace_source_anchor(content: str, identifier: str, *, max_references: int = 8) -> SourceAnchorTrace:
    """Trace one exact identifier to references and their containing declarations."""
    if not re.fullmatch(_IDENTIFIER, identifier):
        raise ValueError("anchor must be one exact source identifier")
    token = re.compile(rf"(?<![A-Za-z0-9_$]){re.escape(identifier)}(?![A-Za-z0-9_$])")
    lines = content.splitlines()
    references = tuple(index + 1 for index, line in enumerate(lines) if token.search(line))[:max_references]
    if not references:
        raise ValueError("anchor does not exist in the current source")

    declarations: list[SourceScope] = []
    for index, line in enumerate(lines):
        if _declaration_name(line) is not None:
            declarations.append(_brace_scope(lines, index))
    scopes: list[SourceScope] = []
    for line_number in references:
        containing = [scope for scope in declarations if scope.start_line <= line_number <= scope.end_line]
        scope = max(containing, key=lambda item: item.start_line) if containing else SourceScope(line_number, line_number, f"line_{line_number}")
        if scope not in scopes:
            scopes.append(scope)
    return SourceAnchorTrace(identifier=identifier, reference_lines=references, scopes=tuple(scopes))


def format_source_anchor_trace(source_path: str, content: str, trace: SourceAnchorTrace) -> str:
    """Render exact references and complete containing scopes with line anchors."""
    lines = content.splitlines()
    output = ["SOURCE_ANCHOR_TRACE=exact-definition-reference-scopes-v1", f"SOURCE={source_path}", f"ANCHOR={trace.identifier}"]
    for line_number in trace.reference_lines:
        output.append(f"REFERENCE={source_path}:{line_number}:{lines[line_number - 1].strip()}")
    for scope in trace.scopes:
        output.append(f"SCOPE={source_path}:{scope.start_line}-{scope.end_line}:{scope.label}")
        for line_number in range(scope.start_line, scope.end_line + 1):
            output.append(f"{source_path}:{line_number}: {lines[line_number - 1]}")
    scope_content = "\n".join(line for scope in trace.scopes for line in lines[scope.start_line - 1 : scope.end_line])
    for related in explicit_relative_source_paths(source_path, scope_content):
        output.append(f"RELATED_SOURCE={related}")
    return "\n".join(output)
