"""源文本交叉校验（防捏造闸门）。

- ``memory_supported_by_source``: mem0 抽取结果的准入校验
  （P0 白名单 gate + 源文本关键字交叉验证，防止凭空捏造进入本地索引）

**判定逻辑**（来源排序游标、同源判定、抽取闸门）已经拆到
``domain/slots/source_order.py``：那一批是「纯函数 + 判定分支」，进变异审计
范围；这里的依据词表是「措辞表」，逐条钉死是覆盖率报表而不是缺陷信号，
由契约用例覆盖。

下面这批名字仍然从这里导出（调用方不用改 import）::

    is_local_index_memory_id
    is_backend_managed_memory_id
    is_same_source_local_p0_memory
    round_order
    source_cursor
    source_cursor_from_refs
    source_has_any
    should_extract_to_l3
"""

from __future__ import annotations

import re
from typing import Any

from thinkback.domain.slots.canonical import (
    memory_conflict_slot,
    p0_canonical_memories_from_source,
)
from thinkback.domain.slots.extractors import (
    extract_current_nickname,
    extract_pet_name,
)

# 判定逻辑已拆到 source_order.py；这里原样再导出，调用方的 import 不用改。
# `X as X` 是「显式再导出」的写法（ruff 认这个，不报未使用）。
from thinkback.domain.slots.source_order import (  # noqa: F401
    _parse_source_timestamp as _parse_source_timestamp,
)
from thinkback.domain.slots.source_order import (
    is_backend_managed_memory_id as is_backend_managed_memory_id,
)
from thinkback.domain.slots.source_order import (
    is_local_index_memory_id as is_local_index_memory_id,
)
from thinkback.domain.slots.source_order import (
    is_same_source_local_p0_memory as is_same_source_local_p0_memory,
)
from thinkback.domain.slots.source_order import (
    round_order as round_order,
)
from thinkback.domain.slots.source_order import (
    should_extract_to_l3 as should_extract_to_l3,
)
from thinkback.domain.slots.source_order import (
    source_cursor as source_cursor,
)
from thinkback.domain.slots.source_order import (
    source_cursor_from_refs as source_cursor_from_refs,
)
from thinkback.domain.slots.source_order import (
    source_has_any as source_has_any,
)


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
