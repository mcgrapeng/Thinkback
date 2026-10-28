"""幂等指纹与作用域键。

写入链路的幂等语义全部建立在这几个纯函数上：
- 指纹相同 => 同一轮对话（幂等去重）
- 指纹不同 => 同一 round_id 下的冲突写入（409）
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from thinkback.domain.entities import JournalEntry
    from thinkback.memory.schemas import AppendMemoryRequest


def scope_key(user_id: str, memory_scope_id: str) -> str:
    """用户-范围维度的组合键，用于多维查询隔离"""
    return f"{user_id}:{memory_scope_id}"


# 未指定 namespace 时的默认作用域。L1/L2 的隔离维度是
# (user, namespace, session)：namespace 是独立字段，不与 scope_id 拼接。
DEFAULT_NAMESPACE = "thinkback"


def namespace_scope_key(user_id: str, namespace: str, memory_scope_id: str) -> str:
    """用户-命名空间-范围维度的组合键。

    用于进程内缓存（L1 缓存、摘要字典）这类没有列约束的键：隔离维度必须
    包含 namespace，否则同 session_id 在不同 namespace 下会互相命中。
    """
    return f"{user_id}:{namespace}:{memory_scope_id}"


# 影响执行行为的 metadata 键：同 round 但改这些键，不能当成同一次写入。
BEHAVIORAL_METADATA_KEYS = (
    "force_extract",
    "memory_type",
    "data_classification",
    "source_type",
    "backend_categories",
)


def _canonical(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(messages: list[dict[str, Any]]) -> str:
    """对话消息指纹。用 canonical JSON 而不是 `|` 拼接，避免内容里的分隔符
    让两组不同消息撞成同一个 hash（幂等误判）。"""
    payload = [{"role": message["role"], "content": message["content"]} for message in messages]
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def request_fingerprint(request: AppendMemoryRequest) -> str:
    """请求级指纹：消息 + 影响行为的 metadata。

    与 ``save_round`` 存库的 ``round_fingerprint`` 必须同源计算，重放才比得上。

    注意：namespace **不进指纹**。指纹是已落库的哈希，改算法会让升级前的
    轮次在重放时算出不匹配的指纹（误报 409，客户端可能因此重复写入）。
    namespace 的隔离由 journal 的 namespace 字段单独判定。
    """
    payload = {
        "messages": [
            {"role": message.role.value, "content": message.content} for message in request.messages
        ],
        "behavior_metadata": {
            key: request.metadata.get(key)
            for key in BEHAVIORAL_METADATA_KEYS
            if key in request.metadata
        },
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def source_ref_key(source_ref: dict[str, str]) -> tuple[str | None, str | None]:
    """源引用键：(session_id, round_id) 对，用于追踪记忆来源"""
    return source_ref.get("session_id"), source_ref.get("round_id")


def round_sort_key(entry: JournalEntry) -> tuple[datetime, int, str]:
    """回合排序键。用于保证回合按时间序列化"""
    round_index = entry.round_index if entry.round_index is not None else -1
    return entry.source_timestamp, round_index, entry.round_id
