#!/usr/bin/env python3
"""No-generation SSE adapter used only to satisfy Tabby's model configuration.

Bug Workbench consumes Tabby's code-attachment event and disconnects before
answer generation. Tabby asks its configured model one fixed routing question
before that retrieval; this loopback adapter answers only that question with
``SNIPPET`` and returns no generated text for every other request.
"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    server_version = "DeerFlowTabbyNoop/1"

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.rstrip("/")
        if path not in {"/completion", "/completions", "/v1/chat/completions"}:
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
        raw = self.rfile.read(size)
        try:
            request = json.loads(raw or b"{}")
        except (TypeError, ValueError):
            request = {}
        prompt = str(request.get("prompt") or "") if isinstance(request, dict) else ""
        if isinstance(request, dict):
            messages = request.get("messages")
            if isinstance(messages, list):
                prompt += "\n" + "\n".join(str(item.get("content") or "") for item in messages if isinstance(item, dict))
        content = "SNIPPET" if "following two kinds of context are supported" in prompt and "FILE_LIST" in prompt else ""
        if path == "/v1/chat/completions":
            response_id = f"chatcmpl-{uuid.uuid4().hex}"
            created = int(time.time())
            if isinstance(request, dict) and request.get("stream"):
                chunks = [
                    {
                        "id": response_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": "deerflow-retrieval-router",
                        "choices": [{"index": 0, "delta": {"role": "assistant", "content": content}, "finish_reason": None}],
                    },
                    {
                        "id": response_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": "deerflow-retrieval-router",
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    },
                ]
                body = ("".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n").encode()
                content_type = "text/event-stream"
            else:
                body = json.dumps(
                    {
                        "id": response_id,
                        "object": "chat.completion",
                        "created": created,
                        "model": "deerflow-retrieval-router",
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": content},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                    }
                ).encode()
                content_type = "application/json"
        else:
            body = f"data: {json.dumps({'content': content, 'stop': True})}\n\n".encode()
            content_type = "text/event-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
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
