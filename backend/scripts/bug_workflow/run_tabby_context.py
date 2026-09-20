#!/usr/bin/env python3
"""Run the private Tabby retrieval stack as one supervised local process group.

The company API key is read only from ``EXAMPLE_EMBEDDING_API_KEY``.  It is inherited by
the loopback embedding adapter and removed from the Tabby child environment.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tabby-root", type=Path, default=Path(".deer-flow/tabby"))
    parser.add_argument(
        "--tabby-bin",
        type=Path,
        default=Path(".deer-flow/tools/tabby/tabby_aarch64-apple-darwin/tabby"),
    )
    parser.add_argument("--tabby-port", type=int, default=8080)
    parser.add_argument("--completion-port", type=int, default=18081)
    parser.add_argument("--embedding-port", type=int, default=18082)
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[3]
    tabby_root = (root / args.tabby_root).resolve() if not args.tabby_root.is_absolute() else args.tabby_root.resolve()
    tabby_bin = (root / args.tabby_bin).resolve() if not args.tabby_bin.is_absolute() else args.tabby_bin.resolve()
    jwt_file = tabby_root / "jwt-secret"
    if not os.environ.get("EXAMPLE_EMBEDDING_API_KEY", "").strip():
        raise SystemExit("missing $EXAMPLE_EMBEDDING_API_KEY")
    for required in (tabby_bin, tabby_root / "config.toml", jwt_file):
        if not required.exists():
            raise SystemExit(f"missing Tabby runtime file: {required}")

    script_dir = Path(__file__).resolve().parent
    allowed_environment = {
        "ALL_PROXY",
        "HOME",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "LANG",
        "LC_ALL",
        "PATH",
        "SSL_CERT_FILE",
        "TMPDIR",
    }
    shared_env = {key: value for key, value in os.environ.items() if key in allowed_environment}
    loopback = "127.0.0.1,localhost"
    shared_env["NO_PROXY"] = loopback
    shared_env["no_proxy"] = loopback
    tabby_env = shared_env.copy()
    tabby_env["TABBY_ROOT"] = str(tabby_root)
    tabby_env["TABBY_WEBSERVER_JWT_TOKEN_SECRET"] = jwt_file.read_text(encoding="utf-8").strip()

    commands = [
        (
            [sys.executable, str(script_dir / "tabby_noop_completion.py"), "--port", str(args.completion_port)],
            tabby_env,
        ),
        (
            [sys.executable, str(script_dir / "tabby_embedding_gateway.py"), "--port", str(args.embedding_port)],
            {**shared_env, "EXAMPLE_EMBEDDING_API_KEY": os.environ["EXAMPLE_EMBEDDING_API_KEY"]},
        ),
        (
            [str(tabby_bin), "serve", "--device", "cpu", "--host", "127.0.0.1", "--port", str(args.tabby_port)],
            tabby_env,
        ),
    ]
    children: list[subprocess.Popen[bytes]] = []
    stopping = False

    def stop(_signum: int | None = None, _frame: object | None = None) -> None:
        nonlocal stopping
        if stopping:
            return
        stopping = True
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        for command, environment in commands:
            children.append(subprocess.Popen(command, cwd=root, env=environment))
            time.sleep(0.2)
            if children[-1].poll() is not None:
                return children[-1].returncode or 1
        while not stopping:
            for child in children:
                status = child.poll()
                if status is not None:
                    stop()
                    return status or 1
            time.sleep(0.5)
    finally:
        stop()
        for child in children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
