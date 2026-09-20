"""LangGraph orchestration for automated Bug analysis and ZenTao note writeback."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph
from zentao_mcp.client import ZentaoClient, ZentaoError

from app.gateway.auth_disabled import AUTH_SOURCE_INTERNAL
from app.gateway.bug_attachment_evidence import collect_bug_attachment_evidence
from app.gateway.bug_business_knowledge import matched_retrieval_aliases, recall_business_rules
from app.gateway.bug_codex_summary import (
    investigate_bug_with_codex,
    require_codex_runtime,
)
from app.gateway.bug_investigator import (
    InvestigationKnowledgeContext,
    build_investigation_knowledge_context,
    compact_visual_evidence,
)
from app.gateway.bug_log_expert import collect_log_material, run_log_expert
from app.gateway.bug_log_query_runtime import LOG_CACHE_DIRECTORY
from app.gateway.bug_phoenix import BugPhoenixTrace
from app.gateway.bug_source_retrieval import build_source_retrieval
from app.gateway.bug_source_view import materialize_bug_source_view
from app.gateway.bug_triage import apply_visual_copy_evidence, select_relevant_assets, unknown_triage
from app.gateway.bug_workflow_state import BugWorkflowRuntime, BugWorkflowState
from app.gateway.deps import get_thread_store
from app.gateway.internal_auth import create_internal_auth_headers, get_internal_user
from deerflow.config.app_config import get_app_config
from deerflow.uploads.manager import ensure_uploads_dir
from deerflow.utils.oneshot_llm import run_oneshot_llm_result

logger = logging.getLogger(__name__)
_ACTIVE_WORKFLOW_TASKS: dict[str, set[asyncio.Task[None]]] = {}
_HANDOFF_SOURCE_REF_PATTERN = re.compile(
    r"(?P<path>(?:/?mnt/repos/[A-Za-z0-9_.-]+/(?:[A-Za-z0-9_.@+-]+/)*[A-Za-z0-9_.@+-]+\.[A-Za-z0-9]+|"
    r"(?:[A-Za-z0-9_.@+-]+/)+[A-Za-z0-9_.@+-]+\.[A-Za-z0-9]+))"
    r"(?::(?P<line>\d+))?"
)
_RUNTIME_PRE_SCAN_RULE_VERSION = "runtime-pre-scan-v7"
_KNOWN_SOURCE_REPOSITORY_PREFIXES = ("sample_platform_repo/", "sample_mobile_repo/")
_TERMINAL_ZENTAO_STATUS_LABELS = {
    "resolved": "已解决",
    "closed": "已关闭",
    "已解决": "已解决",
    "已关闭": "已关闭",
}
_BUG_SNAPSHOT_FIELDS = (
    "id",
    "title",
    "type",
    "description",
    "steps",
    "expected",
    "actual",
    "module",
    "product",
    "status",
    "resolution",
    "resolved_at",
    "closed_at",
    "severity",
)


@dataclass(frozen=True)
class NoteProjection:
    """Deterministic note input derived without replacing the full report."""

    text: str
    use_full_report: bool = False
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlatformResolution:
    """Auditable reproduced-client contract; never an implementation owner."""

    reported_clients: tuple[Literal["android", "ios", "harmony"], ...]
    repository_family: Literal["classic_mobile", "harmony"]
    primary_repository: Literal["sample_mobile_repo", "sample_platform_repo"]
    investigation_mode: Literal["android", "ios", "android_ios_shared", "classic_shared_unknown", "harmony"]
    client_scope_status: Literal["explicit", "module_fallback"]
    candidate_implementation_layers: tuple[str, ...]
    client_evidence: dict[str, tuple[str, ...]]
    evidence: tuple[dict[str, str], ...]
    reason: str
    raw_response: str
    token_usage: dict[str, int]
    model_call_count: int = 1
    model_attempts: tuple[dict[str, Any], ...] = ()

    def payload(self) -> dict[str, Any]:
        return {
            "reported_clients": list(self.reported_clients),
            "repository_family": self.repository_family,
            "primary_repository": self.primary_repository,
            "investigation_mode": self.investigation_mode,
            "client_scope_status": self.client_scope_status,
            "candidate_implementation_layers": list(self.candidate_implementation_layers),
            "client_evidence": {client: list(evidence_ids) for client, evidence_ids in self.client_evidence.items()},
            "evidence": [dict(item) for item in self.evidence],
            "reason": self.reason,
            "raw_response": self.raw_response,
        }


class PlatformResolutionFormatError(ValueError):
    """A platform reply was not JSON; retain content-free provider diagnostics."""

    def __init__(self, attempts: Sequence[Mapping[str, Any]], token_usage: Mapping[str, int]):
        self.attempts = tuple(dict(item) for item in attempts)
        self.token_usage = dict(token_usage)
        self.model_call_count = len(self.attempts)
        outcomes = ", ".join(str(item.get("outcome")) for item in self.attempts)
        super().__init__(f"platform model returned invalid JSON after {self.model_call_count} call(s): {outcomes}")


def _canonical_source_path(path: str) -> str:
    normalized = path.strip().lstrip("/")
    match = re.match(r"mnt/repos/[^/]+/(.+)", normalized)
    if match:
        return match.group(1)
    # Summary models legitimately use either a source-view relative path or
    # prefix that path with the mounted repository name.  Normalize only the
    # two operator-configured repository families; never fuzzy-correct a
    # misspelled or structurally different path.
    for prefix in _KNOWN_SOURCE_REPOSITORY_PREFIXES:
        if normalized.startswith(prefix):
            return normalized[len(prefix) :]
    return normalized


def _source_repository_identity(path: str) -> tuple[str | None, str]:
    """Return the exact repository label and repository-relative source path."""
    normalized = path.strip().strip("`'\"").lstrip("/")
    mounted = re.match(r"mnt/repos/(?P<repository>[^/]+)/(?P<path>.+)", normalized)
    if mounted:
        return mounted.group("repository"), mounted.group("path")
    if normalized.startswith("仓库未确认/"):
        return "仓库未确认", normalized.removeprefix("仓库未确认/")
    for prefix in _KNOWN_SOURCE_REPOSITORY_PREFIXES:
        if normalized.startswith(prefix):
            return prefix.rstrip("/"), normalized[len(prefix) :]
    return None, _canonical_source_path(normalized)


def _verified_repository_for_relative_path(path: str, source_evidence: Sequence[str]) -> str | None:
    """Resolve a bare relative path only when verified reads identify one repository."""
    canonical = _canonical_source_path(path)
    repositories: set[str] = set()
    for value in source_evidence:
        for match in _HANDOFF_SOURCE_REF_PATTERN.finditer(str(value)):
            repository, relative = _source_repository_identity(match.group("path"))
            if repository and _canonical_source_path(relative) == canonical:
                repositories.add(repository)
    return next(iter(repositories)) if len(repositories) == 1 else None


def _display_source_path(path: str, source_evidence: Sequence[str]) -> str:
    """Project one verified path for people without exposing the sandbox mount."""
    repository, relative = _source_repository_identity(path)
    if repository:
        return f"{repository}/{relative}"
    repository = _verified_repository_for_relative_path(relative, source_evidence)
    if repository:
        return f"{repository}/{relative}"
    return f"仓库未确认/{relative}"


def _display_source_references(text: str, source_evidence: Sequence[str]) -> str:
    """Replace source references while preserving line/range suffixes and prose."""

    # A complete Codex section often cites one file first with the selected
    # repository prefix and later uses the shorter repository-relative form.
    # Treat the explicit citation in the same section as local evidence so the
    # later mention does not degrade to the misleading ``仓库未确认`` label.
    local_evidence = list(source_evidence)
    local_evidence.extend(match.group("path") for match in _HANDOFF_SOURCE_REF_PATTERN.finditer(text) if _source_repository_identity(match.group("path"))[0] in {"sample_mobile_repo", "sample_platform_repo"})

    def replace(match: re.Match[str]) -> str:
        line = match.group("line")
        # Import/module literals are source text, not report citations. Never
        # rewrite require('./Config.json') into a different executable literal.
        if not line and (match.group("path").startswith(("./", "../")) or re.search(r"(?:require\s*\(\s*|\bfrom\s+|\bimport\s+)[\"']$", match.string[max(0, match.start() - 30) : match.start()])):
            return match.group(0)
        suffix = f":{line}" if line else ""
        return f"{_display_source_path(match.group('path'), local_evidence)}{suffix}"

    # Backend-rendered source quotes use fenced blocks. Their exact bytes must
    # survive presentation normalization; surrounding citations are projected.
    pieces = re.split(r"(```[^\n]*\n[\s\S]*?```)", text)
    return "".join(piece if index % 2 else _HANDOFF_SOURCE_REF_PATTERN.sub(replace, piece) for index, piece in enumerate(pieces))


_PURE_SOURCE_RANGE_PATTERN = re.compile(
    r"^(?P<path>(?:/?mnt/repos/[A-Za-z0-9_.-]+/(?:[A-Za-z0-9_.@+-]+/)*[A-Za-z0-9_.@+-]+\.[A-Za-z0-9]+|"
    r"(?:[A-Za-z0-9_.@+-]+/)+[A-Za-z0-9_.@+-]+\.[A-Za-z0-9]+))"
    r":(?P<start>\d+)(?:-(?P<end>\d+))?$"
)


def _configured_host_mount(app_config: Any, container_path: str) -> Path | None:
    """Resolve one configured read-only analysis mount on the Gateway host."""
    sandbox = getattr(app_config, "sandbox", None)
    for mount in getattr(sandbox, "mounts", ()) or ():
        if str(getattr(mount, "container_path", "")).rstrip("/") != container_path.rstrip("/"):
            continue
        host_path = Path(str(getattr(mount, "host_path", ""))).expanduser()
        if host_path.is_dir():
            return host_path.resolve()
    return None


def _terminal_zentao_status_label(bug: dict[str, Any]) -> str | None:
    """Return the user-facing terminal status that forbids a new analysis."""
    status = str(bug.get("status") or "").strip()
    return _TERMINAL_ZENTAO_STATUS_LABELS.get(status.lower()) or _TERMINAL_ZENTAO_STATUS_LABELS.get(status)


_UI_BEHAVIOR_OWNER_LINE_PATTERN = re.compile(
    r"^(?P<platform>ANDROID|IOS|RN)_BEHAVIOR_OWNER="
    r"(?P<path>/mnt/repos/[^:]+):(?P<line>\d+):(?P<evidence>.+)$",
    re.IGNORECASE,
)
_UI_BEHAVIOR_ANCHOR_LINE_PATTERN = re.compile(
    r"^(?P<platform>ANDROID|IOS|RN)_BEHAVIOR_ANCHOR="
    r"(?P<path>/mnt/repos/[^:]+):(?P<line>\d+):"
    r"(?P<role>behavior_layout|behavior_constraint|behavior_event|behavior_mutation|behavior_feedback|behavior_refresh|behavior_state)"
    r"(?:\[scope=(?P<scope>[A-Za-z_][A-Za-z0-9_.:$-]*)\])?:"
    r"(?P<evidence>.+)$",
    re.IGNORECASE,
)


def compact_bug_snapshot(bug: dict[str, Any]) -> dict[str, Any]:
    """Keep current ticket facts while excluding recursive, model-written history."""
    snapshot: dict[str, Any] = {}
    remaining = 6000
    for field in _BUG_SNAPSHOT_FIELDS:
        value = bug.get(field)
        if value in (None, ""):
            continue
        if isinstance(value, (str, int, float, bool)):
            compact = re.sub(r"\s+", " ", str(value)).strip()
        else:
            continue
        if not compact or remaining <= 0:
            continue
        compact = compact[:remaining]
        snapshot[field] = value if isinstance(value, (int, float, bool)) else compact
        remaining -= len(compact)
    return snapshot


def _analysis_bug_snapshot(snapshot: Mapping[str, Any] | None) -> dict[str, Any]:
    """Exclude ZenTao's development module label from every analysis input."""
    return {key: value for key, value in dict(snapshot or {}).items() if key != "module"}


def _codex_ticket_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Project one canonical copy of ticket facts into the Codex prompt.

    Full triage and platform evidence remain in workflow audit state.  They are
    intentionally not copied into the model input because fallback triage often
    mirrors the same ZenTao description into several fields.
    """
    retained: dict[str, Any] = {}
    for key in ("id", "title", "type", "product", "status", "severity", "steps", "actual", "expected", "description", "confirmed_product_scope", "confirmed_copy_scope"):
        value = snapshot.get(key)
        if value not in (None, "", [], {}):
            retained[key] = value
    platform = snapshot.get("confirmed_platform")
    if isinstance(platform, Mapping):
        compact_platform = {
            key: platform[key] for key in ("reported_clients", "repository_family", "primary_repository", "investigation_mode", "client_scope_status", "candidate_implementation_layers") if platform.get(key) not in (None, "", [], {})
        }
        if compact_platform:
            retained["confirmed_platform"] = compact_platform
    return retained


_TICKET_SECTION_PATTERN = re.compile(r"[\[【](?P<label>预置条件|前置条件|测试步骤|操作步骤|复现步骤|预期结果|期望结果|实际结果|实测结果|测试结果|恢复方法|问题恢复方法|重现概率|复现概率)[\]】]")
_NUMBERED_FACT_PATTERN = re.compile(r"(?:^|[\s；;])(?P<number>\d{1,2})[、.．)]\s*(?P<text>.+?)(?=(?:[\s；;]+\d{1,2}[、.．)])|$)")


def _bug_client_scope_text(snapshot: dict[str, Any]) -> str:
    """Use reporter/runtime facts for client scope, never development ownership."""
    return " ".join(str(snapshot.get(field, "")) for field in _BUG_SNAPSHOT_FIELDS if field != "module" and snapshot.get(field) not in (None, ""))


def _clients_in_text(text: str) -> tuple[str, ...]:
    patterns = (
        ("android", r"(?:\bAndroid(?![A-Za-z])|安卓)"),
        ("ios", r"(?:\biOS(?![A-Za-z])|\bIOS(?![A-Za-z])|苹果(?:端|系统)?)"),
        ("harmony", r"(?:\bHarmony(?:OS)?\b|鸿蒙)"),
    )
    return tuple(name for name, pattern in patterns if re.search(pattern, text, re.IGNORECASE))


def detect_affected_clients(snapshot: dict[str, Any]) -> tuple[str, ...]:
    """Derive observed reporter clients; this never identifies implementation ownership."""
    return _clients_in_text(_bug_client_scope_text(snapshot))


def detect_reported_clients(snapshot: dict[str, Any]) -> tuple[str, ...]:
    """Derive ticket-reported clients without consulting the development module label."""
    return detect_affected_clients(snapshot)


def _platform_fact_choices(snapshot: Mapping[str, Any]) -> tuple[dict[str, dict[str, str]], str]:
    """Assign stable IDs to ticket facts without interpreting device models."""
    choices: dict[str, dict[str, str]] = {}
    for field in ("module", "title", "description", "steps", "actual", "expected"):
        value = snapshot.get(field)
        if value in (None, ""):
            continue
        quote = re.sub(r"\s+", " ", str(value)).strip()[:4_000]
        if not quote:
            continue
        evidence_id = f"P{len(choices) + 1}"
        choices[evidence_id] = {"evidence_id": evidence_id, "source": field, "quote": quote}
    return choices, "\n".join(json.dumps(item, ensure_ascii=False) for item in choices.values())


def _json_object_from_model_text(text: str) -> dict[str, Any]:
    """Parse one JSON object without the retired summary recovery stack."""
    value = text.strip()
    if value.startswith("```"):
        value = value.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    payload = json.loads(value)
    if not isinstance(payload, dict):
        raise ValueError("model response is not a JSON object")
    return payload


async def _run_platform_resolution(
    *,
    bug_id: int,
    snapshot: Mapping[str, Any],
    model_name: str | None,
    max_output_tokens: int,
    thread_id: str,
) -> PlatformResolution:
    """Confirm reproduced clients once; repository and owner policy stay deterministic."""
    choices, _choice_text = _platform_fact_choices(snapshot)
    if not choices:
        raise ValueError("工单没有可用于确认复现端的 module、设备或正文事实")
    module = str(snapshot.get("module") or "")
    module_is_harmony = bool(re.search(r"鸿蒙|harmony", module, re.IGNORECASE))
    module_is_classic = bool(re.search(r"android|安卓", module, re.IGNORECASE) and re.search(r"ios|苹果", module, re.IGNORECASE))
    repository_family = "harmony" if module_is_harmony else "classic_mobile"
    client_choices = {evidence_id: item for evidence_id, item in choices.items() if item["source"] != "module"}
    client_choice_text = "\n".join(json.dumps(item, ensure_ascii=False) for item in client_choices.values())
    repository_ids = tuple(evidence_id for evidence_id, item in choices.items() if item["source"] == "module")

    def module_fallback(
        *,
        raw_response: str = "",
        token_usage: Mapping[str, int] | None = None,
        model_call_count: int,
        model_attempts: Sequence[Mapping[str, Any]] = (),
    ) -> PlatformResolution:
        if not module.strip() or not repository_ids:
            raise ValueError("工单没有可用于 module 降级的仓库家族事实")
        fallback_mode: Literal["classic_shared_unknown", "harmony"] = "harmony" if repository_family == "harmony" else "classic_shared_unknown"
        return PlatformResolution(
            reported_clients=(),
            repository_family=repository_family,
            primary_repository="sample_platform_repo" if repository_family == "harmony" else "sample_mobile_repo",
            investigation_mode=fallback_mode,
            client_scope_status="module_fallback",
            candidate_implementation_layers=("sample_platform_repo",) if repository_family == "harmony" else ("shared_rn",),
            client_evidence={},
            evidence=tuple(dict(choices[evidence_id]) for evidence_id in repository_ids),
            reason="工单未提供可确认具体复现端的设备或系统事实，已按 module 降级确定仓库家族；具体客户端保持未知。",
            raw_response=raw_response[:4_000],
            token_usage=dict(token_usage or {}),
            model_call_count=model_call_count,
            model_attempts=tuple(dict(item) for item in model_attempts),
        )

    if not client_choices:
        return module_fallback(model_call_count=0)
    prompt = "\n".join(
        (
            f"确认禅道 Bug #{bug_id} 实际复现在哪些客户端。你不能调用工具。",
            "只判断复现端，不判断问题类型、Native/RN、源码责任、target、根因、修复范围或调查顺序。",
            (
                "只输出 JSON，client_evidence_ids 的键必须与 reported_clients 完全一致。"
                'Android 单端示例：{"reported_clients":["android"],"client_evidence_ids":{"android":["P编号"]},"reason":"一句话说明"}；'
                'iOS 单端示例：{"reported_clients":["ios"],"client_evidence_ids":{"ios":["P编号"]},"reason":"一句话说明"}；'
                'Android+iOS 双端示例：{"reported_clients":["android","ios"],"client_evidence_ids":{"android":["P编号"],"ios":["P编号"]},"reason":"一句话说明"}；'
                'Harmony 单端示例：{"reported_clients":["harmony"],"client_evidence_ids":{"harmony":["P编号"]},"reason":"一句话说明"}；'
                '无法确认示例：{"reported_clients":[],"client_evidence_ids":{},"reason":"一句话说明"}。'
            ),
            "reported_clients 只能包含 android、ios、harmony。根据设备型号与系统理解任意新设备，不要依赖固定品牌清单；若所有事实都不能确认具体客户端，必须返回空数组和空 client_evidence_ids，后端将按 module 降级。",
            (
                "后端已仅根据 Harmony module 确定仓库家族；module 不证明 Harmony 实际复现，不能作为客户端证据，也不要增加 android 或 ios。"
                if repository_family == "harmony"
                else "后端已仅根据 module 确定仓库家族为 classic_mobile；module 不在下方复现事实中，也绝不能增加 reported_clients。"
            ),
            "每个输出客户端必须在 client_evidence_ids 中分别引用至少一条本身能证明该客户端复现的事实；同一条 Android 设备事实不能证明 iOS，反之亦然。",
            "Android 与 iOS 设备都明确出现时才同时输出 android、ios。单端复现仍可能属于共享 RN，本调用不要据此扩大复现端或判断实现层。Harmony 不能与 android/ios 混合输出。",
            "可用于确认复现端的工单事实编号：",
            client_choice_text,
        )
    )

    model_attempts: list[dict[str, Any]] = []
    token_usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    for attempt_index in range(2):
        output_budget = max_output_tokens if attempt_index == 0 else min(max_output_tokens * 2, 4_000)
        response = await run_oneshot_llm_result(
            system_instruction="你是一次性的客户端复现端确认器，只把明确工单事实归一化为 android、ios、harmony。",
            user_content=prompt,
            run_name="bug-platform-resolution",
            app_config=get_app_config(),
            model_name=model_name,
            thread_id=thread_id,
            max_tokens=output_budget,
        )
        for key in token_usage:
            token_usage[key] += int(response.usage_metadata.get(key) or 0)
        provider_usage = response.response_metadata.get("token_usage")
        completion_details = provider_usage.get("completion_tokens_details") if isinstance(provider_usage, Mapping) else None
        reasoning_tokens = completion_details.get("reasoning_tokens") if isinstance(completion_details, Mapping) else None
        attempt_record: dict[str, Any] = {
            "attempt": attempt_index + 1,
            "output_budget": output_budget,
            "finish_reason": str(response.response_metadata.get("finish_reason") or "unknown")[:40],
            "text_chars": len(response.text),
            "output_tokens": int(response.usage_metadata.get("output_tokens") or 0),
            "reasoning_tokens": reasoning_tokens if isinstance(reasoning_tokens, int) and reasoning_tokens >= 0 else None,
        }
        try:
            payload = _json_object_from_model_text(response.text)
        except ValueError as exc:
            attempt_record["outcome"] = "invalid_json"
            parse_error = exc.__cause__
            if isinstance(parse_error, json.JSONDecodeError):
                attempt_record["json_error"] = parse_error.msg[:100]
                attempt_record["json_error_position"] = parse_error.pos
            model_attempts.append(attempt_record)
            if attempt_index == 1:
                raise PlatformResolutionFormatError(model_attempts, token_usage) from exc
            continue
        attempt_record["outcome"] = "valid_json"
        model_attempts.append(attempt_record)
        break
    raw_clients = payload.get("reported_clients")
    if not isinstance(raw_clients, list):
        raise ValueError("端确认模型没有返回 reported_clients")
    normalized_clients = [str(value).strip().lower() for value in raw_clients]
    if any(value not in {"android", "ios", "harmony"} for value in normalized_clients) or len(normalized_clients) != len(set(normalized_clients)):
        raise ValueError("端确认模型返回了重复值或非法客户端")
    if not normalized_clients:
        raw_client_evidence = payload.get("client_evidence_ids")
        if raw_client_evidence not in (None, {}) and (not isinstance(raw_client_evidence, Mapping) or bool(raw_client_evidence)):
            raise ValueError("端确认模型返回空客户端时仍携带客户端证据")
        return module_fallback(
            raw_response=response.text,
            token_usage=token_usage,
            model_call_count=len(model_attempts),
            model_attempts=model_attempts,
        )
    order = ("android", "ios", "harmony")
    clients = tuple(client for client in order if client in normalized_clients)
    if "harmony" in clients and len(clients) != 1:
        raise ValueError("Harmony 复现端不能与 Android/iOS 混合")

    expected_family = "harmony" if clients == ("harmony",) else "classic_mobile"
    if repository_family != expected_family:
        raise ValueError("端确认模型返回的复现端与 module 仓库家族冲突")
    if module_is_harmony and expected_family != "harmony":
        raise ValueError("module 明确为鸿蒙，但模型没有确认 Harmony")
    if module_is_classic and expected_family != "classic_mobile":
        raise ValueError("module 明确为 Android&iOS，但模型选择了 Harmony")

    raw_client_evidence = payload.get("client_evidence_ids")
    if not isinstance(raw_client_evidence, Mapping):
        raise ValueError("端确认模型没有逐客户端返回 client_evidence_ids")
    client_evidence: dict[str, tuple[str, ...]] = {}
    for client in clients:
        raw_ids = raw_client_evidence.get(client)
        evidence_ids = tuple(dict.fromkeys(str(value).strip() for value in raw_ids if str(value).strip())) if isinstance(raw_ids, list) else ()
        if not evidence_ids or any(evidence_id not in client_choices for evidence_id in evidence_ids):
            raise ValueError(f"{client} 复现端没有引用独立且合法的非 module 工单事实")
        client_evidence[client] = evidence_ids
    if any(str(key) not in clients for key in raw_client_evidence):
        raise ValueError("client_evidence_ids 包含未输出的客户端")
    evidence_ids = tuple(dict.fromkeys(evidence_id for ids in client_evidence.values() for evidence_id in ids))
    evidence = tuple(dict(choices[evidence_id]) for evidence_id in (*repository_ids, *evidence_ids))

    if clients == ("android", "ios"):
        mode: Literal["android", "ios", "android_ios_shared", "harmony"] = "android_ios_shared"
    elif clients == ("android",):
        mode = "android"
    elif clients == ("ios",):
        mode = "ios"
    else:
        mode = "harmony"
    candidate_layers = {
        "android": ("shared_rn", "android_native"),
        "ios": ("shared_rn", "ios_native"),
        "android_ios_shared": ("shared_rn",),
        "harmony": ("sample_platform_repo", "harmony_native"),
    }[mode]
    reason = re.sub(r"\s+", " ", str(payload.get("reason") or "")).strip()[:500]
    return PlatformResolution(
        reported_clients=clients,
        repository_family=expected_family,
        primary_repository="sample_platform_repo" if expected_family == "harmony" else "sample_mobile_repo",
        investigation_mode=mode,
        client_scope_status="explicit",
        candidate_implementation_layers=candidate_layers,
        client_evidence=client_evidence,
        evidence=evidence,
        reason=reason,
        raw_response=response.text[:4_000],
        token_usage=token_usage,
        model_call_count=len(model_attempts),
        model_attempts=tuple(model_attempts),
    )


WorkflowNode = Callable[[BugWorkflowState], Awaitable[dict[str, Any]]]


def _public_bug_workflow_error(failure_kind: str, technical_error: str) -> str:
    """Return one Chinese, branded message without leaking engine internals."""
    if failure_kind == "platform_resolution_failed":
        return "复现端确认结果与禅道 module 冲突、证据不一致或格式无效；未启动源码调查"
    if failure_kind == "analysis_protocol_error":
        return "Bug Workbench 返回的分析结果格式不符合要求；本次源码调查已保留，可直接恢复审核，无需重新调查"
    if failure_kind == "analysis_execution_failed":
        return "Bug Workbench 主调查未能完成，请稍后重试"
    if failure_kind == "summary_execution_failed":
        return "Bug Workbench 已保留源码调查证据，但最终中文结论未能生成；可直接恢复总结，无需重新调查"
    if failure_kind == "post_analysis_interrupted":
        return "Bug Workbench 已完成并保留源码调查，但分析后收口被服务中断；可直接恢复总结，无需重新调查"
    if failure_kind in {"review_preflight_failed", "review_execution_failed"}:
        return "Bug Workbench 已保留主调查结果，但独立证据审核未能完成，可直接恢复审核"
    if failure_kind == "note_generation_failed":
        return "Bug Workbench 已完成并保留分析，但禅道备注未能生成；可直接重试备注，无需重新调查"
    if failure_kind == "note_write_failed":
        return "Bug Workbench 已完成并保留分析及备注内容，但禅道写入或回读未确认；可直接重试写入，无需重新调查"
    logger.debug("Hidden Bug workflow technical error: %s", technical_error)
    return "Bug Workbench 自动分析未能完成，请查看服务日志或稍后重试"


def _clean_note_section(text: str) -> str:
    """Preserve report structure while removing transport-only whitespace."""
    rendered: list[str] = []
    in_fence = False
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw_line.strip().startswith("```"):
            in_fence = not in_fence
            rendered.append(raw_line.strip())
            continue
        if in_fence:
            rendered.append(raw_line)
            continue
        line = re.sub(r"[ \t]+", " ", raw_line).rstrip()
        if line.strip():
            rendered.append(line.strip())
        elif rendered and rendered[-1]:
            rendered.append("")
    return "\n".join(rendered).strip()


def _zentao_note_from_report(report: str, source_evidence: Sequence[str]) -> str:
    """Deliver the complete source evidence and modification advice to ZenTao."""
    titles = (("三", "根本原因及源码证据"), ("四", "修改范围与其他端风险"))
    bodies = [_display_source_references(_clean_note_section(_heading_body(report, (title,), numbers=(number, str(index + 3)))), source_evidence) for index, (number, title) in enumerate(titles)]
    if not all(bodies):
        return ""
    header = "【自动分析备注｜待人工确认】\n\n"
    return header + "\n\n".join(f"{number}、{title}\n{body}" for (number, title), body in zip(titles, bodies, strict=True))


def _structured_zentao_note_fallback(state: Mapping[str, Any]) -> str:
    """Regenerate the deterministic note from the persisted Codex report."""
    source_evidence = tuple(str(item).strip() for item in (state.get("source_evidence") or ()) if str(item).strip())
    report = str(state.get("analysis_report") or state.get("handoff") or "")
    return _zentao_note_from_report(report, source_evidence)


def _heading_body(
    handoff: str,
    titles: tuple[str, ...],
    *,
    numbers: tuple[str, ...] = (),
) -> str:
    """Slice one Markdown/plain numbered section without interpreting it."""
    title_pattern = "|".join(re.escape(title) for title in titles)
    number_pattern = "|".join(re.escape(number) for number in numbers)
    numbered = rf"(?:(?:{number_pattern})\s*[、.．:：\-]?\s*)?" if numbers else ""
    match = re.search(
        rf"(?im)^\s*(?:#{{1,6}}\s*)?{numbered}(?:{title_pattern})\s*$",
        handoff,
    )
    if not match:
        return ""
    tail = handoff[match.end() :]
    # Plain ``1.`` / ``2.`` lines are commonly evidence lists inside a
    # conclusion.  Only Markdown headings or Chinese top-level section numbers
    # terminate a plain section; otherwise a numbered evidence list was
    # incorrectly truncated to its introductory sentence (Bug #82955).
    next_heading = re.search(r"(?im)^\s*(?:#{1,6}\s+|[一二三四五六七八九十]\s*[、.．]\s*\S)", tail)
    if next_heading:
        tail = tail[: next_heading.start()]
    return tail.strip()


def extract_note_projection(handoff: str) -> NoteProjection:
    """Project sections three and four from the persisted Codex report."""
    note = _zentao_note_from_report(handoff, ())
    return NoteProjection(note, use_full_report=not bool(note), warnings=(() if note else ("note_projection_unparsed",)))


def _zentao_note_quality_error(note: str, _source_evidence: Sequence[str]) -> str | None:
    """Validate the deterministic note without reinterpreting its evidence."""
    compact = re.sub(r"\s+", " ", note).strip()
    if len(compact) < 40:
        return "note_too_short"
    if "【自动分析备注｜待人工确认】" not in note:
        return "note_missing_banner"
    if "三、根本原因及源码证据" not in note:
        return "note_missing_root_cause_section"
    if "四、修改范围与其他端风险" not in note:
        return "note_missing_modification_section"
    if re.search(r"(?m)^\s*(?:一、分析结论|二、责任端与责任层)\s*$", note):
        return "note_contains_unrequested_sections"
    return None


def _build_preanalysis_product_clarification(triage: Mapping[str, Any]) -> dict[str, Any]:
    """Expose only a complete mutually exclusive product decision."""
    decision = triage.get("product_decision")
    if not isinstance(decision, Mapping):
        raise RuntimeError("产品分叉合同缺失")
    raw_options = decision.get("options")
    options = [dict(option) for option in raw_options if isinstance(option, Mapping)] if isinstance(raw_options, list) else []
    if not 2 <= len(options) <= 4:
        raise RuntimeError("产品分叉选项无效")
    return {
        "question": str(decision.get("question") or "请确认本次应实现的产品结果。"),
        "response_mode": "choice",
        "options": options,
        "allow_free_text": False,
        "allow_skip": False,
        "decision_reason": str(decision.get("reason") or ""),
        "decision_impact": str(decision.get("impact") or ""),
    }


def _scoped_copy_attachment_evidence(
    attachment_evidence: Mapping[str, Any] | None,
    confirmed_scope: str,
) -> dict[str, Any] | None:
    """Keep only explicitly numbered copy items for downstream source work."""
    if not isinstance(attachment_evidence, Mapping):
        return None
    visual = attachment_evidence.get("visual_evidence")
    items = visual.get("items") if isinstance(visual, Mapping) else None
    if not isinstance(items, list):
        return dict(attachment_evidence)
    modify_match = re.search(
        r"(?:修改|要改|只改)\s*[:：]?\s*(.+?)(?=(?:[；;]\s*)?(?:不改|排除|跨端|基准)\s*[:：]?|$)",
        confirmed_scope,
        re.IGNORECASE,
    )
    if modify_match is None:
        return dict(attachment_evidence)
    number_words = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    selected_indexes = {int(value) for value in re.findall(r"(?<!\d)(\d{1,2})(?!\d)", modify_match.group(1))}
    selected_indexes.update(number_words[value] for value in re.findall(r"第?([一二三四五六七八九十])(?:项|处|条)?", modify_match.group(1)) if value in number_words)
    if not selected_indexes:
        return dict(attachment_evidence)
    copy_items = [item for item in items if isinstance(item, Mapping) and (str(item.get("actual_visible_text") or item.get("actual_text") or "").strip() or str(item.get("expected_visible_text") or item.get("expected_text") or "").strip())]
    selected = [{**dict(item), "copy_item_id": f"copy_{index}"} for index, item in enumerate(copy_items, start=1) if index in selected_indexes]
    if not selected:
        return dict(attachment_evidence)
    selected_ids = {str(item.get("visual_item_id") or "") for item in selected}
    comparisons = [
        dict(comparison)
        for comparison in visual.get("comparisons", [])
        if isinstance(comparison, Mapping) and all(str(value) in selected_ids for value in comparison.get("actual_item_ids", [])) and all(str(value) in selected_ids for value in comparison.get("expected_item_ids", []))
    ]
    return {
        **dict(attachment_evidence),
        "visual_evidence": {**dict(visual), "items": [dict(item) for item in selected], "comparisons": comparisons},
    }


_VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".webm", ".m4v"})
_TEXT_EVIDENCE_SUFFIXES = frozenset({".log", ".txt", ".json", ".jsonl", ".csv", ".tsv", ".xml", ".yaml", ".yml"})


def _is_video_asset(asset: Mapping[str, Any]) -> bool:
    media_type = str(asset.get("media_type") or "").strip().lower()
    suffix = Path(str(asset.get("name") or "")).suffix.lower()
    return media_type.startswith("video/") or suffix in _VIDEO_SUFFIXES


def _partition_video_assets(assets: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split attachment metadata without downloading or inspecting video bytes."""
    non_video: list[dict[str, Any]] = []
    videos: list[dict[str, Any]] = []
    for asset in assets:
        normalized = dict(asset)
        (videos if _is_video_asset(asset) else non_video).append(normalized)
    return non_video, videos


def _has_effective_non_video_evidence(attachment_evidence: Mapping[str, Any] | None) -> bool:
    """Return whether processed images or readable text/logs can inform analysis."""
    if not isinstance(attachment_evidence, Mapping):
        return False
    visual = attachment_evidence.get("visual_evidence")
    if isinstance(visual, Mapping) and isinstance(visual.get("items"), list) and any(isinstance(item, Mapping) for item in visual["items"]):
        return True
    index = attachment_evidence.get("attachment_index")
    if not isinstance(index, list):
        return False
    for item in index:
        if not isinstance(item, Mapping):
            continue
        media_type = str(item.get("media_type") or "").lower()
        suffix = Path(str(item.get("name") or "")).suffix.lower()
        size = item.get("size")
        if (not isinstance(size, int) or size > 0) and (media_type.startswith("text/") or suffix in _TEXT_EVIDENCE_SUFFIXES):
            return True
    return False


def _merge_attachment_evidence(*parts: Mapping[str, Any] | None) -> dict[str, Any] | None:
    valid = [part for part in parts if isinstance(part, Mapping)]
    if not valid:
        return None
    assets: list[dict[str, Any]] = []
    attachment_index: list[dict[str, Any]] = []
    visual_items: list[dict[str, Any]] = []
    visual_comparisons: list[dict[str, Any]] = []
    visual_extractors: list[dict[str, Any]] = []
    visual_qualities: list[dict[str, Any]] = []
    visual_statuses: list[str] = []
    for part in valid:
        assets.extend(dict(item) for item in part.get("assets", []) if isinstance(item, Mapping))
        attachment_index.extend(dict(item) for item in part.get("attachment_index", []) if isinstance(item, Mapping))
        visual = part.get("visual_evidence")
        if isinstance(visual, Mapping):
            id_map: dict[str, str] = {}
            for item in visual.get("items", []) if isinstance(visual.get("items"), list) else []:
                if not isinstance(item, Mapping):
                    continue
                normalized_item = dict(item)
                old_id = str(normalized_item.get("visual_item_id") or f"visual_{len(id_map) + 1}")
                new_id = f"visual_{len(visual_items) + 1}"
                normalized_item["visual_item_id"] = new_id
                id_map[old_id] = new_id
                visual_items.append(normalized_item)
            for comparison in visual.get("comparisons", []) if isinstance(visual.get("comparisons"), list) else []:
                if not isinstance(comparison, Mapping):
                    continue
                actual_ids = [id_map[str(value)] for value in comparison.get("actual_item_ids", []) if str(value) in id_map]
                expected_ids = [id_map[str(value)] for value in comparison.get("expected_item_ids", []) if str(value) in id_map]
                if not actual_ids or not expected_ids:
                    continue
                visual_comparisons.append(
                    {
                        **dict(comparison),
                        "comparison_id": f"comparison_{len(visual_comparisons) + 1}",
                        "actual_item_ids": actual_ids,
                        "expected_item_ids": expected_ids,
                    }
                )
            if isinstance(visual.get("extractor"), Mapping):
                visual_extractors.append(dict(visual["extractor"]))
            visual_extractors.extend(dict(item) for item in visual.get("extractors", []) if isinstance(item, Mapping))
            if isinstance(visual.get("quality"), Mapping):
                visual_qualities.append(dict(visual["quality"]))
            visual_qualities.extend(dict(item) for item in visual.get("qualities", []) if isinstance(item, Mapping))
            if visual.get("status"):
                visual_statuses.append(str(visual["status"]))
    visual_status = "processed" if visual_items else (visual_statuses[-1] if visual_statuses else "not_run")
    visual_evidence: dict[str, Any] = {
        "schema_version": 2,
        "items": visual_items,
        "comparisons": visual_comparisons,
        "status": visual_status,
    }
    if visual_extractors:
        visual_evidence["extractors"] = visual_extractors
    if visual_qualities:
        visual_evidence["qualities"] = visual_qualities
    return {
        "assets": assets,
        "attachment_index": attachment_index,
        "visual_evidence": visual_evidence,
    }


def _skipped_video_evidence(videos: Sequence[Mapping[str, Any]], *, reason: str) -> dict[str, Any]:
    return {
        "assets": [
            {
                **{key: asset[key] for key in ("id", "name", "source", "media_type", "size") if key in asset},
                "status": "skipped",
                "skip_reason": reason,
            }
            for asset in videos
        ],
        "attachment_index": [],
        "visual_evidence": {"items": [], "status": "not_run"},
    }


def _build_video_clarification(videos: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "question": "视频是本工单唯一有效附件，是否下载并分析视频？",
        "response_mode": "choice",
        "options": [
            {"id": "analyze_video", "label": "分析视频", "value": "analyze"},
            {"id": "skip_video", "label": "跳过视频", "value": "skip"},
        ],
        "allow_free_text": False,
        "decision_reason": "未获得有效图片或可读日志；视频处理可能增加等待时间。",
        "decision_impact": "选择跳过后仍会基于工单文本和源码继续调查。",
        "video_assets": [str(item.get("name") or "视频附件") for item in videos],
    }


def _runtime_log_time_hints(snapshot: Mapping[str, Any]) -> list[str]:
    """Extract explicit ticket date/minute hints without inferring a time window."""
    # Reporter facts only: server timestamps and firmware versions are not
    # reproduction times. Unicode word boundaries fail next to Chinese text.
    text = "\n".join(str(snapshot.get(key) or "") for key in ("description", "steps", "actual", "expected", "title"))
    hints: list[str] = []
    for value in re.findall(r"(?<!\d)20\d{2}[-/]\d{1,2}[-/]\d{1,2}(?!\d)", text):
        year, month, day = re.split(r"[-/]", value)
        value = f"{year}-{int(month):02d}-{int(day):02d}"
        if value not in hints:
            hints.append(value)
    time_text = re.sub(r"(?<![\d.])\d+(?:\.\d+){2,}(?![\d.])", "", text)
    # A dot-separated minute is ambiguous with versions: only accept it after
    # an explicit testing/event time label. Colon times remain usable in prose.
    times = re.findall(r"(?:测试时间|复现时间|事件时间|时间)[^\n\d]{0,16}([01]?\d|2[0-3])\.([0-5]\d)", time_text)
    times.extend(re.findall(r"(?<![\d.:])([01]?\d|2[0-3])[:：]([0-5]\d)(?:[:：][0-5]\d)?(?![\d.:])", time_text))
    for hour, minute in times:
        compact = f"{int(hour):02d}:{minute}"
        if compact not in hints:
            hints.append(compact)
    return hints[:4]


def _ticket_runtime_signals(snapshot: Mapping[str, Any]) -> list[str]:
    """Extract only structured identifiers from ticket facts for the pre-scan."""
    text = "\n".join(str(value) for value in snapshot.values() if isinstance(value, (str, int)))
    patterns = (
        r"(?i)(?:设备\s*sn|device\s*(?:sn|serial)|serial\s*(?:number|no)?|\bsn\b)[^A-Za-z0-9]{0,12}([A-Za-z0-9][A-Za-z0-9-]{5,39})",
        r"(?<![A-Fa-f0-9])[A-Fa-f0-9]{8}(?:-[A-Fa-f0-9]{4}){3}-[A-Fa-f0-9]{12}(?![A-Fa-f0-9])",
        r"(?<![A-Fa-f0-9])(?:[A-Fa-f0-9]{2}:){5}[A-Fa-f0-9]{2}(?![A-Fa-f0-9])",
        r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9-]{9,39}(?![A-Za-z0-9])",
        r"(?<![A-Za-z0-9])/(?:[A-Za-z0-9_.-]+/){1,}[A-Za-z0-9_.?=&%-]+",
        r"(?<![A-Za-z0-9_])[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+(?![A-Za-z0-9_])",
        r"(?<![A-Za-z0-9_$])[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)+(?![A-Za-z0-9_$])",
        r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9]*(?:Request|Response|Error|Exception|Callback|Handler)(?![A-Za-z0-9_])",
    )
    signals: list[str] = []
    for pattern in patterns:
        for value in re.findall(pattern, text):
            if isinstance(value, tuple):
                value = next((part for part in value if part), "")
            value = str(value).strip()
            if not value:
                continue
            if len(value) >= 10 and value[0].isalpha() and "-" not in value and not (any(char.isalpha() for char in value) and any(char.isdigit() for char in value)):
                continue
            if value not in signals:
                signals.append(value)
            if len(signals) >= 16:
                return signals
    return signals


def _balanced_runtime_signals(
    *,
    observed: Sequence[str] = (),
    ticket: Sequence[str] = (),
    map_hints: Sequence[str] = (),
    limit: int = 16,
) -> list[str]:
    """Keep causal source findings dominant without losing independent hints."""
    generic_map_signal = re.compile(r"(?i)(?:^(?:this|that|super|state)\.|(?:^|\.)(?:setState|getInstance|addListener|removeListener|componentDidMount|componentWillUnmount|render|configNav)$)")
    map_hints = [value for value in map_hints if not generic_map_signal.search(str(value).strip())]
    pools = ((observed, 8), (ticket, 4), (map_hints, 4))
    selected: list[str] = []

    def add(values: Sequence[str], quota: int | None = None) -> None:
        added = 0
        for raw_value in values:
            value = str(raw_value).strip()
            if not value or value in selected:
                continue
            selected.append(value)
            added += 1
            if len(selected) >= limit or (quota is not None and added >= quota):
                return

    for values, quota in pools:
        add(values, quota)
        if len(selected) >= limit:
            return selected
    # Backfill unused quota in evidence priority order.
    for values, _quota in pools:
        add(values)
        if len(selected) >= limit:
            break
    return selected


def _codex_preparation_context(
    *,
    knowledge_context: InvestigationKnowledgeContext,
    attachment_evidence: Mapping[str, Any] | None,
    platform_resolution: Mapping[str, Any] | None,
    reported_clients: Sequence[str],
    repository: str,
    log_evidence: Mapping[str, Any],
    runtime_pre_scan: Mapping[str, Any],
    source_evidence: Mapping[str, Any],
    runtime_log_run: str,
    log_index_ready: bool,
) -> dict[str, Any]:
    """Project useful preparation into the first Codex turn; full data stays in audit."""
    visual = compact_visual_evidence(attachment_evidence)
    visual_items = []
    visual_used = 0
    all_visual_items = visual.get("items", []) if isinstance(visual.get("items"), list) else []
    priority_indexes: list[int] = []
    for keys in (("actual_text", "actual_visual"), ("expected_text", "expected_visual"), ("visual_difference", "mismatch_summary")):
        index = next((index for index, item in enumerate(all_visual_items) if isinstance(item, Mapping) and any(item.get(key) for key in keys)), None)
        if index is not None and index not in priority_indexes:
            priority_indexes.append(index)
    selected_indexes: list[int] = []
    for index in [*priority_indexes, *range(len(all_visual_items))]:
        if index in selected_indexes:
            continue
        item = all_visual_items[index]
        if not isinstance(item, Mapping):
            continue
        projected_visual = {
            key: item[key]
            for key in ("visual_item_id", "asset_role", "client", "user_path", "page", "event_or_action", "resource_key", "actual_text", "expected_text", "visual_difference", "confidence", "evidence_refs")
            if item.get(key) not in (None, "", [], {})
        }
        if not projected_visual.get("actual_text") and item.get("highlighted_content"):
            projected_visual["actual_text"] = item["highlighted_content"]
        if not projected_visual.get("expected_text") and item.get("expected_visual"):
            projected_visual["expected_text"] = item["expected_visual"]
        if not projected_visual.get("visual_difference"):
            visual_summary = item.get("mismatch_summary") or item.get("actual_visual")
            if visual_summary:
                projected_visual["visual_difference"] = visual_summary
        cost = len(json.dumps(projected_visual, ensure_ascii=False))
        if len(visual_items) >= 4 or visual_used + cost > 1_800:
            continue
        visual_items.append(projected_visual)
        selected_indexes.append(index)
        visual_used += cost
    visual_items = sorted(visual_items, key=lambda item: str(item.get("visual_item_id") or ""))
    visible_ids = {str(item.get("visual_item_id") or "") for item in visual_items}
    comparisons = [item for item in visual.get("comparisons", []) if isinstance(item, Mapping) and set(item.get("actual_item_ids") or ()) <= visible_ids and set(item.get("expected_item_ids") or ()) <= visible_ids][:2]
    visual_packet = {"items": visual_items, "comparisons": comparisons}
    if len(all_visual_items) > len(visual_items):
        visual_packet["omitted_items"] = len(all_visual_items) - len(visual_items)

    starting_points = []
    for point in list(source_evidence.get("entries") or [])[:5]:
        if not isinstance(point, Mapping):
            continue
        starting_points.append(
            {
                **{key: str(point[key])[:300] for key in ("path", "symbol", "confidence", "provenance") if point.get(key)},
                **({"snippet": str(point["snippet"])[:1_800]} if point.get("snippet") else {}),
                **({"retrieval_score": point["score"]} if point.get("score") is not None else {}),
                **{key: point[key] for key in ("entry_line", "view_range") if point.get(key) not in (None, "", [])},
            }
        )
    verified_source_navigation: dict[str, Any] = {
        "entries": starting_points,
        "semantics": "Tabby retrieval candidates revalidated against the current checkout; candidates are not causal proof",
        "provider": source_evidence.get("provider"),
        "status": source_evidence.get("status"),
    }
    architecture = source_evidence.get("architecture_resolution")
    if isinstance(architecture, Mapping) and architecture.get("status") == "ready":
        verified_source_navigation["architecture_resolution"] = {
            key: architecture[key]
            for key in ("model", "config_path", "match_basis", "modules")
            if architecture.get(key) not in (None, "", [], {})
        }

    def runtime_excerpt(item: Mapping[str, Any]) -> dict[str, Any]:
        projected = {key: str(item[key])[:420] for key in ("summary", "unproven", "status", "relevance", "identity_association") if item.get(key)}
        sources = []
        for source in item.get("sources", []) if isinstance(item.get("sources"), list) else []:
            if not isinstance(source, Mapping):
                continue
            excerpt = {key: source[key] for key in ("candidate_id", "source", "line_start", "line_end") if source.get(key) not in (None, "")}
            quote = str(source.get("quote") or "")
            if quote:
                excerpt["quote_excerpt"] = quote[:700]
                if len(quote) > 700:
                    excerpt["excerpt_only"] = True
            if excerpt:
                sources.append(excerpt)
            if len(sources) >= 1:
                break
        if sources:
            projected["sources"] = sources
        return projected

    direct = [runtime_excerpt(item) for item in runtime_pre_scan.get("transmitted_runtime_evidence", []) if isinstance(item, Mapping)][:2]
    candidates = [runtime_excerpt(item) for item in runtime_pre_scan.get("candidate_runtime_context", []) if isinstance(item, Mapping)][:1]
    business = knowledge_context.business_knowledge
    rules = [dict(item) for item in business.get("rules", []) if isinstance(item, Mapping) and item.get("applicability") in {"matched", "conditional_reference"}]
    selected_rules = []
    rule_used = 0
    for rule in rules:
        cost = len(json.dumps(rule, ensure_ascii=False))
        if len(selected_rules) >= 2 or rule_used + cost > 2_000:
            continue
        selected_rules.append(rule)
        rule_used += cost
    packet: dict[str, Any] = {
        "platform": {
            "reported_clients": list(reported_clients),
            "client_scope_status": (platform_resolution or {}).get("client_scope_status"),
            "investigation_mode": knowledge_context.investigation_mode(),
            "repository": repository,
        },
        "verified_source_navigation": verified_source_navigation,
    }
    if visual_items or comparisons:
        packet["visual_facts"] = visual_packet
    if selected_rules:
        packet["business_reference_only"] = {"semantics": "reference_only", "rules": selected_rules}
    if direct or candidates:
        packet["runtime_observations"] = {"accepted_evidence": direct, "candidate_context_unverified": candidates}
    if log_index_ready:
        packet["log_query"] = {
            "command": f"python .deerflow-log-query.py --run {runtime_log_run} --signal '<actual-source-field>'",
            "scope": "按实际读到的源码字段追加查询；结果仅为运行观察。",
        }
    return packet


def _build_runtime_pre_scan_audit(
    *,
    ticket_signals: Sequence[str],
    map_signals: Sequence[str],
    selected_signals: Sequence[str],
    material: Mapping[str, Any] | None,
    log_evidence: Mapping[str, Any] | None,
    not_run_reason: str,
) -> dict[str, Any]:
    """Persist log curation while keeping speculative matches out of navigation.

    Initial investigation may receive a curated log item only when one of its
    backend-restored quotes contains an identifier explicitly present in the
    ticket, or a same-parsed-device mapping binds that identifier to its alias.
    Both remain observation context and never steer first-source navigation.
    """
    material_payload = (
        dict(material)
        if isinstance(material, Mapping)
        else {
            "status": "not_run",
            "signals": [],
            "files_searched": 0,
            "reason": not_run_reason,
        }
    )
    expert_payload = dict(log_evidence) if isinstance(log_evidence, Mapping) else {}
    explicit_ticket_signals = tuple(str(item).strip() for item in ticket_signals if str(item).strip())
    accepted = expert_payload.get("accepted_evidence")
    transmitted: list[dict[str, Any]] = []
    for raw in accepted if isinstance(accepted, list) else ():
        if not isinstance(raw, Mapping) or str(raw.get("relevance") or "").lower() != "direct":
            continue
        restored_text = "\n".join(str(source.get("quote") or "") for source in raw.get("sources", []) if isinstance(source, Mapping)).casefold()
        if any(signal.casefold() in restored_text for signal in explicit_ticket_signals):
            transmitted.append(dict(raw))
        else:
            links = [
                link
                for link in material_payload.get("identity_links", [])
                if isinstance(link, Mapping) and str(link.get("identifier") or "").casefold() in {signal.casefold() for signal in explicit_ticket_signals} and any(str(alias).casefold() in restored_text for alias in link.get("aliases", []))
            ]
            if links:
                item = dict(raw)
                item["sources"] = [*raw.get("sources", []), *[{key: link[key] for key in ("source", "line_start", "line_end", "quote")} for link in links[:2]]]
                item["identity_association"] = "same_parsed_device_record; observation only, not causal proof"
                transmitted.append(item)
        if len(transmitted) >= 3:
            break
    candidate_context: list[dict[str, Any]] = []
    candidate_context_chars = 0
    selected_ids = {tuple(item.get("candidate_ids", [])) for item in transmitted}
    for item in expert_payload.get("selections", []):
        if not isinstance(item, Mapping) or item.get("relevance") not in {"direct", "supporting"}:
            continue
        if tuple(item.get("candidate_ids", [])) in selected_ids or not item.get("summary") or not item.get("proven"):
            continue
        sources = [{key: source.get(key) for key in ("candidate_id", "source", "line_start", "line_end", "quote")} for source in item.get("sources", [])[:2] if isinstance(source, Mapping) and source.get("quote")]
        if not sources:
            continue
        projected = {"status": "unverified_ticket_association", "summary": item["summary"], "unproven": item.get("unproven") or "设备/事务与本工单关联尚未证明", "sources": sources}
        cost = len(json.dumps(projected, ensure_ascii=False))
        if candidate_context_chars + cost > 6_000:
            continue
        candidate_context.append(projected)
        candidate_context_chars += cost
        if len(candidate_context) >= 2:
            break
    return {
        "rule_version": _RUNTIME_PRE_SCAN_RULE_VERSION,
        "collection_policy": "broad_provenance_preserving_material_then_model_curation",
        "transmission_policy": "explicit_ticket_identifier_or_same_parsed_device_identity_link; never navigation",
        "ticket_signals": [str(item) for item in ticket_signals],
        "map_signals": [str(item) for item in map_signals],
        "selected_signals": [str(item) for item in selected_signals],
        "material": material_payload,
        "log_expert": expert_payload,
        "transmitted_runtime_evidence": transmitted,
        "candidate_runtime_context": candidate_context,
        "runtime_navigation": {
            "status": "disabled",
            "reason": "runtime logs are evidence context, never first-source navigation",
        },
    }


def _copy_scope_item_indexes(text: str) -> set[int]:
    number_words = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    indexes = {int(value) for value in re.findall(r"(?<!\d)(\d{1,2})(?!\d)", text)}
    indexes.update(number_words[value] for value in re.findall(r"第?([一二三四五六七八九十])(?:项|处|条)?", text) if value in number_words)
    return indexes


def _build_confirmed_copy_scope(clarification: Mapping[str, Any], answer: str) -> dict[str, Any] | None:
    """Bind a copy-scope answer to the stable item ids already shown to the user."""
    if clarification.get("response_mode") != "copy_scope":
        return None
    raw_items = clarification.get("items")
    if not isinstance(raw_items, list):
        return None
    modify_match = re.search(
        r"(?:修改|要改|只改)\s*[:：]?\s*(.+?)(?=(?:[；;]\s*)?(?:不改|排除|跨端|基准)\s*[:：]?|$)",
        answer,
        re.IGNORECASE,
    )
    if modify_match is None:
        return None
    selected = _copy_scope_item_indexes(modify_match.group(1))
    excluded_match = re.search(
        r"(?:不改|排除)\s*[:：]?\s*(.+?)(?=(?:[；;]\s*)?(?:修改|要改|只改|跨端|基准)\s*[:：]?|$)",
        answer,
        re.IGNORECASE,
    )
    excluded = _copy_scope_item_indexes(excluded_match.group(1)) if excluded_match is not None else set()
    if "只改" in answer:
        excluded.update(index for index in range(1, len(raw_items) + 1) if index not in selected)
    selected.difference_update(excluded)
    if not selected:
        return None

    def target(index: int, raw: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "target_id": str(raw.get("id") or f"copy_{index}"),
            "observed_clients": [str(value) for value in raw.get("observed_clients", []) if str(value)] if isinstance(raw.get("observed_clients"), list) else [],
            "page": str(raw.get("page") or ""),
            "actual_text": str(raw.get("actual_text") or ""),
            "target_text": str(raw.get("expected_text") or ""),
        }

    indexed_items = [(index, raw) for index, raw in enumerate(raw_items, start=1) if isinstance(raw, Mapping)]
    allowed_targets = [target(index, raw) for index, raw in indexed_items if index in selected]
    excluded_targets = [target(index, raw) for index, raw in indexed_items if index in excluded]
    if not allowed_targets:
        return None
    return {
        "scope_type": "confirmed_multi_copy",
        "confirmed_instruction": answer.strip(),
        "allowed_targets": allowed_targets,
        "excluded_targets": excluded_targets,
    }


def build_bug_workflow(*, analyze: WorkflowNode, note: WorkflowNode):
    """Build controller routing -> specialist analysis -> one ZenTao note."""
    graph = StateGraph(BugWorkflowState)
    graph.add_node("analyze", analyze)
    graph.add_node("note", note)
    graph.add_edge(START, "analyze")
    graph.add_edge("analyze", "note")
    graph.add_edge("note", END)
    return graph.compile()


class _WorkflowRequest(SimpleNamespace):
    """Trusted internal request that keeps a background workflow user-scoped."""

    async def is_disconnected(self) -> bool:
        return False


class _ReadOnlyWorkflowStore:
    """In-memory transition sink used only by the Phoenix replay command."""

    def __init__(self, workflow_id: str, workflow: Mapping[str, Any]) -> None:
        self.workflow_id = workflow_id
        self.workflow = dict(workflow)

    async def get(self, thread_id: str, *, user_id: str) -> dict[str, Any] | None:
        if thread_id != self.workflow_id:
            return None
        return {"metadata": {"bug_workflow": dict(self.workflow)}}

    async def update_metadata(self, thread_id: str, metadata: dict[str, Any], *, user_id: str) -> None:
        workflow = metadata.get("bug_workflow")
        if thread_id == self.workflow_id and isinstance(workflow, Mapping):
            self.workflow.clear()
            self.workflow.update(workflow)

    async def update_status(self, thread_id: str, status: str, *, user_id: str) -> None:
        return None


def _workflow_request(app: Any, *, owner_user_id: str) -> _WorkflowRequest:
    return _WorkflowRequest(
        app=app,
        headers=create_internal_auth_headers(owner_user_id=owner_user_id),
        state=SimpleNamespace(
            user=get_internal_user(owner_user_id),
            auth_source=AUTH_SOURCE_INTERNAL,
        ),
        cookies={},
    )


async def _write_bug_note_once(
    *,
    bug_id: int,
    note_content: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    # ZenTao history, not older workflow metadata, is the authority for an
    # editable backup and for uncertain prior POST outcomes.
    client = ZentaoClient.from_environment()
    result = await client.save_analysis_note(bug_id, note_content)
    previous = {"note_content": result["note_content"]} if result.get("mode") == "reused" else None
    return result, previous


async def run_bug_workflow(
    *,
    app: Any,
    workflow_id: str,
    bug_id: int,
    owner_user_id: str,
    router_thread_id: str,
    analysis_thread_id: str,
    note_thread_id: str,
    router_run_id: str | None = None,
    clarification_answer: str | None = None,
    clarification_round: int = 0,
    bug_snapshot: dict[str, Any] | None = None,
    affected_clients: tuple[str, ...] | None = None,
    write_note_only: bool = False,
    read_only_replay_id: str | None = None,
    replay_workflow: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Preflight, triage, investigate, and persist a Bug workflow from existing facts."""
    request = _workflow_request(app, owner_user_id=owner_user_id)
    if read_only_replay_id:
        if not isinstance(replay_workflow, Mapping):
            raise ValueError("Phoenix replay requires the persisted workflow snapshot")
        thread_store: Any = _ReadOnlyWorkflowStore(workflow_id, replay_workflow)
        initial_workflow = dict(replay_workflow)
    else:
        thread_store = get_thread_store(request)
        record = await thread_store.get(workflow_id, user_id=owner_user_id) if hasattr(thread_store, "get") else None
        initial_workflow = dict(record.get("metadata", {}).get("bug_workflow", {})) if isinstance(record, dict) else {}
    runtime = BugWorkflowRuntime(
        store=thread_store,
        workflow_id=workflow_id,
        bug_id=bug_id,
        owner_user_id=owner_user_id,
        workflow=initial_workflow,
    )
    current_workflow = runtime.workflow
    phoenix = BugPhoenixTrace(workflow_id=workflow_id, bug_id=bug_id, replay_id=read_only_replay_id)
    phoenix.start(attributes={"workflow.read_only_replay": bool(read_only_replay_id)})

    def finish_phoenix(error: BaseException | None = None) -> None:
        usage = current_workflow.get("external_token_usage")
        phoenix.set_attributes(
            phoenix.root,
            {
                "workflow.status": current_workflow.get("status"),
                "workflow.model_call_count": current_workflow.get("external_model_call_count", 0),
                "workflow.token_usage": usage if isinstance(usage, Mapping) else {},
                "workflow.target_count": len(current_workflow.get("final_targets", [])) if isinstance(current_workflow.get("final_targets"), list) else 0,
            },
        )
        phoenix.finish(error=error)
        if read_only_replay_id:
            current_workflow["phoenix_flush_succeeded"] = phoenix.force_flush()

    async def is_cancelled() -> bool:
        return await runtime.is_cancelled()

    async def update(status: str, **details: Any) -> bool:
        return await runtime.transition(status, **details)

    bug: dict[str, Any] | None = None
    runtime_log_indices: list[Path] = []
    snapshot = compact_bug_snapshot(bug_snapshot or {})
    attachment_evidence = current_workflow.get("attachment_evidence")
    if not isinstance(attachment_evidence, dict):
        attachment_evidence = None
    saved_triage = current_workflow.get("triage")
    triage = (
        None
        if read_only_replay_id
        else (
            dict(saved_triage)
            if isinstance(saved_triage, Mapping)
            and isinstance(saved_triage.get("investigation_focus"), Mapping)
            and saved_triage.get("evidence_need") in {"visual_only", "include_technical"}
            and isinstance(saved_triage.get("direction"), str)
            and bool(saved_triage.get("direction", "").strip())
            else None
        )
    )
    pending_assets: list[dict[str, Any]] = []
    if not snapshot:
        try:
            # Do this before any model run. A failed/expired ZenTao token must
            # not spend analysis-agent tokens merely to discover an integration
            # error. A clarification resume intentionally reuses the specialist
            # thread and skips this read: it already has the Bug context.
            bug = await ZentaoClient.from_environment().get_bug(bug_id)
            snapshot = compact_bug_snapshot(bug)
            terminal_status = _terminal_zentao_status_label(bug)
            if terminal_status:
                completion_report = f"禅道 Bug #{bug_id} {terminal_status}，已直接停止；未下载或分析附件，未启动模型分析、写备注或修复。"
                await update(
                    "skipped",
                    router_thread_id=router_thread_id,
                    analysis_thread_id=analysis_thread_id,
                    note_thread_id=note_thread_id,
                    bug_snapshot=snapshot,
                    completion_report=completion_report,
                )
                finish_phoenix()
                return
            raw_assets = bug.get("evidence_assets")
            pending_assets = [item for item in raw_assets if isinstance(item, dict)] if isinstance(raw_assets, list) else []
        except ZentaoError as exc:
            logger.warning("ZenTao note write or verification failed for Bug %s: %s", bug_id, exc)
            await update(
                "failed",
                router_thread_id=router_thread_id,
                analysis_thread_id=analysis_thread_id,
                note_thread_id=note_thread_id,
                failure_kind="workflow_failed",
                error=f"ZenTao preflight failed: {exc}",
            )
            finish_phoenix(exc)
            return

    configured_analysis_engine = "codex"
    saved_platform_resolution = current_workflow.get("platform_resolution")
    platform_resolution_payload = dict(saved_platform_resolution) if isinstance(saved_platform_resolution, Mapping) else None
    platform_usage = dict(current_workflow.get("platform_token_usage") or {}) if isinstance(current_workflow.get("platform_token_usage"), Mapping) else {}
    platform_model_attempts = [dict(item) for item in current_workflow.get("platform_model_attempts", []) if isinstance(item, Mapping)] if isinstance(current_workflow.get("platform_model_attempts"), list) else []
    platform_model_name = str(current_workflow.get("platform_model") or "").strip() or None
    platform_model_call_count = int(current_workflow.get("platform_model_call_count") or 0)
    if platform_resolution_payload is None and not write_note_only and not read_only_replay_id:
        analysis_config = get_app_config().bug_analysis
        platform_model_name = analysis_config.platform.model_name or "gpt-5.6-sol"
        try:
            platform_resolution = await _run_platform_resolution(
                bug_id=bug_id,
                snapshot=snapshot,
                model_name=platform_model_name,
                max_output_tokens=analysis_config.platform.max_output_tokens,
                thread_id=workflow_id,
            )
        except Exception as exc:
            logger.warning("Bug platform resolution failed before source investigation", exc_info=True)
            if isinstance(exc, PlatformResolutionFormatError):
                platform_usage = dict(exc.token_usage)
                platform_model_attempts = [dict(item) for item in exc.attempts]
                platform_model_call_count = exc.model_call_count
            else:
                platform_model_call_count = 1
            await update(
                "failed",
                router_thread_id=router_thread_id,
                analysis_thread_id=analysis_thread_id,
                note_thread_id=note_thread_id,
                analysis_engine=configured_analysis_engine,
                bug_snapshot=snapshot,
                platform_model=platform_model_name,
                platform_model_call_count=platform_model_call_count,
                platform_token_usage=platform_usage,
                platform_model_attempts=platform_model_attempts,
                failure_kind="platform_resolution_failed",
                error=f"platform_resolution_failed: {exc}",
            )
            finish_phoenix(exc)
            return
        platform_resolution_payload = platform_resolution.payload()
        platform_usage = dict(platform_resolution.token_usage)
        platform_model_call_count = platform_resolution.model_call_count
        platform_model_attempts = [dict(item) for item in platform_resolution.model_attempts]
        await update(
            "routing",
            router_thread_id=router_thread_id,
            analysis_thread_id=analysis_thread_id,
            note_thread_id=note_thread_id,
            analysis_engine=configured_analysis_engine,
            bug_snapshot=snapshot,
            platform_resolution=platform_resolution_payload,
            platform_model=platform_model_name,
            platform_model_call_count=platform_model_call_count,
            platform_token_usage=platform_usage,
            platform_model_attempts=platform_model_attempts,
            reported_clients=list(platform_resolution.reported_clients),
            affected_clients=list(platform_resolution.reported_clients),
        )
    resolved_platform_clients = tuple(str(value) for value in (platform_resolution_payload or {}).get("reported_clients", []) if str(value) in {"android", "ios", "harmony"})
    if not resolved_platform_clients and platform_resolution_payload is None:
        # Historical note-only/replay records predate the explicit platform
        # contract and must remain readable without launching a new model call.
        resolved_platform_clients = detect_reported_clients(snapshot)

    if triage is None:
        triage_assets = pending_assets
        if not triage_assets and isinstance(attachment_evidence, Mapping):
            existing_assets = attachment_evidence.get("assets")
            triage_assets = [dict(item) for item in existing_assets if isinstance(item, Mapping)] if isinstance(existing_assets, list) else []
        triage_result = unknown_triage(snapshot, "新主链不在调查前用模型分类或拆分工单；完整事实直接交给主调查会话。")
        triage_details = {
            "triage_model": None,
            "triage_model_call_count": 0,
            "triage_token_usage": {},
            "triage_error": None,
        }
        triage = triage_result.payload()
        triage["observed_clients"] = list(resolved_platform_clients)
        current_workflow["triage"] = triage
        current_workflow.update(triage_details)
        if not write_note_only:
            await update(
                "routing",
                router_thread_id=router_thread_id,
                analysis_thread_id=analysis_thread_id,
                note_thread_id=note_thread_id,
                analysis_engine=configured_analysis_engine,
                triage=triage,
                route_reason=triage["direction"],
                platform_resolution=platform_resolution_payload,
                platform_model=platform_model_name,
                platform_model_call_count=platform_model_call_count,
                platform_token_usage=platform_usage,
                reported_clients=list(resolved_platform_clients),
                affected_clients=list(resolved_platform_clients),
                **triage_details,
            )
    selected_assets = select_relevant_assets(pending_assets, triage) if pending_assets else []
    non_video_assets, video_assets = _partition_video_assets(selected_assets)
    saved_video_assets = current_workflow.get("pending_video_assets")
    if not video_assets and isinstance(saved_video_assets, list):
        video_assets = [dict(item) for item in saved_video_assets if isinstance(item, Mapping)]
    saved_non_video_evidence = current_workflow.get("non_video_attachment_evidence")
    if video_assets and isinstance(saved_non_video_evidence, Mapping):
        attachment_evidence = dict(saved_non_video_evidence)
    elif video_assets and "non_video_attachment_evidence" in current_workflow:
        attachment_evidence = None

    async def collect_assets(
        assets: list[dict[str, Any]],
        *,
        base: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        collected: dict[str, Any] | None = None

        async def report_attachment_progress(states: list[dict[str, Any]]) -> None:
            nonlocal collected
            progress = {"assets": states, "attachment_index": [], "visual_evidence": {"items": [], "status": "not_run"}}
            collected = _merge_attachment_evidence(base, progress)
            await update(
                "routing",
                router_thread_id=router_thread_id,
                analysis_thread_id=analysis_thread_id,
                note_thread_id=note_thread_id,
                triage=triage,
                attachment_evidence=collected,
            )

        try:
            return await collect_bug_attachment_evidence(
                ZentaoClient.from_environment(),
                bug_id,
                assets,
                ensure_uploads_dir(analysis_thread_id, user_id=owner_user_id),
                on_progress=report_attachment_progress,
            )
        except Exception:
            logger.exception("ZenTao attachment evidence collection failed for Bug %s", bug_id)
            return {
                "assets": [
                    {
                        "id": str(item.get("id") or ""),
                        "name": str(item.get("name") or "附件"),
                        "source": str(item.get("source") or "attachment"),
                        "media_type": str(item.get("media_type") or "application/octet-stream"),
                        "status": "failed",
                        "error": "下载失败",
                    }
                    for item in assets
                ],
                "attachment_index": [],
                "visual_evidence": {"items": [], "status": "failed"},
            }

    if non_video_assets and attachment_evidence is None:
        attachment_evidence = await collect_assets(non_video_assets)
        current_workflow["non_video_attachment_evidence"] = attachment_evidence

    video_analysis_decision = str(current_workflow.get("video_analysis_decision") or "")
    if video_assets:
        if _has_effective_non_video_evidence(attachment_evidence):
            attachment_evidence = _merge_attachment_evidence(
                attachment_evidence,
                _skipped_video_evidence(video_assets, reason="已有有效图片或日志，默认不分析视频"),
            )
            current_workflow["video_analysis_decision"] = "not_needed"
            current_workflow["pending_video_assets"] = []
        elif video_analysis_decision == "analyze":
            video_evidence = await collect_assets(video_assets, base=attachment_evidence)
            attachment_evidence = _merge_attachment_evidence(attachment_evidence, video_evidence)
            current_workflow["pending_video_assets"] = []
        elif video_analysis_decision == "skip":
            attachment_evidence = _merge_attachment_evidence(
                attachment_evidence,
                _skipped_video_evidence(video_assets, reason="用户选择不分析视频"),
            )
            current_workflow["pending_video_assets"] = []
        elif not write_note_only:
            waiting_video = {
                "assets": [
                    {
                        **{key: asset[key] for key in ("id", "name", "source", "media_type", "size") if key in asset},
                        "status": "awaiting_confirmation",
                    }
                    for asset in video_assets
                ],
                "attachment_index": [],
                "visual_evidence": {"items": [], "status": "not_run"},
            }
            public_evidence = _merge_attachment_evidence(attachment_evidence, waiting_video)
            await update(
                "awaiting_clarification",
                router_thread_id=router_thread_id,
                router_run_id=router_run_id,
                analysis_thread_id=analysis_thread_id,
                analysis_run_id=None,
                note_thread_id=note_thread_id,
                route="investigation",
                route_reason="视频是唯一有效附件，需由用户决定是否承担额外处理时间。",
                specialist_agent=configured_analysis_engine,
                analysis_engine=configured_analysis_engine,
                triage=triage,
                bug_snapshot=snapshot,
                affected_clients=list(resolved_platform_clients),
                reported_clients=list(resolved_platform_clients),
                platform_resolution=platform_resolution_payload,
                platform_model=platform_model_name,
                platform_model_call_count=platform_model_call_count,
                platform_token_usage=platform_usage,
                attachment_evidence=public_evidence,
                non_video_attachment_evidence=attachment_evidence,
                pending_video_assets=video_assets,
                clarification=_build_video_clarification(video_assets),
                clarification_type="video",
                clarification_stage="pre_analysis",
                clarification_round=clarification_round,
                awaiting_clarification=True,
                analysis_summary="视频尚未下载、抽帧或交给模型；源码调查尚未开始。",
                external_conversation_id=None,
            )
            finish_phoenix()
            return {key: value for key, value in current_workflow.items() if key in BugWorkflowState.__annotations__}
    if attachment_evidence:
        current_workflow["attachment_evidence"] = attachment_evidence
    reported_clients = resolved_platform_clients
    triage = apply_visual_copy_evidence(
        triage,
        attachment_evidence,
        harmony_reported=False,
    )
    triage["observed_clients"] = list(reported_clients)
    current_workflow["triage"] = triage
    clients = reported_clients
    current_workflow.update(
        {
            "bug_snapshot": snapshot,
            "affected_clients": list(clients),
            "reported_clients": list(reported_clients),
            "platform_resolution": platform_resolution_payload,
            "platform_model": platform_model_name,
            "platform_model_call_count": platform_model_call_count,
            "platform_token_usage": platform_usage,
        }
    )
    if attachment_evidence:
        current_workflow["attachment_evidence"] = attachment_evidence

    analysis_engine = "codex"
    assert triage is not None
    if not write_note_only and not current_workflow.get("product_clarification_completed") and not clarification_answer and isinstance(triage.get("product_decision"), Mapping):
        clarification = _build_preanalysis_product_clarification(triage)
        await update(
            "awaiting_clarification",
            router_thread_id=router_thread_id,
            router_run_id=router_run_id,
            analysis_thread_id=analysis_thread_id,
            analysis_run_id=None,
            note_thread_id=note_thread_id,
            route="investigation",
            route_reason="工单同时支持多个会改变业务行为或修改范围的互斥产品结果，需在源码调查前选择。",
            specialist_agent=analysis_engine,
            analysis_engine=analysis_engine,
            triage=triage,
            clarification=clarification,
            clarification_type="product",
            clarification_stage="pre_analysis",
            clarification_round=clarification_round,
            awaiting_clarification=True,
            analysis_summary="存在真实产品分叉；尚未启动源码调查、地图查询、Specs 查询或 Bug Workbench 主调查会话。",
            external_conversation_id=None,
        )
        finish_phoenix()
        return

    async def analyze_with_preparation() -> dict[str, Any]:
        """Prepare shared facts, then use the configured read-only investigator."""
        app_config = get_app_config()
        analysis_config = app_config.bug_analysis
        summary_config = analysis_config.summary
        summary_model_name = f"codex/{summary_config.codex_model_name}"
        await require_codex_runtime(codex_bin=summary_config.codex_bin)
        preanalysis_product_decision = bool(clarification_answer and current_workflow.get("clarification_stage") == "pre_analysis")
        confirmed_copy_scope = current_workflow.get("confirmed_copy_scope")
        preanalysis_copy_scope = preanalysis_product_decision and isinstance(confirmed_copy_scope, Mapping)
        investigation_attachment_evidence = _scoped_copy_attachment_evidence(attachment_evidence, clarification_answer or "") if preanalysis_copy_scope else attachment_evidence
        investigation_snapshot = _analysis_bug_snapshot(snapshot)
        investigation_snapshot["preliminary_triage"] = triage
        if platform_resolution_payload is not None:
            investigation_snapshot["confirmed_platform"] = {key: value for key, value in platform_resolution_payload.items() if key != "raw_response"}
        if preanalysis_product_decision:
            investigation_snapshot["confirmed_product_decision"] = clarification_answer
        if preanalysis_copy_scope:
            # The human-confirmed scope replaces the often convoluted copy
            # prose for source search. Identity fields remain available.
            for field in ("description", "steps", "actual", "expected"):
                investigation_snapshot.pop(field, None)
            investigation_snapshot["confirmed_product_scope"] = clarification_answer
            investigation_snapshot["confirmed_copy_scope"] = dict(confirmed_copy_scope)
        codex_ticket_snapshot = _codex_ticket_snapshot(investigation_snapshot)
        await update(
            "analyzing",
            router_thread_id=router_thread_id,
            analysis_thread_id=analysis_thread_id,
            note_thread_id=note_thread_id,
            route="investigation",
            route_reason="DeerFlow 提供工单、附件、日志和可忽略的源码入口线索；调查者自行验证。",
            specialist_agent=analysis_engine,
            analysis_engine=analysis_engine,
            external_execution_status="connecting",
        )
        observed_investigation_clients = reported_clients
        primary_repository = str((platform_resolution_payload or {}).get("primary_repository") or "")
        if primary_repository not in {"sample_mobile_repo", "sample_platform_repo"}:
            primary_repository = "sample_platform_repo" if "harmony" in reported_clients else "sample_mobile_repo"
        knowledge_root = f"/mnt/repos/{primary_repository}"
        repository_root = _configured_host_mount(app_config, knowledge_root)
        shared_rn_repository_root = _configured_host_mount(app_config, "/mnt/repos/sample_mobile_repo") if primary_repository == "sample_platform_repo" else None
        with phoenix.span(
            "bug_workbench.knowledge_recall",
            attributes={
                "repository.name": primary_repository,
                "knowledge.input": {
                    "bug_snapshot": investigation_snapshot,
                    "observed_clients": observed_investigation_clients,
                    "reported_clients": reported_clients,
                },
            },
        ) as knowledge_span:
            knowledge_context = await asyncio.to_thread(
                build_investigation_knowledge_context,
                bug_snapshot=investigation_snapshot,
                attachment_evidence=investigation_attachment_evidence,
                repository_root=repository_root,
                shared_rn_repository_root=shared_rn_repository_root,
                observed_clients=observed_investigation_clients,
                reported_clients=reported_clients,
                investigation_mode=str((platform_resolution_payload or {}).get("investigation_mode") or ""),
                client_scope_status=str((platform_resolution_payload or {}).get("client_scope_status") or ""),
                confirmed_product_scope=clarification_answer if preanalysis_copy_scope else None,
            )
            phoenix.set_attributes(
                knowledge_span,
                {
                    "knowledge.output": knowledge_context.payload(),
                    "knowledge.fixed_entry_count": len(knowledge_context.compact_payload().get("recommended_starting_points", [])),
                },
            )
        source_view_root = Path(summary_config.source_view_root).expanduser().resolve()
        source_roots = knowledge_context.allowed_repository_roots
        for container_root in source_roots:
            source_mount = _configured_host_mount(app_config, container_root)
            if source_mount is None:
                raise RuntimeError(f"analysis_execution_failed: source mount is unavailable for {container_root}")
            await asyncio.to_thread(
                materialize_bug_source_view,
                source_mount,
                source_view_root / PurePosixPath(container_root).name,
                repository_name=PurePosixPath(container_root).name,
            )
        knowledge_context = replace(
            knowledge_context,
            business_knowledge=await asyncio.to_thread(
                recall_business_rules,
                analysis_config.business_knowledge_file,
                investigation_snapshot,
            ),
        )
        ticket_runtime_signals = _ticket_runtime_signals(investigation_snapshot)
        seed_runtime_signals = _balanced_runtime_signals(
            ticket=ticket_runtime_signals,
            map_hints=(),
        )
        business_signals = [signal for rule in knowledge_context.business_knowledge.get("rules", []) if rule.get("applicability") == "matched" for signal in rule.get("runtime_signals", [])]
        seed_runtime_signals = list(dict.fromkeys([*seed_runtime_signals, *business_signals]))[:24]
        log_expert_config = analysis_config.log_expert
        log_expert_model_name = log_expert_config.model_name or "gpt-5.6-sol"
        runtime_log_run = uuid.uuid4().hex
        runtime_log_index = source_view_root / PurePosixPath(source_roots[0]).name / LOG_CACHE_DIRECTORY / runtime_log_run / "logs.sqlite"
        runtime_log_indices.append(runtime_log_index)
        runtime_log_material: dict[str, Any] = {
            "status": "not_run",
            "files_searched": 0,
            "signals": seed_runtime_signals,
            "time_hints": _runtime_log_time_hints(snapshot),
            "candidates": [],
            "material_chars": 0,
        }
        log_evidence: dict[str, Any] = {
            "status": "not_run",
            "model": log_expert_model_name,
            "model_call_count": 0,
            "token_usage": {},
            "selections": [],
            "accepted_evidence": [],
            "text": "",
        }
        if triage.get("evidence_need") == "include_technical" and investigation_attachment_evidence:
            runtime_log_material = await asyncio.to_thread(
                collect_log_material,
                ensure_uploads_dir(analysis_thread_id, user_id=owner_user_id),
                investigation_attachment_evidence,
                signals=seed_runtime_signals,
                identity_signals=ticket_runtime_signals,
                time_hints=_runtime_log_time_hints(snapshot),
                max_chars=log_expert_config.max_material_chars,
                index_path=runtime_log_index,
            )
            if runtime_log_material.get("status") == "collected":
                try:
                    log_evidence = await run_log_expert(
                        bug_id=bug_id,
                        bug_snapshot=investigation_snapshot,
                        code_signals=seed_runtime_signals,
                        material=runtime_log_material,
                        app_config=app_config,
                        model_name=log_expert_model_name,
                        max_output_tokens=log_expert_config.max_output_tokens,
                        max_items=log_expert_config.max_evidence_items,
                        thinking_enabled=log_expert_config.thinking_enabled,
                        thread_id=workflow_id,
                    )
                except Exception as exc:
                    logger.warning("Runtime log expert failed before Codex; continuing without log evidence", exc_info=True)
                    log_evidence = {
                        "status": "model_failed",
                        "model": log_expert_model_name,
                        "model_call_count": 1,
                        "token_usage": {},
                        "error": f"{type(exc).__name__}: {exc}",
                        "files_searched": int(runtime_log_material.get("files_searched") or 0),
                        "material_chars": int(runtime_log_material.get("material_chars") or 0),
                        "selections": [],
                        "accepted_evidence": [],
                        "text": "",
                    }
        log_evidence = {
            **log_evidence,
            "scan_diagnostics": runtime_log_material.get("scan_diagnostics", []),
            "scan_partial": bool(runtime_log_material.get("scan_partial")),
            "files_searched": int(runtime_log_material.get("files_searched") or 0),
        }
        if runtime_log_material.get("status") == "read_failed":
            log_evidence["status"] = "read_failed"
        runtime_pre_scan = _build_runtime_pre_scan_audit(
            ticket_signals=ticket_runtime_signals,
            map_signals=(),
            selected_signals=seed_runtime_signals,
            material=runtime_log_material,
            log_evidence=log_evidence,
            not_run_reason=("log_material_not_collected" if runtime_log_material.get("status") != "collected" else ""),
        )
        log_evidence["candidate_runtime_context"] = runtime_pre_scan["candidate_runtime_context"]
        source_evidence = await asyncio.to_thread(
            build_source_retrieval,
            repository_root=repository_root,
            repository=primary_repository,
            bug_snapshot=investigation_snapshot,
            query_facts=knowledge_context.query_facts,
            runtime_evidence={"log_evidence": log_evidence, "runtime_pre_scan": runtime_pre_scan},
            config=analysis_config.source_retrieval,
            retrieval_aliases=matched_retrieval_aliases(knowledge_context.business_knowledge),
        )
        knowledge_payload = knowledge_context.payload()
        knowledge_payload["current_source_evidence"] = source_evidence
        prepared_code_trace = [
            {
                "engine": "tabby",
                "status": "matched",
                "relation": "repository_context_candidate",
                "anchor": item.get("symbol"),
                "candidates": [
                    {"path": item.get("path"), "line": item.get("entry_line")},
                ],
            }
            for item in source_evidence.get("entries", [])
            if isinstance(item, Mapping)
        ][:5]
        await update(
            "analyzing",
            router_thread_id=router_thread_id,
            analysis_thread_id=analysis_thread_id,
            note_thread_id=note_thread_id,
            route="investigation",
            route_reason=(
                "工单、附件与日志事实已通过 Tabby 仓库上下文检索，并按当前检出源码重新校验。"
                if source_evidence.get("status") == "ready"
                else "前置检索没有形成当前源码候选；Codex 将直接从规范事实包调查。"
            ),
            specialist_agent=analysis_engine,
            analysis_engine=analysis_engine,
            reported_clients=list(reported_clients),
            affected_clients=list(clients),
            investigation_knowledge_context=knowledge_payload,
            external_execution_status="prepared",
            runtime_pre_scan=runtime_pre_scan,
            runtime_log_evidence=log_evidence,
            code_intelligence_trace=prepared_code_trace,
            source_retrieval=source_evidence,
        )
        if await is_cancelled():
            return {"cancelled": True}
        prepared_context = _codex_preparation_context(
            knowledge_context=knowledge_context,
            attachment_evidence=investigation_attachment_evidence,
            platform_resolution=platform_resolution_payload,
            reported_clients=reported_clients,
            repository=primary_repository,
            log_evidence=log_evidence,
            runtime_pre_scan=runtime_pre_scan,
            source_evidence=source_evidence,
            runtime_log_run=runtime_log_run,
            log_index_ready=runtime_log_material.get("index_status") == "ready",
        )
        navigation_section = prepared_context.get("verified_source_navigation") if isinstance(prepared_context.get("verified_source_navigation"), Mapping) else {}
        transmitted_navigation = [dict(item) for item in navigation_section.get("entries", []) if isinstance(item, Mapping)]
        navigation_rejections = []
        if not transmitted_navigation:
            navigation_rejections.append(
                {
                    "reason": "no_current_ticket_specific_source_entry",
                    "retrieval_status": source_evidence.get("status"),
                    "retrieval_reason": source_evidence.get("reason"),
                }
            )
        await update(
            "analyzing",
            navigation_packet=transmitted_navigation,
            navigation_rejections=navigation_rejections,
            route_reason=("已向 Codex 提供 Tabby 检索且经当前源码校验的候选入口。" if transmitted_navigation else "当前准备阶段没有形成可信候选入口，Codex 将从工单事实直接窄查。"),
        )
        latest_codex_progress: dict[str, Any] = {}

        async def persist_codex_progress(progress: dict[str, Any]) -> None:
            nonlocal latest_codex_progress
            if current_workflow.get("status") in {"failed", "cancelled", "note_written", "analysis_incomplete"}:
                return
            if progress == latest_codex_progress:
                return
            latest_codex_progress = dict(progress)
            await update(
                "analyzing",
                router_thread_id=router_thread_id,
                analysis_thread_id=analysis_thread_id,
                note_thread_id=note_thread_id,
                route="investigation",
                route_reason="同一只读 Codex 会话正在调查源码，完成后直接提交四段报告。",
                specialist_agent="codex",
                analysis_engine="codex",
                codex_thread_id=progress.get("thread_id"),
                analysis_run_id=progress.get("turn_id"),
                external_execution_status=str(progress.get("phase") or "investigating"),
                codex_progress=progress,
                code_intelligence_trace=[
                    *prepared_code_trace,
                    *[dict(item) for item in progress.get("code_intelligence_trace", []) if isinstance(item, Mapping)],
                ][-24:],
                analysis_stage="investigation" if progress.get("phase") != "report_completed" else "completed",
            )

        with phoenix.span(
            "bug_workbench.codex_investigation",
            attributes={
                "model.name": f"codex/{summary_config.codex_model_name}",
                "repository.name": primary_repository,
                "prepared_context": prepared_context,
            },
        ) as codex_span:
            try:
                conclusion = await investigate_bug_with_codex(
                    bug_id=bug_id,
                    repository=primary_repository,
                    source_view_root=source_view_root,
                    model_name=summary_config.codex_model_name,
                    reasoning_effort=summary_config.investigation_reasoning_effort,
                    codex_bin=summary_config.codex_bin,
                    bug_snapshot=codex_ticket_snapshot,
                    prepared_context=prepared_context,
                    timeout_seconds=summary_config.investigation_timeout_seconds,
                    progress_callback=persist_codex_progress,
                )
            except Exception as exc:
                raise RuntimeError(f"analysis_execution_failed: {exc}") from exc
            phoenix.set_attributes(
                codex_span,
                {
                    "codex.thread_id": conclusion.thread_id,
                    "codex.turn_id": conclusion.handoff_manifest.get("codex_turn_id"),
                    "codex.token_usage": conclusion.token_usage,
                    "codex.report": conclusion.report,
                },
            )
        if await is_cancelled():
            return {"cancelled": True}
        report = conclusion.report
        note_projection = extract_note_projection(report)
        format_attempts = int(conclusion.handoff_manifest.get("format_attempts") or 1)
        triage_usage = current_workflow.get("triage_token_usage") if isinstance(current_workflow.get("triage_token_usage"), Mapping) else {}
        log_usage = log_evidence.get("token_usage") if isinstance(log_evidence.get("token_usage"), Mapping) else {}
        input_tokens = sum(int(item.get("input_tokens") or 0) for item in (triage_usage, platform_usage, log_usage, conclusion.token_usage))
        output_tokens = sum(int(item.get("output_tokens") or 0) for item in (triage_usage, platform_usage, log_usage, conclusion.token_usage))
        all_usage = {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "reasoning_tokens": 0,
        }
        details: dict[str, Any] = {
            "router_thread_id": router_thread_id,
            "analysis_thread_id": analysis_thread_id,
            "analysis_run_id": conclusion.handoff_manifest.get("codex_turn_id"),
            "note_thread_id": note_thread_id,
            "handoff": report,
            "analysis_report": report,
            "note_projection": note_projection.text,
            "handoff_state": "analysis_complete",
            "final_targets": [],
            "source_evidence": [],
            "investigation_source_evidence": [],
            "investigation_missing_evidence": [],
            "investigation_phase_closure": report,
            "route": "investigation",
            "route_reason": "同一只读 Codex 会话完成源码调查和四段报告。",
            "specialist_agent": "codex",
            "analysis_engine": "codex",
            # Keep the full source-preparation audit persisted after the final
            # report; only `_codex_preparation_context` is model-facing.
            "investigation_knowledge_context": knowledge_payload,
            "runtime_pre_scan": runtime_pre_scan,
            "runtime_log_evidence": log_evidence,
            "summary_raw_responses": [report],
            "summary_validation_errors": [],
            "summary_model": summary_model_name,
            "codex_thread_id": conclusion.thread_id,
            "codex_progress": latest_codex_progress,
            "summary_model_call_count": format_attempts,
            "summary_token_usage": dict(conclusion.token_usage),
            "summary_model_attempts": [],
            "summary_handoff_manifest": dict(conclusion.handoff_manifest),
            "summary_fallback_reason": "",
            "external_conversation_id": None,
            "external_conversations": {},
            "external_execution_status": "finished",
            "external_model_call_count": (int(current_workflow.get("triage_model_call_count") or 0) + platform_model_call_count + int(log_evidence.get("model_call_count") or 0) + format_attempts),
            "external_token_usage": all_usage,
            "external_models": list(
                dict.fromkeys(
                    filter(
                        None,
                        (
                            current_workflow.get("triage_model"),
                            platform_model_name,
                            log_expert_model_name,
                            summary_model_name,
                        ),
                    )
                )
            ),
            "log_expert_model": log_expert_model_name,
            "log_expert_model_call_count": int(log_evidence.get("model_call_count") or 0),
            "log_expert_token_usage": dict(log_usage),
            "analysis_stage": "completed",
            "analysis_stage_started_at": None,
        }
        await update("writing_note", **details)
        return details

    async def analyze(_state: BugWorkflowState) -> dict[str, Any]:
        if write_note_only:
            return {key: value for key, value in current_workflow.items() if key in BugWorkflowState.__annotations__}
        return await analyze_with_preparation()

    async def note(state: BugWorkflowState) -> dict[str, Any]:
        if read_only_replay_id:
            return {}
        if state.get("cancelled") or state.get("analysis_incomplete") or state.get("awaiting_clarification") or await is_cancelled():
            return {}
        note_source = state.get("handoff") or state.get("analysis_report", "")
        source_evidence = tuple(state.get("source_evidence") or ())
        note_run_id: str | None
        reuse_verified_note = current_workflow.get("note_verified") is True and isinstance(current_workflow.get("note_content"), str) and bool(current_workflow.get("note_content"))
        pending_note = current_workflow.get("note_content")
        reuse_pending_note = not reuse_verified_note and isinstance(pending_note, str) and bool(pending_note.strip()) and _zentao_note_quality_error(pending_note, source_evidence) is None
        if reuse_verified_note:
            # Summary recovery must never duplicate an already verified
            # ZenTao note. Keep the exact persisted payload and continue with
            # analysis/advice completion path; never start a repair.
            note_run_id = current_workflow.get("note_run_id")
            note_content = str(current_workflow["note_content"])
        elif reuse_pending_note:
            # A write/readback retry reuses the exact already validated note.
            # It must not spend another model call or rewrite the analysis.
            note_run_id = current_workflow.get("note_run_id")
            note_content = str(current_workflow["note_content"])
        else:
            note_run_id = None
            note_content = _zentao_note_from_report(str(state.get("analysis_report") or note_source), source_evidence)
            note_quality_error = _zentao_note_quality_error(note_content, source_evidence) if note_content else "note_section_unavailable"
            if note_quality_error:
                note_content = ""
            if not note_content:
                # The only fallback is assembled from already persisted
                # structured findings; it never starts another model run.
                note_content = _structured_zentao_note_fallback(state)
                if note_content and _zentao_note_quality_error(note_content, source_evidence):
                    note_content = ""
                logger.warning("ZenTao report section unavailable; using structured fallback: %s", note_quality_error)
        if await is_cancelled():
            return {"note_run_id": note_run_id, "cancelled": True}
        if not note_content:
            await update(
                "failed",
                router_thread_id=router_thread_id,
                router_run_id=state.get("router_run_id"),
                analysis_thread_id=analysis_thread_id,
                analysis_run_id=state.get("analysis_run_id"),
                note_thread_id=note_thread_id,
                note_run_id=note_run_id,
                handoff=state.get("handoff", ""),
                route=state.get("route"),
                route_reason=state.get("route_reason"),
                specialist_agent=state.get("specialist_agent"),
                failure_kind="note_generation_failed",
                event_type="note_failed",
                summary="分析已完成并保留，但未能生成可写入的禅道备注",
                error="完整报告第三部分与结构化兜底均未形成有效备注，未写入禅道。",
            )
            return {
                "note_run_id": note_run_id,
            }
        duplicate_note: dict[str, Any] | None = None
        try:
            if reuse_verified_note:
                result = {"saved": True, "verified": True, "mode": "reused"}
            else:
                result, duplicate_note = await _write_bug_note_once(
                    bug_id=bug_id,
                    note_content=note_content,
                )
                if duplicate_note is not None:
                    previous_content = duplicate_note.get("note_content")
                    if isinstance(previous_content, str) and previous_content:
                        note_content = previous_content
                    previous_run_id = duplicate_note.get("note_run_id")
                    note_run_id = previous_run_id if isinstance(previous_run_id, str) else note_run_id
        except ZentaoError as exc:
            logger.warning("Bug #%s ZenTao analysis note write failed: %s", bug_id, exc)
            await update(
                "failed",
                router_thread_id=router_thread_id,
                router_run_id=state.get("router_run_id"),
                analysis_thread_id=analysis_thread_id,
                analysis_run_id=state.get("analysis_run_id"),
                note_thread_id=note_thread_id,
                note_run_id=note_run_id,
                handoff=state.get("handoff", ""),
                note_content=note_content,
                route=state.get("route"),
                route_reason=state.get("route_reason"),
                specialist_agent=state.get("specialist_agent"),
                failure_kind="note_write_failed",
                event_type="note_failed",
                summary="分析和备注内容已保留，但禅道写入失败，可直接重试",
                error=_public_bug_workflow_error("note_write_failed", str(exc)),
            )
            return {"note_run_id": note_run_id, "note_content": note_content}
        if not result.get("verified"):
            await update(
                "failed",
                router_thread_id=router_thread_id,
                router_run_id=state.get("router_run_id"),
                analysis_thread_id=analysis_thread_id,
                analysis_run_id=state.get("analysis_run_id"),
                note_thread_id=note_thread_id,
                note_run_id=note_run_id,
                handoff=state.get("handoff", ""),
                note_content=note_content,
                note_verified=False,
                route=state.get("route"),
                route_reason=state.get("route_reason"),
                specialist_agent=state.get("specialist_agent"),
                failure_kind="note_write_failed",
                event_type="note_failed",
                summary="分析和备注内容已保留，但禅道回读未确认，可直接重试",
                error=_public_bug_workflow_error("note_write_failed", "ZenTao note readback was not verified"),
            )
            return {"note_run_id": note_run_id, "note_content": note_content}
        handoff = state.get("handoff", "")
        await update(
            "note_written",
            router_thread_id=router_thread_id,
            router_run_id=state.get("router_run_id"),
            analysis_thread_id=analysis_thread_id,
            analysis_run_id=state.get("analysis_run_id"),
            note_thread_id=note_thread_id,
            note_run_id=note_run_id,
            handoff=handoff,
            note_content=note_content,
            note_verified=True,
            note_write_skipped=duplicate_note is not None,
            note_write_mode=result.get("mode", "reused"),
            note_action_id=result.get("action_id"),
            note_report=(
                "已有相同分析备注，本次未重复写入。"
                if result.get("reuse_reason") == "identical"
                else "已有分析备份不可重写，本次沿用旧备注。"
                if duplicate_note is not None
                else "已重写原分析备份并回读确认。"
                if result.get("mode") == "rewritten"
                else "分析及修改/处理意见已新建于禅道并回读确认。"
            ),
            completion_report="分析与建议交付已完成；未执行代码修改。" + ("已有分析备份，本次沿用旧备注。" if duplicate_note is not None else ""),
            route=state.get("route"),
            route_reason=state.get("route_reason"),
            specialist_agent=state.get("specialist_agent"),
        )
        return {"note_run_id": note_run_id, "note_content": note_content}

    try:
        await build_bug_workflow(analyze=analyze, note=note).ainvoke(
            {
                "workflow_id": workflow_id,
                "bug_id": bug_id,
                "router_thread_id": router_thread_id,
                "analysis_thread_id": analysis_thread_id,
                "note_thread_id": note_thread_id,
            }
        )
        current_workflow["phoenix_trace_id"] = phoenix.trace_id
        finish_phoenix()
        return dict(current_workflow)
    except asyncio.CancelledError as exc:
        finish_phoenix(exc)
        raise
    except Exception as exc:
        logger.exception("Bug workflow %s failed", workflow_id)
        error_text = str(exc)
        if error_text.startswith("analysis_execution_failed:"):
            failure_kind = "analysis_execution_failed"
        elif error_text.startswith("summary_execution_failed:"):
            failure_kind = "summary_execution_failed"
        elif error_text.startswith("analysis_protocol_error:") or "specialist handoff length" in error_text or "bug evidence contract" in error_text.lower() or "formal bug handoff" in error_text.lower():
            failure_kind = "analysis_protocol_error"
        elif error_text.startswith("review_preflight_failed:"):
            failure_kind = "review_preflight_failed"
        elif error_text.startswith("review_execution_failed:"):
            failure_kind = "review_execution_failed"
        else:
            failure_kind = "workflow_failed"
        public_error = _public_bug_workflow_error(failure_kind, error_text)
        failure_details: dict[str, Any] = {}
        if failure_kind == "analysis_execution_failed":
            previous_progress = current_workflow.get("codex_progress") if isinstance(current_workflow.get("codex_progress"), Mapping) else {}
            failure_details = {
                "external_execution_status": "failed",
                "analysis_stage": "failed",
                "codex_progress": {
                    **dict(previous_progress),
                    "last_phase": previous_progress.get("phase"),
                    "phase": "failed",
                    "failure_kind": failure_kind,
                },
            }
        await update(
            "failed",
            router_thread_id=router_thread_id,
            analysis_thread_id=analysis_thread_id,
            note_thread_id=note_thread_id,
            failure_kind=failure_kind,
            error=public_error,
            **failure_details,
        )
        current_workflow["phoenix_trace_id"] = phoenix.trace_id
        finish_phoenix(exc)
        return dict(current_workflow)
    finally:
        # Generated acceleration only; original attachments and persisted
        # excerpts remain in the private audit. Exact run paths, never globbed.
        for index in runtime_log_indices:
            try:
                index.unlink(missing_ok=True)
                index.parent.rmdir()
            except OSError:
                logger.warning("Could not clean generated runtime log index")


def attach_workflow_task(task: asyncio.Task[None], *, workflow_id: str | None = None) -> None:
    """Consume an unexpected background-task exception after persistence attempts."""

    if workflow_id:
        _ACTIVE_WORKFLOW_TASKS.setdefault(workflow_id, set()).add(task)

    def _log_result(completed: asyncio.Task[None]) -> None:
        if workflow_id:
            tasks = _ACTIVE_WORKFLOW_TASKS.get(workflow_id)
            if tasks is not None:
                tasks.discard(completed)
                if not tasks:
                    _ACTIVE_WORKFLOW_TASKS.pop(workflow_id, None)
        if completed.cancelled():
            return
        try:
            completed.result()
        except Exception:
            logger.exception("Bug workflow task crashed before it could persist failure state")

    task.add_done_callback(_log_result)


async def cancel_workflow_tasks(workflow_id: str) -> None:
    """Stop process-local orchestration wrappers owned by one Bug root."""
    tasks = tuple(_ACTIVE_WORKFLOW_TASKS.pop(workflow_id, ()))
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
