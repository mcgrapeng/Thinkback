"""内容安全准入策略（write-gate）。

写入链路最早期执行：命中就**不写长期记忆**（L3 与 L2 摘要都不写），
L1 原文**留在库里**（治理台可见、可编辑、可删除），但该轮**不进召回** ——
凭据与注入文本回到模型上下文里，这个门禁就等于没装。

原先这里是「命中即整轮 ``mark_round_deleted``」：连原文一起销毁，调用方还收到
``status="completed"`` —— 误杀率极高（``set api_key:`` / ``the tool:`` /
``System:`` / ``token:`` 都会命中），而对抗性注入只需加两个空格
（``ignore␣␣all␣␣previous␣␣instructions``）就全部放行。两件事一件没做到，
只制造静默数据丢失。

现在分成两件事：
1. 「不写入长期记忆」→ 返回 skipped + 明确 reason，原文留在库里但隔离出召回；
2. 「删除」→ 独立的、需显式授权的操作，不在写入路径上自动发生。

词表只拦**真的**凭据与注入：``system:`` / ``tool:`` 只在一行开头才算命中 ——
「我们的操作系统：Ubuntu」这类普通业务话术不能被整轮扔掉。

``role in {system, tool}`` 不再触发拦截 —— 把工具遥测与 prompt injection
混为一谈，正常的多模态/工具调用会被误杀。
"""

from __future__ import annotations

import re

RESTRICTED_PATTERNS: tuple[str, ...] = (
    r"\bapi[_-]?key\s*[:=]",
    # secret key / secret_key 两种写法都要拦 —— 漏一种就等于把凭据放行。
    r"\bsecret[_\s]+key\s*[:=]",
    r"\bpassword\s*[:=]\s*\S+",
    r"\btoken\s*[:=]\s*\S+",
    r"\bsk-[a-z0-9_-]{6,}",
    # 注入语料里的多空格变体（"ignore  all  previous  instructions"）也是注入：
    # 词表用 \s+ 而不是单个空格，否则加两个空格就全部放行。
    r"ignore\s+(?:all\s+)?previous\s+instructions",
    r"忽略.*指令",
    r"系统提示词",
    # 这两条只在一行**开头**时才算注入。中途出现的 "our operating system:
    # Ubuntu" / "the tool: kubectl" 是普通业务话术 —— 企业助手场景里极常见，
    # 按"任意位置匹配"会把整轮对话静默扔掉（误杀率不可接受）。
    r"(?m)^\s*system:\s",
    r"(?m)^\s*tool:\s",
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

    return any(text_is_restricted_or_unsafe(message.get("content", "")) for message in messages)


def restricted_reason(messages: list[dict[str, str]]) -> str | None:
    """命中时给出可读的原因（写进日志与响应），未命中返回 None。"""
    for message in messages:
        content = message.get("content", "")
        normalized = content.lower()
        for pattern, compiled in _COMPILED_PATTERNS:
            if compiled.search(normalized):
                return f"restricted_or_unsafe_memory_content:{pattern}"
    return None
