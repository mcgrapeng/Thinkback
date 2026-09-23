"""内容安全准入策略（write-gate）。

写入链路最早期执行：命中即整轮跳过 L1/L2/L3，宁可让一轮对话在
记忆侧"消失"，也不允许 prompt injection 或凭据泄漏进入长期数据。

当前实现是基于关键词与正则的硬编码词表，存在 false positive 风险
（如包含 ``secret``、``password`` 字面词的正常消息）；按真实流量
分布调整词表是后续任务，由产品与数据团队提供样本后进行。
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
    """整轮消息是否被拒绝：system/tool 角色或任一条正文命中词表。"""

    return any(
        message.get("role") in {"system", "tool"}
        or text_is_restricted_or_unsafe(message.get("content", ""))
        for message in messages
    )
