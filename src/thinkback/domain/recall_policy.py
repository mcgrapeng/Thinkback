"""召回后处理策略：去重 + token 预算裁剪 + 质量/强度排序。

层优先级 L3 > L2 > L1；同层按「内容质量 + 强度」排序，同内容跨层取高层。
强度 = f(recency_hours, recall_count) —— Ebbinghaus 式信号 (S1 见 G1)。
token 成本用 ``len(content) // 2`` 近似（中文一字约等于一个 token），
超出预算的条目跳过而不是截断，保证原子性。

强度公式（确定性，无 LLM）：
    strength = (1 + log1p(recall_count)) * recency_factor
    recency_factor = 0.5 ** (hours_since_last_recall / half_life_hours)

- recall_count=0 → strength=0.5（中性基线，未被强化也未衰减）
- 刚被召回 → recency_factor=1.0（最高强度）
- 7 天未被召回（half_life=72h）→ 约 0.5
- 14 天未被召回 → 约 0.25

非 L3（无 memory_id）→ strength 不参与排序（保持原有 layer priority 行为）。

排序键（本次变更）：
    (-层优先级, -内容质量, -强度)

- **层优先级第一位**：L3 > L2 > L1 是架构承诺，任何情况下不破。
- **内容质量第二位**（新）：同层内"模型接得住"的文本先注入。质量分来自
  ``domain/content_quality.py`` 的五个内容内在维度（具体性/自足性/可验证性/
  情感显著性/清晰度），不依赖 LLM、不依赖时钟。
- **强度第三位**：质量相同时，被强化过、更新鲜的记忆优先。

为什么质量排在强度前面：预算裁剪发生在排序之后，排序决定谁活下来。
一条"他最近不太好"强度再高，注入进去模型也接不上话；而质量分是把
"这条记忆能不能被用起来"这一层信号补进排序键（此前排序键完全不含
内容质量）。强度衡量"被强化/多新鲜"，质量衡量"能不能用"，两者不是
同一件事，质量更贴近注入目标，因此排在前面。

质量只对**内容本身**打分，不含新颖性（新颖性依赖"包里已有哪些条目"，
与排序循环依赖）；近似重复由去重环节负责。
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from thinkback.domain.content_quality import intrinsic_quality

if TYPE_CHECKING:
    from thinkback.memory.schemas import MemoryItem

LAYER_PRIORITY = {"L3": 3, "L2": 2, "L1": 1}

# ponytail: half_life=72h is conservative; tune if eval shows over-penalization of recent items.
_STRENGTH_HALF_LIFE_HOURS = 72.0


def _strength(item: MemoryItem, now: datetime | None = None) -> float:
    """S1: Ebbinghaus 式强度因子 (recency × frequency)。

    非 L3（无 memory_id）固定返回 0.5 — 不参与排序但保留字典序位置。
    L3 但从未被召回（recall_count=0）也固定返回 0.5。

    鲁棒性：``last_recalled_at`` 与 ``now`` 可能因 asyncpg 默认行为
    返回 naive datetime。两侧都强制带 UTC tzinfo 后再做差，避免
    "can't subtract naive from aware" TypeError。
    """
    if item.layer != "L3" or item.memory_id is None:
        return 0.5
    if item.recall_count <= 0:
        return 0.5
    last = item.last_recalled_at
    if last is None:
        return 0.5
    current = now if now is not None else datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    hours_since = max(0.0, (current - last).total_seconds() / 3600.0)
    recency = 0.5 ** (hours_since / _STRENGTH_HALF_LIFE_HOURS)
    frequency = 1.0 + math.log1p(float(item.recall_count))
    result: float = recency * frequency
    return result


def _quality(item: MemoryItem) -> float:
    """内容质量（0.0-1.0）：五个内容内在维度的加权分，权重已重新归一。

    L1/L2 同样适用 —— 摘要写得糊、对话原文太长，都会影响模型能不能接住。
    """

    return intrinsic_quality(item.content)


def dedupe_and_clip(
    items: list[MemoryItem], token_budget: int, *, now: datetime | None = None
) -> list[MemoryItem]:
    """按内容去重（层优先 → 强度 → 同层分高者胜）再按预算裁剪。"""

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
            if item_pri > existing_pri:
                deduped[normalized] = item
            elif item_pri == existing_pri:
                existing_strength = _strength(existing, now)
                item_strength = _strength(item, now)
                if item_strength > existing_strength or (
                    item_strength == existing_strength
                    and (item.score or 0.0) > (existing.score or 0.0)
                ):
                    deduped[normalized] = item

    clipped: list[MemoryItem] = []
    used = 0
    ordered_items = sorted(
        deduped.values(),
        key=lambda item: (
            -LAYER_PRIORITY.get(item.layer, 0),
            -_quality(item),
            -_strength(item, now),
        ),
    )
    for item in ordered_items:
        normalized = item.content.strip()
        cost = max(1, len(normalized) // 2)
        if used + cost > token_budget:
            continue
        used += cost
        clipped.append(item)
    return clipped
