"""L2 摘要策略。

两级实现：
- ``summarize_rounds``：V1 拼接式（最近 10 轮 user 消息用 "；" 连接），
  是 LLM 不可用/未启用时的**降级路径**，也是每次 append 的同步占位摘要。
- ``SummaryComposer`` + 结构化 prompt：V2 LLM 综合摘要（后台异步刷新，
  对齐 ChatGPT "Memory Synthesis" / Letta sleep-time 的持续综合范式），
  输出强 schema 4 段画像（主题/进行中事项/行为偏好/近期状态），同时
  保留拼接版 ``summary_text`` 作为向后兼容视图。

G4 落地：``SummaryComposerResult`` 携带结构化 4 段，下游可机器消费各段。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from thinkback.domain.entities import JournalEntry

SummaryComposerResult = tuple[str, dict[str, str]]
"""LLM 摘要合成器结果：(拼接视图, 4 段结构化画像)。

``structured_sections`` 必须包含 4 个 key（无内容的小节写"无"）：
- 主题 / 进行中事项 / 行为偏好 / 近期状态

失败必须抛异常（由后台刷新器捕获并降级为保留拼接版）。"""

SummaryComposer = Callable[[list[JournalEntry], str | None], SummaryComposerResult]
"""LLM 摘要合成器：输入（全部可用轮次, 上一次摘要文本），输出 (拼接视图, 结构化画像)。"""


@dataclass
class StructuredSummary:
    """G4 schema 化的 L2 摘要 4 段画像。"""

    主题: str = ""
    进行中事项: str = ""
    行为偏好: str = ""
    近期状态: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "主题": self.主题,
            "进行中事项": self.进行中事项,
            "行为偏好": self.行为偏好,
            "近期状态": self.近期状态,
        }


L2_STRUCTURED_SUMMARY_SYSTEM_PROMPT = """你是会话记忆整理器。把当前会话的轮次整理成一份持续更新的中文综合画像，供下一轮对话作为上下文使用。

要求：
1. 只依据给到的轮次与上一版摘要，不要编造；上一版摘要中已被新信息推翻的内容要更新掉。
2. **严格按 JSON 输出**：{"主题": "<string>", "进行中事项": "<string>", "行为偏好": "<string>", "近期状态": "<string>"}。每个字段是字符串（无内容写"无"），总长不超过 300 字。绝对不要嵌套对象或数组。
3. 保留具体实体（名字、地点、日期）原文，不要概括掉。
4. 只输出 JSON 本身，不要任何前后缀说明。"""


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
    lines.append("\n请输出更新后的 JSON 画像。")
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


def sections_to_text(sections: dict[str, str]) -> str:
    """把结构化 4 段画像拼成 summary_text 视图（向后兼容）。"""

    order = ["主题", "进行中事项", "行为偏好", "近期状态"]
    parts: list[str] = []
    for key in order:
        value = sections.get(key) or sections.get(f"【{key}】")
        if value and value != "无":
            parts.append(f"【{key}】{value}")
    return "\n".join(parts) if parts else "无"
