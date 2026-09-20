#!/usr/bin/env python3
"""Create the private Tabby repository-context configuration.

The generated file lives below the ignored DeerFlow runtime directory and is
mode 0600.  It contains only loopback adapter endpoints; the company gateway
credential stays in the embedding adapter process environment and is never
    written to Tabby's TOML file. Repository registration and serving remain explicit actions.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tabby-root", type=Path, default=Path(".deer-flow/tabby"))
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path(".deer-flow/tabby/composite-repositories"),
        help="Private composite Git corpora produced by prepare_tabby_repositories.py.",
    )
    parser.add_argument("--embedding-endpoint", default="http://127.0.0.1:18082/v1")
    parser.add_argument("--embedding-model", default="example-embedding-model")
    parser.add_argument("--completion-adapter-endpoint", default="http://127.0.0.1:18081")
    args = parser.parse_args()

    repository_root = args.repository_root.expanduser().resolve()
    repositories = {name: repository_root / name for name in ("sample_mobile_repo", "sample_platform_repo")}
    missing = [f"{name}={path}" for name, path in repositories.items() if not (path / ".git").exists()]
    if missing:
        raise SystemExit("repository checkout missing: " + ", ".join(missing))
    tabby_root = args.tabby_root.expanduser().resolve()
    tabby_root.mkdir(parents=True, exist_ok=True)
    config_path = tabby_root / "config.toml"
    lines = [
        "# Generated private runtime configuration; do not commit.",
        "[model.completion.http]",
        'kind = "llama.cpp/completion"',
        f"api_endpoint = {_toml_string(args.completion_adapter_endpoint.rstrip('/'))}",
        'prompt_template = "{prefix}{suffix}"',
        "",
        "[model.embedding.http]",
        'kind = "openai/embedding"',
        f"model_name = {_toml_string(args.embedding_model)}",
        f"api_endpoint = {_toml_string(args.embedding_endpoint.rstrip('/'))}",
        'api_key = "local-loopback"',
        "",
    ]
    for name, path in repositories.items():
        lines.extend(
            [
                "[[repositories]]",
                f"name = {_toml_string(name)}",
                f"git_url = {_toml_string(path.as_uri())}",
                "",
            ]
        )
    config_path.write_text("\n".join(lines), encoding="utf-8")
    config_path.chmod(0o600)
    print(f"Tabby context config written: {config_path}")
    print(f"Set TABBY_ROOT={tabby_root} before starting both loopback adapters and `tabby serve --device cpu`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
