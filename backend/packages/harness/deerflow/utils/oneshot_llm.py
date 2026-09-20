"""Shared helper for one-shot, non-graph LLM text requests.

Several Gateway routes (input polishing, follow-up suggestions, and title-style
rewrites) do the same thing: build a chat model from config, attach Langfuse
trace metadata, invoke it once with a system + user message pair, and pull the
plain text back out of the response. Centralizing that sequence here keeps the
tracing-metadata fields and invocation shape from drifting between routers — a
fix to one (e.g. a new Langfuse field) now applies to all callers instead of
silently regressing in whichever copy was forgotten.

Response-text *cleaning* (think-block / code-fence stripping, JSON parsing) is
intentionally left to each caller because their post-processing differs; this
helper stops at the extracted raw text.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from deerflow.config.app_config import AppConfig
from deerflow.models import create_chat_model
from deerflow.runtime.user_context import get_effective_user_id
from deerflow.tracing import inject_langfuse_metadata
from deerflow.utils.llm_text import extract_response_text


@dataclass(frozen=True)
class OneShotLLMResult:
    """Plain response text plus provider-reported usage for one direct call."""

    text: str
    usage_metadata: dict[str, int]
    response_metadata: dict[str, Any] = field(default_factory=dict)


def _resolve_environment() -> str | None:
    return os.environ.get("DEER_FLOW_ENV") or os.environ.get("ENVIRONMENT")


async def run_oneshot_llm(
    *,
    system_instruction: str,
    user_content: str,
    run_name: str,
    app_config: AppConfig,
    model_name: str | None = None,
    thread_id: str | None = None,
    max_tokens: int | None = None,
    thinking_enabled: bool = False,
) -> str:
    """Run a single non-graph system+user LLM turn and return the raw text.

    Args:
        system_instruction: System message content.
        user_content: Human message content.
        run_name: LangChain ``run_name`` and Langfuse ``assistant_id`` for the call.
        app_config: Application config used to build the model.
        model_name: Optional model override; ``None`` uses the default model.
        thread_id: Optional thread id, forwarded to Langfuse for tracing only.

    Returns:
        The extracted plain-text content of the model response (uncleaned).
    """
    result = await run_oneshot_llm_result(
        system_instruction=system_instruction,
        user_content=user_content,
        run_name=run_name,
        app_config=app_config,
        model_name=model_name,
        thread_id=thread_id,
        max_tokens=max_tokens,
        thinking_enabled=thinking_enabled,
    )
    return result.text


async def run_oneshot_llm_result(
    *,
    system_instruction: str,
    user_content: str,
    run_name: str,
    app_config: AppConfig,
    model_name: str | None = None,
    thread_id: str | None = None,
    max_tokens: int | None = None,
    thinking_enabled: bool = False,
    streaming: bool = False,
) -> OneShotLLMResult:
    """Run one direct model call and retain its provider token metadata."""
    model = create_chat_model(
        name=model_name,
        thinking_enabled=thinking_enabled,
        app_config=app_config,
        model_overrides={"max_tokens": max_tokens},
    )
    invoke_config: dict = {"run_name": run_name}
    inject_langfuse_metadata(
        invoke_config,
        thread_id=thread_id,
        user_id=get_effective_user_id(),
        assistant_id=run_name,
        model_name=model_name,
        environment=_resolve_environment(),
    )
    messages = [
        SystemMessage(content=system_instruction),
        HumanMessage(content=user_content),
    ]
    if streaming:
        text_parts: list[str] = []
        usage: dict[str, int] = {}
        response_metadata: dict[str, Any] = {}
        async for chunk in model.astream(messages, config=invoke_config):
            chunk_text = extract_response_text(chunk.content)
            if chunk_text:
                text_parts.append(chunk_text)
            raw_chunk_usage: Any = getattr(chunk, "usage_metadata", None)
            if isinstance(raw_chunk_usage, dict):
                for key, value in raw_chunk_usage.items():
                    if isinstance(key, str) and isinstance(value, int) and value >= 0:
                        usage[key] = value
            raw_chunk_metadata = getattr(chunk, "response_metadata", None)
            if isinstance(raw_chunk_metadata, dict):
                response_metadata.update(raw_chunk_metadata)
        return OneShotLLMResult(
            text="".join(text_parts),
            usage_metadata=usage,
            response_metadata=response_metadata,
        )

    response = await model.ainvoke(messages, config=invoke_config)
    raw_usage: Any = getattr(response, "usage_metadata", None)
    usage_items = raw_usage.items() if isinstance(raw_usage, dict) else ()
    usage = {str(key): int(value) for key, value in usage_items if isinstance(key, str) and isinstance(value, int) and value >= 0}
    raw_response_metadata = getattr(response, "response_metadata", None)
    response_metadata = dict(raw_response_metadata) if isinstance(raw_response_metadata, dict) else {}
    return OneShotLLMResult(
        text=extract_response_text(response.content),
        usage_metadata=usage,
        response_metadata=response_metadata,
    )
