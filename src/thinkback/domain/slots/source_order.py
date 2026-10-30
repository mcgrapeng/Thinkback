"""来源排序游标与本地 P0 记忆的判定口径（纯函数 + 判定分支）。

从 ``support.py`` 拆出来的原因：``support.py`` 里混着两类东西 ——
**依据词表**（防捏造闸门的 marker 列表：宠物种类、槽位关键词、沟通偏好
措辞）与**判定逻辑**（哪个来源更新、这条记忆是不是同源、抽取闸门放不放行）。

两者的审计结论完全不同：

- 判定逻辑改坏 = 真缺陷（「用户改了偏好，系统还在用旧的那条」），进变异范围；
- 依据词表改坏 = 词表项变化（措辞调整），逐条钉死是覆盖率报表而不是缺陷信号，
  由 ``support.py`` 的契约用例覆盖，不进变异范围。

所以把判定逻辑单独放一个模块，让变异审计能只审它 —— 见
``pyproject.toml`` 的 ``[tool.mutmut]`` 注释。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from thinkback.domain.keys import source_ref_key
from thinkback.domain.slots.canonical import memory_conflict_slot

if TYPE_CHECKING:
    from thinkback.domain.ports import MemoryRepository


LOCAL_INDEX_ID_PREFIXES = (
    "local-p0:",
    "deleted-source:",
    "delete-all:",
    "delete-session:",
)

# L3 抽取前廉价过滤（LightMem 风格"感觉过滤"）：
# 把"LLM 抽取"塞在确定性闸门后，避免每轮全量送 mem0。
# 设计取舍：
# - **留空（默认）= 不闸门，所有轮都送 mem0**。向后兼容。
# - **非空 allowed_slots = 闸门启用**：只对"明确有 slot 价值的轮"才 send，
#   其他轮只落 L1/L2（避免 Milvus 沉淀不可召回的暗数据，消灭 B-4）。
# - ``force_extract=true`` metadata 始终强制抽取。
# 系统安全门（fail-closed）已被 append 主路径拦下，到达这里的就是 §5 的判定目标。
_MIN_BYTES_FOR_EXTRACTION = 24
_FORCE_EXTRACT_KEY = "force_extract"


def is_local_index_memory_id(memory_id: str) -> bool:
    """是否为服务本地合成的索引 ID（非 mem0 后端记忆）。"""
    return memory_id.startswith(LOCAL_INDEX_ID_PREFIXES)


def is_backend_managed_memory_id(memory_id: str) -> bool:
    """是否为后端（mem0/Milvus）管理的记忆 ID。"""
    return bool(memory_id) and not is_local_index_memory_id(memory_id)


def is_same_source_local_p0_memory(
    memory: Any,
    *,
    memory_text: str,
    l3_metadata: dict[str, Any],
) -> bool:
    """既有本地 P0 记忆是否与新事件同源（同槽位 + source_refs 相交）。"""
    backend_memory_id = str(getattr(memory, "backend_memory_id", ""))
    if not backend_memory_id.startswith("local-p0:"):
        return False
    if memory_conflict_slot(getattr(memory, "memory_text", "")) != memory_conflict_slot(
        memory_text
    ):
        return False
    existing_refs = {source_ref_key(ref) for ref in getattr(memory, "source_refs", [])}
    raw_refs = l3_metadata.get("source_refs")
    if not isinstance(raw_refs, list):
        return False
    new_refs = {source_ref_key(ref) for ref in raw_refs if isinstance(ref, dict)}
    return bool(existing_refs & new_refs)


def round_order(
    repository: MemoryRepository,
    user_id: str,
    session_id: str,
    round_id: str,
    *,
    source_timestamp: datetime | None = None,
) -> tuple[int, float, str]:
    """回合的全序排序键：来源时间 > 库内时间戳 > round_index > round_id 数字后缀。

    ``source_timestamp`` 是调用方手里已经拿到的来源时间（外部历史回放时
    库里没有这轮）。没有它时才会退回 round_id 数字——那只是权宜之计：
    round_id 的字典序和事实新旧无关，外部历史里 "round-z" 并不比 "round-a"
    新。
    """
    entry = repository.get_round(round_id)
    normalized = round_id.lower()
    numbers = [int(part) for part in re.findall(r"\d+", normalized)]
    lexical_order = f"{numbers[-1]:020d}:{normalized}" if numbers else normalized
    if isinstance(source_timestamp, datetime):
        return (4, source_timestamp.timestamp(), lexical_order)
    if entry is not None and entry.user_id == user_id and entry.session_id == session_id:
        timestamp = entry.source_timestamp
        if isinstance(timestamp, datetime):
            round_index = float(entry.round_index) if entry.round_index is not None else -1.0
            return (3, timestamp.timestamp(), f"{round_index:020.6f}:{lexical_order}")
        if entry.round_index is not None:
            return (2, float(entry.round_index), lexical_order)
    if numbers:
        return (1, float(numbers[-1]), lexical_order)
    return (0, 0.0, normalized)


def source_cursor_from_refs(
    repository: MemoryRepository,
    user_id: str,
    raw_refs: list[dict[str, str]],
    *,
    source_timestamp: datetime | None = None,
) -> tuple[tuple[int, float, str], str, str] | None:
    """一组来源引用中最新的回合游标。

    ``source_timestamp`` 是这批引用对应的来源时间（同一轮的所有引用共享
    同一个时间）；有它时不再按 round_id 猜测新旧。
    """
    refs: list[tuple[tuple[int, float, str], str, str]] = []
    for raw_ref in raw_refs:
        if not isinstance(raw_ref, dict):
            continue
        session_id = raw_ref.get("session_id")
        round_id = raw_ref.get("round_id")
        if session_id is None or round_id is None:
            continue
        session_key = str(session_id)
        round_key = str(round_id)
        refs.append(
            (
                round_order(
                    repository,
                    user_id,
                    session_key,
                    round_key,
                    source_timestamp=source_timestamp,
                ),
                session_key,
                round_key,
            )
        )
    if not refs:
        return None
    return max(refs)


def source_cursor(
    repository: MemoryRepository, user_id: str, metadata: dict[str, Any]
) -> tuple[tuple[int, float, str], str, str] | None:
    """从记忆 metadata 的 source_refs 计算来源游标。

    metadata 里的 ``source_timestamp`` 是这批记忆的来源时间：有它就按真实
    时间定新旧，没有才退回 round_id（外部历史回放时库里查不到源轮次）。
    """
    raw_refs = metadata.get("source_refs")
    if not isinstance(raw_refs, list):
        return None
    source_timestamp = _parse_source_timestamp(metadata.get("source_timestamp"))
    return source_cursor_from_refs(repository, user_id, raw_refs, source_timestamp=source_timestamp)


def _parse_source_timestamp(value: Any) -> datetime | None:
    """解析 metadata 里的 ISO 来源时间；naive 按 UTC 处理。"""
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def source_has_any(source_text: str, markers: tuple[str, ...]) -> bool:
    """源文本是否包含任一关键字（大小写不敏感）。"""
    lowered_source = source_text.lower()
    return any(marker.lower() in lowered_source for marker in markers)


def should_extract_to_l3(
    *,
    messages: list[dict[str, Any]],
    metadata: dict[str, Any] | None,
    allowed_slots: list[str] | None = None,
) -> bool:
    """S5: L3 抽取前的廉价过滤层。

    闸门策略（任一命中即抽取）：
    - ``force_extract`` metadata 显式强制 → 始终 True
    - ``allowed_slots`` 留空 → 闸门禁用（向后兼容：所有轮都过 mem0）
    - ``allowed_slots`` 非空 + slot 命中 → True
    - ``allowed_slots`` 非空 + 长消息（≥ MIN_BYTES） → True
    - 其他 → False（只落 L1/L2）
    """
    if metadata and metadata.get(_FORCE_EXTRACT_KEY) is True:
        return True
    if not allowed_slots:
        return True  # 默认放行（保留旧行为）
    from thinkback.domain.slots.canonical import memory_conflict_slot
    from thinkback.domain.slots.query_slots import query_conflict_slot

    source_text = " ".join(
        str(message.get("content", "")) for message in messages if message.get("role") == "user"
    )
    if memory_conflict_slot(source_text) is not None:
        return True
    if query_conflict_slot(source_text) is not None:
        return True
    total_bytes = len(source_text.encode("utf-8"))
    return total_bytes >= _MIN_BYTES_FOR_EXTRACTION
