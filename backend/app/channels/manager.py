"""ChannelManager — consumes inbound messages and dispatches them to the DeerFlow agent via Gateway."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from langgraph_sdk.errors import ConflictError

from app.channels import buzz_run_policy as _buzz_run_policy  # noqa: F401
from app.channels import feishu_run_policy as _feishu_run_policy  # noqa: F401
from app.channels.commands import KNOWN_CHANNEL_COMMANDS
from app.channels.dedupe_store import InboundDedupeStore, MemoryInboundDedupeStore
from app.channels.message_bus import (
    INBOUND_FILE_CONTENT_KEY,
    PENDING_CLARIFICATION_METADATA_KEY,
    InboundMessage,
    InboundMessageType,
    MessageBus,
    OutboundMessage,
    ResolvedAttachment,
)
from app.channels.run_policy import CHANNEL_RUN_POLICY, ChannelRunPolicy
from app.channels.store import ChannelStore
from app.gateway.bug_change_advice import render_change_advice
from app.gateway.csrf_middleware import CSRF_COOKIE_NAME, CSRF_HEADER_NAME, generate_csrf_token

# Import built-in channel run-policy registrars eagerly so direct
# ChannelManager construction sees the same policy map as gateway bootstrap.
from app.gateway.github import run_policy as _github_run_policy  # noqa: F401
from app.gateway.internal_auth import create_internal_auth_headers
from deerflow.config.agents_config import load_agent_config
from deerflow.config.paths import make_safe_user_id
from deerflow.runtime import END_SENTINEL, StreamBridge
from deerflow.runtime.context_keys import BUG_WORKBENCH_OWNER_USER_ID_CONTEXT_KEY
from deerflow.runtime.goal import parse_goal_command
from deerflow.runtime.user_context import get_effective_user_id
from deerflow.skills.slash import parse_slash_skill_reference
from deerflow.skills.storage import get_or_new_skill_storage
from deerflow.skills.storage.skill_storage import SkillStorage
from deerflow.utils.messages import ORIGINAL_USER_CONTENT_KEY

logger = logging.getLogger(__name__)

DEFAULT_LANGGRAPH_URL = "http://localhost:8001/api"
DEFAULT_GATEWAY_URL = "http://localhost:8001"
DEFAULT_ASSISTANT_ID = "lead_agent"
DEFAULT_BUG_WORKBENCH_URL = "http://localhost:2026/workspace/bugs"
CUSTOM_AGENT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9-]+$")

# Lead-agent recursion budget (LangGraph super-steps for the lead graph only).
# This is independent of subagent depth: a `task()` dispatch runs the whole
# subagent inside ONE lead tools-node step, and subagents enforce their own
# limit via `subagents.max_turns` (see SubagentExecutor). Do not conflate this
# 100 with the general-purpose subagent's max_turns.
DEFAULT_RUN_CONFIG: dict[str, Any] = {"recursion_limit": 100}
DEFAULT_RUN_CONTEXT: dict[str, Any] = {
    "thinking_enabled": True,
    "is_plan_mode": False,
    "subagent_enabled": False,
}
STREAM_UPDATE_MIN_INTERVAL_SECONDS = 1.0
STREAM_UPDATE_MIN_CHARS = 60  # flush immediately when this many chars accumulate
# Stream modes requested from the runtime, and the SSE event names under which
# the message-tuple stream may arrive: the embedded runtime (and LangGraph
# Platform) deliver the requested "messages-tuple" mode as event "messages".
STREAM_MODES = ["messages-tuple", "values"]
MESSAGE_STREAM_EVENTS = ("messages-tuple", "messages")
THREAD_BUSY_MESSAGE = "This conversation is already processing another request. Please wait for it to finish and try again."
BOUND_IDENTITY_REQUIRED_MESSAGE = "Connect this channel from DeerFlow Settings, complete the in-channel connect step, then send your message again."
BOUND_IDENTITY_UNAVAILABLE_MESSAGE = "Channel connection verification is temporarily unavailable. Please try again later or contact the DeerFlow operator."
# Feishu Bug messages are dispatched before a general-purpose agent run.  The
# patterns intentionally stay narrow: they recognise a ZenTao URL, an explicit
# Bug/缺陷 reference, or a standalone numeric Bug id.  They must never spend a
# model call merely to decide whether the Bug Workbench should handle a message.
_ZENTAO_BUG_URL_PATTERN = re.compile(r"(?:https?://[^\s]+)?/zentao/bug-view-(\d+)\.html", re.IGNORECASE)
_EXPLICIT_BUG_ID_PATTERN = re.compile(r"(?:禅道\s*)?(?:bug|缺陷)\s*#?\s*(\d{1,10})\b", re.IGNORECASE)
_STANDALONE_BUG_ID_PATTERN = re.compile(r"^\s*#?\s*(\d{4,10})\s*$")
_BUG_FOLLOWUP_PATTERN = re.compile(
    r"^\s*(?:继续(?:分析|查|处理)?(?:这个(?:\s*(?:bug|缺陷))?)?|分析这个(?:\s*(?:bug|缺陷))?|查这个(?:\s*(?:bug|缺陷))?)\s*[。！？!?]*\s*$",
    re.IGNORECASE,
)
_BUG_WORKFLOW_CANCEL_PATTERN = re.compile(
    r"^\s*(?:取消|停止|终止)(?:当前)?(?:禅道)?\s*(?:bug|缺陷|分析|流程)?\s*[。！？!?]*\s*$|^\s*(?:编号写错了|写错编号|不是这个\s*(?:bug|缺陷)?|换个\s*(?:bug|缺陷))\s*[。！？!?]*\s*$",
    re.IGNORECASE,
)
_BUG_CURRENT_SUMMARY_PATTERN = re.compile(
    r"^\s*(?:请帮我|帮我|请)?\s*总结(?:一下)?\s*(?:当前|这个)\s*(?:bug|缺陷)\s*[。！？!?]*\s*$",
    re.IGNORECASE,
)
_BUG_WORKBENCH_SNAPSHOT_METADATA_KEY = "_bug_workbench_summary_snapshot"
_FEISHU_GREETING_PATTERN = re.compile(r"^\s*(?:你好|您好|嗨|哈喽|hello|hi)\s*[!！。,.，]*\s*$", re.IGNORECASE)
_FEISHU_LEADING_MENTION_PATTERN = re.compile(r"^\s*@[^\s@]+(?:\s+|$)")
_BUG_WORKFLOW_ACTIVE_STATUSES = frozenset({"routing", "analyzing", "writing_note", "repairing"})
_BUG_WORKFLOW_WAITING_STATUSES = frozenset({"awaiting_clarification"})
_BUG_WORKFLOW_TERMINAL_STATUSES = frozenset({"awaiting_evidence", "note_written", "awaiting_repair_choice", "awaiting_acceptance", "accepted", "rolled_back", "skipped", "cancelled", "analysis_incomplete", "failed"})
_BUG_WORKFLOW_POLL_INTERVAL_SECONDS = 3.0
_BUG_WORKFLOW_MAX_POLLS = 200
# Inbound-redelivery dedup window. The dedupe state lives in
# ``self._inbound_dedupe_store``: the default in-process Memory store is
# local to this Gateway process (a recorded key survives only for the store's
# TTL / entry cap and is gone across a restart), while a Postgres-backed store
# is shared across pods. 10 minutes is a deliberately bounded window: long
# enough to absorb a near-term redelivery of the same event — whether a
# provider's own automatic retry or an operator resend — without keeping a
# growing ledger.
#
# For GitHub specifically: GitHub does NOT automatically retry or redeliver
# a failed delivery (non-2xx response, timeout, or connection error) — it
# is simply recorded as failed. See GitHub's own documentation:
# https://docs.github.com/en/webhooks/using-webhooks/handling-failed-webhook-deliveries.
# Every redelivery of the same ``X-GitHub-Delivery`` GUID is therefore an
# explicit action — the repo/App "Redeliver" button, the REST API, or an
# operator's own scheduled recovery script polling the failed-deliveries
# endpoint (the pattern GitHub's own docs recommend) — never an automatic
# GitHub-side retry. This TTL exists to absorb exactly those explicit
# near-term replays.
#
# At the boundary: a manual redelivery (e.g. GitHub's "Redeliver" button)
# clicked *after* the TTL has elapsed, or any redelivery following a Gateway
# restart, is no longer recognized as a duplicate — the key has already been
# evicted, or never existed in the new process — so the agent runs again
# and may repeat a real side effect (e.g. a duplicate PR comment on
# GitHub). This is parity with every other IM channel's dedupe (same
# mechanism, same TTL), not a channel-specific gap. True idempotency against
# a late/manual redelivery would require persisting the dedupe key in
# ``ChannelStore`` instead, which is not implemented here.
# Follow-up buffering for busy fire_and_forget threads (issue #4121 Slice 2).
# A ConflictError on a channel opted into ChannelRunPolicy.buffer_followups_on_busy
# buffers the triggering message per-thread instead of only logging it; a
# background watcher drains the buffer into a coalesced follow-up run once the
# busy run's StreamBridge stream reaches END_SENTINEL. See _buffer_followup,
# _drain_followups_for_thread, and _watch_run_and_drain_followups below.
FOLLOWUP_BUFFER_MAX_PER_THREAD = 20
FOLLOWUP_DRAIN_BATCH_SIZE = 10
FOLLOWUP_BLOCK_TAG = "followups-while-busy"
# Only server-stable provider message ids: client-generated ids (client_msg_id,
# client_id) are not guaranteed identical across a provider's own redelivery, so
# keying dedupe on them would miss exactly the retries we want to absorb.
INBOUND_DEDUPE_METADATA_KEYS = ("event_id", "message_id", "msg_id")
# Providers that persist connection.workspace_id = chat_id (telegram / feishu /
# wechat upsert_connection). Unbound inbound has no connection, so msg.workspace_id
# is unset; chat_id is still the tenant scope and is safe for the dedupe key.
# Slack is intentionally excluded: its channel ids are not globally unique.
CHAT_SCOPED_WORKSPACE_CHANNELS = frozenset({"telegram", "feishu", "wechat"})

CHANNEL_CAPABILITIES = {
    "buzz": {"supports_streaming": True},
    "dingtalk": {"supports_streaming": False},
    "discord": {"supports_streaming": False},
    "feishu": {"supports_streaming": True},
    "github": {"supports_streaming": False},
    "slack": {"supports_streaming": False},
    "telegram": {"supports_streaming": True},
    "wechat": {"supports_streaming": False},
    "wecom": {"supports_streaming": True},
}

InboundFileReader = Callable[[dict[str, Any], httpx.AsyncClient], Awaitable[bytes | None]]

_METADATA_DROP_KEYS = frozenset({"raw_message", "ref_msg"})


def _slim_metadata(meta: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy of *meta* with known-large keys removed."""
    return {k: v for k, v in meta.items() if k not in _METADATA_DROP_KEYS}


INBOUND_FILE_READERS: dict[str, InboundFileReader] = {}


def register_inbound_file_reader(channel_name: str, reader: InboundFileReader) -> None:
    INBOUND_FILE_READERS[channel_name] = reader


async def _read_http_inbound_file(file_info: dict[str, Any], client: httpx.AsyncClient) -> bytes | None:
    url = file_info.get("url")
    if not isinstance(url, str) or not url:
        return None

    resp = await client.get(url)
    resp.raise_for_status()
    return resp.content


async def _read_wecom_inbound_file(file_info: dict[str, Any], client: httpx.AsyncClient) -> bytes | None:
    data = await _read_http_inbound_file(file_info, client)
    if data is None:
        return None

    aeskey = file_info.get("aeskey") if isinstance(file_info.get("aeskey"), str) else None
    if not aeskey:
        return data

    try:
        from aibot.crypto_utils import decrypt_file
    except Exception:
        logger.exception("[Manager] failed to import WeCom decrypt_file")
        return None

    return decrypt_file(data, aeskey)


async def _read_wechat_inbound_file(file_info: dict[str, Any], client: httpx.AsyncClient) -> bytes | None:
    raw_path = file_info.get("path")
    if isinstance(raw_path, str) and raw_path.strip():
        try:
            return await asyncio.to_thread(Path(raw_path).read_bytes)
        except OSError:
            logger.exception("[Manager] failed to read WeChat inbound file from local path: %s", raw_path)
            return None

    full_url = file_info.get("full_url")
    if isinstance(full_url, str) and full_url.strip():
        return await _read_http_inbound_file({"url": full_url}, client)

    return None


register_inbound_file_reader("wecom", _read_wecom_inbound_file)
register_inbound_file_reader("wechat", _read_wechat_inbound_file)


class InvalidChannelSessionConfigError(ValueError):
    """Raised when IM channel session overrides contain invalid agent config."""


class SlashSkillCommandResolutionError(RuntimeError):
    """Raised when IM slash-skill command resolution cannot complete safely."""


@dataclass(frozen=True, slots=True)
class _SlashSkillCommandResolution:
    route_to_chat: bool = False
    failure_message: str | None = None


@dataclass(frozen=True, slots=True)
class _BoundIdentityRejection:
    message: str = BOUND_IDENTITY_REQUIRED_MESSAGE
    # Server-side connection id that may be used only as an outbound routing
    # hint for the rejection message. This is never copied from the inbound
    # message; it comes from the repository re-read when available.
    outbound_connection_id: str | None = None
    # Server-side owner for the outbound routing connection above. It lets
    # channel senders preserve per-connection context without trusting the
    # rejected inbound identity assertion.
    outbound_owner_user_id: str | None = None


@dataclass(slots=True)
class _SerializedThreadRunState:
    """Per-thread lock state for channels that queue same-thread turns."""

    lock: asyncio.Lock
    waiters: int = 0


def _is_bug_workbench_read_query(text: str) -> bool:
    """Recognize natural read-only questions without requiring one magic phrase."""
    if _BUG_CURRENT_SUMMARY_PATTERN.fullmatch(text):
        return True
    subject = re.search(r"(?:bug|缺陷|问题|工作台|分析|修复|备注|测试)", text, re.IGNORECASE)
    intent = re.search(r"(?:状态|进度|结果|结论|查到|看到|更新|怎么样|如何|哪一步|为什么|是否|有没有|现在|当前)", text, re.IGNORECASE)
    return bool(subject and intent)


def _is_main_agent_bug_batch_request(text: str) -> bool:
    """Leave list/selection/batch language with the lead agent, not one Bug."""
    assignee_query = re.search(r"(?:名下|指派给|指派人|负责人)", text)
    list_intent = re.search(r"(?:查|列|统计|多少|几个|前\s*\d+|全部|未解决)", text)
    selected_batch = re.search(r"(?:这些|这批|刚才.{0,12}(?:列表|编号|查询|那些|这批)|上面.{0,12}(?:列表|编号|那些|这批)|(?:全部|所有).{0,12}(?:Bug|bug|缺陷|工单)|这\s*\d+\s*个)", text)
    batch_action = re.search(r"(?:运行|分析|启动|进度|结果|完成)", text)
    return bool((assignee_query and list_intent) or (selected_batch and batch_action))


@dataclass(slots=True)
class _FollowupEntry:
    """One inbound message's text, buffered because its thread was busy.

    Routing/policy identity (channel_name, metadata, owner headers) for the
    eventual drained run comes from a separate ``carrier_msg`` — see
    ``ChannelManager._drain_followups_for_thread`` — not from a per-entry
    message, since every buffered entry for one thread_id shares that
    identity already (thread_id is itself derived deterministically from
    (repo, number, agent_name) for GitHub). Only the text needs to survive
    per entry.
    """

    dedupe_key: str
    text: str


@dataclass(slots=True)
class _BugWorkflowConversation:
    """A Feishu conversation's most recently started Bug Workbench run."""

    workflow_id: str
    bug_id: int
    status: str = "routing"
    attachment_notice_sent: bool = False
    last_attachment_progress_notice: str = ""
    last_triage_notice: str = ""
    repair_notice_sent: bool = False
    last_notified_revision: int = 0
    last_notified_status: str = ""


def _extract_zentao_bug_id(text: str) -> int | None:
    """Extract one explicit ZenTao Bug id without involving an LLM."""
    for pattern in (_ZENTAO_BUG_URL_PATTERN, _EXPLICIT_BUG_ID_PATTERN, _STANDALONE_BUG_ID_PATTERN):
        match = pattern.search(text)
        if match:
            return int(match.group(1))
    return None


def _is_bug_workflow_followup(text: str) -> bool:
    """Return whether a short Feishu reply asks to continue the active Bug flow."""
    return bool(_BUG_FOLLOWUP_PATTERN.fullmatch(text))


def _format_bug_attachment_evidence_notice(bug_id: int, evidence: Any) -> str | None:
    """Render one completion notice only after every attachment reaches a final state."""
    if not isinstance(evidence, dict) or not isinstance(evidence.get("assets"), list):
        return None
    assets = [item for item in evidence["assets"] if isinstance(item, dict)]
    terminal = {"downloaded", "indexed", "processed", "skipped", "visual_no_evidence", "visual_analysis_failed", "type_mismatch", "failed"}
    if not assets or any(str(item.get("status") or "") not in terminal for item in assets):
        return None
    labels = {
        "downloaded": "下载成功",
        "indexed": "下载成功，已建立附件索引",
        "processed": "下载成功，已作为分析证据",
        "skipped": "未下载，已跳过视频分析",
        "visual_no_evidence": "下载成功，图片中未提取到明确证据",
        "visual_analysis_failed": "下载成功，但图片识别失败",
        "type_mismatch": "文件类型异常",
        "failed": "下载失败",
    }
    lines = [f"禅道 Bug #{bug_id} 附件处理完成："]
    for item in assets:
        name = str(item.get("name") or "附件")
        status = str(item.get("status") or "failed")
        error = str(item.get("error") or "").strip()
        suffix = f"（{error}）" if error and error != labels[status] else ""
        lines.append(f"- {name}：{labels[status]}{suffix}")
    return "\n".join(lines)


def _format_bug_attachment_evidence_progress_notice(bug_id: int, evidence: Any) -> str | None:
    """Render only a real attachment stage transition; caller deduplicates identical text."""
    if not isinstance(evidence, dict) or not isinstance(evidence.get("assets"), list):
        return None
    assets = [item for item in evidence["assets"] if isinstance(item, dict)]
    if not assets:
        return None

    def names_for(status: str) -> list[str]:
        return [str(item.get("name") or "附件") for item in assets if str(item.get("status") or "") == status]

    retrying = names_for("visual_thinking_retry")
    if retrying:
        return f"禅道 Bug #{bug_id} 图片识别进度：快速识别未得到完整结果，正在开启思考模式重试 {len(retrying)} 张图片：{'、'.join(retrying)}"
    recognizing = names_for("visual_fast_processing")
    if recognizing:
        return f"禅道 Bug #{bug_id} 图片识别进度：正在快速识别 {len(recognizing)} 张图片：{'、'.join(recognizing)}"
    downloading = names_for("downloading")
    if downloading:
        first_name = downloading[0]
        current_index = next((index for index, item in enumerate(assets, start=1) if str(item.get("status") or "") == "downloading"), 1)
        return f"禅道 Bug #{bug_id} 附件进度：正在下载 {current_index}/{len(assets)}：{first_name}"
    if any(str(item.get("status") or "") == "queued" for item in assets):
        return f"禅道 Bug #{bug_id} 附件进度：已发现 {len(assets)} 个附件，准备下载。"
    return None


def _format_bug_triage_notice(bug_id: int, triage: Any) -> str | None:
    """Describe preliminary symptom classification without claiming source ownership."""
    if not isinstance(triage, Mapping):
        return None
    problem_type = str(triage.get("problem_type") or "unknown")
    subtype = str(triage.get("ui_subtype") or "")
    display_name = str(triage.get("display_name") or "问题方向待源码确认")
    direction = str(triage.get("direction") or "").strip().rstrip("。.!！")
    raw_clients = triage.get("observed_clients")
    clients = [str(value).upper() if str(value).lower() != "ios" else "iOS" for value in raw_clients if value] if isinstance(raw_clients, list) else []
    client_text = f"，已观察端：{'/'.join(clients)}" if clients else ""
    if problem_type == "ui" and subtype == "copy":
        return f"禅道 Bug #{bug_id} 初步呈现为{display_name}{client_text}。源码调查尚未开始；调查提示：{direction or '先确认本次要修改的文案和明确排除项'}。责任端与根因随后由源码证据确认。"
    if problem_type == "unknown":
        return f"禅道 Bug #{bug_id} 暂不预判问题类型。调查提示：{direction or '从现有工单事实开始核对源码行为链'}；最终责任由源码证据确定。"
    return f"禅道 Bug #{bug_id} 初步判断为{display_name}{client_text}。调查提示：{direction or '沿对应行为链核对源码'}。这不代表已确认责任端或本地可修复性。"


def _has_confirmed_preanalysis_copy_scope(state: Mapping[str, Any]) -> bool:
    """Return whether attachment/triage notices belong to an earlier phase."""

    return state.get("clarification_stage") == "pre_analysis" and isinstance(state.get("confirmed_copy_scope"), Mapping)


_BUG_HANDOFF_DISPLAY_FIELDS = (
    "自动修复准备度",
    "已证实",
    "当前证据",
    "根因",
    "修复目标",
    "修改目标",
    "确认入口",
    "允许修复范围",
    "验证",
    "风险/待确认",
    "最小补充信息",
    "交接给",
)


def _bug_handoff_display_field(handoff: str, *labels: str) -> str:
    """Extract one compact labelled handoff field for channel presentation."""
    compact = re.sub(r"</?bug_handoff>", " ", handoff, flags=re.IGNORECASE)
    compact = re.sub(r"\s+", " ", compact).strip()
    if not compact:
        return ""
    requested = "|".join(re.escape(label) for label in labels)
    boundaries = "|".join(re.escape(label) for label in _BUG_HANDOFF_DISPLAY_FIELDS)
    match = re.search(
        rf"(?:^|\s)(?:{requested})\s*[:：]\s*(.*?)(?=\s+(?:{boundaries})\s*[:：]|$)",
        compact,
        re.IGNORECASE,
    )
    return match.group(1).strip() if match else ""


def _format_feishu_specialist_analysis(handoff: str) -> str:
    """Render the completed specialist contract consistently in Feishu."""
    report = re.sub(r"^\s*<bug_handoff>\s*|\s*</bug_handoff>\s*$", "", handoff, flags=re.IGNORECASE | re.DOTALL).strip()
    has_four_part_conclusion = re.search(r"(?:^|\n)一、\s*分析结论", report) is not None
    has_complete_report_shape = has_four_part_conclusion or re.search(r"(?:^|\n)一、\s*Bug\s*事实", report) is not None or len(re.findall(r"(?m)^#{1,6}\s+\S+", report)) >= 2
    if has_complete_report_shape:
        if has_four_part_conclusion:
            return report
        summary_match = re.search(
            r"结论摘要(?:\s*[（(][^）)\n]{0,40}[）)])?\s*[:：]\s*(.*?)(?=\n\s*(?:修复准备度|自动修复准备度)\s*[:：]|$)",
            report,
            re.DOTALL,
        )
        summary = re.sub(r"\s+", " ", summary_match.group(1)).strip() if summary_match else ""
        prefix = f"结论摘要\n{summary}\n\n" if summary else ""
        return f"{prefix}完整分析报告\n{report}"

    fields = (
        ("根因/当前判断", ("根因", "已证实", "当前证据"), 700),
        ("修复目标", ("修复目标", "修改目标"), 600),
        ("确认入口", ("确认入口",), 700),
        ("允许修复范围", ("允许修复范围",), 500),
        ("验证", ("验证",), 600),
        ("风险/待确认", ("风险/待确认", "最小补充信息"), 600),
    )
    lines: list[str] = []
    for title, labels, limit in fields:
        value = _bug_handoff_display_field(handoff, *labels)
        if value:
            lines.extend([title, value[:limit]])
    if not lines and handoff.strip():
        compact = re.sub(r"</?bug_handoff>", " ", handoff, flags=re.IGNORECASE)
        compact = re.sub(r"\s+", " ", compact).strip()
        lines.extend(["专家分析摘要", compact[:1200]])
    return "\n\n".join(lines)


def _format_feishu_bug_result(
    bug_id: int,
    workflow_id: str,
    state: Mapping[str, Any],
    *,
    workbench_base_url: str = DEFAULT_BUG_WORKBENCH_URL,
) -> str:
    """Project the canonical persisted Bug result into one Feishu message."""

    lines = [f"禅道 Bug #{bug_id}：Bug Workbench 已完成分析。"]
    platform = state.get("platform_resolution")
    if isinstance(platform, Mapping):
        client_labels = {"android": "Android", "ios": "iOS", "harmony": "Harmony"}
        raw_clients = platform.get("reported_clients")
        clients = [client_labels.get(str(value).lower(), str(value)) for value in raw_clients if value] if isinstance(raw_clients, list) else []
        primary_repository = str(platform.get("primary_repository") or "").strip()
        platform_parts: list[str] = []
        if clients:
            platform_parts.append(f"确认平台：{' / '.join(clients)}")
        if primary_repository:
            platform_parts.append(f"主仓库：{primary_repository}")
        if platform_parts:
            lines.extend(["", "平台确认", "；".join(platform_parts)])

    report = str(state.get("analysis_report") or state.get("handoff") or "").strip()
    if report:
        lines.extend(["", "完整分析", _format_feishu_specialist_analysis(report)])
    else:
        lines.extend(["", "完整分析", "未生成可交付的四段报告。"])
    return "\n".join(lines)


def _format_feishu_repair_terminal_result(
    bug_id: int,
    workflow_id: str,
    state: Mapping[str, Any],
    *,
    workbench_base_url: str = DEFAULT_BUG_WORKBENCH_URL,
) -> str:
    """Report only the repair outcome after Feishu already received the analysis."""
    report = re.sub(r"\s+", " ", str(state.get("repair_report") or state.get("completion_report") or "")).strip()
    changed = state.get("changed_files")
    changed_files = [str(item) for item in changed if item] if isinstance(changed, list) else []
    if changed_files:
        lines = [f"禅道 Bug #{bug_id}：自动修复已形成代码差异，等待验收。", f"修改文件：{'、'.join(changed_files[:8])}"]
    else:
        lines = [f"禅道 Bug #{bug_id}：自动修复已结束，未修改代码。"]
    if report:
        lines.append(f"修复专家结论：{report[:1000]}")
    if state.get("note_verified") is True:
        note_status = "已有确认备注，本次未重复写入" if state.get("note_write_skipped") is True else "已写入并回读确认"
    else:
        note_status = "尚未确认"
    lines.append(f"禅道备注：{note_status}")
    base_url = workbench_base_url.strip().rstrip("?")
    if base_url:
        separator = "&" if "?" in base_url else "?"
        lines.append(f"Bug 工作台：{base_url}{separator}workflow={quote(workflow_id, safe='')}")
    return "\n".join(lines)


def _format_feishu_failed_bug_result(
    bug_id: int,
    workflow_id: str,
    state: Mapping[str, Any],
    *,
    workbench_base_url: str = DEFAULT_BUG_WORKBENCH_URL,
) -> str:
    """Distinguish a completed analysis from a later repair failure."""
    repair_attempted = state.get("repair_attempted") is True or bool(state.get("repair_engine"))
    analysis_complete = bool(str(state.get("analysis_report") or state.get("handoff") or "").strip())
    error = re.sub(r"\s+", " ", str(state.get("error") or "未知错误")).strip()
    if analysis_complete and state.get("failure_kind") in {"note_generation_failed", "note_write_failed"}:
        lines = [
            f"禅道 Bug #{bug_id}",
            "分析状态：已完成并保留",
            "禅道备注：生成或写入未完成，可直接重试备注，无需重新调查",
            f"失败原因：{error[:700]}",
        ]
        base_url = workbench_base_url.strip().rstrip("?")
        if base_url:
            separator = "&" if "?" in base_url else "?"
            lines.append(f"Bug 工作台：{base_url}{separator}workflow={quote(workflow_id, safe='')}")
        return "\n".join(lines)
    if not repair_attempted or not analysis_complete:
        return f"禅道 Bug #{bug_id} 自动分析未完成：{error[:700]}。可重新发送该 Bug ID 再试。"

    lines = [
        f"禅道 Bug #{bug_id}",
        "分析状态：已完成",
        "自动修复状态：失败",
    ]
    raw_targets = state.get("final_targets")
    targets = [item for item in raw_targets if isinstance(item, Mapping)] if isinstance(raw_targets, list) else []
    nearest_failure = next((str(item.get("nearest_failure") or "").strip() for item in targets if item.get("nearest_failure")), "")
    if nearest_failure:
        lines.append(f"分析定位：{nearest_failure[:600]}")
    repair_report = re.sub(r"\s+", " ", str(state.get("repair_report") or "")).strip()
    if repair_report:
        lines.append(f"修复专家结论：{repair_report[:1000]}")
    lines.append(f"失败原因：{error[:700]}")
    if state.get("note_verified") is True:
        note_status = "已有确认备注，本次未重复写入" if state.get("note_write_skipped") is True else "已写入并回读确认"
        lines.append(f"禅道备注：{note_status}")
    base_url = workbench_base_url.strip().rstrip("?")
    if base_url:
        separator = "&" if "?" in base_url else "?"
        lines.append(f"Bug 工作台：{base_url}{separator}workflow={quote(workflow_id, safe='')}")
    return "\n".join(lines)


def _format_feishu_incomplete_bug_analysis(bug_id: int, state: Mapping[str, Any]) -> str:
    """Show useful specialist findings before the non-repairable gate result."""
    handoff = str(state.get("handoff") or "").strip()
    completion = re.sub(r"\s+", " ", str(state.get("completion_report") or "")).strip()
    lines = [f"禅道 Bug #{bug_id} 分析结果"]
    specialist_analysis = _format_feishu_specialist_analysis(handoff)
    if specialist_analysis:
        lines.extend(["", specialist_analysis])

    terminal_marker = "本次分析到此结束："
    reason, separator, terminal = completion.partition(terminal_marker)
    reason = reason.strip(" \t\r\n。；;，,！？!?") or "专项分析尚未形成可安全执行的修复交接"
    lines.extend(["", "自动修复：暂不可执行", f"原因：{reason[:500]}。"])
    if separator:
        lines.append(terminal.strip()[:300])
    else:
        lines.append("本次未写入禅道备注，也未启动修复专家。")
    return "\n".join(lines)


def _is_bug_workflow_cancellation(text: str) -> bool:
    """Recognise an explicit request to abandon a paused Bug workflow."""
    return bool(_BUG_WORKFLOW_CANCEL_PATTERN.fullmatch(text))


def _is_thread_busy_error(exc: BaseException | None) -> bool:
    if exc is None:
        return False
    if isinstance(exc, ConflictError):
        return True
    return "already running a task" in str(exc)


def _followup_dedupe_key(msg: InboundMessage) -> str:
    """Best-effort stable identifier for a buffered follow-up comment.

    Mirrors ``_inbound_dedupe_key``'s provider-id preference order (a GitHub
    webhook delivery id first, then the generic provider-message-id metadata
    keys), but scoped to one thread's follow-up buffer rather than the
    global cross-channel inbound dedupe map, and always returns a usable key
    — falling back to an object-identity key — since the follow-up buffer
    must still accept an entry even when a provider omits every known id
    field (unlike ``_inbound_dedupe_key``, which returns ``None`` to skip
    dedupe entirely in that case).
    """
    metadata = msg.metadata or {}
    gh = metadata.get("github")
    if isinstance(gh, dict):
        delivery_id = gh.get("delivery_id")
        if delivery_id:
            return f"github:delivery:{delivery_id}"

    for key in INBOUND_DEDUPE_METADATA_KEYS:
        value = metadata.get(key)
        if value:
            return f"{key}:{value}"

    raw_message = metadata.get("raw_message")
    if isinstance(raw_message, Mapping):
        for key in INBOUND_DEDUPE_METADATA_KEYS:
            value = raw_message.get(key)
            if value:
                return f"{key}:{value}"

    # No stable provider id available: fall back to a per-message key so the
    # entry is still buffered (just never deduped against a redelivery).
    return f"__no_id__:{id(msg)}:{msg.created_at}"


def _format_followup_block(entries: list[_FollowupEntry]) -> str:
    """Coalesce buffered follow-up entries into one templated input block."""
    lines = [
        f"<{FOLLOWUP_BLOCK_TAG}>",
        "The following messages arrived on this thread while a previous run was still in progress. They were queued and are now delivered together as one turn:",
        "",
    ]
    for idx, entry in enumerate(entries, start=1):
        escaped_text = escape(entry.text, quote=False).replace(
            "\n",
            "\n   ",
        )
        lines.append(f"{idx}. {escaped_text}")
    lines.append(f"</{FOLLOWUP_BLOCK_TAG}>")
    return "\n".join(lines)


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _merge_dicts(*layers: Any) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for layer in layers:
        if isinstance(layer, Mapping):
            merged.update(layer)
    return merged


def _normalize_custom_agent_name(raw_value: str) -> str:
    """Normalize legacy channel assistant IDs into valid custom agent names."""
    normalized = raw_value.strip().lower().replace("_", "-")
    if not normalized:
        raise InvalidChannelSessionConfigError("Channel session assistant_id is empty. Use 'lead_agent' or a valid custom agent name.")
    if not CUSTOM_AGENT_NAME_PATTERN.fullmatch(normalized):
        raise InvalidChannelSessionConfigError(f"Invalid channel session assistant_id {raw_value!r}. Use 'lead_agent' or a custom agent name containing only letters, digits, and hyphens.")
    return normalized


def _extract_response_text(result: dict | list) -> str:
    """Extract the last AI message text from a LangGraph runs.wait result.

    ``runs.wait`` returns the final state dict which contains a ``messages``
    list.  Each message is a dict with at least ``type`` and ``content``.

    Handles special cases:
    - Regular AI text responses
    - Clarification interrupts (``ask_clarification`` tool messages)
    """
    if isinstance(result, list):
        messages = result
    elif isinstance(result, dict):
        messages = result.get("messages", [])
    else:
        return ""

    # Walk backwards to find usable response text, but stop at the last
    # human message to avoid returning text from a previous turn.
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue

        msg_type = msg.get("type")

        # Stop at the last human message — anything before it is a previous turn
        if msg_type == "human":
            if _is_hidden_human_control_message(msg):
                continue
            break

        # Check for tool messages from ask_clarification (interrupt case)
        if msg_type == "tool" and msg.get("name") == "ask_clarification":
            content = msg.get("content", "")
            if isinstance(content, str) and content:
                return content

        # Regular AI message with text content
        if msg_type == "ai":
            content = msg.get("content", "")
            if isinstance(content, str) and content:
                return content
            # content can be a list of content blocks
            if isinstance(content, list):
                parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append(block.get("text", ""))
                    elif isinstance(block, str):
                        parts.append(block)
                text = "".join(parts)
                if text:
                    return text
    return ""


def _messages_from_result(result: dict | list) -> list[Any]:
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        messages = result.get("messages", [])
        if isinstance(messages, list):
            return messages
    return []


def _current_turn_messages(result: dict | list) -> list[dict[str, Any]]:
    messages = _messages_from_result(result)
    current_turn: list[dict[str, Any]] = []
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        if msg.get("type") == "human":
            break
        current_turn.append(msg)
    current_turn.reverse()
    return current_turn


def _has_current_turn_clarification(result: dict | list) -> bool:
    """Return True only when the current turn's final result is clarification."""
    for msg in reversed(_current_turn_messages(result)):
        msg_type = msg.get("type")
        if msg_type == "tool":
            return msg.get("name") == "ask_clarification"
        if msg_type == "ai":
            content = msg.get("content")
            if isinstance(content, str):
                if content:
                    return False
            elif content:
                return False
            if msg.get("tool_calls"):
                return False
    return False


def _response_metadata(base_metadata: dict[str, Any], *, pending_clarification: bool = False) -> dict[str, Any]:
    metadata = _slim_metadata(base_metadata)
    if pending_clarification:
        metadata[PENDING_CLARIFICATION_METADATA_KEY] = True
    return metadata


def _thread_channel_metadata(msg: InboundMessage) -> dict[str, Any]:
    channel_source: dict[str, Any] = {
        "type": "im_channel",
        "provider": msg.channel_name,
        "chat_id": msg.chat_id,
    }
    if msg.topic_id:
        channel_source["topic_id"] = msg.topic_id
    if msg.thread_ts:
        channel_source["thread_ts"] = msg.thread_ts
    if msg.connection_id:
        channel_source["connection_id"] = msg.connection_id

    return {"channel_source": channel_source}


def _extract_text_content(content: Any) -> str:
    """Extract text from a streaming payload content field."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, Mapping):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
                else:
                    nested = block.get("content")
                    if isinstance(nested, str):
                        parts.append(nested)
        return "".join(parts)
    if isinstance(content, Mapping):
        for key in ("text", "content"):
            value = content.get(key)
            if isinstance(value, str):
                return value
    return ""


def _merge_stream_text(existing: str, chunk: str) -> str:
    """Merge either delta text or cumulative text into a single snapshot."""
    if not chunk:
        return existing
    if not existing:
        return chunk
    # Cumulative re-delivery: strictly longer and starts with existing.
    if len(chunk) > len(existing) and chunk.startswith(existing):
        return chunk
    # Everything else is a delta — always append, even when the delta
    # happens to match the buffer suffix (e.g. 'hel' + 'l') or equals
    # the buffer (CJK reduplication: '谢' + '谢' = '谢谢'). Channels feed
    # only delta ('messages-tuple') events to this function; 'values'
    # snapshots are consumed via a separate branch, so a same-content
    # delta (chunk == existing) still represents a fresh token to keep.
    return existing + chunk


def _extract_stream_message_id(payload: Any, metadata: Any) -> str | None:
    """Best-effort extraction of the streamed AI message identifier."""
    candidates = [payload, metadata]
    if isinstance(payload, Mapping):
        candidates.append(payload.get("kwargs"))

    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        for key in ("id", "message_id"):
            value = candidate.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _stream_payload_type(payload: Mapping[str, Any]) -> str:
    """Resolve the message ``type`` of one ``messages-tuple`` payload.

    Two payload shapes reach this function and they name the message type in
    different places:

    * The shape DeerFlow's own gateway emits (``runtime/serialization.py``
      calls ``model_dump()``): ``type`` is the LangChain literal directly --
      ``"ai"`` / ``"AIMessageChunk"`` / ``"human"`` / ``"tool"`` / ``"system"``.
    * LangChain's ``to_json()`` constructor shape, which
      ``_extract_stream_message_id`` and the content extraction below already
      accommodate: the wrapper's own ``type`` is the literal string
      ``"constructor"`` and the real class name is the last element of the
      ``id`` path (``["langchain", "schema", "messages", "AIMessageChunk"]``),
      with the constructor kwargs under ``kwargs``.

    Reading only the top level would classify every constructor-shaped payload
    as ``"constructor"``, which an allowlist rejects (safe) but which would
    also mean hidden context and assistant output are treated identically --
    so the class name is resolved properly instead of guessed.
    """
    raw_type = payload.get("type")
    if isinstance(raw_type, str) and raw_type and raw_type != "constructor":
        return raw_type
    kwargs = payload.get("kwargs")
    if isinstance(kwargs, Mapping):
        nested = kwargs.get("type")
        if isinstance(nested, str) and nested:
            return nested
    lc_path = payload.get("id")
    if isinstance(lc_path, (list, tuple)) and lc_path:
        tail = lc_path[-1]
        if isinstance(tail, str) and tail:
            return tail
    return raw_type if isinstance(raw_type, str) else ""


def _is_assistant_stream_type(payload_type: str) -> bool:
    """Is this message type assistant output, i.e. displayable in an IM channel?

    An ALLOWLIST, deliberately.  The previous denylist ("reject anything whose
    type contains 'tool'") published every other message type, and DeerFlow
    writes hidden model context into the ``messages`` channel as ordinary
    messages: ``DynamicContextMiddleware`` injects the ``<memory>`` block as a
    hidden ``HumanMessage`` (``type == "human"``) and rewrites the user's own
    turn into a new ``HumanMessage``, and ``DurableContextMiddleware`` injects a
    hidden ``<durable_context_data>`` ``HumanMessage``.  LangGraph fans state
    writes out on the ``messages-tuple`` stream, so all of those reached the
    channel as if they were the assistant's reply -- proved live on a Buzz
    relay, where each streaming update is an immutable public Nostr event and a
    later corrective edit cannot unpublish the leaked one.

    The accepted spellings are the ones assistant output actually carries:
    LangChain serializes ``AIMessage.type`` as ``"ai"`` and
    ``AIMessageChunk.type`` as ``"AIMessageChunk"``; ``"assistant"`` is the
    OpenAI-style spelling a foreign runtime may use.  Matching is by prefix
    rather than substring because a substring test is not safe here -- ordinary
    English words contain "ai" ("chain", "domain"), so ``"ai" in type`` would
    admit a future/foreign type name by accident, which is exactly the class of
    mistake this allowlist exists to prevent.  No LangChain message type other
    than the AI ones begins with "ai" or "assistant".
    """
    normalized = payload_type.strip().lower()
    return normalized.startswith(("ai", "assistant"))


def _accumulate_stream_text(
    buffers: dict[str, str],
    current_message_id: str | None,
    event_data: Any,
) -> tuple[str | None, str | None]:
    """Convert a ``messages-tuple`` event into the latest displayable AI text.

    Only assistant output is displayable.  Hidden human/system context (memory
    facts, durable context, the middleware-rewritten echo of the user's own
    message) and tool traffic must never be published to an IM channel; see
    :func:`_is_assistant_stream_type`.

    A bare ``str`` payload -- previously accepted here and buffered under the
    current message id -- carries no type information at all, so it cannot be
    attributed to the assistant.  Nothing in DeerFlow produces it (the gateway
    always serializes a ``messages-tuple`` chunk as ``[message_dict, metadata]``
    via ``runtime/serialization.py::serialize_messages_tuple``), and a runtime
    that did emit raw text deltas would emit hidden context the same way, with
    no way to tell them apart.  Under an allowlist an unattributable payload is
    dropped rather than published.
    """
    payload = event_data
    metadata: Any = None
    if isinstance(event_data, (list, tuple)):
        if event_data:
            payload = event_data[0]
        if len(event_data) > 1:
            metadata = event_data[1]

    if not isinstance(payload, Mapping):
        return None, current_message_id

    if not _is_assistant_stream_type(_stream_payload_type(payload)):
        return None, current_message_id

    text = _extract_text_content(payload.get("content"))
    if not text and isinstance(payload.get("kwargs"), Mapping):
        text = _extract_text_content(payload["kwargs"].get("content"))
    if not text:
        return None, current_message_id

    message_id = _extract_stream_message_id(payload, metadata) or current_message_id or "__default__"
    buffers[message_id] = _merge_stream_text(buffers.get(message_id, ""), text)
    return buffers[message_id], message_id


def _extract_artifacts(result: dict | list) -> list[str]:
    """Extract artifact paths from the last AI response cycle only.

    Instead of reading the full accumulated ``artifacts`` state (which contains
    all artifacts ever produced in the thread), this inspects the messages after
    the last human message and collects file paths from ``present_files`` tool
    calls.  This ensures only newly-produced artifacts are returned.
    """
    if isinstance(result, list):
        messages = result
    elif isinstance(result, dict):
        messages = result.get("messages", [])
    else:
        return []

    artifacts: list[str] = []
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        # Stop at the last human message — anything before it is a previous turn
        if msg.get("type") == "human":
            if _is_hidden_human_control_message(msg):
                continue
            break
        # Look for AI messages with present_files tool calls
        if msg.get("type") == "ai":
            for tc in msg.get("tool_calls", []):
                if isinstance(tc, dict) and tc.get("name") == "present_files":
                    args = tc.get("args", {})
                    paths = args.get("filepaths", [])
                    if isinstance(paths, list):
                        artifacts.extend(p for p in paths if isinstance(p, str))
    return artifacts


def _is_hidden_human_control_message(msg: Mapping[str, Any]) -> bool:
    """Return whether a human message is an internal control message hidden from UI."""
    if msg.get("type") != "human":
        return False

    additional_kwargs = msg.get("additional_kwargs")
    if not isinstance(additional_kwargs, Mapping):
        return False

    return additional_kwargs.get("hide_from_ui") is True


def _format_artifact_text(artifacts: list[str]) -> str:
    """Format artifact paths into a human-readable text block listing filenames."""
    import posixpath

    filenames = [posixpath.basename(p) for p in artifacts]
    if len(filenames) == 1:
        return f"Created File: 📎 {filenames[0]}"
    return "Created Files: 📎 " + "、".join(filenames)


_OUTPUTS_VIRTUAL_PREFIX = "/mnt/user-data/outputs/"


def _unknown_command_reply(command: str | None = None) -> str:
    available = " | ".join(sorted(KNOWN_CHANNEL_COMMANDS))
    if command:
        return f"Unknown command: /{command}. Available commands: {available}"
    return f"Unknown command. Available commands: {available}"


def _human_input_message(content: str, *, original_content: str | None = None, files: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "human", "content": content}
    if original_content is not None and original_content != content or files:
        additional_kwargs: dict[str, Any] = {}
        if original_content is not None and original_content != content:
            additional_kwargs[ORIGINAL_USER_CONTENT_KEY] = original_content
        if files:
            additional_kwargs["files"] = files
        message["additional_kwargs"] = additional_kwargs
    return message


def _channel_agent_input_text(msg: InboundMessage) -> str:
    """Add deterministic, model-facing context for narrow channel interactions."""
    text = msg.text or ""
    bug_snapshot = msg.metadata.get(_BUG_WORKBENCH_SNAPSHOT_METADATA_KEY)
    if msg.channel_name == "feishu" and isinstance(bug_snapshot, str) and bug_snapshot:
        return f"{text}\n\n{bug_snapshot}"
    return text


def _format_bug_workbench_summary_snapshot(state: Mapping[str, Any]) -> str:
    """Expose the latest canonical Bug Workbench state to the normal lead agent."""
    lines = [
        "<bug_workbench_snapshot>",
        (
            "以下是 Bug 工作台的最新只读状态，来自工作台持久化数据，不是禅道列表。"
            "用户询问该 Bug、工作台状态、分析进展、修复或测试结果时必须优先依据这里回答，且只能依据这里回答；"
            "不得调用禅道接口替代工作台状态，不得查询最新禅道 Bug，"
            "不得重新猜测或重复源码定位；与当前问题无关时忽略。"
        ),
        f"Bug ID：{state.get('bug_id', '未知')}",
        f"当前状态：{state.get('status', '未知')}",
        f"状态版本：revision {state.get('revision', 0)}",
    ]
    if state.get("updated_at"):
        lines.append(f"最后更新：{state['updated_at']}")
    if state.get("status") == "awaiting_repair_choice":
        lines.append("历史状态说明：这是旧版只读任务，不能继续选择、补证或启动修复；如需处理请重新提交 Bug ID。")
    bug_snapshot = state.get("bug_snapshot")
    if isinstance(bug_snapshot, Mapping):
        for key, label in (("title", "标题"), ("steps", "测试步骤"), ("actual", "实测结果"), ("expected", "预期结果")):
            value = bug_snapshot.get(key)
            if isinstance(value, str) and value.strip():
                lines.append(f"{label}：{value.strip()[:1200]}")
    # Same canonical report as Workbench; no prefix cut losing advice.
    report = str(state.get("analysis_report") or state.get("handoff") or "").strip()
    if report:
        lines.extend(("完整分析与修改/处理意见：", report))
    elif state.get("final_targets"):
        lines.extend(("修改/处理意见：", render_change_advice(state["final_targets"])))
    labels = (
        ("route", "专项类型", 80),
        ("route_reason", "分流原因", 300),
        ("repair_decision", "代码执行状态（report_only仅表示未执行，不代表不能本地修改）", 80),
        ("decision_reason", "分析阶段判断原因", 500),
        ("note_content", "禅道备注", 4000),
        ("repair_report", "历史修复结果", 800),
        ("completion_report", "完成说明", 500),
        ("error", "错误", 300),
    )
    for key, label, limit in labels:
        value = state.get(key)
        if isinstance(value, str) and value.strip():
            lines.append(f"{label}：{value.strip()[:limit]}")
    changed_files = state.get("changed_files")
    if isinstance(changed_files, list) and changed_files:
        lines.append("修改文件：" + "、".join(str(item) for item in changed_files[:8]))
    last_event = state.get("last_event")
    if isinstance(last_event, Mapping):
        lines.append(f"最近操作：revision {last_event.get('revision', '?')} / {last_event.get('actor_source', '未知来源')} / {last_event.get('event_type', 'state_updated')} / {last_event.get('summary') or '无摘要'}")
    events = state.get("events")
    if isinstance(events, list):
        latest_human_event = next(
            (event for event in reversed(events) if isinstance(event, Mapping) and event.get("actor_source") in {"workbench", "feishu", "main_agent"}),
            None,
        )
        if latest_human_event is not None and latest_human_event.get("revision") != (last_event.get("revision") if isinstance(last_event, Mapping) else None):
            lines.append(f"最近人工操作：revision {latest_human_event.get('revision', '?')} / {latest_human_event.get('actor_source')} / {latest_human_event.get('summary') or latest_human_event.get('event_type', 'state_updated')}")
    lines.append("</bug_workbench_snapshot>")
    return "\n".join(lines)


def _strip_leading_feishu_mention(text: str) -> str:
    """Remove a group-routing mention without depending on the bot display name."""
    return _FEISHU_LEADING_MENTION_PATTERN.sub("", text, count=1)


def _auth_disabled_owner_user_id() -> str | None:
    try:
        from app.gateway.auth_disabled import AUTH_DISABLED_USER_ID, is_auth_disabled
    except Exception:
        logger.debug("Unable to inspect auth-disabled mode for channel owner fallback", exc_info=True)
        return None
    return AUTH_DISABLED_USER_ID if is_auth_disabled() else None


def _effective_owner_user_id(msg: InboundMessage) -> str | None:
    return _auth_disabled_owner_user_id() or msg.owner_user_id


def _apply_effective_owner(msg: InboundMessage) -> InboundMessage:
    owner_user_id = _effective_owner_user_id(msg)
    if owner_user_id:
        msg.owner_user_id = owner_user_id
    return msg


def _owner_headers(msg: InboundMessage) -> dict[str, str] | None:
    owner_user_id = _effective_owner_user_id(msg)
    if not owner_user_id:
        return None
    return create_internal_auth_headers(owner_user_id=owner_user_id)


def _safe_user_id_for_run(raw_user_id: str) -> str:
    from deerflow.config.paths import get_paths

    try:
        return get_paths().prepare_user_dir_for_raw_id(raw_user_id)
    except Exception:
        logger.exception("Failed to prepare channel run user directory")
        return make_safe_user_id(raw_user_id)


def _channel_storage_user_id(msg: InboundMessage) -> str | None:
    """Resolve the canonical DeerFlow user id for a channel-triggered message.

    Single source of truth for both the agent **run identity**
    (``_resolve_run_params`` → ``run_context["user_id"]``) and the **file/artifact
    storage bucket** (``receive_file`` / ``_ingest_inbound_files`` /
    ``_prepare_artifact_delivery``), so the bucket the agent reads/writes always
    matches where channel files are staged. Prefer the bound DeerFlow owner,
    otherwise fall back to the sanitized raw platform user id. Without that
    fallback, an unbound auth-enabled channel would run under ``safe(msg.user_id)``
    but stage files under ``get_effective_user_id()`` (the dispatcher task's unset
    contextvar → ``"default"``), so uploads would land in ``users/default/...``
    while the agent reads ``users/{safe_platform_user_id}/...``. Returns ``None``
    only when neither identity is available, leaving the caller to fall back to the
    contextvar/default user.

    Distinct from :func:`_owner_headers`, which deliberately sends the *raw* owner
    id (no sanitize, no platform fallback) over HTTP for gateway to re-resolve;
    this helper is the in-process, sanitized, filesystem-facing identity.
    """
    owner_user_id = _effective_owner_user_id(msg)
    if owner_user_id:
        return _safe_user_id_for_run(owner_user_id)
    if msg.user_id:
        return _safe_user_id_for_run(msg.user_id)
    return None


def _resolve_slash_skill_command(
    text: str,
    available_skills: set[str] | None = None,
    storage: SkillStorage | Callable[[], SkillStorage] | None = None,
) -> _SlashSkillCommandResolution | None:
    reference = parse_slash_skill_reference(text)
    if reference is None:
        return None
    try:
        resolved_storage = storage() if callable(storage) else storage or get_or_new_skill_storage()
        skills = resolved_storage.load_skills(enabled_only=False)

        skill = next((candidate for candidate in skills if candidate.name == reference.name), None)
        if skill is None:
            return None
        if not skill.enabled:
            return _SlashSkillCommandResolution(failure_message=f"Skill `/{reference.name}` is installed but disabled. Enable it before using slash activation.")
        if available_skills is not None and reference.name not in available_skills:
            return _SlashSkillCommandResolution(failure_message=f"Skill `/{reference.name}` is not available for this agent.")

        return _SlashSkillCommandResolution(route_to_chat=True)
    except Exception as exc:
        logger.exception("[Manager] failed to resolve slash skill command")
        raise SlashSkillCommandResolutionError("Failed to resolve slash skill command. Please check the skill configuration.") from exc


def _resolve_attachments(thread_id: str, artifacts: list[str], *, user_id: str | None = None) -> list[ResolvedAttachment]:
    """Resolve virtual artifact paths to host filesystem paths with metadata.

    Only paths under ``/mnt/user-data/outputs/`` are accepted; any other
    virtual path is rejected with a warning to prevent exfiltrating uploads
    or workspace files via IM channels.

    Skips artifacts that cannot be resolved (missing files, invalid paths)
    and logs warnings for them.
    """
    from deerflow.config.paths import get_paths

    attachments: list[ResolvedAttachment] = []
    paths = get_paths()
    effective_user_id = user_id or get_effective_user_id()
    outputs_dir = paths.sandbox_outputs_dir(thread_id, user_id=effective_user_id).resolve()
    for virtual_path in artifacts:
        # Security: only allow files from the agent outputs directory
        if not virtual_path.startswith(_OUTPUTS_VIRTUAL_PREFIX):
            logger.warning("[Manager] rejected non-outputs artifact path: %s", virtual_path)
            continue
        try:
            actual = paths.resolve_virtual_path(thread_id, virtual_path, user_id=effective_user_id)
            # Verify the resolved path is actually under the outputs directory
            # (guards against path-traversal even after prefix check)
            try:
                actual.resolve().relative_to(outputs_dir)
            except ValueError:
                logger.warning("[Manager] artifact path escapes outputs dir: %s -> %s", virtual_path, actual)
                continue
            if not actual.is_file():
                logger.warning("[Manager] artifact not found on disk: %s -> %s", virtual_path, actual)
                continue
            mime, _ = mimetypes.guess_type(str(actual))
            mime = mime or "application/octet-stream"
            attachments.append(
                ResolvedAttachment(
                    virtual_path=virtual_path,
                    actual_path=actual,
                    filename=actual.name,
                    mime_type=mime,
                    size=actual.stat().st_size,
                    is_image=mime.startswith("image/"),
                )
            )
        except (ValueError, OSError) as exc:
            logger.warning("[Manager] failed to resolve artifact %s: %s", virtual_path, exc)
    return attachments


def _prepare_artifact_delivery(
    thread_id: str,
    response_text: str,
    artifacts: list[str],
    *,
    user_id: str | None = None,
) -> tuple[str, list[ResolvedAttachment]]:
    """Resolve attachments and append filename fallbacks to the text response."""
    attachments: list[ResolvedAttachment] = []
    if not artifacts:
        return response_text, attachments

    attachments = _resolve_attachments(thread_id, artifacts, user_id=user_id)
    resolved_virtuals = {attachment.virtual_path for attachment in attachments}
    unresolved = [path for path in artifacts if path not in resolved_virtuals]

    if unresolved:
        artifact_text = _format_artifact_text(unresolved)
        response_text = (response_text + "\n\n" + artifact_text) if response_text else artifact_text

    # Always include resolved attachment filenames as a text fallback so files
    # remain discoverable even when the upload is skipped or fails.
    if attachments:
        resolved_text = _format_artifact_text([attachment.virtual_path for attachment in attachments])
        response_text = (response_text + "\n\n" + resolved_text) if response_text else resolved_text

    return response_text, attachments


async def _ingest_inbound_files(thread_id: str, msg: InboundMessage, *, user_id: str | None = None) -> list[dict[str, Any]]:
    if not msg.files:
        return []

    from deerflow.uploads.manager import (
        UnsafeUploadPathError,
        claim_unique_filename,
        ensure_uploads_dir,
        normalize_filename,
        write_upload_file_no_symlink,
    )

    def _prepare_uploads_dir() -> tuple[Path, set[str]]:
        # Worker thread: ensure_uploads_dir's mkdir and the iterdir enumeration are
        # blocking filesystem IO that must stay off the event loop.
        target = ensure_uploads_dir(thread_id, user_id=user_id)
        existing = {entry.name for entry in target.iterdir() if entry.is_file()}
        return target, existing

    uploads_dir, seen_names = await asyncio.to_thread(_prepare_uploads_dir)

    created: list[dict[str, Any]] = []
    file_reader = INBOUND_FILE_READERS.get(msg.channel_name, _read_http_inbound_file)
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as client:
        for idx, f in enumerate(msg.files):
            if not isinstance(f, dict):
                continue

            ftype = f.get("type") if isinstance(f.get("type"), str) else "file"
            filename = f.get("filename") if isinstance(f.get("filename"), str) else ""

            inline_content = f.pop(INBOUND_FILE_CONTENT_KEY, None)
            if isinstance(inline_content, bytes):
                data = inline_content
            elif isinstance(inline_content, (bytearray, memoryview)):
                data = bytes(inline_content)
            else:
                try:
                    data = await file_reader(f, client)
                except Exception:
                    logger.exception(
                        "[Manager] failed to read inbound file: channel=%s, file=%s",
                        msg.channel_name,
                        f.get("url") or filename or idx,
                    )
                    continue

            if data is None:
                logger.warning(
                    "[Manager] inbound file reader returned no data: channel=%s, file=%s",
                    msg.channel_name,
                    f.get("url") or filename or idx,
                )
                continue

            if not filename:
                ext = ".bin"
                if ftype == "image":
                    ext = ".png"
                filename = f"{msg.thread_ts or 'msg'}_{idx}{ext}"

            try:
                safe_name = claim_unique_filename(normalize_filename(filename), seen_names)
            except ValueError:
                logger.warning(
                    "[Manager] skipping inbound file with unsafe filename: channel=%s, file=%r",
                    msg.channel_name,
                    filename,
                )
                continue

            dest = uploads_dir / safe_name
            try:
                dest = await asyncio.to_thread(write_upload_file_no_symlink, uploads_dir, safe_name, data)
            except UnsafeUploadPathError:
                logger.warning("[Manager] skipping inbound file with unsafe destination: %s", safe_name)
                continue
            except Exception:
                logger.exception("[Manager] failed to write inbound file: %s", dest)
                continue

            created.append(
                {
                    "filename": safe_name,
                    "size": len(data),
                    "path": f"/mnt/user-data/uploads/{safe_name}",
                    "is_image": ftype == "image",
                }
            )

    return created


class ChannelManager:
    """Core dispatcher that bridges IM channels to the DeerFlow agent.

    It reads from the MessageBus inbound queue, creates/reuses threads on
    Gateway's LangGraph-compatible API, sends messages via ``runs.wait``, and publishes
    outbound responses back through the bus.
    """

    def __init__(
        self,
        bus: MessageBus,
        store: ChannelStore,
        *,
        max_concurrency: int = 5,
        langgraph_url: str = DEFAULT_LANGGRAPH_URL,
        gateway_url: str = DEFAULT_GATEWAY_URL,
        assistant_id: str = DEFAULT_ASSISTANT_ID,
        default_session: dict[str, Any] | None = None,
        channel_sessions: dict[str, Any] | None = None,
        bug_workbench_owner_user_id: str | None = None,
        bug_workbench_url: str = DEFAULT_BUG_WORKBENCH_URL,
        connection_repo: Any | None = None,
        require_bound_identity: bool = False,
        inbound_dedupe_store: InboundDedupeStore | None = None,
        get_stream_bridge: Callable[[], StreamBridge | None] | None = None,
    ) -> None:
        self.bus = bus
        self._message_tasks: set[asyncio.Task] = set()
        self.store = store
        self._max_concurrency = max_concurrency
        self._langgraph_url = langgraph_url
        self._gateway_url = gateway_url
        self._assistant_id = assistant_id
        self._default_session = _as_dict(default_session)
        self._channel_sessions = dict(channel_sessions or {})
        # Bug Workbench is intentionally a shared service: its specialist
        # agents and ZenTao credential live under one operator-configured
        # DeerFlow owner, while normal channel chat keeps each sender's usual
        # identity.  Never accept this value from an inbound message.
        self._bug_workbench_owner_user_id = (bug_workbench_owner_user_id.strip() if isinstance(bug_workbench_owner_user_id, str) else "") or None
        self._bug_workbench_url = bug_workbench_url.strip() or DEFAULT_BUG_WORKBENCH_URL
        self._connection_repo = connection_repo
        self._require_bound_identity = require_bound_identity
        # Zero-arg accessor for the FastAPI app's StreamBridge singleton,
        # threaded in from app.py's lifespan via start_channel_service() ->
        # ChannelService.__init__ (mirrors how ScheduledTaskService gets a
        # launch_run closure over `app` in the same lifespan function). None
        # when not wired (e.g. a ChannelManager constructed directly in
        # tests) — follow-up buffering still works, but no watcher is
        # spawned to auto-drain it (see _maybe_spawn_followup_watcher).
        self._get_stream_bridge = get_stream_bridge
        self._client = None  # lazy init — langgraph_sdk async client
        self._channel_metadata_synced: set[str] = set()
        # Per-conversation locks so concurrent inbound messages for the same
        # chat don't race to create duplicate threads (see _get_or_create_thread).
        self._thread_create_locks: dict[tuple[str, str, str | None], asyncio.Lock] = {}
        # Per-thread run locks for channels that want in-manager serialization
        # instead of surfacing the runtime's generic busy reply.
        self._serialized_thread_runs: dict[tuple[str, str], _SerializedThreadRunState] = {}
        self._skill_storage: SkillStorage | None = None
        self._csrf_token = generate_csrf_token()
        self._semaphore: asyncio.Semaphore | None = None
        self._running = False
        # Distinct from self._running: that flag is also False before the
        # very first start() (so tests that call internal drain/handler
        # methods directly without going through start()/stop() keep
        # working unchanged). self._stopped tracks specifically whether
        # stop() has run, for the follow-up drain guard below.
        self._stopped = False
        self._task: asyncio.Task | None = None
        # Inbound webhook dedupe store. Defaults to the in-process Memory store
        # (pre-#4120 behavior). Multi-pod deployments inject a shared store so
        # duplicate deliveries landing on different pods are collapsed.
        self._inbound_dedupe_store = inbound_dedupe_store if inbound_dedupe_store is not None else MemoryInboundDedupeStore()
        # Per-thread follow-up buffers for busy fire_and_forget channels that
        # opted into ChannelRunPolicy.buffer_followups_on_busy (issue #4121
        # Slice 2). Keyed by thread_id -> OrderedDict[dedupe_key -> entry],
        # oldest-first, mirroring the dedupe store's shape but scoped
        # per-thread with a hard cap instead of a global TTL (see
        # _buffer_followup / _enforce_followup_cap).
        self._followup_buffers: dict[str, OrderedDict[str, _FollowupEntry]] = {}
        # Background watcher tasks spawned by _maybe_spawn_followup_watcher,
        # tracked so stop() can cancel+await them instead of leaving them as
        # orphaned fire-and-forget tasks that could still fire a follow-up
        # run after this manager has been shut down. Discarded via the same
        # task's done-callback (see _maybe_spawn_followup_watcher).
        self._followup_watcher_tasks: set[asyncio.Task] = set()
        # Feishu Bug Workbench runs are separate from the normal chat thread.
        # Keep only the newest workflow per active conversation so a concise
        # “继续” can resume Bug handling without falling through to the lead
        # agent. This is intentionally an in-memory convenience mapping: the
        # durable workflow record itself remains the source of truth.
        self._bug_workflow_conversations: dict[tuple[str, str, str, str], _BugWorkflowConversation] = {}
        self._bug_workflow_watcher_tasks: set[asyncio.Task] = set()

    @staticmethod
    def _channel_supports_streaming(channel_name: str) -> bool:
        from .service import get_channel_service

        service = get_channel_service()
        if service:
            channel = service.get_channel(channel_name)
            if channel is not None:
                return channel.supports_streaming
        return CHANNEL_CAPABILITIES.get(channel_name, {}).get("supports_streaming", False)

    def _resolve_session_layer(self, msg: InboundMessage) -> tuple[dict[str, Any], dict[str, Any]]:
        channel_layer = _as_dict(self._channel_sessions.get(msg.channel_name))
        users_layer = _as_dict(channel_layer.get("users"))
        user_layer = _as_dict(users_layer.get(msg.user_id))
        return channel_layer, user_layer

    def _begin_serialized_thread_run(
        self,
        *,
        channel_name: str,
        thread_id: str,
    ) -> tuple[_SerializedThreadRunState | None, bool]:
        policy = CHANNEL_RUN_POLICY.get(channel_name)
        if policy is None or not policy.serialize_thread_runs:
            return None, False

        key = (channel_name, thread_id)
        state = self._serialized_thread_runs.get(key)
        if state is None:
            state = _SerializedThreadRunState(lock=asyncio.Lock())
            self._serialized_thread_runs[key] = state
        queued = state.lock.locked()
        state.waiters += 1
        return state, queued

    def _finish_serialized_thread_run(
        self,
        *,
        channel_name: str,
        thread_id: str,
        state: _SerializedThreadRunState | None,
        lock_acquired: bool,
    ) -> None:
        if state is None:
            return

        if lock_acquired:
            state.lock.release()
        state.waiters -= 1
        if state.waiters == 0 and not state.lock.locked():
            self._serialized_thread_runs.pop((channel_name, thread_id), None)

    # -- follow-up buffering for busy fire_and_forget threads (issue #4121) --

    def _resolve_stream_bridge(self) -> StreamBridge | None:
        """Resolve the current StreamBridge via the injected accessor, if any."""
        if self._get_stream_bridge is None:
            return None
        try:
            return self._get_stream_bridge()
        except Exception:
            logger.exception("[Manager] get_stream_bridge callable raised; follow-up watch disabled for this run")
            return None

    def _enforce_followup_cap(self, thread_id: str, buffer: OrderedDict[str, _FollowupEntry]) -> None:
        """Drop the OLDEST buffered entries once *buffer* exceeds the per-thread cap.

        Dropping the oldest (rather than the newest, incoming) entry means a
        thread that is deep enough in the backlog to hit the cap still keeps
        the most recent activity — a better signal for the eventual coalesced
        turn than the stalest queued comment. No reaction/acknowledgment is
        sent on drop (out of scope for this slice); a WARNING is logged so
        operators can see it in gateway.log.
        """
        while len(buffer) > FOLLOWUP_BUFFER_MAX_PER_THREAD:
            dropped_key, _ = buffer.popitem(last=False)
            logger.warning(
                "[Manager] follow-up buffer overflow for thread_id=%s (cap=%d); dropped oldest buffered comment (dedupe_key=%s)",
                thread_id,
                FOLLOWUP_BUFFER_MAX_PER_THREAD,
                dropped_key,
            )

    def _buffer_followup(self, thread_id: str, msg: InboundMessage) -> None:
        """Append *msg* to thread_id's follow-up buffer (ConflictError path).

        Dedupe mirrors ``_is_duplicate_inbound``'s OrderedDict idiom, scoped
        per-thread instead of global: a redelivered webhook for a comment
        already buffered (same dedupe key) is a no-op instead of a second
        entry.
        """
        key = _followup_dedupe_key(msg)
        buffer = self._followup_buffers.setdefault(thread_id, OrderedDict())
        if key in buffer:
            logger.info(
                "[Manager] duplicate follow-up ignored for thread_id=%s (dedupe_key=%s)",
                thread_id,
                key,
            )
            return

        buffer[key] = _FollowupEntry(dedupe_key=key, text=msg.text)
        self._enforce_followup_cap(thread_id, buffer)
        logger.info(
            "[Manager] buffered follow-up for busy thread_id=%s (dedupe_key=%s, buffered=%d)",
            thread_id,
            key,
            len(buffer),
        )

    def _pop_followup_batch(self, thread_id: str, *, limit: int) -> list[_FollowupEntry]:
        """Pop up to *limit* buffered entries FIFO (oldest first)."""
        buffer = self._followup_buffers.get(thread_id)
        if not buffer:
            return []

        batch: list[_FollowupEntry] = []
        for _ in range(min(limit, len(buffer))):
            _, entry = buffer.popitem(last=False)
            batch.append(entry)

        if not buffer:
            self._followup_buffers.pop(thread_id, None)
        return batch

    def _requeue_followups(self, thread_id: str, entries: list[_FollowupEntry]) -> None:
        """Put a popped batch back at the front of the buffer (oldest-first).

        Used when the drain's own ``runs.create`` call itself fails —
        including the ``ConflictError`` edge case where something this
        manager did not create (a manual Web UI turn, a scheduled run) is
        occupying the thread. The entries are not lost: the next time *any*
        run this manager creates on this thread completes, its watcher will
        attempt another drain and find them still buffered.
        """
        if not entries:
            return

        existing = self._followup_buffers.get(thread_id, OrderedDict())
        merged: OrderedDict[str, _FollowupEntry] = OrderedDict()
        for entry in entries:
            merged[entry.dedupe_key] = entry
        for key, entry in existing.items():
            merged.setdefault(key, entry)

        self._enforce_followup_cap(thread_id, merged)
        self._followup_buffers[thread_id] = merged

    def _maybe_spawn_followup_watcher(
        self,
        thread_id: str,
        run_result: Any,
        carrier_msg: InboundMessage,
    ) -> None:
        """Spawn a background watcher for a just-created run, if wired up.

        No-ops (spawns nothing) when no ``get_stream_bridge`` accessor was
        threaded in — e.g. a ``ChannelManager`` constructed directly without
        going through ``start_channel_service()`` — so tests and any
        not-yet-wired deployment never see a dangling background task for
        this. When wired, mirrors the existing ``_dispatch_loop`` pattern:
        ``asyncio.create_task`` + ``add_done_callback(self._log_task_error)``
        so an unexpected watcher failure is surfaced in the logs instead of
        silently vanishing. The task is also tracked in
        ``self._followup_watcher_tasks`` (discarded via its own done-callback)
        so ``stop()`` can cancel+await any watcher still in flight instead of
        leaving it to fire a follow-up run after shutdown.
        """
        if self._get_stream_bridge is None:
            return

        run_id = run_result.get("run_id") if isinstance(run_result, dict) else None
        if not run_id:
            logger.warning(
                "[Manager] runs.create returned no run_id for thread_id=%s; cannot watch for follow-up drain",
                thread_id,
            )
            return

        task = asyncio.create_task(self._watch_run_and_drain_followups(thread_id, run_id, carrier_msg))
        self._followup_watcher_tasks.add(task)
        task.add_done_callback(self._followup_watcher_tasks.discard)
        task.add_done_callback(self._log_task_error)

    async def _watch_run_and_drain_followups(
        self,
        thread_id: str,
        run_id: str,
        carrier_msg: InboundMessage,
    ) -> None:
        """Watch *run_id* until it ends, then attempt to drain thread_id's buffer.

        Subscribes to the StreamBridge the same way existing consumers do
        (``entry is END_SENTINEL``, see ``app/gateway/services.py``). Runs
        for as long as the underlying run does — GitHub coding runs
        routinely take several minutes, so this deliberately does not apply
        an artificial timeout, mirroring why the dispatch path itself uses
        ``runs.create`` instead of ``runs.wait`` in the first place. Draining
        is a no-op when the buffer is empty, which is the common case (most
        runs never hit a busy-thread conflict).
        """
        stream_bridge = self._resolve_stream_bridge()
        if stream_bridge is None:
            logger.warning(
                "[Manager] no stream bridge available; cannot watch run_id=%s for thread_id=%s follow-up drain (any buffered follow-ups will be drained by a later watched run on this thread)",
                run_id,
                thread_id,
            )
            return

        try:
            async for entry in stream_bridge.subscribe(run_id):
                if entry is END_SENTINEL:
                    break
        except Exception:
            logger.exception(
                "[Manager] error watching run_id=%s for thread_id=%s follow-up drain",
                run_id,
                thread_id,
            )
            return

        client = self._get_client()
        await self._drain_followups_for_thread(client, thread_id, carrier_msg)

    async def _drain_followups_for_thread(
        self,
        client,
        thread_id: str,
        carrier_msg: InboundMessage,
    ) -> None:
        """Coalesce up to one batch of buffered follow-ups into a fresh run.

        ``carrier_msg`` supplies routing/policy identity (channel_name,
        metadata, owner headers) for the drained run — it is safe to reuse
        across an entire drain chain because every buffered entry for one
        thread_id shares that identity (thread_id itself is derived
        deterministically from (repo, number, agent_name) for GitHub).

        A batch larger than ``FOLLOWUP_DRAIN_BATCH_SIZE`` is intentionally
        NOT drained in one shot: only the oldest batch is popped here, and
        the run created for it is itself watched (via
        ``_maybe_spawn_followup_watcher``), so a deeper backlog chains into
        another drain cycle once this run ends, rather than growing one
        unbounded coalesced input block.

        If anything from here through ``runs.create`` fails — resolving run
        params, applying channel policy, or ``runs.create`` itself
        (including ``ConflictError`` from something this manager did not
        create racing onto the same thread) — the popped batch is requeued
        (not lost) and this coroutine returns without raising or looping:
        the next run this manager successfully creates and watches on this
        thread will attempt the drain again.

        No-ops if the manager has already been ``stop()``-ped: a watcher
        task that slips past its own cancellation and reaches this point
        after shutdown must not fire a brand new run into a stopped manager.
        (Deliberately keyed on ``self._stopped``, not ``self._running`` —
        the latter is also ``False`` before the very first ``start()``,
        which would otherwise make this guard fire for callers that invoke
        the drain directly without going through the dispatch lifecycle.)
        """
        if self._stopped:
            logger.info(
                "[Manager] skipping follow-up drain for thread_id=%s; manager is stopped",
                thread_id,
            )
            return

        entries = self._pop_followup_batch(thread_id, limit=FOLLOWUP_DRAIN_BATCH_SIZE)
        if not entries:
            return

        logger.info(
            "[Manager] draining %d buffered follow-up(s) for thread_id=%s",
            len(entries),
            thread_id,
        )
        try:
            # Everything from here through runs.create is covered by the
            # same except below: a pre-create failure (e.g. the target agent
            # config was removed mid-run, or channel-policy/credential
            # resolution raises) must requeue the popped batch exactly like
            # a runs.create failure does — none of these steps get to
            # silently drop entries that were already popped off the buffer.
            assistant_id, run_config, run_context = self._resolve_run_params(carrier_msg, thread_id)
            await self._apply_channel_policy(carrier_msg, run_context)

            human_message = _human_input_message(_format_followup_block(entries))
            run_kwargs: dict[str, Any] = {
                "input": {"messages": [human_message]},
                "config": run_config,
                "context": run_context,
                "multitask_strategy": "reject",
            }
            if owner_headers := _owner_headers(carrier_msg):
                run_kwargs["headers"] = owner_headers

            result = await client.runs.create(thread_id, assistant_id, **run_kwargs)
        except Exception as exc:
            if _is_thread_busy_error(exc):
                logger.warning(
                    "[Manager] follow-up drain hit a busy thread_id=%s (a run this manager did not create is active); re-buffering %d entries",
                    thread_id,
                    len(entries),
                )
            else:
                logger.exception(
                    "[Manager] follow-up drain failed for thread_id=%s; re-buffering %d entries",
                    thread_id,
                    len(entries),
                )
            self._requeue_followups(thread_id, entries)
            return

        self._maybe_spawn_followup_watcher(thread_id, result, carrier_msg)

    async def _publish_progress_update(self, msg: InboundMessage, thread_id: str, text: str) -> None:
        await self.bus.publish_outbound(
            OutboundMessage(
                channel_name=msg.channel_name,
                chat_id=msg.chat_id,
                thread_id=thread_id,
                text=text,
                is_final=False,
                thread_ts=msg.thread_ts,
                connection_id=msg.connection_id,
                owner_user_id=msg.owner_user_id,
                metadata=_response_metadata(msg.metadata),
            )
        )

    def _resolve_run_params(self, msg: InboundMessage, thread_id: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
        channel_layer, user_layer = self._resolve_session_layer(msg)

        # Per-message agent override (e.g. GitHub webhook fan-out: multiple
        # agents may bind the same repo, each gets its own inbound message
        # with its own agent_name in metadata).  Honors the same shape as
        # channel/user session config: the bare agent name routes through
        # the lead_agent + agent_name context pattern below.
        message_assistant_id: str | None = None
        msg_metadata = msg.metadata if isinstance(msg.metadata, dict) else {}
        meta_assistant_id = msg_metadata.get("assistant_id") or msg_metadata.get("agent_name")
        if isinstance(meta_assistant_id, str) and meta_assistant_id.strip():
            message_assistant_id = meta_assistant_id

        assistant_id = message_assistant_id or user_layer.get("assistant_id") or channel_layer.get("assistant_id") or self._default_session.get("assistant_id") or self._assistant_id
        if not isinstance(assistant_id, str) or not assistant_id.strip():
            assistant_id = self._assistant_id

        run_config = _merge_dicts(
            DEFAULT_RUN_CONFIG,
            self._default_session.get("config"),
            channel_layer.get("config"),
            user_layer.get("config"),
        )

        configurable = run_config.get("configurable")
        if isinstance(configurable, Mapping):
            configurable = dict(configurable)
        else:
            configurable = {}
        run_config["configurable"] = configurable
        # Pin channel-triggered runs to the root graph namespace so follow-up
        # turns continue from the same conversation checkpoint.
        configurable["checkpoint_ns"] = ""
        configurable["thread_id"] = thread_id

        # ``user_id`` drives DeerFlow-owned memory, files, and thread buckets.
        # For browser-connected IM channels, prefer the DeerFlow account that
        # owns the connection. Preserve the raw platform user under
        # ``channel_user_id`` for platform-facing lookups and audits.
        run_context_identity: dict[str, Any] = {"thread_id": thread_id}
        # ``channel_name`` lets in-graph code (e.g. ``_make_lead_agent``)
        # decide whether a tool is safe to expose for this run. Webhook
        # channels carry untrusted external prompts (GitHub comments,
        # Telegram chats from non-owners, etc.), so admin-shaped tools
        # like ``update_agent`` are dropped when the run was triggered
        # via one. See ``_make_lead_agent`` for the gate.
        run_context_identity["channel_name"] = msg.channel_name
        # Single source of truth for the run identity: the same helper that scopes
        # inbound files and outbound artifacts, so the bucket the agent reads/writes
        # always matches where channel files are staged.
        run_user_id = _channel_storage_user_id(msg)
        if run_user_id:
            run_context_identity["user_id"] = run_user_id
        if msg.user_id:
            run_context_identity["channel_user_id"] = msg.user_id
        if msg.channel_name == "feishu" and self._bug_workbench_owner_user_id:
            # Bug Workbench is shared, but ordinary chat memory/files remain
            # scoped to run_context_identity["user_id"].  Carry the separate
            # owner only through the trusted internal run context so the
            # read-only workbench tool can query the same tasks as the web UI.
            run_context_identity[BUG_WORKBENCH_OWNER_USER_ID_CONTEXT_KEY] = self._bug_workbench_owner_user_id

        run_context = _merge_dicts(
            DEFAULT_RUN_CONTEXT,
            self._default_session.get("context"),
            channel_layer.get("context"),
            user_layer.get("context"),
            run_context_identity,
        )

        # Custom agents are implemented as lead_agent + agent_name context.
        # Keep backward compatibility for channel configs that set
        # assistant_id: <custom-agent-name> by routing through lead_agent.
        if assistant_id != DEFAULT_ASSISTANT_ID:
            run_context.setdefault("agent_name", _normalize_custom_agent_name(assistant_id))
            assistant_id = DEFAULT_ASSISTANT_ID

        # Apply per-channel run policy (recursion_limit bump for webhook
        # channels, etc.). Looking the policy up by channel_name keeps
        # GitHub-specific knobs out of this method — adding the next
        # webhook channel is a one-row CHANNEL_RUN_POLICY entry, not a
        # new if-branch here.
        policy = CHANNEL_RUN_POLICY.get(msg.channel_name)
        if policy is not None and policy.default_recursion_limit is not None:
            # Per-message override (via msg.metadata[channel_name]) honors
            # the operator's explicit per-agent recursion_limit verbatim —
            # including values below the channel default. A safety-conscious
            # ``github.recursion_limit: 50`` on a review-only agent now halts
            # at 50 super-steps as documented in GitHubAgentConfig, instead
            # of being silently clamped up to the channel default. When no
            # override is present, the channel default acts as a floor over
            # whatever session config supplied (the higher value wins).
            channel_meta = (msg.metadata or {}).get(msg.channel_name, {})
            override = channel_meta.get("recursion_limit") if isinstance(channel_meta, dict) else None
            if isinstance(override, int) and override > 0:
                run_config["recursion_limit"] = override
            else:
                run_config["recursion_limit"] = max(run_config.get("recursion_limit", 100), policy.default_recursion_limit)

        if run_context.get("agent_name") == "personal-assistant":
            from deerflow.runtime.main_assistant_policy import MAIN_ASSISTANT_RECURSION_LIMIT

            explicit_limit = any("recursion_limit" in (layer.get("config") or {}) for layer in (self._default_session, channel_layer, user_layer))
            if not explicit_limit:
                run_config["recursion_limit"] = MAIN_ASSISTANT_RECURSION_LIMIT
        return assistant_id, run_config, run_context

    async def _apply_channel_policy(self, msg: InboundMessage, run_context: dict[str, Any]) -> ChannelRunPolicy | None:
        """Apply per-channel run policy that needs ``run_context`` access.

        Run AFTER ``_resolve_run_params`` (which produced ``run_context``)
        and BEFORE the agent runs. Covers:

        * ``disable_clarification`` for non-interactive channels —
          ``ClarificationMiddleware`` would otherwise dead-end a webhook
          run waiting for a synchronous reply that only arrives as a
          later, separate webhook delivery.
        * Channel-specific credentials provider — e.g. the GitHub channel
          installs a token-mint callable so ``bash_tool`` can resolve a
          fresh installation token on every invocation (longer than the
          1h GitHub TTL).

        ``recursion_limit`` is applied inside :meth:`_resolve_run_params`
        instead because it lives on ``run_config`` (not ``run_context``)
        and the resolver already builds ``run_config``.

        Returns the resolved :class:`ChannelRunPolicy` (or ``None`` when
        the channel has no entry) so :meth:`_handle_chat` can branch on
        flags like ``fire_and_forget`` without doing a second dict
        lookup.
        """
        policy = CHANNEL_RUN_POLICY.get(msg.channel_name)
        if policy is None:
            return None
        if not policy.is_interactive:
            run_context["disable_clarification"] = True
        if policy.credentials_provider is not None:
            try:
                await policy.credentials_provider(msg, run_context)
            except Exception:
                # Credential failures must NOT drop the delivery — the
                # provider's own logging records the cause; we keep the
                # run going (read-only is better than no response).
                logger.warning(
                    "[Manager] channel=%s credentials_provider raised; run proceeds without injected credentials",
                    msg.channel_name,
                    exc_info=True,
                )
        return policy

    def _resolve_available_skill_names(self, msg: InboundMessage) -> set[str] | None:
        thread_id = self.store.get_thread_id(msg.channel_name, msg.chat_id, topic_id=msg.topic_id) or ""
        _, _, run_context = self._resolve_run_params(msg, thread_id)
        if run_context.get("is_bootstrap"):
            return {"bootstrap"}

        agent_name = run_context.get("agent_name")
        if not isinstance(agent_name, str) or not agent_name.strip():
            return None

        # Read the agent config from the same owner bucket the run uses:
        # ``run_context["user_id"]`` is the resolved owner (``_channel_storage_user_id``),
        # but without it ``load_agent_config`` falls back to the dispatch loop's unset
        # contextvar (``"default"``), reading the wrong user's per-user custom agent.
        agent_config = load_agent_config(_normalize_custom_agent_name(agent_name), user_id=run_context.get("user_id"))
        if agent_config and agent_config.skills is not None:
            return set(agent_config.skills)
        return None

    # -- LangGraph SDK client (lazy) ----------------------------------------

    def _get_client(self):
        """Return the ``langgraph_sdk`` async client, creating it on first use."""
        if self._client is None:
            from langgraph_sdk import get_client

            self._client = get_client(
                url=self._langgraph_url,
                headers={
                    **create_internal_auth_headers(),
                    CSRF_HEADER_NAME: self._csrf_token,
                    "Cookie": f"{CSRF_COOKIE_NAME}={self._csrf_token}",
                },
            )
        return self._client

    def _get_skill_storage(self) -> SkillStorage:
        if self._skill_storage is None:
            self._skill_storage = get_or_new_skill_storage()
        return self._skill_storage

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Start the dispatch loop."""
        if self._running:
            return
        self._running = True
        self._stopped = False
        self._semaphore = asyncio.Semaphore(self._max_concurrency)
        self._task = asyncio.create_task(self._dispatch_loop())
        logger.info("ChannelManager started (max_concurrency=%d)", self._max_concurrency)

    async def stop(self) -> None:
        """Stop the dispatch loop."""
        self._running = False
        self._stopped = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        # Keep channel listeners alive until accepted chat replies are finalized.
        pending_messages = set(self._message_tasks)
        if pending_messages:
            _, pending_messages = await asyncio.wait(pending_messages, timeout=20.0)
            for task in pending_messages:
                task.cancel()
            if pending_messages:
                await asyncio.gather(*pending_messages, return_exceptions=True)

        # Follow-up watchers are long-lived background tasks (they await a
        # run's full stream, which can take minutes) started outside the
        # dispatch loop, so cancelling self._task above does not touch them.
        # Left unmanaged, one still subscribed to a run that ends AFTER this
        # point would drain its buffer and fire a brand new runs.create()
        # into a manager that has already been stopped.
        watcher_tasks = list(self._followup_watcher_tasks)
        for task in watcher_tasks:
            task.cancel()
        for task in watcher_tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("[Manager] follow-up watcher task raised during stop()")
        self._followup_watcher_tasks.clear()

        bug_watcher_tasks = list(self._bug_workflow_watcher_tasks)
        for task in bug_watcher_tasks:
            task.cancel()
        for task in bug_watcher_tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("[Manager] Bug workflow watcher task raised during stop()")
        self._bug_workflow_watcher_tasks.clear()

        logger.info("ChannelManager stopped")

    # -- dispatch loop -----------------------------------------------------

    async def _dispatch_loop(self) -> None:
        logger.info("[Manager] dispatch loop started, waiting for inbound messages")
        while self._running:
            try:
                msg = await asyncio.wait_for(self.bus.get_inbound(), timeout=1.0)
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            # Dedupe before logging "received" so a provider retrying an event N
            # times does not log N accepts; duplicates are logged once as ignored.
            # Note: this manager-level dedupe only guards the agent run / final
            # answer. Provider adapters may emit ack side-effects (a "Working on
            # it…" reply, an "eyes" reaction) before publish_inbound, so those are
            # intentionally not deduped here.
            if await self._is_duplicate_inbound(msg):
                continue
            logger.info(
                "[Manager] received inbound: channel=%s, chat_id=%s, type=%s, text_len=%d, files=%d",
                msg.channel_name,
                msg.chat_id,
                msg.msg_type.value,
                len(msg.text or ""),
                len(msg.files),
            )
            task = asyncio.create_task(self._handle_message(msg))
            self._message_tasks.add(task)
            task.add_done_callback(self._message_tasks.discard)
            task.add_done_callback(self._log_task_error)

    @staticmethod
    def _inbound_dedupe_key(msg: InboundMessage) -> tuple[str, str, str, str] | None:
        metadata = msg.metadata or {}
        message_id = None
        for key in INBOUND_DEDUPE_METADATA_KEYS:
            value = metadata.get(key)
            if value:
                message_id = str(value)
                break
        if message_id is None:
            raw_message = metadata.get("raw_message")
            if isinstance(raw_message, Mapping):
                for key in INBOUND_DEDUPE_METADATA_KEYS:
                    value = raw_message.get(key)
                    if value:
                        message_id = str(value)
                        break
        if message_id is None:
            return None

        # Fail closed: without a workspace/team/guild identifier we cannot tell two
        # workspaces apart (e.g. Slack channel ids are not globally unique), so
        # skip dedupe rather than risk collapsing distinct workspaces' messages.
        # Both fallbacks are appended last and gated on every earlier source being
        # absent, so they can only turn "no key" into a key — never change one.
        # A conversation_id not reused across a provider's own redelivery would
        # degrade to today's no-dedupe behaviour, never collapse two conversations
        # (chat_id and message_id stay in the tuple). conversation_id covers
        # DingTalk (group + P2P); chat-scoped providers fall back to chat_id.
        workspace_id = msg.workspace_id or metadata.get("workspace_id") or metadata.get("team_id") or metadata.get("guild_id") or metadata.get("aibotid") or metadata.get("conversation_id")
        if not workspace_id and msg.channel_name in CHAT_SCOPED_WORKSPACE_CHANNELS:
            workspace_id = msg.chat_id or None
        if not workspace_id:
            return None
        return (msg.channel_name, str(workspace_id), msg.chat_id, message_id)

    async def _is_duplicate_inbound(self, msg: InboundMessage) -> bool:
        key = self._inbound_dedupe_key(msg)
        if key is None:
            return False

        # Delegated to the shared/per-pod dedupe store. The store owns TTL eviction
        # and capacity bounds; try_record returns True when the key was already
        # present (i.e. this is a duplicate delivery to drop).
        is_duplicate = await self._inbound_dedupe_store.try_record(key)
        if is_duplicate:
            logger.info(
                "[Manager] duplicate inbound ignored: channel=%s, chat_id=%s, message_id=%s",
                msg.channel_name,
                msg.chat_id,
                key[-1],
            )
        return is_duplicate

    async def _release_inbound_dedupe_key(self, msg: InboundMessage) -> None:
        """Drop a recorded dedupe key so a provider redelivery can be reprocessed.

        Called only on transient/unexpected handling failures: the key was
        recorded on receipt so retries arriving *while* the message is being
        handled are still deduped, but if handling fails we must not turn a
        recoverable error into a TTL-long black hole for the same message_id.
        """
        key = self._inbound_dedupe_key(msg)
        if key is not None:
            await self._inbound_dedupe_store.release(key)

    @staticmethod
    def _log_task_error(task: asyncio.Task) -> None:
        """Surface unhandled exceptions from background tasks."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error("[Manager] unhandled error in message task: %s", exc, exc_info=exc)

    async def _handle_message(self, msg: InboundMessage) -> None:
        msg = _apply_effective_owner(msg)
        # Team assistant uses one operator-owned account across Feishu surfaces.
        if msg.channel_name == "feishu" and self._bug_workbench_owner_user_id:
            _, _, assistant_context = self._resolve_run_params(msg, "")
            if assistant_context.get("agent_name") == "personal-assistant":
                msg.owner_user_id = self._bug_workbench_owner_user_id
        try:
            # Non-command chat can be rejected before it consumes a semaphore
            # slot. Commands are handled below because provider adapters consume
            # binding commands before manager dispatch, and _handle_command()
            # applies its own admission gate for manager-level commands.
            bound_identity_rejection = None
            if msg.msg_type != InboundMessageType.COMMAND:
                bound_identity_rejection = await self._get_bound_identity_rejection(msg)
            if bound_identity_rejection is not None:
                await self._reject_unbound_channel_message(msg, bound_identity_rejection=bound_identity_rejection)
                return

            async with self._semaphore:
                if msg.msg_type == InboundMessageType.COMMAND:
                    await self._handle_command(msg)
                else:
                    # Numeric shortcut is terminal; natural-language Bug requests
                    # belong to the main assistant. Preserve pending clarification.
                    shortcut_text = _strip_leading_feishu_mention((msg.text or "").strip())
                    explicit_bug = _extract_zentao_bug_id(shortcut_text)
                    natural_bug_request = explicit_bug is not None and not shortcut_text.isdecimal()
                    batch_request = _is_main_agent_bug_batch_request(shortcut_text)
                    if not natural_bug_request and not batch_request and await self._try_dispatch_feishu_bug_workflow(msg):
                        return
                    await self._handle_chat(msg, bound_identity_checked=True)
        except InvalidChannelSessionConfigError as exc:
            logger.warning(
                "Invalid channel session config for %s (chat=%s): %s",
                msg.channel_name,
                msg.chat_id,
                exc,
            )
            await self._send_error(msg, str(exc))
        except SlashSkillCommandResolutionError as exc:
            logger.warning(
                "Slash skill command resolution failed for %s (chat=%s): %s",
                msg.channel_name,
                msg.chat_id,
                exc,
            )
            await self._send_error(msg, str(exc))
        except Exception:
            logger.exception(
                "Error handling message from %s (chat=%s)",
                msg.channel_name,
                msg.chat_id,
            )
            # Transient/unexpected failure: release the dedupe key so a provider
            # redelivery of the same message can recover instead of being dropped
            # for the dedupe TTL.
            await self._release_inbound_dedupe_key(msg)
            await self._send_error(msg, "An internal error occurred. Please try again.")

    # -- Feishu Bug Workbench dispatch ------------------------------------

    @staticmethod
    def _bug_workflow_conversation_key(msg: InboundMessage) -> tuple[str, str, str, str]:
        """Scope a Bug workflow to the exact external conversation."""
        return (msg.channel_name, msg.connection_id or "", msg.chat_id, msg.topic_id or "")

    @staticmethod
    def _serialize_bug_workflow_conversation_key(key: tuple[str, str, str, str]) -> str:
        """Persist an exact, opaque channel scope for restart recovery."""
        # Provider IDs do not contain ASCII Unit Separator. It avoids a JSON
        # dependency and cannot collide with normal Feishu IDs.
        return "\x1f".join(key)

    async def _publish_bug_workflow_message(self, msg: InboundMessage, text: str) -> None:
        """Reply directly to Feishu without creating a normal agent run."""
        await self.bus.publish_outbound(
            OutboundMessage(
                channel_name=msg.channel_name,
                chat_id=msg.chat_id,
                thread_id="",
                text=text,
                thread_ts=msg.thread_ts,
                connection_id=msg.connection_id,
                owner_user_id=msg.owner_user_id,
                metadata=_slim_metadata(msg.metadata),
            )
        )

    async def publish_bug_workflow_external_action(self, workflow: Mapping[str, Any]) -> None:
        """Notify and resume watching when Workbench advances a Feishu-bound task."""
        serialized_key = workflow.get("channel_key")
        if not isinstance(serialized_key, str):
            return
        parts = serialized_key.split("\x1f")
        if len(parts) != 4 or parts[0] != "feishu":
            return
        channel_name, connection_id, chat_id, topic_id = parts
        event = workflow.get("last_event") if isinstance(workflow.get("last_event"), Mapping) else {}
        if event.get("actor_source") != "workbench":
            return
        bug_id = int(workflow.get("bug_id") or 0)
        event_type = str(event.get("event_type") or "state_updated")
        summaries = {
            "product_target_confirmed": (
                "已在工作台确认文案修改范围，Bug Workbench 现在开始源码调查，最终责任端与根因以源码证据为准。"
                if _has_confirmed_preanalysis_copy_scope(workflow)
                else "已在工作台确认产品目标，Bug Workbench 现在开始源码调查，最终责任端与根因以源码证据为准。"
            ),
            "repair_started": "Bug 工作台已自动开始修复，Bug Workbench 正在执行。",
            "repair_accepted": "已在工作台确认验收，本次修复已完成。",
            "repair_rolled_back": "已在工作台选择回退，本次自动修改已恢复。",
            "workflow_cancelled": "已在工作台取消当前任务。",
        }
        action_text = summaries.get(event_type)
        if not action_text:
            return
        message = InboundMessage(
            channel_name=channel_name,
            connection_id=connection_id or None,
            chat_id=chat_id,
            topic_id=topic_id or None,
            thread_ts=topic_id or None,
            user_id="bug-workbench",
            owner_user_id=str(workflow.get("channel_owner_user_id") or self._bug_workbench_owner_user_id or "") or None,
            text="",
        )
        await self._publish_bug_workflow_message(message, f"禅道 Bug #{bug_id}：{action_text}")
        key = (channel_name, connection_id, chat_id, topic_id)
        conversation = self._bug_workflow_conversations.get(key)
        if conversation is None:
            conversation = _BugWorkflowConversation(
                workflow_id=str(workflow.get("id") or ""),
                bug_id=bug_id,
            )
            self._bug_workflow_conversations[key] = conversation
        conversation.status = str(workflow.get("status") or conversation.status)
        conversation.last_notified_revision = int(workflow.get("revision") or 0)
        conversation.last_notified_status = conversation.status
        if conversation.status in _BUG_WORKFLOW_ACTIVE_STATUSES:
            self._watch_bug_workflow(message, key)

    async def _start_bug_workflow(
        self,
        msg: InboundMessage,
        bug_id: int,
        *,
        conversation_key: tuple[str, str, str, str],
    ) -> dict[str, Any]:
        """Start the existing Gateway workflow through its trusted local API."""
        headers = self._bug_workflow_headers(msg)
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                f"{self._gateway_url}/api/bug-workflows",
                json={
                    "bug_id": bug_id,
                    "auto_repair": False,
                    "channel_key": self._serialize_bug_workflow_conversation_key(conversation_key),
                },
                headers=headers,
            )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("id"), str):
            raise RuntimeError("Bug Workbench returned an invalid workflow response")
        return payload

    async def _get_bug_workflow(self, msg: InboundMessage, workflow_id: str) -> dict[str, Any]:
        """Read one workflow state using the same owner-scoped internal auth."""
        headers = self._bug_workflow_headers(msg)
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(
                f"{self._gateway_url}/api/bug-workflows/{quote(workflow_id, safe='')}",
                headers=headers,
            )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Bug Workbench returned an invalid workflow state")
        return payload

    async def launch_main_agent_bug(self, msg: InboundMessage, bug_id: int) -> dict[str, Any]:
        """Reuse launch and notification handling and report the actual binding."""
        msg.text = str(bug_id)
        await self._try_dispatch_feishu_bug_workflow(msg)
        conversation = self._bug_workflow_conversations.get(self._bug_workflow_conversation_key(msg))
        if conversation is None:
            return {"started": False, "completion_notification": False, "message": "工作台未创建任务，请依据当前会话的错误通知处理。"}
        return {"workflow_id": conversation.workflow_id, "bug_id": conversation.bug_id, "status": conversation.status, "completion_notification": True}

    async def _attach_latest_bug_workflow_snapshot(
        self,
        msg: InboundMessage,
        workflow: _BugWorkflowConversation,
    ) -> bool:
        """Attach fresh canonical state for the lead agent on any natural-language turn."""
        try:
            state = await self._get_bug_workflow(msg, workflow.workflow_id)
        except Exception:
            logger.exception("[Manager] failed to attach Bug workflow snapshot: workflow=%s", workflow.workflow_id)
            return False
        workflow.status = str(state.get("status") or workflow.status)
        msg.metadata = dict(msg.metadata)
        msg.metadata[_BUG_WORKBENCH_SNAPSHOT_METADATA_KEY] = _format_bug_workbench_summary_snapshot(state)
        return True

    async def _get_active_bug_workflow(
        self,
        msg: InboundMessage,
        conversation_key: tuple[str, str, str, str],
    ) -> dict[str, Any] | None:
        """Recover a paused/in-flight Bug workflow after a Gateway restart."""
        headers = self._bug_workflow_headers(msg)
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(
                f"{self._gateway_url}/api/bug-workflows/active",
                params={"channel_key": self._serialize_bug_workflow_conversation_key(conversation_key)},
                headers=headers,
            )
        response.raise_for_status()
        payload = response.json()
        if payload is None:
            return None
        if not isinstance(payload, dict) or not isinstance(payload.get("id"), str):
            raise RuntimeError("Bug Workbench returned an invalid active workflow response")
        return payload

    async def _get_latest_bug_workflow(
        self,
        msg: InboundMessage,
        conversation_key: tuple[str, str, str, str] | None,
    ) -> dict[str, Any] | None:
        """Read the newest chat-bound task, or the owner's latest Workbench task."""
        headers = self._bug_workflow_headers(msg)
        params = {"channel_key": self._serialize_bug_workflow_conversation_key(conversation_key)} if conversation_key is not None else None
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(
                f"{self._gateway_url}/api/bug-workflows/latest",
                params=params,
                headers=headers,
            )
        response.raise_for_status()
        payload = response.json()
        if payload is None:
            return None
        if not isinstance(payload, dict) or not isinstance(payload.get("id"), str):
            raise RuntimeError("Bug Workbench returned an invalid latest workflow response")
        return payload

    async def _submit_bug_workflow_clarification(
        self,
        msg: InboundMessage,
        workflow_id: str,
        *,
        answer: str,
        option_id: str | None = None,
    ) -> dict[str, Any]:
        """Resume a UI specialist with the team's requested product decision."""
        headers = self._bug_workflow_headers(msg)
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                f"{self._gateway_url}/api/bug-workflows/{quote(workflow_id, safe='')}/clarification",
                json={
                    "answer": answer,
                    "option_id": option_id,
                },
                headers=headers,
            )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Bug Workbench returned an invalid clarification response")
        return payload

    async def _cancel_bug_workflow(self, msg: InboundMessage, workflow_id: str) -> dict[str, Any]:
        """Cancel only a workflow waiting for a human response."""
        headers = self._bug_workflow_headers(msg)
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                f"{self._gateway_url}/api/bug-workflows/{quote(workflow_id, safe='')}/cancel",
                headers=headers,
            )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Bug Workbench returned an invalid cancel response")
        return payload

    def _bug_workflow_headers(self, msg: InboundMessage) -> dict[str, str]:
        """Build trusted headers for the shared Bug Workbench API path.

        The Gateway's CSRF middleware intentionally protects every mutating
        API route, including server-to-server channel calls.  The normal
        LangGraph SDK path sends this double-submit pair already; Bug
        Workbench uses plain HTTP and must send the same pair explicitly.
        """
        owner_user_id = self._bug_workbench_owner_user_id or _effective_owner_user_id(msg)
        headers = create_internal_auth_headers(owner_user_id=owner_user_id) if owner_user_id else create_internal_auth_headers()
        headers[CSRF_HEADER_NAME] = self._csrf_token
        headers["Cookie"] = f"{CSRF_COOKIE_NAME}={self._csrf_token}"
        headers["X-DeerFlow-Bug-Action-Source"] = "feishu"
        return headers

    def _watch_bug_workflow(self, msg: InboundMessage, conversation_key: tuple[str, str, str, str]) -> None:
        """Report terminal workflow state back to the same Feishu conversation."""

        async def watch() -> None:
            for _ in range(_BUG_WORKFLOW_MAX_POLLS):
                await asyncio.sleep(_BUG_WORKFLOW_POLL_INTERVAL_SECONDS)
                workflow = self._bug_workflow_conversations.get(conversation_key)
                if workflow is None:
                    return
                try:
                    state = await self._get_bug_workflow(msg, workflow.workflow_id)
                except asyncio.CancelledError:
                    raise
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 404:
                        current = self._bug_workflow_conversations.get(conversation_key)
                        if current is workflow:
                            self._bug_workflow_conversations.pop(conversation_key, None)
                        return
                    logger.warning("[Manager] failed to read Bug workflow %s", workflow.workflow_id, exc_info=True)
                    continue
                except Exception:
                    logger.warning("[Manager] failed to read Bug workflow %s", workflow.workflow_id, exc_info=True)
                    continue
                status = str(state.get("status", ""))
                workflow.status = status
                if not _has_confirmed_preanalysis_copy_scope(state):
                    progress_notice = _format_bug_attachment_evidence_progress_notice(workflow.bug_id, state.get("attachment_evidence"))
                    if progress_notice and progress_notice != workflow.last_attachment_progress_notice:
                        await self._publish_bug_workflow_message(msg, progress_notice)
                        workflow.last_attachment_progress_notice = progress_notice
                    if not workflow.attachment_notice_sent:
                        attachment_notice = _format_bug_attachment_evidence_notice(workflow.bug_id, state.get("attachment_evidence"))
                        if attachment_notice:
                            await self._publish_bug_workflow_message(msg, attachment_notice)
                            workflow.attachment_notice_sent = True
                    triage_notice = _format_bug_triage_notice(workflow.bug_id, state.get("triage"))
                    if triage_notice and triage_notice != workflow.last_triage_notice:
                        await self._publish_bug_workflow_message(msg, triage_notice)
                        workflow.last_triage_notice = triage_notice
                if status == "awaiting_clarification":
                    clarification = state.get("clarification")
                    clarification_type = str(state.get("clarification_type", "product"))
                    if clarification_type not in {"product", "video"} or state.get("clarification_stage") != "pre_analysis":
                        await self._publish_bug_workflow_message(
                            msg,
                            f"禅道 Bug #{workflow.bug_id} 是旧版只读任务，已不再支持补充技术证据后继续调查。请重新发送该 Bug ID 创建当前流程。",
                        )
                        self._bug_workflow_conversations.pop(conversation_key, None)
                        return
                    question = clarification.get("question") if isinstance(clarification, dict) else None
                    options = clarification.get("options") if isinstance(clarification, dict) else None
                    response_mode = clarification.get("response_mode") if isinstance(clarification, dict) else None
                    copy_items = clarification.get("items") if isinstance(clarification, dict) else None
                    decision_reason = str(clarification.get("decision_reason") or "").strip() if isinstance(clarification, dict) else ""
                    decision_impact = str(clarification.get("decision_impact") or "").strip() if isinstance(clarification, dict) else ""
                    option_text = ""
                    if isinstance(options, list):
                        cleaned = []
                        for option in options:
                            label = option.get("label") if isinstance(option, dict) else option
                            if isinstance(label, str) and label.strip():
                                cleaned.append(label.strip())
                        if cleaned:
                            option_text = "\n可选：" + "；".join(f"{chr(65 + index)}：{label}" for index, label in enumerate(cleaned[:4]))
                    prompt = str(question).strip() if isinstance(question, str) else "请补充本次分析所需的关键信息。"
                    if response_mode == "copy_scope":
                        copy_scope_kind = str(clarification.get("copy_scope_kind") or "default") if isinstance(clarification, dict) else "default"
                        item_lines: list[str] = []
                        for index, item in enumerate(copy_items if isinstance(copy_items, list) else [], start=1):
                            if not isinstance(item, Mapping):
                                continue
                            clients = "/".join(str(value).upper() if str(value).lower() != "ios" else "iOS" for value in item.get("observed_clients", []) if value) or "客户端待确认"
                            page = str(item.get("page") or "").strip()
                            actual = str(item.get("actual_text") or "未提取").strip()
                            expected = str(item.get("expected_text") or "未提供").strip()
                            item_lines.append(f"{index}. {clients}{f' · {page}' if page else ''}\n当前：{actual}\n参考：{expected}")
                        option_text = ("\n\n待确认文案：\n" + "\n\n".join(item_lines)) if item_lines else ""
                        suffix = (
                            "\n请按“修改：编号→目标文案；不改：编号”回复。确认后 Bug Workbench 将开始源码调查，并在 Harmony 原生与 Harmony RN 中确认真实责任归属。"
                            if copy_scope_kind == "harmony"
                            else "\n请按“修改：编号→目标文案；不改：编号；跨端基准：Android/iOS/新文案”回复。确认后 Bug Workbench 将开始源码调查，最终责任端与根因以源码证据为准。"
                        )
                        intro = f"禅道 Bug #{workflow.bug_id} 的工单呈现多项用户可见文案差异，需要先确认本次产品目标和排除项。"
                    else:
                        suffix = f"\n请直接回复 {'、'.join(chr(65 + index) for index in range(len(cleaned[:4])))}，或回复其中一个选项的完整文字。" if response_mode == "choice" else "\n请只回复修改后的完整最终文案，不要描述修改方式。"
                        intro = (
                            f"禅道 Bug #{workflow.bug_id} 的视频是唯一有效附件，需要先确认是否下载并分析。" if clarification_type == "video" else f"禅道 Bug #{workflow.bug_id} 需要先确认最终产品目标；确认后 Bug Workbench 才会开始源码调查。"
                        )
                    decision_context = ""
                    if decision_reason:
                        decision_context += f"\n需要选择的原因：{decision_reason}"
                    if decision_impact:
                        decision_context += f"\n选择影响：{decision_impact}"
                    await self._publish_bug_workflow_message(
                        msg,
                        f"{intro}{decision_context}\n问题：{prompt}{option_text}{suffix}",
                    )
                    return
                if status == "repairing":
                    if not workflow.repair_notice_sent:
                        specialist_analysis = _format_feishu_specialist_analysis(str(state.get("analysis_report") or state.get("handoff") or ""))
                        analysis_text = f"\n\n分析结论：\n{specialist_analysis}" if specialist_analysis else ""
                        notice = (
                            f"禅道 Bug #{workflow.bug_id}：Bug Workbench 已完成分析，已有确认写入的禅道备注，本次未重复写入，现在已自动进入修复，无需再选择是否开始。{analysis_text}"
                            if state.get("note_write_skipped") is True
                            else f"禅道 Bug #{workflow.bug_id}：Bug Workbench 已完成分析并写入禅道备注，现在已自动进入修复，无需再选择是否开始。{analysis_text}"
                        )
                        await self._publish_bug_workflow_message(
                            msg,
                            notice,
                        )
                        workflow.repair_notice_sent = True
                    continue
                if status not in _BUG_WORKFLOW_TERMINAL_STATUSES:
                    continue
                if status == "note_written":
                    repair_attempted = state.get("repair_attempted") is True or bool(state.get("repair_engine"))
                    if repair_attempted and workflow.repair_notice_sent:
                        message = _format_feishu_repair_terminal_result(
                            workflow.bug_id,
                            workflow.workflow_id,
                            state,
                            workbench_base_url=self._bug_workbench_url,
                        )
                    else:
                        message = _format_feishu_bug_result(
                            workflow.bug_id,
                            workflow.workflow_id,
                            state,
                            workbench_base_url=self._bug_workbench_url,
                        )
                        if repair_attempted:
                            terminal = _format_feishu_repair_terminal_result(
                                workflow.bug_id,
                                workflow.workflow_id,
                                state,
                                workbench_base_url="",
                            )
                            terminal = "\n".join(line for line in terminal.splitlines() if not line.startswith("禅道备注："))
                            message = f"{message}\n\n自动修复结果\n{terminal}"
                    await self._publish_bug_workflow_message(
                        msg,
                        message,
                    )
                elif status == "awaiting_acceptance":
                    if not workflow.repair_notice_sent:
                        specialist_analysis = _format_feishu_specialist_analysis(str(state.get("analysis_report") or state.get("handoff") or ""))
                        analysis_text = f"\n\n分析结论：\n{specialist_analysis}" if specialist_analysis else ""
                        notice = (
                            f"禅道 Bug #{workflow.bug_id}：Bug Workbench 已完成分析，已有确认写入的禅道备注，本次未重复写入。{analysis_text}"
                            if state.get("note_write_skipped") is True
                            else f"禅道 Bug #{workflow.bug_id}：Bug Workbench 已完成分析并写入禅道备注。{analysis_text}"
                        )
                        await self._publish_bug_workflow_message(
                            msg,
                            notice,
                        )
                        workflow.repair_notice_sent = True
                    changed = state.get("changed_files")
                    files = "、".join(str(item) for item in changed[:8]) if isinstance(changed, list) else ""
                    suffix = f"\n修改文件：{files}" if files else ""
                    repair_label = "OpenHands 修复" if state.get("repair_engine") == "openhands" else "修复"
                    await self._publish_bug_workflow_message(
                        msg,
                        f"禅道 Bug #{workflow.bug_id} 已由 Bug Workbench {repair_label}完成代码修改，等待你验收。{suffix}",
                    )
                elif status == "accepted":
                    await self._publish_bug_workflow_message(msg, f"禅道 Bug #{workflow.bug_id} 已记录验收完成。")
                elif status == "rolled_back":
                    await self._publish_bug_workflow_message(msg, f"禅道 Bug #{workflow.bug_id} 的本次代码修改已回退。")
                elif status == "skipped":
                    completion = str(state.get("completion_report") or "该 Bug 已解决或关闭，未启动分析。")[:500]
                    await self._publish_bug_workflow_message(msg, completion)
                elif status == "cancelled":
                    await self._publish_bug_workflow_message(
                        msg,
                        f"已停止禅道 Bug #{workflow.bug_id} 的当前分析，未写入禅道备注。现在可以发送正确的 Bug ID。",
                    )
                elif status == "analysis_incomplete":
                    await self._publish_bug_workflow_message(
                        msg,
                        _format_feishu_incomplete_bug_analysis(workflow.bug_id, state),
                    )
                elif status == "awaiting_repair_choice":
                    await self._publish_bug_workflow_message(
                        msg,
                        f"禅道 Bug #{workflow.bug_id} 是旧版只读任务，已不再支持继续选择或启动修复。请重新发送该 Bug ID 创建当前流程。",
                    )
                elif status == "awaiting_evidence":
                    await self._publish_bug_workflow_message(
                        msg,
                        f"禅道 Bug #{workflow.bug_id} 是旧版只读任务，已不再支持补充技术证据后继续调查。请重新发送该 Bug ID 创建当前流程。",
                    )
                else:
                    message = _format_feishu_failed_bug_result(
                        workflow.bug_id,
                        workflow.workflow_id,
                        state,
                        workbench_base_url=self._bug_workbench_url,
                    )
                    await self._publish_bug_workflow_message(msg, message)
                self._bug_workflow_conversations.pop(conversation_key, None)
                return
            logger.warning("[Manager] Bug workflow watcher timed out: %s", conversation_key)

        task = asyncio.create_task(watch())
        self._bug_workflow_watcher_tasks.add(task)

        def discard(completed: asyncio.Task) -> None:
            self._bug_workflow_watcher_tasks.discard(completed)
            if not completed.cancelled() and (exc := completed.exception()) is not None:
                logger.error(
                    "[Manager] Bug workflow watcher failed",
                    exc_info=(type(exc), exc, exc.__traceback__),
                )

        task.add_done_callback(discard)

    async def _try_dispatch_feishu_bug_workflow(self, msg: InboundMessage) -> bool:
        """Route only clear Feishu Bug requests around the normal main agent."""
        if msg.channel_name != "feishu":
            return False

        text = (msg.text or "").strip()
        if msg.channel_name == "feishu":
            text = _strip_leading_feishu_mention(text)
        if not text and not msg.files:
            return False
        conversation_key = self._bug_workflow_conversation_key(msg)
        existing = self._bug_workflow_conversations.get(conversation_key)
        bug_id = _extract_zentao_bug_id(text)

        # The in-memory mapping makes a hot process fast, but it cannot be the
        # source of truth: a Feishu follow-up may arrive after Gateway restart.
        # Recover before routing normal conversation text to the lead agent.
        if existing is None and bug_id is None and not _FEISHU_GREETING_PATTERN.fullmatch(text):
            try:
                persisted = await self._get_active_bug_workflow(msg, conversation_key)
            except Exception:
                logger.warning("[Manager] failed to recover active Bug workflow", exc_info=True)
                persisted = None
            if persisted is not None:
                existing = _BugWorkflowConversation(
                    workflow_id=persisted["id"],
                    bug_id=int(persisted["bug_id"]),
                    status=str(persisted.get("status", "routing")),
                )
                self._bug_workflow_conversations[conversation_key] = existing

        if existing is None and bug_id is None and _is_bug_workbench_read_query(text):
            persisted = None
            latest_lookup_failed = False
            for lookup_key in (conversation_key, None):
                try:
                    persisted = await self._get_latest_bug_workflow(msg, lookup_key)
                except Exception:
                    latest_lookup_failed = True
                    logger.warning(
                        "[Manager] failed to recover latest Bug workflow: scope=%s",
                        "chat" if lookup_key is not None else "owner",
                        exc_info=True,
                    )
                if persisted is not None:
                    break
            if persisted is not None:
                existing = _BugWorkflowConversation(
                    workflow_id=persisted["id"],
                    bug_id=int(persisted["bug_id"]),
                    status=str(persisted.get("status", "note_written")),
                )
                self._bug_workflow_conversations[conversation_key] = existing
            else:
                await self._publish_bug_workflow_message(
                    msg,
                    ("暂时无法读取 Bug 工作台状态，请稍后重试。" if latest_lookup_failed else "当前用户没有可读取的 Bug 工作台任务。请先在工作台启动一个 Bug，或发送 Bug ID 创建任务。"),
                )
                return True

        if existing is not None and existing.status in _BUG_WORKFLOW_WAITING_STATUSES:
            if _is_bug_workflow_cancellation(text):
                try:
                    await self._cancel_bug_workflow(msg, existing.workflow_id)
                except Exception:
                    logger.exception("[Manager] failed to cancel Bug workflow: workflow=%s", existing.workflow_id)
                    await self._publish_bug_workflow_message(msg, f"禅道 Bug #{existing.bug_id} 停止失败，请稍后重试。")
                    return True
                self._bug_workflow_conversations.pop(conversation_key, None)
                await self._publish_bug_workflow_message(
                    msg,
                    f"已停止禅道 Bug #{existing.bug_id} 的当前分析，未写入禅道备注。现在可以发送正确的 Bug ID。",
                )
                return True
            if bug_id is not None and bug_id != existing.bug_id:
                await self._publish_bug_workflow_message(
                    msg,
                    f"禅道 Bug #{existing.bug_id} 正在等待你的补充或选择。若刚才编号写错，请先回复“取消当前 Bug”，再发送 Bug #{bug_id}。",
                )
                return True
            if bug_id == existing.bug_id:
                await self._publish_bug_workflow_message(
                    msg,
                    f"禅道 Bug #{existing.bug_id} 正在等待你的补充或选择。请直接回复上一个问题，或回复“取消当前 Bug”。",
                )
                return True

        if existing is not None and existing.status == "awaiting_clarification" and bug_id is None:
            answer = text.strip()
            preanalysis_copy_confirmation = False
            video_confirmation = False
            try:
                state = await self._get_bug_workflow(msg, existing.workflow_id)
                clarification_type = str(state.get("clarification_type", "product"))
                if clarification_type not in {"product", "video"} or state.get("clarification_stage") != "pre_analysis":
                    self._bug_workflow_conversations.pop(conversation_key, None)
                    await self._publish_bug_workflow_message(
                        msg,
                        f"禅道 Bug #{existing.bug_id} 是旧版只读任务，已不再支持补充技术证据后继续调查。请重新发送该 Bug ID 创建当前流程。",
                    )
                    return True
                option_id: str | None = None
                clarification = state.get("clarification")
                video_confirmation = clarification_type == "video"
                preanalysis_copy_confirmation = clarification_type == "product" and isinstance(clarification, Mapping) and str(clarification.get("response_mode") or "") == "copy_scope"
                if clarification_type in {"product", "video"} and isinstance(clarification, Mapping) and str(clarification.get("response_mode") or "") == "choice":
                    options = [item for item in clarification.get("options", []) if isinstance(item, Mapping)]
                    choice_match = re.fullmatch(r"\s*([A-Da-d])[。.!！]?\s*", answer)
                    if choice_match:
                        option_index = ord(choice_match.group(1).upper()) - ord("A")
                        if option_index < len(options) and isinstance(options[option_index].get("id"), str):
                            option_id = str(options[option_index]["id"])
                            answer = ""
                if (not answer and option_id is None) or _is_bug_workflow_followup(answer):
                    await self._publish_bug_workflow_message(msg, "请直接回复上一个问题的选择。")
                    return True
                submit_kwargs: dict[str, Any] = {"answer": answer}
                if option_id is not None:
                    submit_kwargs["option_id"] = option_id
                payload = await self._submit_bug_workflow_clarification(msg, existing.workflow_id, **submit_kwargs)
            except Exception:
                logger.exception("[Manager] failed to submit Bug workflow clarification: workflow=%s", existing.workflow_id)
                await self._publish_bug_workflow_message(msg, f"禅道 Bug #{existing.bug_id} 的需求确认提交失败，请稍后重试。")
                return True
            existing.status = str(payload.get("status", "analyzing"))
            self._watch_bug_workflow(msg, conversation_key)
            if video_confirmation:
                message = f"已记录禅道 Bug #{existing.bug_id} 的视频分析选择，Bug Workbench 将按该选择继续处理附件和源码调查。"
            elif preanalysis_copy_confirmation:
                message = f"已确认禅道 Bug #{existing.bug_id} 的文案修改范围。Bug Workbench 现在开始源码调查，最终责任端与根因以源码证据为准。"
            else:
                message = f"已确认禅道 Bug #{existing.bug_id} 的最终产品目标。Bug Workbench 现在开始源码调查，最终责任端与根因以源码证据为准。"
            await self._publish_bug_workflow_message(msg, message)
            return True

        if bug_id is None and _is_bug_workflow_followup(text):
            if existing is None:
                await self._publish_bug_workflow_message(msg, "当前会话没有可继续的禅道 Bug。请发送 Bug ID 或禅道 Bug 链接。")
                return True
            if existing.status in _BUG_WORKFLOW_ACTIVE_STATUSES:
                await self._publish_bug_workflow_message(msg, f"禅道 Bug #{existing.bug_id} 正由 Bug Workbench 分析，完成后会在此回复并写入禅道备注。")
                return True
            bug_id = existing.bug_id

        if bug_id is None:
            # An unfinished Bug Workbench run owns its Feishu conversation.
            # Ordinary text cannot escape into the lead agent mid-workflow.
            if existing is not None and existing.status in _BUG_WORKFLOW_ACTIVE_STATUSES:
                await self._publish_bug_workflow_message(
                    msg,
                    f"禅道 Bug #{existing.bug_id} 正由 Bug Workbench 处理，完成当前工作流后会在此回复。",
                )
                return True
            if existing is not None:
                await self._attach_latest_bug_workflow_snapshot(msg, existing)
            return False

        if existing is not None and existing.bug_id == bug_id and existing.status in _BUG_WORKFLOW_ACTIVE_STATUSES:
            try:
                current = await self._get_bug_workflow(msg, existing.workflow_id)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    logger.warning("[Manager] failed to verify cached Bug workflow %s", existing.workflow_id, exc_info=True)
                    await self._publish_bug_workflow_message(msg, f"禅道 Bug #{bug_id} 正由 Bug Workbench 分析，完成后会在此回复并写入禅道备注。")
                    return True
                self._bug_workflow_conversations.pop(conversation_key, None)
                existing = None
            except Exception:
                logger.warning("[Manager] failed to verify cached Bug workflow %s", existing.workflow_id, exc_info=True)
                await self._publish_bug_workflow_message(msg, f"禅道 Bug #{bug_id} 正由 Bug Workbench 分析，完成后会在此回复并写入禅道备注。")
                return True
            else:
                current_status = str(current.get("status") or existing.status)
                existing.status = current_status
                if current_status in _BUG_WORKFLOW_ACTIVE_STATUSES:
                    await self._publish_bug_workflow_message(msg, f"禅道 Bug #{bug_id} 正由 Bug Workbench 分析，完成后会在此回复并写入禅道备注。")
                    return True
                if current_status in _BUG_WORKFLOW_TERMINAL_STATUSES:
                    self._bug_workflow_conversations.pop(conversation_key, None)
                    existing = None

        if existing is not None and existing.status in _BUG_WORKFLOW_ACTIVE_STATUSES:
            await self._publish_bug_workflow_message(
                msg,
                f"禅道 Bug #{existing.bug_id} 正由 Bug Workbench 处理，完成当前工作流后会在此回复。",
            )
            return True

        try:
            payload = await self._start_bug_workflow(msg, bug_id, conversation_key=conversation_key)
        except Exception:
            logger.exception("[Manager] failed to start Feishu Bug workflow: bug_id=%s", bug_id)
            await self._publish_bug_workflow_message(msg, f"禅道 Bug #{bug_id} 工作台启动失败，请稍后重试。")
            return True

        self._bug_workflow_conversations[conversation_key] = _BugWorkflowConversation(
            workflow_id=payload["id"],
            bug_id=bug_id,
            status=str(payload.get("status", "routing")),
        )
        self._watch_bug_workflow(msg, conversation_key)
        await self._publish_bug_workflow_message(
            msg,
            (
                f"Bug Workbench 已开始处理禅道 Bug #{bug_id}：正在读取禅道正文，并在存在图片或附件时按类型下载和提取事实。"
                "事实整理后会先说明初步问题方向；文案问题将在源码调查前确认修改目标和排除项，其他问题直接进入源码调查。"
                "最终将呈现四部分分析结论和修改／处理意见，写入并回读确认禅道备注后完成；不会自动修改代码。"
            ),
        )
        return True

    # -- chat handling -----------------------------------------------------

    async def _get_bound_identity_rejection(self, msg: InboundMessage) -> _BoundIdentityRejection | None:
        """Return None when *msg* may proceed; otherwise return rejection routing hints.

        The returned object means the message lacks a verified bound identity.
        Its fields are intentionally limited to server-side values re-read from
        the connection repository, so rejection outbounds never trust a rejected
        inbound message's asserted connection metadata.
        """
        if not self._require_bound_identity:
            return None
        # Webhook-authenticated channels (GitHub) opt out via
        # ChannelRunPolicy.requires_bound_identity=False. Authenticity is
        # enforced at the webhook route by HMAC, and the "sender → DeerFlow
        # user" binding is encoded in the agent's config.yaml ownership, not
        # in the channel-connections table — there is no per-sender
        # /connect handshake to perform.
        policy = CHANNEL_RUN_POLICY.get(msg.channel_name)
        if policy is not None and not policy.requires_bound_identity:
            return None
        if _auth_disabled_owner_user_id():
            return None

        has_connection = bool(msg.connection_id)
        has_owner = bool(msg.owner_user_id)
        if not (has_connection and has_owner):
            return _BoundIdentityRejection()
        if self._connection_repo is None:
            return _BoundIdentityRejection(message=BOUND_IDENTITY_UNAVAILABLE_MESSAGE)

        # The manager is the run-creation security boundary, so it does not
        # trust mutable InboundMessage identity fields by themselves. Re-read
        # the binding by provider identity before creating DeerFlow threads or
        # runs. If the asserted identity does not match, keep only the
        # server-side connection fields as outbound routing hints.
        connection = await self._connection_repo.find_connection_by_external_identity(
            provider=msg.channel_name,
            external_account_id=msg.user_id,
            workspace_id=msg.workspace_id or None,
        )
        if connection is None:
            return _BoundIdentityRejection()

        connection_id = connection.get("id")
        owner_user_id = connection.get("owner_user_id")
        if connection_id == msg.connection_id and owner_user_id == msg.owner_user_id:
            return None
        return _BoundIdentityRejection(outbound_connection_id=connection_id, outbound_owner_user_id=owner_user_id)

    async def _reject_unbound_channel_message(
        self,
        msg: InboundMessage,
        *,
        bound_identity_rejection: _BoundIdentityRejection,
    ) -> None:
        logger.info(
            "[Manager] rejecting unbound channel message: channel=%s, chat_id=%s",
            msg.channel_name,
            msg.chat_id,
        )
        outbound = OutboundMessage(
            channel_name=msg.channel_name,
            chat_id=msg.chat_id,
            thread_id="",
            text=bound_identity_rejection.message,
            thread_ts=msg.thread_ts,
            connection_id=bound_identity_rejection.outbound_connection_id,
            owner_user_id=bound_identity_rejection.outbound_owner_user_id,
            metadata=_slim_metadata(msg.metadata),
        )
        await self.bus.publish_outbound(outbound)

    async def _lookup_thread_id(self, msg: InboundMessage) -> str | None:
        if msg.connection_id and self._connection_repo is not None:
            return await self._connection_repo.get_thread_id(
                msg.connection_id,
                msg.chat_id,
                msg.topic_id,
            )
        return self.store.get_thread_id(msg.channel_name, msg.chat_id, topic_id=msg.topic_id)

    async def _store_thread_id(self, msg: InboundMessage, thread_id: str) -> None:
        if msg.connection_id and msg.owner_user_id and self._connection_repo is not None:
            await self._connection_repo.set_thread_id(
                connection_id=msg.connection_id,
                owner_user_id=msg.owner_user_id,
                provider=msg.channel_name,
                external_conversation_id=msg.chat_id,
                external_topic_id=msg.topic_id,
                thread_id=thread_id,
            )
            return

        self.store.set_thread_id(
            msg.channel_name,
            msg.chat_id,
            thread_id,
            topic_id=msg.topic_id,
            user_id=msg.user_id,
        )

    async def _create_thread(self, client, msg: InboundMessage) -> str:
        """Create a new thread through Gateway and store the mapping."""
        metadata = _thread_channel_metadata(msg)
        owner_headers = _owner_headers(msg)
        # Some channels (notably GitHub) supply a deterministic preferred
        # thread id so a (repo, PR/issue number) always lands on the same
        # LangGraph thread, even after a store wipe. When absent, Gateway
        # mints a random id as before.
        meta = msg.metadata if isinstance(msg.metadata, dict) else {}
        preferred_thread_id = meta.get("preferred_thread_id")
        create_kwargs: dict[str, Any] = {"metadata": metadata}
        if isinstance(preferred_thread_id, str) and preferred_thread_id:
            create_kwargs["thread_id"] = preferred_thread_id
        if owner_headers:
            create_kwargs["headers"] = owner_headers
        try:
            thread = await client.threads.create(**create_kwargs)
        except ConflictError as exc:
            # True race: two webhook deliveries for the same (repo, number)
            # land within ms with the same preferred_thread_id. The Gateway
            # ``POST /threads`` route is idempotent on sequential reads (it
            # returns the existing record when present), so this branch only
            # fires for a real concurrent-create conflict that the underlying
            # store surfaced as 409.
            #
            # Narrow the recovery to ConflictError specifically: any other
            # exception (transient DB outage, network error, 5xx) used to
            # land here too and silently wrote ``preferred_thread_id`` into
            # the store, mapping subsequent webhooks to a thread that was
            # never created — every later run would 404 forever with no
            # retry path. Those non-conflict failures now propagate so the
            # caller fails the delivery cleanly.
            if not (isinstance(preferred_thread_id, str) and preferred_thread_id):
                # Without a preferred id we cannot deterministically recover.
                raise
            # Verify the racing-write target actually exists before we
            # cache the mapping. If ConflictError fires but threads.get
            # also rejects, the store underneath is in an inconsistent
            # state and we surface the failure rather than poisoning the
            # mapping for every future delivery on this issue/PR.
            try:
                get_kwargs: dict[str, Any] = {}
                if owner_headers:
                    get_kwargs["headers"] = owner_headers
                await client.threads.get(preferred_thread_id, **get_kwargs)
            except Exception as verify_exc:
                logger.warning(
                    "[Manager] threads.create raced on preferred_thread_id=%s (%s) but follow-up threads.get failed (%s); not caching the mapping",
                    preferred_thread_id,
                    exc.__class__.__name__,
                    verify_exc.__class__.__name__,
                )
                raise
            logger.info(
                "[Manager] threads.create raced on preferred_thread_id=%s (%s); reusing the deterministic id",
                preferred_thread_id,
                exc.__class__.__name__,
            )
            await self._store_thread_id(msg, preferred_thread_id)
            return preferred_thread_id
        thread_id = thread["thread_id"]
        await self._store_thread_id(msg, thread_id)
        logger.info("[Manager] new thread created through Gateway: thread_id=%s for chat_id=%s topic_id=%s", thread_id, msg.chat_id, msg.topic_id)
        return thread_id

    async def _get_or_create_thread(self, client, msg: InboundMessage) -> tuple[str, bool]:
        """Return ``(thread_id, created)``, creating a thread only if needed.

        Each inbound message is dispatched on its own task, so two messages that
        arrive close together for the same chat would both look up a missing
        thread and then both create one — the second store silently overwrites
        the first, orphaning a Gateway thread and splitting the conversation.
        Serialize the create path per conversation and re-check inside the lock
        so only the first message creates a thread and the rest reuse it.
        """
        thread_id = await self._lookup_thread_id(msg)
        if thread_id:
            if msg.channel_name != "feishu" or msg.owner_user_id != self._bug_workbench_owner_user_id:
                return thread_id, False
            try:
                await client.threads.get(thread_id, headers=_owner_headers(msg) or {})
                return thread_id, False
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
                # Preserve the old conversation under its previous owner.
                return await self._create_thread(client, msg), True

        key = (msg.channel_name, msg.chat_id, msg.topic_id)
        lock = self._thread_create_locks.setdefault(key, asyncio.Lock())
        try:
            async with lock:
                # A concurrent message for the same chat may have created the
                # thread while we were waiting on the lock.
                thread_id = await self._lookup_thread_id(msg)
                if thread_id:
                    return thread_id, False
                return await self._create_thread(client, msg), True
        finally:
            # Once the thread is stored, later messages short-circuit on the
            # lookup above and never reach this lock, so it's safe to drop the
            # entry and keep the registry bounded to in-flight conversations.
            self._thread_create_locks.pop(key, None)

    async def _update_thread_channel_metadata(self, client, msg: InboundMessage, thread_id: str) -> None:
        """Best-effort source metadata backfill for existing IM-created threads."""
        # The metadata (provider/chat/topic) is constant for a thread, so one
        # successful backfill per manager lifetime is enough — skip the
        # redundant PATCH on every subsequent inbound message.
        if thread_id in self._channel_metadata_synced:
            return
        update_kwargs: dict[str, Any] = {"metadata": _thread_channel_metadata(msg)}
        if owner_headers := _owner_headers(msg):
            update_kwargs["headers"] = owner_headers
        try:
            await client.threads.update(thread_id, **update_kwargs)
        except Exception:
            logger.debug("[Manager] failed to update channel metadata for thread_id=%s", thread_id, exc_info=True)
            return
        if len(self._channel_metadata_synced) > 4096:
            self._channel_metadata_synced.clear()
        self._channel_metadata_synced.add(thread_id)

    async def _handle_chat(
        self,
        msg: InboundMessage,
        extra_context: dict[str, Any] | None = None,
        *,
        bound_identity_checked: bool = False,
    ) -> None:
        # Normal entry paths already run the bound-identity check in
        # _handle_message() or _handle_command(). Keep this default False so
        # direct callers and future internal paths still fail closed.
        bound_identity_rejection = None if bound_identity_checked else await self._get_bound_identity_rejection(msg)
        if bound_identity_rejection is not None:
            await self._reject_unbound_channel_message(msg, bound_identity_rejection=bound_identity_rejection)
            return

        client = self._get_client()
        storage_user_id = _channel_storage_user_id(msg)

        # Look up the existing DeerFlow thread, creating one if this is the
        # first message for the chat. topic_id may be None (e.g. Telegram
        # private chats) — the store handles this by using the "channel:chat_id"
        # key without a topic suffix.
        thread_id, created = await self._get_or_create_thread(client, msg)
        if not created:
            logger.info("[Manager] reusing thread: thread_id=%s for topic_id=%s", thread_id, msg.topic_id)
            await self._update_thread_channel_metadata(client, msg, thread_id)

        serial_state, queued = self._begin_serialized_thread_run(
            channel_name=msg.channel_name,
            thread_id=thread_id,
        )
        serial_lock_acquired = False
        try:
            if queued:
                await self._publish_progress_update(
                    msg,
                    thread_id,
                    "Queued behind another request in this conversation. I’ll start working on this as soon as it finishes.",
                )
            if serial_state is not None:
                await serial_state.lock.acquire()
                serial_lock_acquired = True
            if queued:
                await self._publish_progress_update(msg, thread_id, "thinking...")
            await self._handle_chat_on_thread(
                client,
                msg,
                thread_id,
                extra_context=extra_context,
                storage_user_id=storage_user_id,
            )
        finally:
            self._finish_serialized_thread_run(
                channel_name=msg.channel_name,
                thread_id=thread_id,
                state=serial_state,
                lock_acquired=serial_lock_acquired,
            )

    async def _handle_chat_on_thread(
        self,
        client,
        msg: InboundMessage,
        thread_id: str,
        *,
        extra_context: dict[str, Any] | None = None,
        storage_user_id: str | None = None,
    ) -> None:
        if storage_user_id is None:
            storage_user_id = _channel_storage_user_id(msg)

        assistant_id, run_config, run_context = self._resolve_run_params(msg, thread_id)

        # Apply per-channel policy: credentials provider (e.g. GitHub
        # installation-token mint) and the non-interactive flag for
        # webhook channels. Driven by CHANNEL_RUN_POLICY so each new
        # webhook channel is a one-row registration, not a fresh
        # if-branch here.
        policy = await self._apply_channel_policy(msg, run_context)

        # If the inbound message contains file attachments, let the channel
        # materialize (download) them and update msg.text to include sandbox file paths.
        # This enables downstream models to access user-uploaded files by path.
        # Channels that do not support file download will simply return the original message.
        if msg.files:
            from .service import get_channel_service

            service = get_channel_service()
            channel = service.get_channel(msg.channel_name) if service else None
            logger.info("[Manager] preparing receive file context for %d attachments", len(msg.files))
            msg = await channel.receive_file(msg, thread_id, user_id=storage_user_id) if channel else msg
        if extra_context:
            run_context.update(extra_context)

        original_text = msg.text
        uploaded = await _ingest_inbound_files(thread_id, msg, user_id=storage_user_id)
        human_message = _human_input_message(
            _channel_agent_input_text(msg),
            original_content=original_text,
            files=uploaded or None,
        )

        if self._channel_supports_streaming(msg.channel_name):
            await self._handle_streaming_chat(
                client,
                msg,
                thread_id,
                assistant_id,
                run_config,
                run_context,
                human_message,
                storage_user_id=storage_user_id,
            )
            return

        run_kwargs: dict[str, Any] = {
            "input": {"messages": [human_message]},
            "config": run_config,
            "context": run_context,
            "multitask_strategy": "reject",
        }
        if owner_headers := _owner_headers(msg):
            run_kwargs["headers"] = owner_headers

        if policy is not None and policy.fire_and_forget:
            # Fire-and-forget path: the channel does its own outbound
            # during the run (GitHub agents post to the issue/PR via the
            # ``gh`` CLI from inside the sandbox), so there is nothing
            # for the manager to ferry back. Use ``runs.create`` — a
            # short POST that returns once the run is ``pending`` — to
            # avoid the SDK's 300s ``httpx.ReadTimeout`` on legitimately
            # long autonomous runs, and the false "internal error"
            # outbound that follows when it fires. ``ConflictError`` is
            # still raised synchronously by ``start_run`` if a previous
            # run on this thread is still active, so the existing
            # busy-thread path is preserved.
            logger.info(
                "[Manager] invoking runs.create(thread_id=%s, text_len=%d) [fire_and_forget]",
                thread_id,
                len(msg.text or ""),
            )
            try:
                # Capturing the return value is new (issue #4121 Slice 2):
                # it carries ``run_id``, which the follow-up watcher below
                # needs to subscribe to this run's StreamBridge stream. When
                # ``buffer_followups_on_busy`` is off this is otherwise
                # behaviorally identical to the previous bare ``await``.
                result = await client.runs.create(thread_id, assistant_id, **run_kwargs)
            except Exception as exc:
                if _is_thread_busy_error(exc):
                    logger.warning("[Manager] thread busy (concurrent run rejected): thread_id=%s", thread_id)
                    if policy.buffer_followups_on_busy:
                        self._buffer_followup(thread_id, msg)
                    else:
                        # Swallowed like the generic handler would not be: release the
                        # key so the provider's redelivery can retry once the thread
                        # frees, instead of being dropped for the dedupe TTL.
                        await self._release_inbound_dedupe_key(msg)
                    await self._send_error(msg, THREAD_BUSY_MESSAGE)
                    return
                raise
            if policy.buffer_followups_on_busy:
                self._maybe_spawn_followup_watcher(thread_id, result, msg)
            return

        logger.info("[Manager] invoking runs.wait(thread_id=%s, text_len=%d)", thread_id, len(msg.text or ""))
        try:
            result = await client.runs.wait(
                thread_id,
                assistant_id,
                **run_kwargs,
            )
        except Exception as exc:
            if _is_thread_busy_error(exc):
                logger.warning("[Manager] thread busy (concurrent run rejected): thread_id=%s", thread_id)
                # Same reason as the fire-and-forget branch above: this error is
                # handled here rather than re-raised, so release explicitly.
                await self._release_inbound_dedupe_key(msg)
                await self._send_error(msg, THREAD_BUSY_MESSAGE)
                return
            else:
                raise

        response_text = _extract_response_text(result)
        pending_clarification = _has_current_turn_clarification(result)
        artifacts = _extract_artifacts(result)

        logger.info(
            "[Manager] agent response received: thread_id=%s, response_len=%d, artifacts=%d",
            thread_id,
            len(response_text) if response_text else 0,
            len(artifacts),
        )

        # Reuse the storage owner cached at the top of _handle_chat so uploads and
        # artifact delivery always resolve to the same bucket, even if a future
        # channel.receive_file returns a rewritten InboundMessage.
        response_text, attachments = _prepare_artifact_delivery(thread_id, response_text, artifacts, user_id=storage_user_id)

        if not response_text:
            if attachments:
                response_text = _format_artifact_text([a.virtual_path for a in attachments])
            else:
                response_text = "(No response from agent)"

        outbound = OutboundMessage(
            channel_name=msg.channel_name,
            chat_id=msg.chat_id,
            thread_id=thread_id,
            text=response_text,
            artifacts=artifacts,
            attachments=attachments,
            thread_ts=msg.thread_ts,
            connection_id=msg.connection_id,
            owner_user_id=msg.owner_user_id,
            metadata=_response_metadata(msg.metadata, pending_clarification=pending_clarification),
        )
        logger.info("[Manager] publishing outbound message to bus: channel=%s, chat_id=%s", msg.channel_name, msg.chat_id)
        await self.bus.publish_outbound(outbound)

    async def _handle_streaming_chat(
        self,
        client,
        msg: InboundMessage,
        thread_id: str,
        assistant_id: str,
        run_config: dict[str, Any],
        run_context: dict[str, Any],
        human_message: dict[str, Any],
        storage_user_id: str | None = None,
    ) -> None:
        logger.info("[Manager] invoking runs.stream(thread_id=%s, text_len=%d)", thread_id, len(msg.text or ""))

        last_values: dict[str, Any] | list | None = None
        streamed_buffers: dict[str, str] = {}
        current_message_id: str | None = None
        latest_text = ""
        last_published_text = ""
        last_published_len = 0
        last_publish_at = 0.0
        stream_error: BaseException | None = None
        stream_kwargs: dict[str, Any] = {
            "input": {"messages": [human_message]},
            "config": run_config,
            "context": run_context,
            "stream_mode": list(STREAM_MODES),
            "multitask_strategy": "reject",
        }
        if owner_headers := _owner_headers(msg):
            stream_kwargs["headers"] = owner_headers

        try:
            async for chunk in client.runs.stream(
                thread_id,
                assistant_id,
                **stream_kwargs,
            ):
                event = getattr(chunk, "event", "")
                data = getattr(chunk, "data", None)

                if event == "error":
                    stream_error = RuntimeError("Agent runtime reported a failed run")
                    continue

                if event in MESSAGE_STREAM_EVENTS:
                    accumulated_text, current_message_id = _accumulate_stream_text(streamed_buffers, current_message_id, data)
                    if accumulated_text:
                        latest_text = accumulated_text
                elif event == "values" and isinstance(data, (dict, list)):
                    last_values = data
                    # Clarification text is only in the values snapshot;
                    # publish it so the user sees the question mid-stream.
                    if _has_current_turn_clarification(data):
                        clarification_text = _extract_response_text(data)
                        if clarification_text and clarification_text != latest_text:
                            latest_text = clarification_text

                if not latest_text or latest_text == last_published_text:
                    continue

                now = time.monotonic()
                new_chars = len(latest_text) - last_published_len
                # OR logic: flush when interval elapsed OR enough chars accumulated
                if last_published_text:
                    if now - last_publish_at < STREAM_UPDATE_MIN_INTERVAL_SECONDS and new_chars < STREAM_UPDATE_MIN_CHARS:
                        continue

                display_text = latest_text + " ▉"
                await self.bus.publish_outbound(
                    OutboundMessage(
                        channel_name=msg.channel_name,
                        chat_id=msg.chat_id,
                        thread_id=thread_id,
                        text=display_text,
                        is_final=False,
                        thread_ts=msg.thread_ts,
                        connection_id=msg.connection_id,
                        owner_user_id=msg.owner_user_id,
                        metadata=_response_metadata(msg.metadata),
                    )
                )
                last_published_text = latest_text
                last_published_len = len(latest_text)
                last_publish_at = now
        except Exception as exc:
            stream_error = exc
            if _is_thread_busy_error(exc):
                logger.warning("[Manager] thread busy (concurrent run rejected): thread_id=%s", thread_id)
            else:
                logger.exception("[Manager] streaming error: thread_id=%s", thread_id)
        finally:
            result = last_values if last_values is not None else {"messages": [{"type": "ai", "content": latest_text}]}
            main_stop_reason = result.get("main_assistant_stop_reason") if isinstance(result, dict) else None
            main_failed = run_context.get("agent_name") == "personal-assistant" and stream_error is not None
            response_text = _extract_response_text(result)
            if main_stop_reason or main_failed:
                response_text = "本次主助手处理已停止，任务未完成。尚未完成的操作不能视为成功，也不能宣称技能已经学会。"
            pending_clarification = _has_current_turn_clarification(result)
            artifacts = _extract_artifacts(result)
            # Reuse the storage owner resolved by _handle_chat so artifact delivery
            # matches the upload bucket and we avoid re-running _safe_user_id_for_run
            # (and its possible filesystem touch) on the streaming-error path.
            response_text, attachments = _prepare_artifact_delivery(thread_id, response_text, artifacts, user_id=storage_user_id)

            if not response_text:
                if attachments:
                    response_text = _format_artifact_text([attachment.virtual_path for attachment in attachments])
                elif stream_error:
                    if _is_thread_busy_error(stream_error):
                        response_text = THREAD_BUSY_MESSAGE
                    else:
                        response_text = "An error occurred while processing your request. Please try again."
                else:
                    response_text = latest_text or "(No response from agent)"

            logger.info(
                "[Manager] streaming response completed: thread_id=%s, response_len=%d, artifacts=%d, error=%s",
                thread_id,
                len(response_text),
                len(artifacts),
                stream_error,
            )
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel_name=msg.channel_name,
                    chat_id=msg.chat_id,
                    thread_id=thread_id,
                    text=response_text,
                    artifacts=artifacts,
                    attachments=attachments,
                    is_final=True,
                    thread_ts=msg.thread_ts,
                    connection_id=msg.connection_id,
                    owner_user_id=msg.owner_user_id,
                    metadata={**_response_metadata(msg.metadata, pending_clarification=pending_clarification), **({"main_assistant_incomplete": True} if main_stop_reason or main_failed else {})},
                )
            )
            if stream_error is not None:
                # This path swallows its own errors, so _handle_message's generic
                # handler never runs and never releases the key. Release only
                # after publishing the final outbound so a provider redelivery
                # cannot overtake this attempt's terminal reply.
                await self._release_inbound_dedupe_key(msg)

    # -- command handling --------------------------------------------------

    async def _handle_command(self, msg: InboundMessage) -> None:
        # Commands are the other run-creation entry point besides chat: /new
        # calls _create_thread() directly, and /bootstrap routes into
        # _handle_chat(). Apply the same bound-identity admission boundary here
        # so unbound platform users cannot create unowned threads/checkpoints or
        # query Gateway state via commands. Provider-level binding flows
        # (/connect <code>, /start <code>) are consumed by the provider adapter
        # before the message reaches the manager, so they are unaffected.
        bound_identity_rejection = await self._get_bound_identity_rejection(msg)
        if bound_identity_rejection is not None:
            await self._reject_unbound_channel_message(msg, bound_identity_rejection=bound_identity_rejection)
            return

        raw_text = msg.text
        text = raw_text.strip()
        parts = text.split(maxsplit=1)
        reply: str | None = None
        if not parts:
            command = None
            reply = _unknown_command_reply()
        else:
            command = parts[0].lower().removeprefix("/")

        if reply is None and not raw_text.startswith("/"):
            reply = _unknown_command_reply(command)

        if reply is None and command == "bootstrap":
            from dataclasses import replace as _dc_replace

            chat_text = parts[1] if len(parts) > 1 else "Initialize workspace"
            chat_msg = _dc_replace(
                msg,
                text=chat_text,
                msg_type=InboundMessageType.CHAT,
                metadata=dict(msg.metadata),
            )
            await self._handle_chat(chat_msg, extra_context={"is_bootstrap": True}, bound_identity_checked=True)
            return

        if reply is None and command == "new":
            # Create a new thread through Gateway
            client = self._get_client()
            await self._create_thread(client, msg)
            reply = "New conversation started."
        elif reply is None and command == "status":
            thread_id = await self._lookup_thread_id(msg)
            reply = f"Active thread: {thread_id}" if thread_id else "No active conversation."
        elif reply is None and command == "models":
            reply = await self._fetch_gateway("/api/models", "models", msg=msg)
        elif reply is None and command == "memory":
            reply = await self._fetch_gateway("/api/memory", "memory", msg=msg)
        elif reply is None and command == "goal":
            reply = await self._handle_goal_command(msg, parts[1] if len(parts) > 1 else "")
            if reply is None:
                return
        elif reply is None and command == "help":
            reply = (
                "Available commands:\n"
                "/bootstrap — Start a bootstrap session (enables agent setup)\n"
                "/goal [condition|clear] — Set, show, or clear an active goal\n"
                "/new — Start a new conversation\n"
                "/status — Show current thread info\n"
                "/models — List available models\n"
                "/memory — Show memory status\n"
                "/<skill-name> <task> — Activate an enabled skill for one turn\n"
                "/help — Show this help"
            )
        elif reply is None:
            slash_resolution = await asyncio.to_thread(
                lambda: _resolve_slash_skill_command(
                    raw_text,
                    self._resolve_available_skill_names(msg),
                    self._get_skill_storage,
                )
            )
            if slash_resolution and slash_resolution.failure_message:
                reply = slash_resolution.failure_message
            elif slash_resolution and slash_resolution.route_to_chat:
                from dataclasses import replace as _dc_replace

                chat_msg = _dc_replace(msg, msg_type=InboundMessageType.CHAT)
                await self._handle_chat(chat_msg, bound_identity_checked=True)
                return
            else:
                reply = _unknown_command_reply(command)

        outbound = OutboundMessage(
            channel_name=msg.channel_name,
            chat_id=msg.chat_id,
            thread_id=await self._lookup_thread_id(msg) or "",
            text=reply,
            thread_ts=msg.thread_ts,
            connection_id=msg.connection_id,
            owner_user_id=msg.owner_user_id,
            metadata=_slim_metadata(msg.metadata),
        )
        await self.bus.publish_outbound(outbound)

    async def _goal_request(
        self,
        method: str,
        thread_id: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with httpx.AsyncClient() as http:
            request = getattr(http, method.lower())
            kwargs: dict[str, Any] = {"timeout": 10, "headers": headers}
            if json is not None:
                kwargs["json"] = json
            response = await request(f"{self._gateway_url}/api/threads/{quote(thread_id, safe='')}/goal", **kwargs)
            response.raise_for_status()
            return response.json() or {}

    async def _handle_goal_command(self, msg: InboundMessage, args: str) -> str | None:
        command = parse_goal_command(args)
        thread_id = await self._lookup_thread_id(msg)
        headers = _owner_headers(msg) or create_internal_auth_headers()

        if command.kind == "status":
            if not thread_id:
                return "No active goal."
            try:
                goal = (await self._goal_request("get", thread_id, headers=headers)).get("goal")
            except Exception:
                logger.exception("Failed to fetch goal from gateway")
                return "Failed to fetch goal information."
            return f"Goal: {goal.get('objective')}" if goal else "No active goal."

        if command.kind == "clear":
            if not thread_id:
                return "Goal cleared."
            try:
                await self._goal_request("delete", thread_id, headers=headers)
            except Exception:
                logger.exception("Failed to clear goal through gateway")
                return "Failed to clear goal."
            return "Goal cleared."

        if not thread_id:
            thread_id = await self._create_thread(self._get_client(), msg)

        try:
            await self._goal_request("put", thread_id, headers=headers, json={"objective": command.objective})
        except Exception:
            logger.exception("Failed to set goal through gateway")
            return "Failed to set goal."

        from dataclasses import replace as _dc_replace

        chat_msg = _dc_replace(msg, text=command.objective, msg_type=InboundMessageType.CHAT)
        await self._handle_chat(chat_msg, bound_identity_checked=True)
        return None

    async def _fetch_gateway(self, path: str, kind: str, *, msg: InboundMessage | None = None) -> str:
        """Fetch data from the Gateway API for command responses."""
        import httpx

        try:
            headers = _owner_headers(msg) if msg is not None else None
            async with httpx.AsyncClient() as http:
                resp = await http.get(
                    f"{self._gateway_url}{path}",
                    timeout=10,
                    headers=headers or create_internal_auth_headers(),
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception:
            logger.exception("Failed to fetch %s from gateway", kind)
            return f"Failed to fetch {kind} information."

        if kind == "models":
            names = [m["name"] for m in data.get("models", [])]
            return ("Available models:\n" + "\n".join(f"• {n}" for n in names)) if names else "No models configured."
        elif kind == "memory":
            facts = data.get("facts", [])
            return f"Memory contains {len(facts)} fact(s)."
        return str(data)

    # -- error helper ------------------------------------------------------

    async def _send_error(self, msg: InboundMessage, error_text: str) -> None:
        outbound = OutboundMessage(
            channel_name=msg.channel_name,
            chat_id=msg.chat_id,
            thread_id=await self._lookup_thread_id(msg) or "",
            text=error_text,
            thread_ts=msg.thread_ts,
            connection_id=msg.connection_id,
            owner_user_id=msg.owner_user_id,
            metadata=_slim_metadata(msg.metadata),
        )
        await self.bus.publish_outbound(outbound)
