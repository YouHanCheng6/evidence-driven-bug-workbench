"""Thin main-agent launch adapter; does not change investigation configuration."""

import json
import os
from collections.abc import Mapping

import httpx

from app.gateway.csrf_middleware import CSRF_COOKIE_NAME, CSRF_HEADER_NAME, generate_csrf_token
from app.gateway.internal_auth import create_internal_auth_headers
from deerflow.persistence.engine import get_session_factory
from deerflow.persistence.thread_meta import make_thread_store
from deerflow.runtime.context_keys import BUG_WORKBENCH_OWNER_USER_ID_CONTEXT_KEY
from deerflow.runtime.user_context import resolve_runtime_user_id


async def start_from_main_agent(runtime, bug_id: int) -> str:
    context = runtime.context if isinstance(runtime.context, Mapping) else {}
    user_id = resolve_runtime_user_id(runtime)
    shared_owner = context.get(BUG_WORKBENCH_OWNER_USER_ID_CONTEXT_KEY)
    owner = shared_owner if context.get("is_internal") is True and isinstance(shared_owner, str) and shared_owner else user_id
    store = make_thread_store(get_session_factory(), runtime.store)
    thread_id = context.get("thread_id") or runtime.config.get("configurable", {}).get("thread_id")
    thread = await store.get(thread_id, user_id=user_id) if thread_id else None
    source = (thread or {}).get("metadata", {}).get("channel_source", {})
    from app.channels.service import get_channel_service

    service = get_channel_service()
    # source comes from the owner-filtered persisted thread, not model arguments.
    # Internal-auth flags are not consistently exposed by ToolRuntime; they must
    # not decide whether a real Feishu launch gets its completion watcher.
    if source.get("provider") == "feishu" and service is not None:
        from app.channels.message_bus import InboundMessage

        msg = InboundMessage(
            channel_name="feishu", chat_id=source["chat_id"], user_id=user_id,
            owner_user_id=user_id, connection_id=source.get("connection_id"),
            topic_id=source.get("topic_id"), thread_ts=source.get("thread_ts"),
            text=str(bug_id),
        )
        # Reuse original dispatch, active-task handling and completion watcher.
        result = await service.manager.launch_main_agent_bug(msg, bug_id)
        return json.dumps(result, ensure_ascii=False)

    if source.get("provider") == "feishu":
        return "无法启动调查：飞书通知服务不可用。请稍后重试，尚未创建工作台任务。"

    token = generate_csrf_token()
    headers = create_internal_auth_headers(owner_user_id=owner)
    headers.update({CSRF_HEADER_NAME: token, "Cookie": f"{CSRF_COOKIE_NAME}={token}", "X-DeerFlow-Bug-Action-Source": "main_agent"})
    gateway_url = service.manager._gateway_url if service is not None else os.environ.get("DEER_FLOW_GATEWAY_URL", "http://127.0.0.1:8001")
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(f"{gateway_url.rstrip('/')}/api/bug-workflows", headers=headers, json={"bug_id": bug_id, "auto_repair": False})
    response.raise_for_status()
    payload = response.json()
    return json.dumps(
        {
            "workflow_id": payload.get("id"),
            "bug_id": bug_id,
            "status": payload.get("status"),
            "completion_notification": False,
            "instruction": "任务在工作台执行；当前入口未绑定完成通知，不得承诺主动通知。",
        },
        ensure_ascii=False,
    )
