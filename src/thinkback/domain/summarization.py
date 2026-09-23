"""L2 摘要策略。

两级实现：
- ``summarize_rounds``：V1 拼接式（最近 10 轮 user 消息用 "；" 连接），
  是 LLM 不可用/未启用时的**降级路径**，也是每次 append 的同步占位摘要。
- ``SummaryComposer`` + 结构化 prompt：V2 LLM 综合摘要（后台异步刷新，
  对齐 ChatGPT "Memory Synthesis" / Letta sleep-time 的持续综合范式），
  输出带结构标记的中文摘要（主题/进行中事项/行为偏好/近期状态）。

召回侧只消费 ``summary_text``，对两种来源透明。
"""

from __future__ import annotations

from collections.abc import Callable

from thinkback.domain.entities import JournalEntry

SummaryComposer = Callable[[list[JournalEntry], str | None], str]
"""LLM 摘要合成器：输入（全部可用轮次, 上一次摘要文本），输出新摘要文本。

失败必须抛异常（由后台刷新器捕获并降级为保留拼接版）；
输入 ``previous_summary`` 为 None 表示首次生成。"""

L2_STRUCTURED_SUMMARY_SYSTEM_PROMPT = """你是会话记忆整理器。把当前会话的轮次整理成一份持续更新的中文综合摘要，供下一轮对话作为上下文使用。

要求：
1. 只依据给到的轮次与上一版摘要，不要编造；上一版摘要中已被新信息推翻的内容要更新掉。
2. 用以下固定结构输出（无内容的小节写"无"），总长不超过 300 字：
【主题】本会话在聊什么
【进行中事项】用户提到还未解决/等待跟进的事
【行为偏好】用户表达过的喜好、边界、沟通偏好
【近期状态】用户当前的情绪、处境或阶段性事实
3. 保留具体实体（名字、地点、日期）原文，不要概括掉。
4. 直接输出摘要正文，不要任何前后缀说明。"""


def build_structured_summary_user_prompt(
    rounds: list[JournalEntry], previous_summary: str | None
) -> str:
    """构造 L2 结构化摘要的 user prompt（纯函数）。"""

    lines = []
    if previous_summary:
        lines.append(f"上一版摘要：\n{previous_summary}\n")
    lines.append("本会话轮次（按时间顺序）：")
    for entry in rounds:
        for message in entry.messages:
            if message.get("role") == "user" and str(message.get("content", "")).strip():
                lines.append(f"user: {str(message['content']).strip()}")
    lines.append("\n请输出更新后的综合摘要。")
    return "\n".join(lines)


def summarize_rounds(rounds: list[JournalEntry]) -> tuple[str, JournalEntry | None]:
    """从最近 10 轮的用户消息生成纯文本摘要（简单拼接，降级路径）

    返回：(摘要文本, 最新回合)
    """
    text_parts: list[str] = []
    latest: JournalEntry | None = None
    for entry in rounds[-10:]:
        latest = entry
        text_parts.extend(
            message["content"] for message in entry.messages if message["role"] == "user"
        )
    return "；".join(text_parts), latest
