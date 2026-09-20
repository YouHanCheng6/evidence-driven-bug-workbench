"""Optional Phoenix tracing for the Bug Workbench only.

The tracer owns a private OpenTelemetry provider so enabling it cannot replace or
reconfigure DeerFlow's process-global tracing providers.  Every public helper is
fail-open; the replay command performs its own fail-closed acceptance check after
the workflow has completed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, set_span_in_context

logger = logging.getLogger(__name__)

_CONTENT_LIMIT = 32_768
_REDACTED = "[REDACTED]"
_SECRET_KEY = re.compile(r"(?:authorization|cookie|password|passwd|secret|token|api[_-]?key|credential)", re.IGNORECASE)
_TOKEN_METRIC_KEY = re.compile(
    r"(?:^|[._-])(?:token_usage|token_count|tokens|prompt_tokens|completion_tokens|input_tokens|output_tokens|total_tokens|cache_read_tokens|cache_write_tokens|reasoning_tokens)$",
    re.IGNORECASE,
)
_BEARER = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+")
_ASSIGNMENT_SECRET = re.compile(r"(?i)\b(authorization|cookie|password|passwd|secret|token|api[_-]?key)\b\s*[:=]\s*([^\s,;]+)")
_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}\b")
_MAC = re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")
_LABELED_ID = re.compile(r"(?i)(\b(?:sn|serial(?:_?number)?|uuid|mac|user(?:_?id)?)\b[\"']?\s*[:=]\s*[\"']?)([A-Za-z0-9_.:@-]+)")
_HOST_REPOSITORY = re.compile(r"(?:/Users/[^/]+|/home/[^/]+|/root)(?:/[^\s:'\"<>]+)*/(sample_mobile_repo|sample_platform_repo)(?P<suffix>/[^\s:'\"<>]*)?")

_SPAN_LABELS_ZH = {
    "bug_workbench.analysis": "Bug 分析",
    "bug_workbench.knowledge_recall": "地图入口召回",
    "bug_workbench.codex_investigation": "Codex 源码调查",
}


def _display_span_name(stable_name: str, *, detail: str = "") -> str:
    label = _SPAN_LABELS_ZH.get(stable_name)
    if label is None:
        return stable_name
    suffix = f" {detail}" if detail else ""
    return f"{label}{suffix} / {stable_name}"


def _span_identity(stable_name: str, *, detail: str = "") -> dict[str, str]:
    label = _SPAN_LABELS_ZH.get(stable_name)
    return {
        "deerflow.span_key": stable_name,
        "deerflow.description_zh": f"{label or stable_name}{f'：{detail}' if detail else ''}",
    }


def _enabled(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class PhoenixTraceSettings:
    enabled: bool
    endpoint: str
    base_url: str
    project_name: str
    capture_content: bool

    @classmethod
    def from_environment(cls) -> PhoenixTraceSettings:
        return cls(
            enabled=_enabled(os.environ.get("PHOENIX_TRACING")),
            endpoint=os.environ.get("PHOENIX_OTLP_ENDPOINT", "http://127.0.0.1:6006/v1/traces").strip(),
            base_url=os.environ.get("PHOENIX_BASE_URL", "http://127.0.0.1:6006").strip().rstrip("/"),
            project_name=os.environ.get("PHOENIX_PROJECT_NAME", "deerflow-bug-workbench").strip() or "deerflow-bug-workbench",
            capture_content=_enabled(os.environ.get("PHOENIX_CAPTURE_CONTENT", "true")),
        )


def _stable_hash(value: str) -> str:
    digest = hashlib.sha256(("deerflow-bug-workbench:" + value).encode()).hexdigest()[:16]
    return f"sha256:{digest}"


def _sanitize_text(value: str, *, limit: int = _CONTENT_LIMIT) -> str:
    text = _BEARER.sub(lambda match: f"{match.group(1)} {_REDACTED}", value)
    text = _ASSIGNMENT_SECRET.sub(lambda match: f"{match.group(1)}={_REDACTED}", text)
    text = _LABELED_ID.sub(lambda match: f"{match.group(1)}{_stable_hash(match.group(2))}", text)
    text = _UUID.sub(lambda match: _stable_hash(match.group(0).lower()), text)
    text = _MAC.sub(lambda match: _stable_hash(match.group(0).lower()), text)
    text = _HOST_REPOSITORY.sub(lambda match: f"{match.group(1)}/{(match.group('suffix') or '').lstrip('/')}", text)
    return text[:limit]


def sanitize_phoenix_value(value: Any, *, key: str = "", capture_content: bool = True) -> Any:
    """Return a JSON-safe, bounded, redacted copy suitable for span attributes."""
    if _SECRET_KEY.search(key) and not _TOKEN_METRIC_KEY.search(key):
        return _REDACTED
    if isinstance(value, bytes):
        return f"<binary:{len(value)} bytes omitted>"
    if isinstance(value, Path):
        return _sanitize_text(value.as_posix())
    if isinstance(value, str):
        return _sanitize_text(value) if capture_content else f"<content omitted:{len(value)} chars>"
    if isinstance(value, Mapping):
        return {str(item_key): sanitize_phoenix_value(item, key=str(item_key), capture_content=capture_content) for item_key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [sanitize_phoenix_value(item, key=key, capture_content=capture_content) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _sanitize_text(str(value))


def _json_attribute(value: Any, *, capture_content: bool) -> str:
    sanitized = sanitize_phoenix_value(value, capture_content=capture_content)
    return json.dumps(sanitized, ensure_ascii=False, sort_keys=True, default=str)[:_CONTENT_LIMIT]


_PROVIDERS: dict[tuple[str, str], TracerProvider] = {}
_PROVIDERS_LOCK = threading.Lock()
_WARNED_SETUP_FAILURES: set[str] = set()


def _provider(settings: PhoenixTraceSettings) -> TracerProvider | None:
    key = (settings.endpoint, settings.project_name)
    with _PROVIDERS_LOCK:
        if key in _PROVIDERS:
            return _PROVIDERS[key]
        try:
            provider = TracerProvider(
                resource=Resource.create(
                    {
                        "service.name": "deerflow-bug-workbench",
                        "openinference.project.name": settings.project_name,
                    }
                )
            )
            exporter = OTLPSpanExporter(endpoint=settings.endpoint)
            provider.add_span_processor(BatchSpanProcessor(exporter))
            _PROVIDERS[key] = provider
            return provider
        except Exception:
            if settings.endpoint not in _WARNED_SETUP_FAILURES:
                _WARNED_SETUP_FAILURES.add(settings.endpoint)
                logger.warning("Phoenix tracing setup failed; Bug Workbench will continue without tracing", exc_info=True)
            return None


def _span_context(span: Span | None) -> Any:
    return set_span_in_context(span) if span is not None else None


class BugPhoenixTrace:
    """One optional Bug Workbench trace with explicit, observer-only spans."""

    def __init__(self, *, workflow_id: str, bug_id: int, replay_id: str | None = None) -> None:
        self.settings = PhoenixTraceSettings.from_environment()
        self.workflow_id = workflow_id
        self.bug_id = bug_id
        self.replay_id = replay_id
        self._provider = _provider(self.settings) if self.settings.enabled else None
        self._tracer = self._provider.get_tracer("deerflow.bug_workbench", "1") if self._provider is not None else None
        self.root: Span | None = None
        self.trace_id: str | None = None

    @property
    def active(self) -> bool:
        return self._tracer is not None and self.root is not None

    def start(self, *, attributes: Mapping[str, Any] | None = None) -> None:
        if self._tracer is None or self.root is not None:
            return
        try:
            stable_name = "bug_workbench.analysis"
            self.root = self._tracer.start_span(_display_span_name(stable_name))
            context = self.root.get_span_context()
            self.trace_id = f"{context.trace_id:032x}" if context.is_valid else None
            self.set_attributes(
                self.root,
                {
                    "workflow.id": self.workflow_id,
                    "bug.id": self.bug_id,
                    "replay.id": self.replay_id or "",
                    **_span_identity(stable_name),
                    "deerflow.guide_zh": "从左侧调查树查看准备线索、Codex 源码调查、四段报告和备注写回。",
                    **dict(attributes or {}),
                },
            )
        except Exception:
            logger.warning("Phoenix root span creation failed; Bug Workbench will continue", exc_info=True)
            self.root = None

    def finish(self, *, error: BaseException | None = None) -> None:
        if self.root is None:
            return
        try:
            if error is not None:
                self.root.record_exception(error)
                self.root.set_status(trace.Status(trace.StatusCode.ERROR, str(error)[:500]))
            else:
                self.root.set_status(trace.Status(trace.StatusCode.OK))
            self.root.end()
        except Exception:
            logger.warning("Phoenix root span finalization failed", exc_info=True)
        finally:
            self.root = None

    @contextmanager
    def span(self, name: str, *, attributes: Mapping[str, Any] | None = None, parent: Span | None = None) -> Iterator[Span | None]:
        if self._tracer is None or (parent or self.root) is None:
            yield None
            return
        child: Span | None = None
        try:
            child = self._tracer.start_span(_display_span_name(name), context=_span_context(parent or self.root))
            self.set_attributes(child, {**_span_identity(name), **dict(attributes or {})})
            yield child
            child.set_status(trace.Status(trace.StatusCode.OK))
        except BaseException as exc:
            if child is not None:
                child.record_exception(exc)
                child.set_status(trace.Status(trace.StatusCode.ERROR, str(exc)[:500]))
            raise
        finally:
            if child is not None:
                child.end()

    def set_attributes(self, span: Span | None, attributes: Mapping[str, Any]) -> None:
        if span is None:
            return
        try:
            for key, value in attributes.items():
                if value is None:
                    continue
                if isinstance(value, (bool, int, float, str)):
                    safe = sanitize_phoenix_value(value, key=str(key), capture_content=self.settings.capture_content)
                else:
                    safe = _json_attribute(value, capture_content=self.settings.capture_content)
                span.set_attribute(str(key), safe)
        except Exception:
            logger.warning("Phoenix span attribute recording failed", exc_info=True)

    def add_event(self, span: Span | None, name: str, attributes: Mapping[str, Any] | None = None) -> None:
        if span is None:
            return
        try:
            safe = {
                str(key): (
                    _json_attribute(value, capture_content=self.settings.capture_content) if not isinstance(value, (bool, int, float, str)) else sanitize_phoenix_value(value, key=str(key), capture_content=self.settings.capture_content)
                )
                for key, value in (attributes or {}).items()
                if value is not None
            }
            span.add_event(name, safe)
        except Exception:
            logger.warning("Phoenix span event recording failed", exc_info=True)


    def force_flush(self, timeout_millis: int = 10_000) -> bool:
        if self._provider is None:
            return False
        try:
            return bool(self._provider.force_flush(timeout_millis=timeout_millis))
        except Exception:
            logger.warning("Phoenix trace flush failed", exc_info=True)
            return False




def phoenix_trace_query(
    *,
    base_url: str,
    project_name: str,
    replay_id: str,
    timeout: int = 10,
) -> list[dict[str, Any]]:
    """Query a replay trace through Phoenix's versioned spans API."""
    import httpx

    client = httpx.Client(base_url=base_url.rstrip("/") + "/", timeout=timeout)
    response = client.get(
        f"v1/projects/{project_name}/spans",
        params={"attribute": f"replay.id:{replay_id}", "limit": 10},
        headers={"accept": "application/json"},
    )
    response.raise_for_status()
    payload = response.json()
    roots = payload.get("data") if isinstance(payload, Mapping) else None
    roots = roots if isinstance(roots, list) else []
    if len(roots) != 1:
        return []
    root_payload = roots[0]
    trace_id = str(root_payload.get("context", {}).get("trace_id") or root_payload.get("trace_id") or "") if isinstance(root_payload, Mapping) else ""
    if not trace_id:
        return []
    spans: list[dict[str, Any]] = []
    cursor: str | None = None
    while len(spans) < 10_000:
        params: dict[str, Any] = {"trace_id": trace_id, "limit": 100}
        if cursor:
            params["cursor"] = cursor
        response = client.get(f"v1/projects/{project_name}/spans", params=params, headers={"accept": "application/json"})
        response.raise_for_status()
        page = response.json()
        data = page.get("data") if isinstance(page, Mapping) else None
        spans.extend(dict(item) for item in data or () if isinstance(item, Mapping))
        cursor_value = page.get("next_cursor") if isinstance(page, Mapping) else None
        cursor = str(cursor_value) if cursor_value else None
        if not cursor:
            break
    return spans
