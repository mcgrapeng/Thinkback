"""槽位归类与规范化记忆文本。

- ``memory_conflict_slot``: 记忆文本 → P0 槽位（冲突检测的共用口径）
- ``canonical_memory_text*``: 记忆文本 → 规范化模板文本（冲突比较前的归一）
- ``conflict_partition*``: 冲突分区的扩展点，当前返回空分区（保留扩展位）
"""

from __future__ import annotations

from typing import Any

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


def conflict_partition_from_metadata(_metadata: dict[str, Any]) -> tuple[()]:
    return ()


def conflict_partition(_memory: Any) -> tuple[()]:
    return ()


def memory_conflict_slot(memory_text: str) -> str | None:
    """记忆文本命中的 P0 槽位；与查询侧 ``query_conflict_slot`` 共用槽位词表。"""
    normalized = canonical_memory_text(memory_text).lower()
    if extract_current_nickname(memory_text):
        return "preferred_nickname"
    nickname_markers = (
        "叫我",
        "称呼",
        "preferred name",
        "prefers to be called",
        "prefers to be addressed as",
        "addressed as",
        "changed preferred name",
    )
    if any(marker in normalized for marker in nickname_markers):
        return "preferred_nickname"
    pet_kind = extract_pet_name(memory_text)
    if pet_kind:
        return f"pet_name:{pet_kind[0]}"
    if extract_current_location(memory_text):
        return "current_location"
    if extract_current_work_status(memory_text):
        return "current_work_status"
    if any(
        marker in normalized
        for marker in (
            "communication preference",
            "reassurance",
            "direct advice",
            "before advice",
            "instead of reassurance",
            "沟通偏好",
            "直接建议",
            "先安慰",
            "说教",
        )
    ):
        return "communication_preference"
    if extract_birthday(memory_text):
        return "birthday"
    favorite = extract_favorite_consumable(memory_text)
    if favorite:
        return f"favorite:{favorite[0]}"
    if extract_sleep_reminder_preference(memory_text):
        return "sleep_reminder_preference"
    return None


def p0_canonical_memories_from_source(source_text: str) -> list[str]:
    """把对话源文本解析成一组规范化 P0 记忆文本（用于源交叉校验）。"""
    canonical: list[str] = []
    nickname = extract_current_nickname(source_text)
    if nickname:
        canonical.append(f"User prefers to be called {nickname}")
    pet_name = extract_pet_name(source_text)
    if pet_name:
        pet_kind, name = pet_name
        canonical.append(f"User has a {pet_kind} named {name}")
    current_location = extract_current_location(source_text)
    if current_location:
        canonical.append(f"User lives in {current_location}")
    work_status = extract_current_work_status(source_text)
    if work_status:
        canonical.append(f"User current work status: {work_status}")
    communication_preference = extract_communication_preference(source_text)
    if communication_preference:
        canonical.append(f"User communication preference: {communication_preference}")
    birthday = extract_birthday(source_text)
    if birthday:
        canonical.append(f"User birthday: {birthday}")
    favorite = extract_favorite_consumable(source_text)
    if favorite:
        kind, value = favorite
        canonical.append(f"User favorite {kind}: {value}")
    sleep_reminder_preference = extract_sleep_reminder_preference(source_text)
    if sleep_reminder_preference:
        canonical.append(f"User sleep reminder preference: {sleep_reminder_preference}")
    return canonical


def canonical_memory_text(memory_text: str) -> str:
    """把 P0 槽位记忆归一成模板文本；非 P0 记忆原样返回。"""
    canonical_nickname = extract_current_nickname(memory_text)
    if canonical_nickname:
        return f"User prefers to be called {canonical_nickname}"
    pet_name = extract_pet_name(memory_text)
    if pet_name:
        pet_kind, name = pet_name
        return f"User has a {pet_kind} named {name}"
    current_location = extract_current_location(memory_text)
    if current_location:
        return f"User lives in {current_location}"
    work_status = extract_current_work_status(memory_text)
    if work_status:
        return f"User current work status: {work_status}"
    communication_preference = extract_communication_preference(memory_text)
    if communication_preference:
        return f"User communication preference: {communication_preference}"
    birthday = extract_birthday(memory_text)
    if birthday:
        return f"User birthday: {birthday}"
    favorite = extract_favorite_consumable(memory_text)
    if favorite:
        kind, value = favorite
        return f"User favorite {kind}: {value}"
    sleep_reminder_preference = extract_sleep_reminder_preference(memory_text)
    if sleep_reminder_preference:
        return f"User sleep reminder preference: {sleep_reminder_preference}"
    return memory_text


def canonical_memory_text_from_source(memory_text: str, l3_metadata: dict[str, Any]) -> str:
    """优先采用源文本侧的规范化文本，避免 mem0 抽取噪声进入比较。"""
    source_text = str(l3_metadata.get("source_text") or "")
    source_canonical = p0_canonical_memory_for_source_slot(memory_text, source_text)
    if source_canonical is not None:
        return source_canonical
    source_pet_name = extract_pet_name(source_text)
    memory_pet_name = extract_pet_name(memory_text)
    if source_pet_name and memory_pet_name and source_pet_name[0] == memory_pet_name[0]:
        pet_kind, name = source_pet_name
        return f"User has a {pet_kind} named {name}"
    return canonical_memory_text(memory_text)


def p0_canonical_memory_for_source_slot(memory_text: str, source_text: str) -> str | None:
    """若源文本能产出与记忆同槽位的规范化文本，返回该文本。"""
    if not source_text:
        return None
    memory_slot = memory_conflict_slot(memory_text)
    if memory_slot is None:
        return None
    for source_memory_text in p0_canonical_memories_from_source(source_text):
        if memory_conflict_slot(source_memory_text) == memory_slot:
            return source_memory_text
    return None
