"""Middleware to enforce per-run token budget limits.
Tracks cumulative token usage (input, output, total) across model calls within
a single agent run and enforces configurable soft-warning and hard-stop
thresholds.
Detection strategy:
  1. After each model response, sum the `usage_metadata` of all `AIMessage`s
     in the current thread history. This automatically captures tokens from
     subagents because `TokenUsageMiddleware` retroactively adds them to the
     history.
  2. If the highest fraction (input, output, or total) >= warn_threshold,
     queue a warning.
  3. If the highest fraction >= hard_stop_threshold, strip tool_calls.
Warning injection uses the deferred pattern:
  - after_model queues the warning (does NOT mutate state).
  - wrap_model_call injects it as a HumanMessage at the next model call.
This preserves AIMessage(tool_calls) → ToolMessage pairing.

Stop-reason surfacing (#3875 Phase 2):
  The hard stop does NOT raise — it strips tool_calls so the agent loop
  terminates naturally and produces a final answer. To let the caller (e.g.
  the subagent executor) distinguish a budget-capped completion from a clean
  one, the run that triggered the hard stop is recorded in ``_stop_reason``
  and exposed via :meth:`consume_stop_reason`. That dict is intentionally NOT
  cleared by ``after_agent``/``_clear_run_state`` so the executor can read it
  after the run returns; the bounded dict prevents unbounded growth on
  abandoned runs, and each subagent run builds a fresh middleware instance so
  there is no cross-run contamination.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse, hook_config
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.runtime import Runtime

from deerflow.agents.middlewares._bounded_dict import BoundedDict
from deerflow.config.token_budget_config import TokenBudgetConfig
from deerflow.runtime.context_keys import BUG_UI_ANALYSIS_CONTEXT_KEY
from deerflow.utils.messages import message_to_text

logger = logging.getLogger(__name__)

_BUDGET_WARNING_MSG = (
    "[TOKEN BUDGET WARNING] You have used {used:,} of your {budget:,} {reason} token budget ({percent:.0f}%). Wrap up your current work and produce a final answer. Avoid starting new tool calls unless absolutely necessary."
)
_BUDGET_EXCEEDED_MSG = "[TOKEN BUDGET EXCEEDED] The {reason} token usage ({used:,}) has exceeded the safety limit ({budget:,}). Producing final answer with results collected so far."
_BUG_UI_CONVERGENCE_MSG = "[UI BUG 12万收敛] 不得重跑地图、重读 Specs、全仓搜索或开启无来源的新候选。owner 已确认时只补当前源码证据范围的直接缺项；owner 未确认时只完成当前证据最完整的一条所有权验证链，然后输出完整 <bug_handoff>。"
_BUG_SPECIALIST_FINALIZATION_ENABLED_KEY = "bug_specialist_finalization_enabled"
_BUG_SPECIALIST_FINALIZATION_KEY = "bug_specialist_finalization"
_BUG_SPECIALIST_FINALIZATION_OUTPUT_RESERVE = 6_000
_BUG_HANDOFF_PATTERN = re.compile(r"<bug_handoff>\s*(.*?)\s*</bug_handoff>", re.IGNORECASE | re.DOTALL)
_BUG_SPECIALIST_FINALIZATION_PROMPT = (
    "现在只根据已有证据输出完整 <bug_handoff>，不得再调用工具。"
    "八段字符上限依次为：350/450/650/500/350/650/300/1000。"
    "同一证据只展开一次；第五段最多3个锚点；第七段最多3项；第八段摘要最多220字。"
    "保留影响范围证据分级表；删除无关附件细节、重复执行链和无证据推测。"
    "第八段必须为“结论与修复交接”，并包含：结论摘要、修复准备度、修复目标、确认入口、允许修改、允许只读、禁止修改、验证要求。"
    "字段无法由已有证据确定时，如实填写“仅人工处理”或“无”，不得重新搜索或猜测。"
)


@dataclass
class TokenUsage:
    input: int = 0
    output: int = 0
    total: int = 0


class TokenBudgetMiddleware(AgentMiddleware[AgentState]):
    """Enforce per-run token budget limits."""

    def __init__(self, config: TokenBudgetConfig) -> None:
        super().__init__()
        self._config = config
        self._lock = threading.Lock()

        # Keyed strictly by run_id (clobber-safe) and bounded (leak-safe)
        self._warned: BoundedDict[str, bool] = BoundedDict(1000)
        self._pending_warnings: BoundedDict[str, list[str]] = BoundedDict(1000)
        self._seen_messages: BoundedDict[str, dict[str, tuple[int, int]]] = BoundedDict(1000)
        self._cumulative_usage: BoundedDict[str, TokenUsage] = BoundedDict(1000)
        # Stop reason set when the hard-stop fires. NOT cleared by
        # ``_clear_run_state``/``after_agent`` so the executor can consume it
        # after the run returns; bounded so abandoned runs cannot leak.
        self._stop_reason: BoundedDict[str, str] = BoundedDict(1000)

    @classmethod
    def from_config(cls, config: TokenBudgetConfig) -> TokenBudgetMiddleware:
        return cls(config=config)

    def reset(self) -> None:
        with self._lock:
            self._warned.clear()
            self._pending_warnings.clear()
            self._seen_messages.clear()
            self._cumulative_usage.clear()
            self._stop_reason.clear()

    def consume_stop_reason(self, run_id: str | None) -> str | None:
        """Pop and return the stop reason the hard-stop set for this run.

        Returns ``"token_capped"`` when the budget hard-stop fired during the
        run, otherwise ``None``. The executor calls this after the run returns
        to decide whether a completed subagent was actually budget-capped
        (and should carry ``stop_reason=token_capped`` to the lead). Popping
        keeps the dict from accumulating across runs on a reused instance.
        """
        with self._lock:
            return self._stop_reason.pop(run_id, None)

    @staticmethod
    def _get_run_id(runtime: Runtime) -> str:
        ctx = getattr(runtime, "context", None)
        if isinstance(ctx, dict) and "run_id" in ctx:
            return ctx["run_id"]
        # Fallback to runtime object ID to prevent collisions across embedded client runs
        return str(id(runtime))

    def _clear_run_state(self, run_id: str) -> None:
        with self._lock:
            self._warned.pop(run_id, None)
            self._pending_warnings.pop(run_id, None)
            self._seen_messages.pop(run_id, None)
            self._cumulative_usage.pop(run_id, None)

    @override
    def before_agent(self, state: AgentState, runtime: Runtime) -> None:
        if not self._config.enabled:
            return

        # Mark all old messages from previous runs as 'seen' so they don't count toward THIS run's budget
        messages = state.get("messages", [])
        if not messages:
            return

        run_id = self._get_run_id(runtime)
        with self._lock:
            seen = self._seen_messages.setdefault(run_id, {})
            self._cumulative_usage.setdefault(run_id, TokenUsage())

            for msg in messages:
                if isinstance(msg, AIMessage) and msg.id and hasattr(msg, "usage_metadata"):
                    usage = msg.usage_metadata or {}
                    input_tokens = usage.get("input_tokens", 0)
                    output_tokens = usage.get("output_tokens", 0)
                    seen[msg.id] = (input_tokens, output_tokens)

    @override
    async def abefore_agent(self, state: AgentState, runtime: Runtime) -> None:
        self.before_agent(state, runtime)

    @override
    def after_agent(self, state: AgentState, runtime: Runtime) -> None:
        if not self._config.enabled:
            return
        self._clear_run_state(self._get_run_id(runtime))

    @override
    async def aafter_agent(self, state: AgentState, runtime: Runtime) -> None:
        self.after_agent(state, runtime)

    @staticmethod
    def _append_text(content: str | list[dict | None] | None, stop_msg: str) -> str | list[dict | str]:
        """Append a stop message to an AIMessage.content field."""
        if content is None:
            return stop_msg
        if isinstance(content, str):
            if content:
                return f"{content}\n\n{stop_msg}"
            return f"\n\n{stop_msg}"
        if isinstance(content, list):
            new_content = list(content)
            new_content.append({"type": "text", "text": f"\n\n{stop_msg}"})
            return new_content
        return f"{content}\n\n{stop_msg}"

    def _build_hard_stop_update(self, msg: AIMessage, stop_msg: str) -> dict[str, Any]:
        """Build the state update dictionary for a hard stop."""
        updated_content = self._append_text(msg.content, stop_msg)
        kwargs = dict(msg.additional_kwargs) if msg.additional_kwargs else {}
        if "tool_calls" in kwargs:
            del kwargs["tool_calls"]
        if "function_call" in kwargs:
            del kwargs["function_call"]

        response_metadata = dict(getattr(msg, "response_metadata", {}) or {})

        if response_metadata.get("finish_reason") == "tool_calls":
            response_metadata["finish_reason"] = "stop"

        stopped_msg = msg.model_copy(update={"content": updated_content, "tool_calls": [], "additional_kwargs": kwargs, "response_metadata": response_metadata})
        return {"messages": [stopped_msg]}

    @staticmethod
    def _runtime_context(runtime: Runtime) -> dict[str, Any] | None:
        context = getattr(runtime, "context", None)
        return context if isinstance(context, dict) else None

    def _specialist_finalization_enabled(self, runtime: Runtime) -> bool:
        context = self._runtime_context(runtime)
        return bool(context and context.get(_BUG_SPECIALIST_FINALIZATION_ENABLED_KEY) is True)

    def _specialist_finalization_active(self, runtime: Runtime) -> bool:
        context = self._runtime_context(runtime)
        return bool(context and context.get(_BUG_SPECIALIST_FINALIZATION_KEY) is True)

    @staticmethod
    def _has_valid_bug_handoff(message: AIMessage) -> bool:
        text = str(message_to_text(message) or "")
        match = _BUG_HANDOFF_PATTERN.search(text)
        if match is None:
            return False
        body = re.sub(r"\s+", " ", match.group(1)).strip()
        return bool(body)

    def _build_specialist_completion_update(self, msg: AIMessage, *, finalization: bool) -> dict[str, Any]:
        kwargs = dict(msg.additional_kwargs) if msg.additional_kwargs else {}
        kwargs.pop("tool_calls", None)
        kwargs.pop("function_call", None)
        if finalization:
            kwargs[_BUG_SPECIALIST_FINALIZATION_KEY] = True
        response_metadata = dict(getattr(msg, "response_metadata", {}) or {})
        if response_metadata.get("finish_reason") == "tool_calls":
            response_metadata["finish_reason"] = "stop"
        completed = msg.model_copy(
            update={
                "tool_calls": [],
                "additional_kwargs": kwargs,
                "response_metadata": response_metadata,
            }
        )
        return {"messages": [completed], "jump_to": "end"}

    @staticmethod
    def _fallback_token_estimate(messages: list[Any]) -> int:
        """Network-free estimate used only before this run has real input usage."""
        total = 0
        for message in messages:
            text = str(message_to_text(message) or "")
            ascii_chars = sum(1 for char in text if ord(char) < 128)
            total += (ascii_chars + 3) // 4 + (len(text) - ascii_chars)
        return max(total, 1)

    def _estimated_next_input_tokens(self, request: ModelRequest) -> int:
        last_ai_index = -1
        last_input_tokens = 0
        for index in range(len(request.messages) - 1, -1, -1):
            message = request.messages[index]
            if not isinstance(message, AIMessage):
                continue
            usage = message.usage_metadata or {}
            last_input_tokens = int(usage.get("input_tokens", 0) or 0)
            last_ai_index = index
            break
        if last_input_tokens > 0:
            growth = self._fallback_token_estimate(list(request.messages[last_ai_index + 1 :]))
            return last_input_tokens + growth

        counter = getattr(request.model, "get_num_tokens_from_messages", None)
        if callable(counter):
            try:
                estimate = counter(request.messages)
                if isinstance(estimate, int) and estimate > 0:
                    return estimate
            except Exception:
                logger.debug("Model tokenizer could not estimate specialist finalization input", exc_info=True)
        return self._fallback_token_estimate(list(request.messages))

    def _should_finalize_specialist(self, request: ModelRequest) -> bool:
        if not self._specialist_finalization_enabled(request.runtime) or self._specialist_finalization_active(request.runtime):
            return self._specialist_finalization_active(request.runtime)
        run_id = self._get_run_id(request.runtime)
        with self._lock:
            usage = self._cumulative_usage.get(run_id, TokenUsage())
            used = usage.total
        effective_budget = int(self._config.max_tokens * self._config.hard_stop_threshold)
        remaining = max(0, effective_budget - used)
        estimated_input = self._estimated_next_input_tokens(request)
        return remaining <= estimated_input + _BUG_SPECIALIST_FINALIZATION_OUTPUT_RESERVE

    def _prepare_model_request(self, request: ModelRequest) -> ModelRequest:
        warnings = self._drain_pending_warnings(request.runtime)
        request = self._inject_warnings(request, warnings)
        if not self._should_finalize_specialist(request):
            return request

        context = self._runtime_context(request.runtime)
        if context is not None:
            context[_BUG_SPECIALIST_FINALIZATION_KEY] = True
        if request.messages and isinstance(request.messages[-1], HumanMessage) and request.messages[-1].name == "bug_specialist_finalization":
            messages = list(request.messages)
        else:
            messages = [
                *request.messages,
                HumanMessage(content=_BUG_SPECIALIST_FINALIZATION_PROMPT, name="bug_specialist_finalization"),
            ]
        return request.override(messages=messages, tools=[], tool_choice=None)

    def _apply(self, state: AgentState, runtime: Runtime) -> dict | None:
        if not self._config.enabled:
            return None

        messages = state.get("messages", [])
        if not messages:
            return None

        last_msg = messages[-1]
        if not isinstance(last_msg, AIMessage):
            return None

        run_id = self._get_run_id(runtime)
        finalization = self._specialist_finalization_active(runtime)
        valid_handoff = self._specialist_finalization_enabled(runtime) and self._has_valid_bug_handoff(last_msg)

        with self._lock:
            seen = self._seen_messages.setdefault(run_id, {})
            usage_accum = self._cumulative_usage.setdefault(run_id, TokenUsage())

            for msg in messages:
                if isinstance(msg, AIMessage) and msg.id and hasattr(msg, "usage_metadata"):
                    usage = msg.usage_metadata or {}

                    input_tokens = usage.get("input_tokens", 0)
                    output_tokens = usage.get("output_tokens", 0)

                    # Check what previously recorded for this exact message
                    prev_input, prev_output = seen.get(msg.id, (0, 0))

                    # Calculate if any new tokens were added (handles retroactive subagent tokens)
                    diff_input = max(0, input_tokens - prev_input)
                    diff_output = max(0, output_tokens - prev_output)

                    if diff_input > 0 or diff_output > 0:
                        usage_accum.input += diff_input
                        usage_accum.output += diff_output
                        usage_accum.total += diff_input + diff_output
                        seen[msg.id] = (input_tokens, output_tokens)

            if usage_accum.total <= 0:
                return self._build_specialist_completion_update(last_msg, finalization=finalization) if valid_handoff or finalization else None

            fractions = [("total", usage_accum.total, self._config.max_tokens)]
            if self._config.max_input_tokens:
                fractions.append(("input", usage_accum.input, self._config.max_input_tokens))
            if self._config.max_output_tokens:
                fractions.append(("output", usage_accum.output, self._config.max_output_tokens))

            highest_fraction = 0.0
            trigger_reason = ""
            trigger_used = 0
            trigger_budget = 0

            for reason, used, limit in fractions:
                frac = used / limit
                if frac > highest_fraction:
                    highest_fraction = frac
                    trigger_reason = reason
                    trigger_used = used
                    trigger_budget = limit

            if highest_fraction >= self._config.hard_stop_threshold:
                logger.warning("Token budget hard stop triggered for run %s: %s limit exceeded", run_id, trigger_reason)
                # Record the stop reason so the executor can surface
                # ``stop_reason=token_capped`` to the lead after the run
                # returns (the hard stop itself does not raise). See
                # ``consume_stop_reason``.
                self._stop_reason[run_id] = "token_capped"
                # Also write to runtime.context so the lead worker can read it
                # without needing a reference to this middleware instance (#4176).
                ctx = getattr(runtime, "context", None)
                if isinstance(ctx, dict):
                    ctx["stop_reason"] = "token_capped"
                stop_text = _BUDGET_EXCEEDED_MSG.format(reason=trigger_reason, used=trigger_used, budget=trigger_budget)
                update = self._build_hard_stop_update(last_msg, stop_text)
                stopped = update["messages"][0]
                if finalization:
                    update = self._build_specialist_completion_update(stopped, finalization=True)
                else:
                    update["jump_to"] = "end"
                return update

            if valid_handoff or finalization:
                return self._build_specialist_completion_update(last_msg, finalization=finalization)

            if highest_fraction >= self._config.warn_threshold and not self._warned.get(run_id, False):
                self._warned[run_id] = True
                percent = highest_fraction * 100
                warn_text = _BUDGET_WARNING_MSG.format(reason=trigger_reason, used=trigger_used, budget=trigger_budget, percent=percent)
                context = getattr(runtime, "context", None)
                if isinstance(context, dict) and context.get(BUG_UI_ANALYSIS_CONTEXT_KEY) is True:
                    warn_text = f"{warn_text}\n{_BUG_UI_CONVERGENCE_MSG}"
                logger.info("Token budget warning triggered for run %s: %s limit at %.1f%%", run_id, trigger_reason, percent)
                # queue warning for wrap_model_call
                warnings = self._pending_warnings.setdefault(run_id, [])
                warnings.append(warn_text)
                return None

            return None

    @hook_config(can_jump_to=["end"])
    @override
    def after_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._apply(state, runtime)

    @hook_config(can_jump_to=["end"])
    @override
    async def aafter_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._apply(state, runtime)

    def _drain_pending_warnings(self, runtime: Runtime) -> list[str]:
        if not self._config.enabled:
            return []

        run_id = self._get_run_id(runtime)
        with self._lock:
            warnings = self._pending_warnings.pop(run_id, None)
        return warnings or []

    def _inject_warnings(self, request: ModelRequest, warnings: list[str]) -> ModelRequest:
        if not warnings:
            return request

        merged_text = "\n\n".join(warnings)
        warning_msg = HumanMessage(content=merged_text, name="budget_warning")

        messages = getattr(request, "messages", [])
        new_messages = list(messages) + [warning_msg]
        return request.override(messages=new_messages)

    @override
    def wrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]) -> ModelCallResult:

        return handler(self._prepare_model_request(request))

    @override
    async def awrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]) -> ModelCallResult:
        return await handler(self._prepare_model_request(request))
