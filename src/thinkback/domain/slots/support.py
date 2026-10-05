"""源文本交叉校验与来源排序游标。

- ``memory_supported_by_source``: mem0 抽取结果的准入校验
  （P0 白名单 gate + 源文本关键字交叉验证，防止凭空捏造进入本地索引）
- ``is_same_source_local_p0_memory`` / ``is_local_index_memory_id``:
  本地索引记忆的识别
- ``round_order`` / ``source_cursor*``: 来源回合的排序游标
  （"哪个来源更新"仲裁，需要仓储读取回合）

依赖说明：``round_order`` 等函数把 ``repository`` 作为参数传入，
保持本模块对基础设施零依赖。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING, Any

from thinkback.domain.keys import source_ref_key
from thinkback.domain.slots.canonical import (
    memory_conflict_slot,
    p0_canonical_memories_from_source,
)
from thinkback.domain.slots.extractors import (
    extract_current_nickname,
    extract_pet_name,
)

if TYPE_CHECKING:
    from thinkback.domain.ports import MemoryRepository

LOCAL_INDEX_ID_PREFIXES = (
    "local-p0:",
    "deleted-source:",
    "delete-all:",
    "delete-session:",
)


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
    repository: MemoryRepository, user_id: str, session_id: str, round_id: str
) -> tuple[int, float, str]:
    """回合的全序排序键：库内时间戳 > round_index > round_id 数字后缀。"""
    entry = repository.get_round(round_id)
    normalized = round_id.lower()
    numbers = [int(part) for part in re.findall(r"\d+", normalized)]
    lexical_order = f"{numbers[-1]:020d}:{normalized}" if numbers else normalized
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
    repository: MemoryRepository, user_id: str, raw_refs: list[dict[str, str]]
) -> tuple[tuple[int, float, str], str, str] | None:
    """一组来源引用中最新的回合游标。"""
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
            (round_order(repository, user_id, session_key, round_key), session_key, round_key)
        )
    if not refs:
        return None
    return max(refs)


def source_cursor(
    repository: MemoryRepository, user_id: str, metadata: dict[str, Any]
) -> tuple[tuple[int, float, str], str, str] | None:
    """从记忆 metadata 的 source_refs 计算来源游标。"""
    raw_refs = metadata.get("source_refs")
    if not isinstance(raw_refs, list):
        return None
    return source_cursor_from_refs(repository, user_id, raw_refs)


def source_has_any(source_text: str, markers: tuple[str, ...]) -> bool:
    """源文本是否包含任一关键字（大小写不敏感）。"""
    lowered_source = source_text.lower()
    return any(marker.lower() in lowered_source for marker in markers)


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
        str(message.get("content", ""))
        for message in messages
        if message.get("role") == "user"
    )
    if memory_conflict_slot(source_text) is not None:
        return True
    if query_conflict_slot(source_text) is not None:
        return True
    total_bytes = len(source_text.encode("utf-8"))
    return total_bytes >= _MIN_BYTES_FOR_EXTRACTION


def memory_supported_by_source(
    memory_text: str,
    l3_metadata: dict[str, Any],
    *,
    allowed_slots: list[str] | None = None,
) -> bool:
    """决定 mem0 抽出的 L3 事件是否进本地 MemoryRecord 表（即 /items 是否可见）。

    双重判断：

    1. **P0 白名单 gate**（``allowed_slots``，来自 MEMORY_P0_SLOTS 配置）：
       - 留空 = 全部放行（默认，/items 看到所有 L3 记忆）。
       - 非空 = 只放行 slot 在该列表中的记忆，其它仍写 mem0 但不入本地表。
    2. **源文本交叉校验**（白名单命中后）：
       对 pet_name / nickname / birthday / location / work_status / 等 P0 slot
       做源文本关键字匹配，确保 mem0 抽取不是凭空捏造。

    配置项变更语义：MEMORY_P0_SLOTS 从「必填 8 个 slot」变成「可选白名单」，
    这样 ``/items`` 不会因为一个非 P0 事实（如"用户打算明年去日本读硕士"）
    就被静默丢弃。Slot 检测本身仍走 ``memory_conflict_slot``，与冲突解决
    逻辑共用，行为是一致的。
    """

    source_text = str(l3_metadata.get("source_text") or "")
    if not source_text:
        return True
    # P0 白名单 gate：先算 slot，再判断是否在白名单里
    slot = memory_conflict_slot(memory_text)
    if allowed_slots and slot not in allowed_slots:
        return False
    pet_name = extract_pet_name(memory_text)
    if pet_name:
        pet_kind, name = pet_name
        # H-1 修复：用单词/字符边界 + 上下文模式替代裸 substring 匹配，
        # 避免 "cat" 命中 "catastrophe"、"猫" 命中 "招财猫" 等误判。
        ascii_markers = {
            "cat": ("cat",),
            "dog": ("dog",),
            "bird": ("bird", "parrot"),
            "rabbit": ("rabbit", "bunny"),
        }.get(pet_kind, (pet_kind,))
        cjk_markers = {
            "cat": ("猫",),
            "dog": ("狗",),
            "bird": ("鸟", "鹦鹉"),
            "rabbit": ("兔", "兔子"),
        }.get(pet_kind, ())
        lowered_source = source_text.lower()
        source_pet_name = extract_pet_name(source_text)
        if source_pet_name:
            return source_pet_name[0] == pet_kind
        # 英文 marker：必须整词匹配（\b 边界），杜绝 "cat" 命中 "catastrophe"
        ascii_match = any(
            re.search(rf"\b{re.escape(marker)}\b", lowered_source) for marker in ascii_markers
        )
        # 中文 marker：要求前面是"我/养/有/只/是/了/的/叫/名字"等持有关系词，
        # 杜绝 "猫" 命中 "招财猫"、"狗" 命中 "狗粮广告" 等纯名词误命中。
        cjk_match = any(
            re.search(
                r"(?:我|你|他|她|它|我们|我家|养了?|有|是|只|名叫?|叫|名字叫|的)".replace(
                    "?", ""
                )  # 中文 marker 不允许可选字符
                + re.escape(marker),
                source_text,
            )
            or re.search(
                r"叫\s*[一-鿿A-Za-z0-9_-]{0,12}\s*[，。,.\s]*" + re.escape(marker),
                source_text,
            )
            or re.search(
                re.escape(marker) + r"(?:叫|名叫|名字叫|的?名字)",
                source_text,
            )
            for marker in cjk_markers
        )
        return (ascii_match or cjk_match) and name in source_text
    nickname = extract_current_nickname(memory_text)
    if nickname:
        lowered_source = source_text.lower()
        nickname_markers = (
            "叫我",
            "称呼我",
            "叫用户",
            "称呼",
            "preferred name",
            "name should be",
            "name is",
            "called",
            "addressed as",
        )
        return nickname in source_text and any(
            marker in lowered_source for marker in nickname_markers
        )
    if slot is None:
        # 没识别出 P0 slot；gate 留空时（默认）放行，非空时已在上面 return False。
        return True
    if slot == "birthday":
        return source_has_any(source_text, ("生日", "birthday"))
    source_slots = {
        memory_conflict_slot(source_memory_text)
        for source_memory_text in p0_canonical_memories_from_source(source_text)
    }
    source_slots.discard(None)
    if source_slots and slot not in source_slots:
        return False
    if slot == "current_location":
        return source_has_any(
            source_text,
            ("住", "搬", "城市", "location", "live", "lived", "moving", "moved", "relocated"),
        )
    if slot == "current_work_status":
        return source_has_any(source_text, ("工作", "offer", "job", "moonshot", "换工作"))
    if slot == "communication_preference":
        lowered_memory = memory_text.lower()
        if source_has_any(
            memory_text,
            ("anxiety", "anxious", "break down", "breaking down", "焦虑", "拆解"),
        ):
            return source_has_any(
                source_text,
                ("焦虑", "拆解", "anxiety", "anxious", "break down", "breaking down"),
            )
        if source_has_any(
            memory_text,
            ("concise", "direct", "简洁", "直接"),
        ):
            return source_has_any(
                source_text,
                ("简洁", "直接", "concise", "direct"),
            )
        if "reassurance" in lowered_memory or "comfort" in lowered_memory or "安慰" in memory_text:
            return source_has_any(
                source_text,
                ("安慰", "reassurance", "comfort"),
            )
        return source_has_any(
            source_text,
            (
                "沟通",
                "建议",
                "安慰",
                "说教",
                "焦虑",
                "communication",
                "advice",
                "suggestions",
                "reassurance",
                "anxious",
            ),
        )
    if slot == "favorite:drink":
        return source_has_any(
            source_text,
            ("饮品", "饮料", "喝", "咖啡", "茶", "drink", "beverage", "coffee", "tea"),
        )
    if slot == "favorite:food":
        return source_has_any(source_text, ("食物", "吃", "food"))
    if slot == "sleep_reminder_preference":
        return source_has_any(source_text, ("睡", "提醒", "sleep", "reminder"))
    return True
