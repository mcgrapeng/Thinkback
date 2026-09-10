"""P0 槽位引擎（正则 NLP）。

L3 长期记忆的关键事实补偿索引：mem0 抽出的记忆按「槽位」归类
（昵称 / 宠物 / 地点 / 工作状态 / 沟通偏好 / 生日 / 饮食偏好 / 睡眠提醒），
用于冲突检测、来源交叉校验与召回回填。全部为纯函数，无 I/O。

模块划分：
- ``query_slots``: 查询侧槽位识别与上下文词表
- ``extractors``: 记忆/源文本侧的 8 个槽位抽取器
- ``canonical``: 槽位归类与规范化记忆文本
- ``support``: 源文本交叉校验 + 来源排序游标
"""

from innies_memory.domain.slots.canonical import (
    canonical_memory_text,
    canonical_memory_text_from_source,
    conflict_partition,
    conflict_partition_from_metadata,
    memory_conflict_slot,
    p0_canonical_memories_from_source,
    p0_canonical_memory_for_source_slot,
)
from innies_memory.domain.slots.extractors import (
    extract_birthday,
    extract_communication_preference,
    extract_current_location,
    extract_current_nickname,
    extract_current_work_status,
    extract_favorite_consumable,
    extract_pet_name,
    extract_sleep_reminder_preference,
)
from innies_memory.domain.slots.query_slots import (
    context_terms,
    memory_context_allowed,
    query_conflict_slot,
    query_is_broad_memory_request,
)
from innies_memory.domain.slots.support import (
    is_backend_managed_memory_id,
    is_local_index_memory_id,
    is_same_source_local_p0_memory,
    memory_supported_by_source,
    round_order,
    source_cursor,
    source_cursor_from_refs,
    source_has_any,
)

__all__ = [
    "canonical_memory_text",
    "canonical_memory_text_from_source",
    "conflict_partition",
    "conflict_partition_from_metadata",
    "context_terms",
    "extract_birthday",
    "extract_communication_preference",
    "extract_current_location",
    "extract_current_nickname",
    "extract_current_work_status",
    "extract_favorite_consumable",
    "extract_pet_name",
    "extract_sleep_reminder_preference",
    "is_backend_managed_memory_id",
    "is_local_index_memory_id",
    "is_same_source_local_p0_memory",
    "memory_conflict_slot",
    "memory_context_allowed",
    "memory_supported_by_source",
    "p0_canonical_memory_for_source_slot",
    "p0_canonical_memories_from_source",
    "query_conflict_slot",
    "query_is_broad_memory_request",
    "round_order",
    "source_cursor",
    "source_cursor_from_refs",
    "source_has_any",
]
