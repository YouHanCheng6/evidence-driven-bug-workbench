#!/usr/bin/env python3
"""No-generation SSE adapter used only to satisfy Tabby's model configuration.

Bug Workbench retrieves source through GraphQL ``repositoryGrep`` and never
calls Tabby's completion endpoint. This loopback-only adapter keeps Tabby from
requiring a second language model while serving repository APIs.
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    server_version = "DeerFlowTabbyNoop/1"

    def do_POST(self) -> None:  # noqa: N802
        # llama.cpp exposes the singular path.  Accept the plural spelling as
        # well so the adapter remains usable with older Tabby bindings.
        if self.path.rstrip("/") not in {"/completion", "/completions"}:
            self.send_error(404)
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400)
            return
        if size > 2_000_000:
            self.send_error(413)
            return
        self.rfile.read(size)
        body = f"data: {json.dumps({'content': '', 'stop': True})}\n\n".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18081)
    args = parser.parse_args()
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
