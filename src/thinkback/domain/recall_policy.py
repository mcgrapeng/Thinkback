"""召回后处理策略：去重 + token 预算裁剪。

层优先级 L3 > L2 > L1；同层按分数取高；同内容跨层取高层。
token 成本用 ``len(content) // 2`` 近似（中文一字约等于一个 token），
超出预算的条目跳过而不是截断，保证原子性。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from thinkback.memory.schemas import MemoryItem

LAYER_PRIORITY = {"L3": 3, "L2": 2, "L1": 1}


def dedupe_and_clip(items: list[MemoryItem], token_budget: int) -> list[MemoryItem]:
    """按内容去重（层优先、同层分高者胜）再按预算裁剪。"""

    deduped: dict[str, MemoryItem] = {}
    for item in items:
        normalized = item.content.strip()
        if not normalized:
            continue
        existing = deduped.get(normalized)
        if existing is None:
            deduped[normalized] = item
        else:
            item_pri = LAYER_PRIORITY.get(item.layer, 0)
            existing_pri = LAYER_PRIORITY.get(existing.layer, 0)
            if item_pri > existing_pri or (
                item_pri == existing_pri and (item.score or 0.0) > (existing.score or 0.0)
            ):
                deduped[normalized] = item

    clipped: list[MemoryItem] = []
    used = 0
    ordered_items = sorted(
        enumerate(deduped.values()),
        key=lambda indexed_item: (
            -LAYER_PRIORITY.get(indexed_item[1].layer, 0),
            indexed_item[0],
        ),
    )
    for _, item in ordered_items:
        normalized = item.content.strip()
        cost = max(1, len(normalized) // 2)
        if used + cost > token_budget:
            continue
        used += cost
        clipped.append(item)
    return clipped
