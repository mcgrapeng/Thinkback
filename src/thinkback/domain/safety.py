"""内容安全准入策略（write-gate）。

写入链路最早期执行：命中就**不写长期记忆**（L3 与 L2 摘要都不写），
但**保留 L1 原文** —— 用户刚说的那轮对话不该被静默删掉，
他自己还能在治理台看到、能编辑、能删除。

原先这里是「命中即整轮 ``mark_round_deleted``」：
连 L1 原文一起销毁，调用方还收到 ``status="completed"`` ——
误杀率极高（``set api_key:`` / ``the tool:`` / ``System:`` / ``token:`` 都会命中），
而对抗性注入只需加两个空格（``ignore␣␣all␣␣previous␣␣instructions``）就全部放行。
两件事一件没做到，只制造静默数据丢失。

现在分成两件事：
1. 「不写入长期记忆」→ 返回 skipped + 明确 reason，保留 L1；
2. 「删除」→ 独立的、需显式授权的操作，不在写入路径上自动发生。

``role in {system, tool}`` 不再触发拦截 —— 把工具遥测与 prompt injection
混为一谈，正常的多模态/工具调用会被误杀。
"""

from __future__ import annotations

import re

RESTRICTED_PATTERNS: tuple[str, ...] = (
    r"\bapi[_-]?key\s*[:=]",
    r"\bsecret\s+key\s*[:=]",
    r"\bpassword\s*[:=]\s*\S+",
    r"\btoken\s*[:=]\s*\S+",
    r"\bsk-[a-z0-9_-]{6,}",
    r"ignore previous instructions",
    r"忽略.*指令",
    r"系统提示词",
    r"system:\s",
    r"tool:\s",
)

_COMPILED_PATTERNS = tuple(
    (pattern, re.compile(pattern, re.IGNORECASE)) for pattern in RESTRICTED_PATTERNS
)


def text_is_restricted_or_unsafe(content: str) -> bool:
    """单条文本是否命中受限/不安全词表。"""

    normalized = content.lower()
    return any(pattern.search(normalized) for _, pattern in _COMPILED_PATTERNS)


def messages_are_restricted_or_unsafe(messages: list[dict[str, str]]) -> bool:
    """整轮消息是否命中词表（只看正文，不看角色）。"""

    return any(
        text_is_restricted_or_unsafe(message.get("content", "")) for message in messages
    )


def restricted_reason(messages: list[dict[str, str]]) -> str | None:
    """命中时给出可读的原因（写进日志与响应），未命中返回 None。"""
    for message in messages:
        content = message.get("content", "")
        normalized = content.lower()
        for pattern, compiled in _COMPILED_PATTERNS:
            if compiled.search(normalized):
                return f"restricted_or_unsafe_memory_content:{pattern}"
    return None
