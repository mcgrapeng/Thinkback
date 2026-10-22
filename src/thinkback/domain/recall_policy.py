"""召回后处理策略：去重 + token 预算裁剪 + 质量/强度排序。

层优先级 L3 > L2 > L1；同层按「内容质量 + 强度」排序，同内容跨层取高层。
强度 = f(recency_hours, recall_count) —— Ebbinghaus 式信号 (S1 见 G1)。
token 成本用 :func:`estimate_tokens` 按字符集分档估算（中文一字约一个 token，
ASCII 约四字符一个 token），超出预算的条目**停止裁剪**（而不是跳过继续试），
保证原子性。

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
from thinkback.domain.dedup import content_dedup_key, slot_dedup_key

if TYPE_CHECKING:
    from thinkback.memory.schemas import MemoryItem

LAYER_PRIORITY = {"L3": 3, "L2": 2, "L1": 1}

# ponytail: half_life=72h is conservative; tune if eval shows over-penalization of recent items.
_STRENGTH_HALF_LIFE_HOURS = 72.0

# ── token 估算 ──────────────────────────────────────────────────────────────
#
# 口径（经验值，cl100k / 常见中文模型都落在同一量级）：
# - CJK 表意文字、日韩文字：1 字 ≈ 1 token（汉字在 BPE 里通常切 1-2 个 token）
# - ASCII：≈ 4 字符 1 token（英文单词平均 4-5 字符）
# - 其余非 ASCII：≈ 2 字符 1 token（西里尔/希腊文等介于两者之间，取保守值）
#
# 旧口径 ``len(content) // 2`` 对中文低估约 2 倍、对英文高估约 2.4 倍：
# 中文记忆在预算里显得比实际便宜（超预算注入），英文记忆显得比实际贵
# （该注入的没注入）。两种语言都失准的预算裁剪等于没有裁剪。
_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x3040, 0x30FF),  # 平假名 / 片假名
    (0x3400, 0x4DBF),  # CJK 扩展 A
    (0x4E00, 0x9FFF),  # CJK 基本区
    (0xAC00, 0xD7AF),  # 谚文音节
    (0xF900, 0xFAFF),  # CJK 兼容表意文字
    (0x20000, 0x2A6DF),  # CJK 扩展 B
)

_ASCII_CHARS_PER_TOKEN = 4
_OTHER_CHARS_PER_TOKEN = 2


def _is_cjk(code_point: int) -> bool:
    return any(low <= code_point <= high for low, high in _CJK_RANGES)


def estimate_tokens(text: str) -> int:
    """按字符集分档估算 token 数（至少 1）。

    >>> estimate_tokens("用户喜欢喝美式咖啡")
    9
    >>> estimate_tokens("user likes americano")
    5
    """

    cjk = 0
    ascii_chars = 0
    other = 0
    for char in text:
        code_point = ord(char)
        if _is_cjk(code_point):
            cjk += 1
        elif code_point < 128:
            ascii_chars += 1
        else:
            other += 1
    total = (
        cjk
        + math.ceil(ascii_chars / _ASCII_CHARS_PER_TOKEN)
        + math.ceil(other / _OTHER_CHARS_PER_TOKEN)
    )
    return max(1, total)


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
    """按内容去重（层优先 → 强度 → 同层分高者胜）再按预算裁剪。

    两级去重：

    1. **正文去重**（跨通道）：键是归一化后的正文（``domain.dedup``），
       L1/L2/L3 三条通道里语义等价的措辞只保留一条 —— 层优先级高的胜出，
       同层比强度、再比 score。旧口径按「字符串完全相等」去重，
       只差一个标点或大小写的同一条事实会被注入两三次。
    2. **槽位冲突消解**（仅 L3）：同一个 P0 槽位（生日 / 现居城市 / 宠物名……）
       在一个上下文包里只能有一条事实。写入期已经用 supersede 保证过，
       但异步竞争 / 非本副本写入会留下同槽位的多条 ACTIVE 记忆；
       召回期再兜一次底：保留 ``valid_at`` 最新的一条（事实生效时间），
       时间相同再比 score、强度。
    """

    deduped: dict[str, MemoryItem] = {}
    for item in items:
        key = content_dedup_key(item.content)
        if not key:
            continue
        existing = deduped.get(key)
        if existing is None:
            deduped[key] = item
        else:
            item_pri = LAYER_PRIORITY.get(item.layer, 0)
            existing_pri = LAYER_PRIORITY.get(existing.layer, 0)
            if item_pri > existing_pri:
                deduped[key] = item
            elif item_pri == existing_pri:
                existing_strength = _strength(existing, now)
                item_strength = _strength(item, now)
                if item_strength > existing_strength or (
                    item_strength == existing_strength
                    and (item.score or 0.0) > (existing.score or 0.0)
                ):
                    deduped[key] = item

    deduped = _resolve_slot_conflicts(deduped, now)

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
        cost = estimate_tokens(normalized)
        if used + cost > token_budget:
            # 停止裁剪而不是跳过这一条继续试后面的：排序键已经定好优先级，
            # 预算裁剪应当取「排名前缀」—— 跳过后面的便宜条目会得到一个
            # 「高质量的被砍掉、低质量的挤进来」的集合，注入的上下文与
            # 排序结论自相矛盾。宁可少注入，也不要注入错的。
            break
        used += cost
        clipped.append(item)
    return clipped


def _resolve_slot_conflicts(
    deduped: dict[str, MemoryItem], now: datetime | None = None
) -> dict[str, MemoryItem]:
    """同槽位的 L3 记忆只保留一条（事实生效时间最新的胜出）。

    写入期用 supersede 保证「同一 P0 槽位只有一条 ACTIVE 记忆」，但并发写入、
    非本副本写入、以及重建路径都可能留下同槽位的多条 ACTIVE 记忆。
    两条同槽位记忆同时进上下文包，模型会同时看到旧值与新值 —— 这是比
    「重复」更严重的错误（旧值会污染回答）。

    仲裁顺序：``valid_at``（事实生效时间）→ ``score``（语义相关度）→ 强度。
    ``valid_at`` 缺失（None）视为最旧。
    """

    winner_by_slot: dict[str, str] = {}
    losers: set[str] = set()
    for key, item in deduped.items():
        if item.layer != "L3":
            continue
        slot = slot_dedup_key(item.content)
        if slot is None:
            continue
        winner_key = winner_by_slot.get(slot)
        if winner_key is None:
            winner_by_slot[slot] = key
            continue
        winner = deduped[winner_key]
        if _prefer_item(item, winner, now):
            losers.add(winner_key)
            winner_by_slot[slot] = key
        else:
            losers.add(key)
    if not losers:
        return deduped
    return {key: item for key, item in deduped.items() if key not in losers}


def _prefer_item(candidate: MemoryItem, current: MemoryItem, now: datetime | None = None) -> bool:
    """候选是否应当取代当前保留项（valid_at → score → 强度）。"""

    candidate_valid_at = _aware(candidate.valid_at)
    current_valid_at = _aware(current.valid_at)
    if candidate_valid_at != current_valid_at:
        if candidate_valid_at is None:
            return False
        if current_valid_at is None:
            return True
        return candidate_valid_at > current_valid_at
    candidate_score = candidate.score or 0.0
    current_score = current.score or 0.0
    if candidate_score != current_score:
        return candidate_score > current_score
    return _strength(candidate, now) > _strength(current, now)


def _aware(value: datetime | None) -> datetime | None:
    """把 naive datetime 归一成 UTC aware；None 透传。"""

    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value
