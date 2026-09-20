#!/usr/bin/env python3
"""Offline release guard. Findings contain filenames, never matched credentials."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve()
SKIP_DIRS = {'.git', '.venv', 'node_modules', '__pycache__', '.next', '.pnpm-store'}
PRIVATE_DIRS = {'.deer-flow', '.openhands', '.claude', '.codex', 'source-views'}
RUNTIME_DIRS = {'uploads', 'secrets', 'logs'}
PRIVATE_NAMES = {'.DS_Store', '.jwt_secret', 'config.yaml', 'extensions_config.json', 'mcp_config.json', 'credentials.json', 'auth.json', 'id_rsa', 'id_ed25519'}
PRIVATE_SUFFIXES = {'.db', '.sqlite', '.sqlite3', '.log', '.pem', '.key', '.p12', '.pfx', '.mobileprovision'}
RULES = {
    'private key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'API token': re.compile(r'\b(?:sk|rk|pk)-[A-Za-z0-9_-]{20,}\b'),
    'GitHub token': re.compile(r'\b(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,})\b'),
    'AWS key': re.compile(r'\b(?:AKIA|ASIA)[0-9A-Z]{16}\b'),
    'Slack token': re.compile(r'\bxox[baprs]-[A-Za-z0-9-]{10,}\b'),
    'private network URL': re.compile(r'https?://(?:10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+)'),
}


def scan(root: Path) -> tuple[int, list[str]]:
    root = root.resolve()
    allowlist_path = root / 'scripts/public_fixture_hashes.json'
    allowlist = json.loads(allowlist_path.read_text()) if allowlist_path.is_file() else {}
    findings: list[str] = []
    scanned = 0
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root)
        if SKIP_DIRS.intersection(relative.parts):
            continue
        if path.is_symlink():
            resolved = path.resolve()
            if not resolved.is_relative_to(root) or not resolved.exists():
                findings.append(f'unsafe symlink: {relative}')
            continue
        if not path.is_file():
            continue
        scanned += 1
        runtime_root = (relative.parts[0] in RUNTIME_DIRS or
                        (len(relative.parts) > 1 and relative.parts[0] in {'backend', 'frontend'} and relative.parts[1] in RUNTIME_DIRS))
        if PRIVATE_DIRS.intersection(relative.parts) or runtime_root:
            findings.append(f'private runtime directory: {relative}')
        if (path.name in PRIVATE_NAMES or path.suffix.lower() in PRIVATE_SUFFIXES
                or (path.name.startswith('.env') and path.name != '.env.example')
                or re.search(r'\.db-(?:wal|shm|journal)$', path.name)):
            findings.append(f'private file: {relative}')
        if path.resolve() == SELF:
            continue
        raw = path.read_bytes()
        # Exact-byte public security fixtures only, not blanket test exemptions.
        fixture = allowlist.get(relative.as_posix()) == hashlib.sha256(raw).hexdigest()
        text = raw.decode('utf-8', errors='ignore')
        for label, pattern in RULES.items():
            if pattern.search(text) and not fixture:
                findings.append(f'{label}: {relative}')
    return scanned, sorted(set(findings))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args()
    scanned, findings = scan(args.root)
    if findings:
        print('Public release check FAILED:')
        for finding in findings:
            print(f'- {finding}')
        return 1
    print(f'Public release check passed: {scanned} files scanned.')
    print('Heuristic only: manually review company data, personal identity, domains and staged changes.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
