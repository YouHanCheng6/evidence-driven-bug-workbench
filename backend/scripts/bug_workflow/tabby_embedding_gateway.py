#!/usr/bin/env python3
"""Loopback batching adapter to the company embeddings gateway.

Bug Workbench reranks only a bounded set of Tabby grep candidates. This
adapter coalesces concurrent OpenAI-compatible requests into bounded upstream
batches and retries only transient rate/gateway failures. It never logs source
text, vectors, or credentials.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import queue
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass
class Pending:
    model: str
    inputs: list[str]
    done: threading.Event = field(default_factory=threading.Event)
    status: int = 500
    response: dict[str, Any] = field(default_factory=dict)


class Batcher:
    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str,
        max_batch_inputs: int,
        max_batch_chars: int,
        batch_window_ms: int,
        workers: int,
    ):
        self.endpoint = endpoint.rstrip("/") + "/embeddings"
        self.api_key = api_key
        self.max_batch_inputs = max_batch_inputs
        self.max_batch_chars = max_batch_chars
        self.batch_window = batch_window_ms / 1000
        # Prefer the newest interactive retrieval when requests briefly queue.
        self.pending: queue.LifoQueue[Pending] = queue.LifoQueue(maxsize=256)
        for index in range(workers):
            threading.Thread(
                target=self._run,
                name=f"tabby-embedding-batcher-{index + 1}",
                daemon=True,
            ).start()

    def submit(self, item: Pending) -> None:
        self.pending.put(item)
        item.done.wait()

    def _run(self) -> None:
        while True:
            first = self.pending.get()
            batch = [first]
            count = len(first.inputs)
            char_count = sum(len(value) for value in first.inputs)
            deadline = time.monotonic() + self.batch_window
            while count < self.max_batch_inputs:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    item = self.pending.get(timeout=remaining)
                except queue.Empty:
                    break
                item_chars = sum(len(value) for value in item.inputs)
                if (
                    item.model != first.model
                    or count + len(item.inputs) > self.max_batch_inputs
                    or char_count + item_chars > self.max_batch_chars
                ):
                    self.pending.put(item)
                    break
                batch.append(item)
                count += len(item.inputs)
                char_count += item_chars
            try:
                self._dispatch(batch)
            except Exception as exc:  # Keep a long-lived worker alive on malformed upstream responses.
                print(
                    f"embedding batch failed unexpectedly: type={type(exc).__name__}",
                    file=sys.stderr,
                    flush=True,
                )
                for item in batch:
                    item.status = 502
                    item.response = {
                        "error": {
                            "message": "upstream embedding request failed",
                            "type": "upstream_error",
                        }
                    }
                    item.done.set()

    def _dispatch(self, batch: list[Pending]) -> None:
        inputs = [text for item in batch for text in item.inputs]
        body = json.dumps({"model": batch[0].model, "input": inputs}).encode()
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        status = 502
        payload: dict[str, Any] = {"error": {"message": "embedding gateway unavailable"}}
        for attempt in range(6):
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    status = response.status
                    payload = json.loads(response.read())
                break
            except urllib.error.HTTPError as exc:
                status = exc.code
                try:
                    error_payload = json.loads(exc.read())
                except (ValueError, OSError):
                    error_payload = {}
                error = error_payload.get("error", {}) if isinstance(error_payload, dict) else {}
                print(
                    "embedding upstream rejected batch: "
                    f"status={status} inputs={len(inputs)} chars={sum(len(value) for value in inputs)} "
                    f"type={error.get('type', 'unknown')} code={error.get('code', 'unknown')}",
                    file=sys.stderr,
                    flush=True,
                )
                if status not in {429, 502, 503, 504}:
                    break
            except (
                http.client.IncompleteRead,
                http.client.RemoteDisconnected,
                OSError,
                TimeoutError,
                ValueError,
                socket.timeout,
                urllib.error.URLError,
            ) as exc:
                status = 502
                print(
                    "embedding upstream transport failure: "
                    f"inputs={len(inputs)} chars={sum(len(value) for value in inputs)} "
                    f"type={type(exc).__name__}",
                    file=sys.stderr,
                    flush=True,
                )
            time.sleep(min(16, 2**attempt))

        data = payload.get("data") if isinstance(payload, dict) else None
        if status == 200 and isinstance(data, list) and len(data) == len(inputs):
            offset = 0
            for item in batch:
                selected = data[offset : offset + len(item.inputs)]
                offset += len(item.inputs)
                item.status = 200
                item.response = {
                    "object": "list",
                    "model": payload.get("model", item.model),
                    "data": [{**entry, "index": index} for index, entry in enumerate(selected)],
                    "usage": payload.get("usage", {}),
                }
                item.done.set()
            return
        for item in batch:
            item.status = status
            item.response = {"error": {"message": "upstream embedding request failed", "type": "upstream_error"}}
            item.done.set()


class Handler(BaseHTTPRequestHandler):
    batcher: Batcher
    server_version = "DeerFlowEmbeddingBatcher/1"

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") != "/v1/embeddings":
            self.send_error(404)
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size <= 0 or size > 8_000_000:
                raise ValueError
            request = json.loads(self.rfile.read(size))
            raw_input = request["input"]
            inputs = [raw_input] if isinstance(raw_input, str) else list(raw_input)
            if not inputs or not all(isinstance(item, str) and item for item in inputs):
                raise ValueError
            item = Pending(model=str(request.get("model") or "example-embedding-model"), inputs=inputs)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            self.send_error(400)
            return
        self.batcher.submit(item)
        body = json.dumps(item.response).encode()
        self.send_response(item.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # Tabby may cancel outstanding index requests while shutting down.
            return

    def log_message(self, _format: str, *_args: object) -> None:
        return


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18082)
    parser.add_argument("--upstream", default="https://service.example.invalid")
    parser.add_argument("--api-key-env", default="EXAMPLE_EMBEDDING_API_KEY")
    # Eight source-sized inputs are accepted by the gateway.
    parser.add_argument("--max-batch-inputs", type=int, default=8)
    parser.add_argument("--max-batch-chars", type=int, default=12_000)
    parser.add_argument("--batch-window-ms", type=int, default=75)
    # The company route accepts concurrent probes but serializes or stalls under
    # sustained load. One upstream worker provides the stable long-running
    # envelope while retaining batches of 8.
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()
    api_key = os.environ.get(args.api_key_env, "").strip()
    if not api_key:
        raise SystemExit(f"missing ${args.api_key_env}")
    Handler.batcher = Batcher(
        endpoint=args.upstream,
        api_key=api_key,
        max_batch_inputs=max(1, args.max_batch_inputs),
        max_batch_chars=max(1, args.max_batch_chars),
        batch_window_ms=max(0, args.batch_window_ms),
        workers=max(1, min(args.workers, 8)),
    )
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
