import fnmatch
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

IGNORE_PATTERNS = [
    ".git",
    ".svn",
    ".hg",
    ".bzr",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".env",
    "env",
    ".tox",
    ".nox",
    ".eggs",
    "*.egg-info",
    "site-packages",
    "dist",
    "build",
    ".next",
    ".nuxt",
    ".output",
    ".turbo",
    "target",
    "out",
    ".idea",
    ".vscode",
    "*.swp",
    "*.swo",
    "*~",
    ".project",
    ".classpath",
    ".settings",
    ".DS_Store",
    "Thumbs.db",
    "desktop.ini",
    "*.lnk",
    "*.log",
    "*.tmp",
    "*.temp",
    ".upload-*.part",
    "*.bak",
    "*.cache",
    ".cache",
    "logs",
    ".coverage",
    "coverage",
    ".nyc_output",
    "htmlcov",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
]

DEFAULT_MAX_FILE_SIZE_BYTES = 1_000_000
DEFAULT_LINE_SUMMARY_LENGTH = 200


@dataclass(frozen=True)
class GrepMatch:
    path: str
    line_number: int
    line: str


# ``should_ignore_name`` runs once per directory entry during glob/grep tree
# walks, so we avoid ~50 ``fnmatch`` calls per name. Most ignore patterns are
# literal names (O(1) set lookup after normcase); the few glob patterns are
# pre-translated into a single combined regex. ``os.path.normcase`` keeps the
# same case behavior ``fnmatch`` applies (case-sensitive on POSIX, folded on
# Windows).
_EXACT_IGNORE_NAMES = frozenset(os.path.normcase(p) for p in IGNORE_PATTERNS if not any(c in p for c in "*?["))
_GLOB_IGNORE_PATTERNS = [p for p in IGNORE_PATTERNS if any(c in p for c in "*?[")]
_GLOB_IGNORE_RE = re.compile("|".join(fnmatch.translate(os.path.normcase(p)) for p in _GLOB_IGNORE_PATTERNS)) if _GLOB_IGNORE_PATTERNS else None


def should_ignore_name(name: str) -> bool:
    normalized = os.path.normcase(name)
    if normalized in _EXACT_IGNORE_NAMES:
        return True
    return _GLOB_IGNORE_RE is not None and _GLOB_IGNORE_RE.match(normalized) is not None


def should_ignore_path(path: str) -> bool:
    return any(should_ignore_name(segment) for segment in path.replace("\\", "/").split("/") if segment)


def path_matches(pattern: str, rel_path: str) -> bool:
    """Match a portable glob, including a deliberately bounded brace set.

    ``PurePosixPath.match`` does not understand the common ``*.{py,pyi}``
    spelling.  Returning no matches for it is a misleading false negative, so
    expand only simple comma-separated alternatives before delegating to the
    standard matcher.  Nested/range braces are intentionally unsupported.
    """
    alternatives = _expand_brace_glob(pattern)
    path = PurePosixPath(rel_path)
    for alternative in alternatives:
        if path.match(alternative) or (alternative.startswith("**/") and path.match(alternative[3:])):
            return True
    return False


_MAX_BRACE_GLOB_ALTERNATIVES = 16


def _expand_brace_glob(pattern: str) -> tuple[str, ...]:
    """Expand simple brace alternatives or raise a clear parameter error."""
    if "{" not in pattern and "}" not in pattern:
        return (pattern,)
    if pattern.count("{") != 1 or pattern.count("}") != 1:
        raise ValueError("invalid brace glob: only one non-nested brace group is supported")
    start = pattern.index("{")
    end = pattern.index("}")
    if end < start or "{" in pattern[start + 1 : end] or "}" in pattern[:start] or "{" in pattern[end + 1 :]:
        raise ValueError("invalid brace glob")
    values = pattern[start + 1 : end].split(",")
    if not values or any(not value or any(char in value for char in "{}") for value in values):
        raise ValueError("invalid brace glob")
    if len(values) > _MAX_BRACE_GLOB_ALTERNATIVES:
        raise ValueError(f"brace glob exceeds {_MAX_BRACE_GLOB_ALTERNATIVES} alternatives")
    return tuple(f"{pattern[:start]}{value}{pattern[end + 1 :]}" for value in values)


def truncate_line(line: str, max_chars: int = DEFAULT_LINE_SUMMARY_LENGTH) -> str:
    line = line.rstrip("\n\r")
    if len(line) <= max_chars:
        return line
    return line[: max_chars - 3] + "..."


def is_binary_file(path: Path, sample_size: int = 8192) -> bool:
    try:
        with path.open("rb") as handle:
            return b"\0" in handle.read(sample_size)
    except OSError:
        return True


def find_glob_matches(root: Path, pattern: str, *, include_dirs: bool = False, max_results: int = 200) -> tuple[list[str], bool]:
    matches: list[str] = []
    truncated = False
    root = root.resolve()

    if not root.exists():
        raise FileNotFoundError(root)
    if not root.is_dir():
        raise NotADirectoryError(root)

    for current_root, dirs, files in os.walk(root):
        dirs[:] = [name for name in dirs if not should_ignore_name(name)]
        # root is already resolved; os.walk builds current_root by joining under root,
        # so relative_to() works without an extra stat()/resolve() per directory.
        rel_dir = Path(current_root).relative_to(root)

        if include_dirs:
            for name in dirs:
                rel_path = (rel_dir / name).as_posix()
                if path_matches(pattern, rel_path):
                    matches.append(str(Path(current_root) / name))
                    if len(matches) >= max_results:
                        truncated = True
                        return matches, truncated

        for name in files:
            if should_ignore_name(name):
                continue
            rel_path = (rel_dir / name).as_posix()
            if path_matches(pattern, rel_path):
                matches.append(str(Path(current_root) / name))
                if len(matches) >= max_results:
                    truncated = True
                    return matches, truncated

    return matches, truncated


def find_grep_matches(
    root: Path,
    pattern: str,
    *,
    glob_pattern: str | None = None,
    literal: bool = False,
    case_sensitive: bool = False,
    max_results: int = 100,
    max_file_size: int = DEFAULT_MAX_FILE_SIZE_BYTES,
    line_summary_length: int = DEFAULT_LINE_SUMMARY_LENGTH,
) -> tuple[list[GrepMatch], bool]:
    matches: list[GrepMatch] = []
    truncated = False
    root = root.resolve()

    if not root.exists():
        raise FileNotFoundError(root)
    root_is_file = root.is_file()
    if not root_is_file and not root.is_dir():
        raise NotADirectoryError(root)

    regex_source = re.escape(pattern) if literal else pattern
    flags = 0 if case_sensitive else re.IGNORECASE
    regex = re.compile(regex_source, flags)

    # Skip lines longer than this to prevent ReDoS on minified / no-newline files.
    _max_line_chars = line_summary_length * 10

    def candidate_files():
        if root_is_file:
            yield root, root.name
            return

        for current_root, dirs, files in os.walk(root):
            dirs[:] = [name for name in dirs if not should_ignore_name(name)]
            rel_dir = Path(current_root).relative_to(root)
            for name in files:
                if should_ignore_name(name):
                    continue
                yield Path(current_root) / name, (rel_dir / name).as_posix()

    for candidate_path, rel_path in candidate_files():
        if glob_pattern is not None and not path_matches(glob_pattern, rel_path):
            continue

        try:
            if not root_is_file and candidate_path.is_symlink():
                continue
            file_path = candidate_path.resolve()
            if not root_is_file and not file_path.is_relative_to(root):
                continue
            if file_path.stat().st_size > max_file_size or is_binary_file(file_path):
                continue
            with file_path.open(encoding="utf-8", errors="replace") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if len(line) > _max_line_chars:
                        continue
                    if regex.search(line):
                        matches.append(
                            GrepMatch(
                                path=str(file_path),
                                line_number=line_number,
                                line=truncate_line(line, line_summary_length),
                            )
                        )
                        if len(matches) >= max_results:
                            truncated = True
                            return matches, truncated
        except OSError:
            continue

    return matches, truncated
