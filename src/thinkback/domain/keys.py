"""幂等指纹与作用域键。

写入链路的幂等语义全部建立在这几个纯函数上：
- 指纹相同 => 同一轮对话（幂等去重）
- 指纹不同 => 同一 round_id 下的冲突写入（409）
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from thinkback.domain.entities import JournalEntry
    from thinkback.memory.schemas import AppendMemoryRequest


def scope_key(user_id: str, memory_scope_id: str) -> str:
    """用户-范围维度的组合键，用于多维查询隔离"""
    return f"{user_id}:{memory_scope_id}"


def fingerprint(messages: list[dict[str, Any]]) -> str:
    """对话消息指纹。用于幂等性检查：两个指纹相同的回合被视为同一回合"""
    raw = "|".join(f"{message['role']}:{message['content']}" for message in messages)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def request_fingerprint(request: AppendMemoryRequest) -> str:
    """请求级指纹，由消息指纹计算得出"""
    return fingerprint(
        [{"role": message.role.value, "content": message.content} for message in request.messages]
    )


def source_ref_key(source_ref: dict[str, str]) -> tuple[str | None, str | None]:
    """源引用键：(session_id, round_id) 对，用于追踪记忆来源"""
    return source_ref.get("session_id"), source_ref.get("round_id")


def round_sort_key(entry: JournalEntry) -> tuple[datetime, int, str]:
    """回合排序键。用于保证回合按时间序列化"""
    round_index = entry.round_index if entry.round_index is not None else -1
    return entry.source_timestamp, round_index, entry.round_id
