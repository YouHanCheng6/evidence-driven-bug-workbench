"""Model-only intent recognition for personal-assistant Feishu control messages."""

from __future__ import annotations

import re
from dataclasses import dataclass

from deerflow.config import get_app_config
from deerflow.config.agents_config import load_agent_config
from deerflow.utils.oneshot_llm import run_oneshot_llm

_OPERATION_PATTERN = re.compile(r"(?:回退|恢复|拉取|提交拉取并推送) #\d{1,10}")
_MISSING_ID_OUTPUTS = {
    "缺少 Bug 编号：回退": "回退",
    "缺少 Bug 编号：恢复": "恢复",
    "缺少 Bug 编号：拉取": "拉取",
    "缺少 Bug 编号：提交拉取并推送": "提交拉取并推送",
}
_STATIC_OPERATIONS = frozenset({"取消当前 Bug"})
_OPERATION_INTENT_CUES = ("取消", "停止", "回退", "恢复", "拉取", "提交", "推送")

_SYSTEM_INSTRUCTION = """你只负责识别当前这一条飞书消息的操作意图，不调用工具，不回答问题，不参考历史消息。

只能输出下面一种结果，必须只输出一行，不加解释、标点、Markdown 或代码块：
- 普通消息
- 取消当前 Bug
- 回退 #消息中的实际Bug编号
- 恢复 #消息中的实际Bug编号
- 拉取 #消息中的实际Bug编号
- 提交拉取并推送 #消息中的实际Bug编号
- 缺少 Bug 编号：回退
- 缺少 Bug 编号：恢复
- 缺少 Bug 编号：拉取
- 缺少 Bug 编号：提交拉取并推送

识别规则：
- 用户明确要求取消或停止当前 Bug，直接输出“取消当前 Bug”；取消当前任务不需要 Bug 编号。
- 用户明确要求回退、恢复、拉取、提交拉取并推送时，消息里有 Bug 编号就原样带上编号；没有编号就输出对应的“缺少 Bug 编号”结果，绝不从历史消息猜编号。
- 除以上操作外全部输出“普通消息”。Bug 分析、禅道查询、视频 A/B 选择和普通聊天都属于普通消息。
"""


@dataclass(frozen=True, slots=True)
class FeishuOperationIntent:
    command: str | None = None
    missing_bug_id_action: str | None = None


def should_recognize_feishu_operation_intent(text: str) -> bool:
    """Gate only possible control requests into the model; never decide the intent here."""
    return any(cue in text for cue in _OPERATION_INTENT_CUES)


async def recognize_feishu_operation_intent(
    text: str,
    *,
    user_id: str | None,
    thread_id: str | None,
) -> FeishuOperationIntent:
    """Classify one message without exposing tools or entering the full Agent."""
    app_config = get_app_config()
    agent_config = load_agent_config("personal-assistant", user_id=user_id)
    output = (
        await run_oneshot_llm(
            system_instruction=_SYSTEM_INSTRUCTION,
            user_content=text,
            run_name="personal-assistant-feishu-operation-intent",
            app_config=app_config,
            model_name=agent_config.model if agent_config else None,
            thread_id=thread_id,
            max_tokens=40,
            thinking_enabled=False,
        )
    ).strip()

    if output == "普通消息":
        return FeishuOperationIntent()
    if output in _STATIC_OPERATIONS or _OPERATION_PATTERN.fullmatch(output):
        return FeishuOperationIntent(command=output)
    if output in _MISSING_ID_OUTPUTS:
        return FeishuOperationIntent(missing_bug_id_action=_MISSING_ID_OUTPUTS[output])
    raise ValueError(f"飞书操作意图模型返回了无效结果：{output!r}")
