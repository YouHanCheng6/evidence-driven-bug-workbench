"""Opt-in hard per-run tool-call budget for bounded autonomous agents."""

from __future__ import annotations

import threading
from collections.abc import Awaitable, Callable
from typing import override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse, hook_config
from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.runtime import Runtime
from langgraph.types import Command

from deerflow.agents.middlewares._bounded_dict import BoundedDict
from deerflow.runtime.context_keys import (
    BUG_UI_ANALYSIS_CONTEXT_KEY,
    BUG_UI_ANCHOR_CANDIDATES_CONTEXT_KEY,
    BUG_UI_ANCHOR_SOURCE_PATH_CONTEXT_KEY,
    BUG_UI_ENTRY_SOURCE_PATH_CONTEXT_KEY,
    BUG_UI_PENDING_BEHAVIOR_FOCUS_CONTEXT_KEY,
    BUG_UI_PENDING_STATE_SEARCH_TERMS_CONTEXT_KEY,
)

_CONTEXT_KEY = "tool_call_limit"
_DUPLICATE_FAILED_READS_CONTEXT_KEY = "prevent_duplicate_failed_reads"
_BLOCKED_TOOL_NAMES_CONTEXT_KEY = "blocked_tool_names"
_ALLOWED_BASH_PREFIXES_CONTEXT_KEY = "allowed_bash_command_prefixes"
_BASH_CALL_LIMIT_CONTEXT_KEY = "bash_call_limit"
_PERSISTENT_RUN_REMINDER_CONTEXT_KEY = "persistent_run_reminder"
_REDUNDANT_READ_LIMIT_CONTEXT_KEY = "max_redundant_read_file_calls_without_write"
_FINALIZE_PROMPT = "<system_reminder>\nThe tool-call budget for this run is exhausted. Do not call tools. Using only the evidence already collected, now produce the required final answer.\n</system_reminder>"
_BLOCKED_TOOL_MESSAGE = "[TOOL BUDGET EXCEEDED] This run has already used its allowed tool calls. Do not make further tool calls; produce the required final answer from the evidence already collected."
_RUN_POLICY_BLOCKED_TOOL_MESSAGE = "[TOOL BLOCKED BY RUN POLICY] This tool is unavailable in the current internal workflow run. Use the context already supplied by the workflow."
_REDUNDANT_READ_BLOCKED_MESSAGE = (
    "[REDUNDANT READ BLOCKED] This source range is already covered by successful reads. Use the locked evidence now: apply the minimal patch, or report the exact missing fact and stop. Do not read the same source again."
)
_REDUNDANT_READ_REMINDER = (
    "<system_reminder>\nRepeated read_file calls have stopped adding source evidence. "
    "read_file is now unavailable for this run. Use the locked evidence to apply the minimal patch "
    "with str_replace/write_file, or state the exact missing fact in the required final report and stop.\n"
    "</system_reminder>"
)
_UI_ANCHOR_SELECTION_REMINDER = (
    "<system_reminder>\nSuccessful searches in the confirmed UI implementation now provide exact source anchors. "
    "Choose the one anchor that best matches the current Bug and call trace_ui_source_anchor. "
    "This is the only available source action until its exact definitions, references, and containing scopes are returned.\n"
    "source: {source}\nanchors: {anchors}\n</system_reminder>"
)
_UI_STATE_SEARCH_REMINDER = (
    "<system_reminder>\nThe selected source scope does not prove the UI state explicitly reported by the Bug. "
    "Search only the confirmed implementation file for these exact state terms before reading or finalizing: {terms}. "
    "A successful search will enter exact-anchor tracing automatically.\n</system_reminder>"
)
_UI_BEHAVIOR_FOCUS_REMINDER = (
    "<system_reminder>\nThe selected owner scope now contains executable layout evidence for the reported visual defect. "
    "Call trace_ui_behavior_owner with exactly platform={platform}, owner={owner}, focus_line={focus_line}. "
    "This is the only available action until the backend captures the required behavior anchors.\n</system_reminder>"
)


class ToolCallLimitMiddleware(AgentMiddleware[AgentState]):
    """Enforce ``runtime.context.tool_call_limit`` without relying on model obedience.

    The limit is opt-in and intended for short autonomous evidence-gathering
    runs. Once exhausted, the next model call receives no tool schemas and a
    finalization instruction, then the graph stops after that one final model
    response. Concurrent surplus calls receive a normal error ``ToolMessage``
    instead of executing, preserving tool-call/message pairing.
    """

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._used: BoundedDict[tuple[str, str], int] = BoundedDict(1000)
        self._bash_used: BoundedDict[tuple[str, str], int] = BoundedDict(1000)
        self._failed_read_paths: BoundedDict[tuple[str, str], set[str]] = BoundedDict(1000)
        self._read_ranges: BoundedDict[tuple[str, str], dict[str, list[tuple[int, float]]]] = BoundedDict(1000)
        self._redundant_read_streak: BoundedDict[tuple[str, str], tuple[str, int]] = BoundedDict(1000)
        self._redundant_read_tripped: BoundedDict[tuple[str, str], bool] = BoundedDict(1000)

    @staticmethod
    def _key(runtime: Runtime) -> tuple[str, str]:
        context = getattr(runtime, "context", None)
        if isinstance(context, dict):
            return (
                str(context.get("thread_id") or "unknown-thread"),
                str(context.get("run_id") or context.get("run_attempt_id") or id(runtime)),
            )
        return "unknown-thread", str(id(runtime))

    @staticmethod
    def _limit(runtime: Runtime) -> int | None:
        context = getattr(runtime, "context", None)
        value = context.get(_CONTEXT_KEY) if isinstance(context, dict) else None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    def _used_calls(self, runtime: Runtime) -> int:
        with self._lock:
            return self._used.get(self._key(runtime), 0)

    @staticmethod
    def _blocked_tool_names(runtime: Runtime) -> frozenset[str]:
        context = getattr(runtime, "context", None)
        value = context.get(_BLOCKED_TOOL_NAMES_CONTEXT_KEY) if isinstance(context, dict) else None
        if not isinstance(value, (list, tuple)):
            return frozenset()
        return frozenset(name for name in value if isinstance(name, str) and name)

    @staticmethod
    def _allowed_bash_prefixes(runtime: Runtime) -> tuple[str, ...] | None:
        context = getattr(runtime, "context", None)
        value = context.get(_ALLOWED_BASH_PREFIXES_CONTEXT_KEY) if isinstance(context, dict) else None
        if not isinstance(value, (list, tuple)):
            return None
        return tuple(prefix for prefix in value if isinstance(prefix, str) and prefix)

    @staticmethod
    def _bash_call_limit(runtime: Runtime) -> int | None:
        context = getattr(runtime, "context", None)
        value = context.get(_BASH_CALL_LIMIT_CONTEXT_KEY) if isinstance(context, dict) else None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    @staticmethod
    def _redundant_read_limit(runtime: Runtime) -> int | None:
        context = getattr(runtime, "context", None)
        value = context.get(_REDUNDANT_READ_LIMIT_CONTEXT_KEY) if isinstance(context, dict) else None
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            return None
        return value

    @staticmethod
    def _persistent_run_reminder(runtime: Runtime) -> str | None:
        context = getattr(runtime, "context", None)
        value = context.get(_PERSISTENT_RUN_REMINDER_CONTEXT_KEY) if isinstance(context, dict) else None
        return value.strip() if isinstance(value, str) and value.strip() else None

    @staticmethod
    def _ui_source_entry_confirmed(runtime: Runtime) -> bool:
        context = getattr(runtime, "context", None)
        if not isinstance(context, dict) or context.get(BUG_UI_ANALYSIS_CONTEXT_KEY) is not True:
            return False
        entry = context.get(BUG_UI_ENTRY_SOURCE_PATH_CONTEXT_KEY)
        return isinstance(entry, str) and bool(entry)

    @staticmethod
    def _ui_anchor_selection(runtime: Runtime) -> tuple[str, tuple[str, ...]] | None:
        context = getattr(runtime, "context", None)
        if not isinstance(context, dict) or context.get(BUG_UI_ANALYSIS_CONTEXT_KEY) is not True:
            return None
        source = context.get(BUG_UI_ANCHOR_SOURCE_PATH_CONTEXT_KEY)
        candidates = context.get(BUG_UI_ANCHOR_CANDIDATES_CONTEXT_KEY)
        if not isinstance(source, str) or not source or not isinstance(candidates, list):
            return None
        anchors = tuple(item for item in candidates if isinstance(item, str) and item)
        return (source, anchors) if anchors else None

    @staticmethod
    def _ui_pending_state_search(runtime: Runtime) -> tuple[str, ...]:
        context = getattr(runtime, "context", None)
        values = context.get(BUG_UI_PENDING_STATE_SEARCH_TERMS_CONTEXT_KEY) if isinstance(context, dict) else None
        return tuple(item for item in values or [] if isinstance(item, str) and item)

    @staticmethod
    def _ui_pending_behavior_focus(runtime: Runtime) -> dict[str, str | int] | None:
        context = getattr(runtime, "context", None)
        value = context.get(BUG_UI_PENDING_BEHAVIOR_FOCUS_CONTEXT_KEY) if isinstance(context, dict) else None
        if not isinstance(value, dict):
            return None
        platform = value.get("platform")
        owner = value.get("owner")
        focus_line = value.get("focus_line")
        if not isinstance(platform, str) or not platform or not isinstance(owner, str) or not owner:
            return None
        if isinstance(focus_line, bool) or not isinstance(focus_line, int) or focus_line < 1:
            return None
        return {"platform": platform, "owner": owner, "focus_line": focus_line}

    def _bash_limit_reached(self, runtime: Runtime) -> bool:
        limit = self._bash_call_limit(runtime)
        if limit is None:
            return False
        with self._lock:
            return self._bash_used.get(self._key(runtime), 0) >= limit

    def _reserve_allowed_bash(self, request: ToolCallRequest) -> bool:
        if str(request.tool_call.get("name") or "") != "bash":
            return True
        args = request.tool_call.get("args")
        command = args.get("command") if isinstance(args, dict) else None
        prefixes = self._allowed_bash_prefixes(request.runtime)
        if prefixes is not None and (not isinstance(command, str) or not any(command.startswith(prefix) for prefix in prefixes)):
            return False
        limit = self._bash_call_limit(request.runtime)
        if limit is None:
            return True
        key = self._key(request.runtime)
        with self._lock:
            used = self._bash_used.get(key, 0)
            if used >= limit:
                return False
            self._bash_used[key] = used + 1
        return True

    @staticmethod
    def _read_path(request: ToolCallRequest) -> str | None:
        tool_call = request.tool_call
        if str(tool_call.get("name") or "") != "read_file":
            return None
        arguments = tool_call.get("args")
        path = arguments.get("path") if isinstance(arguments, dict) else None
        return path.strip() if isinstance(path, str) and path.strip() else None

    @staticmethod
    def _enabled_duplicate_failed_read_guard(runtime: Runtime) -> bool:
        context = getattr(runtime, "context", None)
        return bool(context.get(_DUPLICATE_FAILED_READS_CONTEXT_KEY)) if isinstance(context, dict) else False

    @staticmethod
    def _failed_read_result(result: ToolMessage | Command) -> bool:
        if not isinstance(result, ToolMessage):
            return False
        content = str(result.content).lower()
        return result.status == "error" or any(marker in content for marker in ("does not exist", "no such file", "not found", "404"))

    def _duplicate_failed_read(self, request: ToolCallRequest) -> str | None:
        if not self._enabled_duplicate_failed_read_guard(request.runtime):
            return None
        path = self._read_path(request)
        if path is None:
            return None
        with self._lock:
            if path in self._failed_read_paths.get(self._key(request.runtime), set()):
                return path
        return None

    def _remember_failed_read(self, request: ToolCallRequest, result: ToolMessage | Command) -> None:
        if not self._enabled_duplicate_failed_read_guard(request.runtime):
            return
        path = self._read_path(request)
        if path is None or not self._failed_read_result(result):
            return
        key = self._key(request.runtime)
        with self._lock:
            paths = set(self._failed_read_paths.get(key, set()))
            paths.add(path)
            self._failed_read_paths[key] = paths

    @staticmethod
    def _read_range(request: ToolCallRequest) -> tuple[int, float] | None:
        if ToolCallLimitMiddleware._read_path(request) is None:
            return None
        arguments = request.tool_call.get("args")
        if not isinstance(arguments, dict):
            return 1, float("inf")
        start = arguments.get("start_line")
        end = arguments.get("end_line")
        normalized_start = start if isinstance(start, int) and not isinstance(start, bool) and start > 0 else 1
        normalized_end = float(end) if isinstance(end, int) and not isinstance(end, bool) and end >= normalized_start else float("inf")
        return normalized_start, normalized_end

    def _redundant_read_guard_tripped(self, runtime: Runtime) -> bool:
        if self._redundant_read_limit(runtime) is None:
            return False
        with self._lock:
            return bool(self._redundant_read_tripped.get(self._key(runtime), False))

    def _remember_successful_tool_progress(self, request: ToolCallRequest, result: ToolMessage | Command) -> None:
        if self._redundant_read_limit(request.runtime) is None or self._failed_read_result(result):
            return
        tool_name = str(request.tool_call.get("name") or "")
        key = self._key(request.runtime)
        if tool_name in {"str_replace", "write_file"}:
            with self._lock:
                self._read_ranges.pop(key, None)
                self._redundant_read_streak.pop(key, None)
                self._redundant_read_tripped.pop(key, None)
            return
        path = self._read_path(request)
        current_range = self._read_range(request)
        if path is None or current_range is None:
            return
        with self._lock:
            ranges_by_path = dict(self._read_ranges.get(key, {}))
            known_ranges = list(ranges_by_path.get(path, []))
            start, end = current_range
            redundant = any(known_start <= start and known_end >= end for known_start, known_end in known_ranges)
            if redundant:
                previous_path, previous_count = self._redundant_read_streak.get(key, ("", 0))
                streak = previous_count + 1 if previous_path == path else 1
                self._redundant_read_streak[key] = (path, streak)
                if streak >= (self._redundant_read_limit(request.runtime) or 1):
                    self._redundant_read_tripped[key] = True
                return
            known_ranges.append(current_range)
            ranges_by_path[path] = known_ranges
            self._read_ranges[key] = ranges_by_path
            self._redundant_read_streak.pop(key, None)

    @staticmethod
    def _duplicate_failed_read_message(request: ToolCallRequest, path: str) -> ToolMessage:
        tool_call = request.tool_call
        return ToolMessage(
            content=(f"[DUPLICATE_FAILED_READ_BLOCKED] read_file already failed for '{path}' in this run. Use a confirmed caller path or summarize the evidence; do not retry this path."),
            tool_call_id=str(tool_call.get("id") or "missing_tool_call_id"),
            name="read_file",
            status="error",
        )

    def _reserve_call(self, runtime: Runtime) -> bool:
        limit = self._limit(runtime)
        if limit is None:
            return True
        key = self._key(runtime)
        with self._lock:
            used = self._used.get(key, 0)
            if used >= limit:
                return False
            self._used[key] = used + 1
            return True

    def _prepare_model_request(self, request: ModelRequest) -> ModelRequest:
        blocked_names = self._blocked_tool_names(request.runtime)
        if self._bash_limit_reached(request.runtime):
            blocked_names = blocked_names | {"bash"}
        redundant_read_tripped = self._redundant_read_guard_tripped(request.runtime)
        if redundant_read_tripped:
            blocked_names = blocked_names | {"read_file"}
        if blocked_names:
            request = request.override(tools=[tool for tool in request.tools if getattr(tool, "name", None) not in blocked_names])
        anchor_selection = self._ui_anchor_selection(request.runtime)
        pending_state_search = self._ui_pending_state_search(request.runtime)
        pending_behavior_focus = self._ui_pending_behavior_focus(request.runtime)
        if anchor_selection is not None:
            request = request.override(tools=[tool for tool in request.tools if getattr(tool, "name", None) == "trace_ui_source_anchor"])
        elif pending_state_search:
            request = request.override(tools=[tool for tool in request.tools if getattr(tool, "name", None) == "grep"])
        elif pending_behavior_focus is not None:
            request = request.override(tools=[tool for tool in request.tools if getattr(tool, "name", None) == "trace_ui_behavior_owner"])
        elif self._ui_source_entry_confirmed(request.runtime):
            request = request.override(tools=[tool for tool in request.tools if getattr(tool, "name", None) not in {"ls", "glob"}])
        reminder = self._persistent_run_reminder(request.runtime)
        appended_messages = list(request.messages)
        if reminder:
            appended_messages.append(
                HumanMessage(
                    content=f"<system_reminder>\n{reminder}\n</system_reminder>",
                    name="persistent_run_reminder",
                    additional_kwargs={"hide_from_ui": True},
                )
            )
        if redundant_read_tripped:
            appended_messages.append(
                HumanMessage(
                    content=_REDUNDANT_READ_REMINDER,
                    name="redundant_read_guard",
                    additional_kwargs={"hide_from_ui": True},
                )
            )
        if anchor_selection is not None:
            source, anchors = anchor_selection
            appended_messages.append(
                HumanMessage(
                    content=_UI_ANCHOR_SELECTION_REMINDER.format(source=source, anchors=", ".join(anchors)),
                    name="ui_source_anchor_selection",
                    additional_kwargs={"hide_from_ui": True},
                )
            )
        elif pending_state_search:
            appended_messages.append(
                HumanMessage(
                    content=_UI_STATE_SEARCH_REMINDER.format(terms=", ".join(pending_state_search)),
                    name="ui_state_source_search",
                    additional_kwargs={"hide_from_ui": True},
                )
            )
        elif pending_behavior_focus is not None:
            appended_messages.append(
                HumanMessage(
                    content=_UI_BEHAVIOR_FOCUS_REMINDER.format(**pending_behavior_focus),
                    name="ui_behavior_focus",
                    additional_kwargs={"hide_from_ui": True},
                )
            )
        if len(appended_messages) != len(request.messages):
            request = request.override(messages=appended_messages)
        limit = self._limit(request.runtime)
        if limit is None or self._used_calls(request.runtime) < limit:
            return request
        return request.override(
            tools=[],
            messages=[
                *request.messages,
                HumanMessage(
                    content=_FINALIZE_PROMPT,
                    name="tool_call_limit",
                    additional_kwargs={"hide_from_ui": True},
                ),
            ],
        )

    @override
    def before_agent(self, state: AgentState, runtime: Runtime) -> None:
        if self._limit(runtime) is not None or self._redundant_read_limit(runtime) is not None:
            with self._lock:
                self._used.pop(self._key(runtime), None)
                self._bash_used.pop(self._key(runtime), None)
                self._failed_read_paths.pop(self._key(runtime), None)
                self._read_ranges.pop(self._key(runtime), None)
                self._redundant_read_streak.pop(self._key(runtime), None)
                self._redundant_read_tripped.pop(self._key(runtime), None)

    @override
    async def abefore_agent(self, state: AgentState, runtime: Runtime) -> None:
        self.before_agent(state, runtime)

    @override
    def after_agent(self, state: AgentState, runtime: Runtime) -> None:
        with self._lock:
            self._used.pop(self._key(runtime), None)
            self._bash_used.pop(self._key(runtime), None)
            self._failed_read_paths.pop(self._key(runtime), None)
            self._read_ranges.pop(self._key(runtime), None)
            self._redundant_read_streak.pop(self._key(runtime), None)
            self._redundant_read_tripped.pop(self._key(runtime), None)

    @override
    async def aafter_agent(self, state: AgentState, runtime: Runtime) -> None:
        self.after_agent(state, runtime)

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        return handler(self._prepare_model_request(request))

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        return await handler(self._prepare_model_request(request))

    @hook_config(can_jump_to=["end"])
    @override
    def after_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        """End the run after its single tool-free summary opportunity."""
        limit = self._limit(runtime)
        if limit is not None and self._used_calls(runtime) >= limit:
            return {"jump_to": "end"}
        return None

    @hook_config(can_jump_to=["end"])
    @override
    async def aafter_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self.after_model(state, runtime)

    @staticmethod
    def _blocked_tool_message(request: ToolCallRequest) -> ToolMessage:
        tool_call = request.tool_call
        return ToolMessage(
            content=_BLOCKED_TOOL_MESSAGE,
            tool_call_id=str(tool_call.get("id") or "missing_tool_call_id"),
            name=str(tool_call.get("name") or "unknown_tool"),
            status="error",
        )

    @staticmethod
    def _run_policy_blocked_tool_message(request: ToolCallRequest) -> ToolMessage:
        tool_call = request.tool_call
        return ToolMessage(
            content=_RUN_POLICY_BLOCKED_TOOL_MESSAGE,
            tool_call_id=str(tool_call.get("id") or "missing_tool_call_id"),
            name=str(tool_call.get("name") or "unknown_tool"),
            status="error",
        )

    @staticmethod
    def _redundant_read_blocked_message(request: ToolCallRequest) -> ToolMessage:
        tool_call = request.tool_call
        return ToolMessage(
            content=_REDUNDANT_READ_BLOCKED_MESSAGE,
            tool_call_id=str(tool_call.get("id") or "missing_tool_call_id"),
            name="read_file",
            status="error",
        )

    def _is_blocked_by_run_policy(self, request: ToolCallRequest) -> bool:
        tool_name = str(request.tool_call.get("name") or "")
        if tool_name in self._blocked_tool_names(request.runtime):
            return True
        if self._ui_anchor_selection(request.runtime) is not None:
            return tool_name != "trace_ui_source_anchor"
        if self._ui_pending_state_search(request.runtime):
            return tool_name != "grep"
        return self._ui_pending_behavior_focus(request.runtime) is not None and tool_name != "trace_ui_behavior_owner"

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        if self._is_blocked_by_run_policy(request):
            return self._run_policy_blocked_tool_message(request)
        if self._read_path(request) is not None and self._redundant_read_guard_tripped(request.runtime):
            return self._redundant_read_blocked_message(request)
        if not self._reserve_allowed_bash(request):
            return self._run_policy_blocked_tool_message(request)
        failed_path = self._duplicate_failed_read(request)
        if failed_path is not None:
            return self._duplicate_failed_read_message(request, failed_path)
        if not self._reserve_call(request.runtime):
            return self._blocked_tool_message(request)
        result = handler(request)
        self._remember_failed_read(request, result)
        self._remember_successful_tool_progress(request, result)
        return result

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        if self._is_blocked_by_run_policy(request):
            return self._run_policy_blocked_tool_message(request)
        if self._read_path(request) is not None and self._redundant_read_guard_tripped(request.runtime):
            return self._redundant_read_blocked_message(request)
        if not self._reserve_allowed_bash(request):
            return self._run_policy_blocked_tool_message(request)
        failed_path = self._duplicate_failed_read(request)
        if failed_path is not None:
            return self._duplicate_failed_read_message(request, failed_path)
        if not self._reserve_call(request.runtime):
            return self._blocked_tool_message(request)
        result = await handler(request)
        self._remember_failed_read(request, result)
        self._remember_successful_tool_progress(request, result)
        return result
