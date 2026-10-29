"""召回后处理策略（去重 + 预算裁剪 + 排序）的契约测试。

``domain/recall_policy.py`` 决定**哪些记忆进上下文包**。它的每一个阈值、
每一个比较方向、每一个配额口径错了，症状都是「模型看到的上下文不对」——
不报错、不崩，只是答得不好。所以这一层把口径钉死，而不是只测「跑得通」。

盯住的口径：

1. **token 估算的边界**：ASCII / CJK 区间的端点算哪一边。差一个字，预算
   分配就偏一条，注入的内容就少一条。
2. **强度（S1）**：非 L3 与未召回的条目固定 0.5；召回次数与新鲜度的组合；
   ``now`` 真的被用上（不是当前时间）。
3. **层间配额**：L1 保底、L3 为主；没用完的额度回流；每层至少留一条。
4. **类型级上限**：某一类记忆不能吃掉整份 L3 额度，且没吃满的额度留给别的类型。
5. **去重与槽位仲裁**：层优先 → 强度 → 分数；同槽位取最新事实。
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from thinkback.domain.recall_policy import (
    _allocate_by_layer,
    _aware,
    _clip_by_layer,
    _memory_type_of,
    _order_items,
    _prefer_item,
    _resolve_slot_conflicts,
    _strength,
    dedupe_and_clip,
    estimate_tokens,
)
from thinkback.memory.schemas import MemoryItem

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
# 远离真实时钟：用来验证「now 真的被用上了」—— 若实现偷偷改用当前时间，
# 这里的差值是好几十年，任何断言都会立刻炸。
ANCIENT = datetime(2020, 1, 1, tzinfo=UTC)


def _item(
    layer: str = "L3",
    content: str = "内容",
    *,
    memory_id: str | None = None,
    score: float | None = None,
    metadata: dict[str, Any] | None = None,
    recall_count: int = 0,
    last_recalled_at: datetime | None = None,
    valid_at: datetime | None = None,
) -> MemoryItem:
    return MemoryItem(
        layer=layer,
        content=content,
        memory_id=memory_id,
        score=score,
        metadata=metadata if metadata is not None else {},
        recall_count=recall_count,
        last_recalled_at=last_recalled_at,
        valid_at=valid_at,
    )


def _cjk(count: int, tag: str = "汉") -> str:
    """count 个 token 的汉字串。

    tag 让每条内容互不相同 —— 否则内容相同的条目会先被正文去重合并掉，
    测的就不是层配额而是去重了。
    """
    return tag + "汉" * (count - 1)


# ── 层间配额分配 ───────────────────────────────────────────────────────────


def test_a_layer_outside_the_share_table_is_still_allocated_from_the_leftovers() -> None:
    """不在份额表里的层（如未知层）默认分配 0，额度从回流里拿。

    默认值写成 None 会在后续的算术里炸掉；写成 1 会让这一层白拿一个 token，
    挤掉别人。预算不够分满时，两者的差别会体现在最终额度上。
    """
    assert _allocate_by_layer({"L1": 100, "L9": 50}, 60) == {"L1": 50, "L9": 8}


def test_the_unused_budget_flows_back_to_the_layers_that_need_it() -> None:
    """没用完的额度按剩余需求重新分配（不够分的按比例分）。

    L1/L3 各要 100、预算只有 100：先按份额给（30/50），剩下的 20 再按缺口
    比例回流 —— 多轮分配，直到额度用完或没有缺口。
    """
    allocation = _allocate_by_layer({"L1": 100, "L3": 100}, 100)
    assert allocation == {"L1": 45, "L3": 54}
    assert sum(allocation.values()) <= 100


def test_the_allocation_never_exceeds_what_a_layer_asked_for() -> None:
    """分配的额度不能超过该层的实际需求 —— 多了就是浪费。"""
    assert _allocate_by_layer({"L1": 10, "L3": 20}, 100) == {"L1": 10, "L3": 20}


def test_a_single_layer_allocation_gives_the_whole_budget() -> None:
    """只有一层时，别的层没占的额度全额给它。"""
    assert _allocate_by_layer({"L3": 100}, 100) == {"L3": 100}


def test_a_single_layer_with_a_small_need_gets_what_it_needs() -> None:
    assert _allocate_by_layer({"L3": 30}, 100) == {"L3": 30}


# ── token 估算的边界 ───────────────────────────────────────────────────────


def test_ascii_stops_at_127() -> None:
    """chr(127) 是 ASCII，chr(128) 不是。

    边界写错会让英文内容的价格差 2 倍（4 字/token vs 2 字/token）。
    """
    assert estimate_tokens("a" * 5) == 2
    assert estimate_tokens("\x80" * 5) == 3


def test_the_cjk_lower_boundary_is_inclusive() -> None:
    """U+4E00 是 CJK 基本区的第一个字，算 CJK（1 字 1 token）。"""
    assert estimate_tokens("\u4e00" * 5) == 5


def test_the_cjk_upper_boundary_is_inclusive() -> None:
    """U+9FFF 是 CJK 基本区的最后一个字，算 CJK。"""
    assert estimate_tokens("\u9fff" * 5) == 5


def test_the_code_point_just_outside_the_cjk_range_is_not_cjk() -> None:
    """区间外的码点按「其他非 ASCII」计（2 字 1 token）。

    U+4DFF 紧贴 CJK 基本区下界之前，U+A000 紧贴其上界之后。
    """
    assert estimate_tokens("\u4dff" * 5) == 3
    assert estimate_tokens("\ua000" * 5) == 3


def test_an_empty_text_still_costs_one_token() -> None:
    assert estimate_tokens("") == 1


# ── 强度（S1） ─────────────────────────────────────────────────────────────


def test_a_non_l3_item_gets_the_baseline_even_with_a_memory_id() -> None:
    """非 L3 固定 0.5 —— 哪怕它带着 memory_id 和召回次数。"""
    item = _item("L1", memory_id="m1", recall_count=5, last_recalled_at=NOW - timedelta(hours=1))
    assert _strength(item, now=NOW) == 0.5


def test_an_l3_item_with_no_recalls_gets_the_baseline_even_with_a_timestamp() -> None:
    """recall_count=0 → 0.5（未被强化也未衰减），哪怕有 last_recalled_at。"""
    item = _item("L3", memory_id="m1", recall_count=0, last_recalled_at=NOW)
    assert _strength(item, now=NOW) == 0.5


def test_an_l3_item_with_one_recall_is_already_strengthened() -> None:
    """recall_count=1 → 已经算被强化过，走频率因子，不是 0.5 基线。"""
    item = _item("L3", memory_id="m1", recall_count=1, last_recalled_at=NOW)
    assert _strength(item, now=NOW) == pytest.approx(1.0 * (1 + math.log1p(1)))


def test_strength_uses_the_given_now() -> None:
    """传了 now 就用它 —— 用当前时间算出来的强度是飘的。"""
    item = _item("L3", memory_id="m1", recall_count=3, last_recalled_at=NOW - timedelta(hours=72))
    assert _strength(item, now=NOW) == pytest.approx(0.5 * (1 + math.log1p(3)))


def test_strength_without_an_explicit_now_does_not_crash() -> None:
    """不传 now 时用 UTC 当前时间 —— 与 aware 的 last_recalled_at 相减不炸。"""
    item = _item("L3", memory_id="m1", recall_count=3, last_recalled_at=datetime.now(UTC))
    assert _strength(item) > 0


def test_a_fresh_recall_has_no_decay() -> None:
    """刚刚召回 → 新鲜度因子恰好 1.0（不衰减）。"""
    item = _item("L3", memory_id="m1", recall_count=3, last_recalled_at=NOW)
    assert _strength(item, now=NOW) == pytest.approx(1.0 * (1 + math.log1p(3)))


def test_the_half_life_is_72_hours() -> None:
    """72 小时未召回 → 新鲜度恰好减半。"""
    item = _item("L3", memory_id="m1", recall_count=1, last_recalled_at=NOW - timedelta(hours=72))
    assert _strength(item, now=NOW) == pytest.approx(0.5 * (1 + math.log1p(1)))


def test_a_future_recall_is_clamped_to_no_decay() -> None:
    """时钟往前飘了（last_recalled_at 在 now 之后）→ 负数小时被夹到 0。"""
    item = _item("L3", memory_id="m1", recall_count=3, last_recalled_at=NOW + timedelta(hours=5))
    assert _strength(item, now=NOW) == pytest.approx(1.0 * (1 + math.log1p(3)))


def test_the_recall_count_enters_the_strength() -> None:
    """召回次数越多越强 —— 频率因子是 1 + ln(1+count)。"""
    low = _item("L3", memory_id="m1", recall_count=1, last_recalled_at=NOW - timedelta(hours=1))
    high = _item("L3", memory_id="m2", recall_count=10, last_recalled_at=NOW - timedelta(hours=1))
    assert _strength(high, now=NOW) > _strength(low, now=NOW)


def test_a_naive_last_recalled_at_is_read_as_utc() -> None:
    """asyncpg 可能把 tzinfo 丢掉 —— 两侧归一化 UTC 再相减。"""
    naive = (NOW - timedelta(hours=1)).replace(tzinfo=None)
    item = _item("L3", memory_id="m1", recall_count=3, last_recalled_at=naive)
    assert _strength(item, now=NOW) == pytest.approx(0.5 ** (1 / 72) * (1 + math.log1p(3)))


# ── _aware ─────────────────────────────────────────────────────────────────


def test_aware_passes_none_through() -> None:
    assert _aware(None) is None


def test_aware_attaches_utc_to_a_naive_datetime() -> None:
    naive = datetime(2026, 10, 28, 12, 0)
    assert _aware(naive) == NOW


def test_aware_keeps_an_aware_datetime_as_is() -> None:
    assert _aware(NOW) is NOW


# ── 记忆类型 ───────────────────────────────────────────────────────────────


def test_the_memory_type_comes_from_the_metadata() -> None:
    item = _item("L3", memory_id="m1", metadata={"memory_type": "profile"})
    assert _memory_type_of(item) == "profile"


def test_an_item_without_a_memory_type_has_none() -> None:
    assert _memory_type_of(_item("L3", memory_id="m1", metadata={})) is None


def test_an_empty_memory_type_is_none() -> None:
    """空串等于「没声明」—— 不该被当成一个类型名。"""
    item = _item("L3", memory_id="m1", metadata={"memory_type": ""})
    assert _memory_type_of(item) is None


def test_a_non_string_memory_type_is_none() -> None:
    item = _item("L3", memory_id="m1", metadata={"memory_type": 123})
    assert _memory_type_of(item) is None


def test_a_non_dict_metadata_has_no_memory_type() -> None:
    class _Weird:
        metadata = "not-a-dict"

    assert _memory_type_of(cast(MemoryItem, _Weird())) is None


def test_an_item_without_a_metadata_attribute_has_no_memory_type() -> None:
    class _Bare:
        pass

    assert _memory_type_of(cast(MemoryItem, _Bare())) is None


# ── 排序 ───────────────────────────────────────────────────────────────────


def test_l3_items_are_ordered_by_score_descending() -> None:
    """L3 的顺序由引擎相关度主导 —— Thinkback 不自己重排事实。"""
    high = _item("L3", "同一个内容", memory_id="m1", score=0.9)
    low = _item("L3", "同一个内容", memory_id="m2", score=0.2)
    assert _order_items([low, high])[0] is high


def test_an_item_without_a_score_sorts_last() -> None:
    """score 缺失按 0 算 —— 不是按 1 算（那会让无分记忆排到前面）。"""
    without = _item("L3", "同一个内容", memory_id="m1", score=None)
    scored = _item("L3", "同一个内容", memory_id="m2", score=0.5)
    assert _order_items([without, scored])[0] is scored


def test_the_ordering_uses_the_given_now_for_the_strength_tie_break() -> None:
    """同分同时效时用强度仲裁 —— 强度按传入的 now 算，不是当前时间。

    ``fresh`` 按给定的 now 算还有强度，``baseline`` 永远是 0.5 基线。
    若实现偷偷用当前时间，fresh 的强度会衰减到 0，顺序就反了。
    """
    fresh = _item(
        "L3",
        "同一个内容",
        memory_id="m1",
        score=0.5,
        recall_count=5,
        last_recalled_at=ANCIENT - timedelta(hours=1),
    )
    baseline = _item("L3", "同一个内容", memory_id="m2", score=0.5, recall_count=0)
    assert _order_items([baseline, fresh], ANCIENT)[0] is fresh


def test_l3_items_appear_exactly_once() -> None:
    """L3 只进 l3 那条路 —— 不能在层分组里再出现一遍（会重复注入）。"""
    item = _item("L3", "内容", memory_id="m1", score=0.5)
    ordered = _order_items([item])
    assert ordered == [item]
    assert len(ordered) == 1


def test_non_l3_layers_keep_their_input_order() -> None:
    """摘要与对话按传入顺序输出，不按内容质量重排。"""
    first = _item("L2", "摘要甲")
    second = _item("L2", "摘要乙")
    ordered = _order_items([first, second])
    assert ordered == [first, second]


def test_the_output_order_is_l3_then_l2_then_l1() -> None:
    l1 = _item("L1", "对话")
    l2 = _item("L2", "摘要")
    l3 = _item("L3", "事实", memory_id="m1", score=0.5)
    assert _order_items([l1, l2, l3]) == [l3, l2, l1]


# ── 槽位仲裁 ───────────────────────────────────────────────────────────────


def test_the_newest_fact_wins_a_slot_conflict() -> None:
    """同槽位取 valid_at 最新的 —— 旧值会污染回答。"""
    old = _item(
        "L3", "我的生日是 1990 年 1 月 1 日", memory_id="m1", valid_at=NOW - timedelta(days=1)
    )
    new = _item("L3", "我的生日是 1991 年 2 月 2 日", memory_id="m2", valid_at=NOW)
    winner = _prefer_item(new, old, NOW)
    assert winner is True
    assert _prefer_item(old, new, NOW) is False


def test_a_fact_without_a_valid_at_loses_to_one_with_it() -> None:
    """valid_at 缺失视为最旧。"""
    dated = _item("L3", "我的生日是 1990 年 1 月 1 日", memory_id="m1", valid_at=NOW)
    undated = _item("L3", "我的生日是 1990 年 1 月 1 日", memory_id="m2", valid_at=None)
    assert _prefer_item(dated, undated, NOW) is True
    assert _prefer_item(undated, dated, NOW) is False


def test_an_undated_fact_wins_over_an_older_dated_one() -> None:
    """候选没有 valid_at、当前有 —— 当前更旧，候选不该赢。"""
    dated = _item(
        "L3", "我的生日是 1990 年 1 月 1 日", memory_id="m1", valid_at=NOW - timedelta(days=1)
    )
    undated = _item("L3", "我的生日是 1990 年 1 月 1 日", memory_id="m2", valid_at=None)
    assert _prefer_item(undated, dated, NOW) is False


def test_the_score_breaks_a_tie_on_valid_at() -> None:
    """valid_at 相同 → 比分数。"""
    high = _item("L3", "我的生日是 1990 年 1 月 1 日", memory_id="m1", score=0.9, valid_at=NOW)
    low = _item("L3", "我的生日是 1990 年 1 月 1 日", memory_id="m2", score=0.2, valid_at=NOW)
    assert _prefer_item(high, low, NOW) is True
    assert _prefer_item(low, high, NOW) is False


def test_a_missing_score_counts_as_zero() -> None:
    """分数缺失按 0 算 —— 不是按 1 算。"""
    scored = _item("L3", "我的生日是 1990 年 1 月 1 日", memory_id="m1", score=0.5, valid_at=NOW)
    unscored = _item("L3", "我的生日是 1990 年 1 月 1 日", memory_id="m2", score=None, valid_at=NOW)
    assert _prefer_item(scored, unscored, NOW) is True
    assert _prefer_item(unscored, scored, NOW) is False


def test_an_equal_score_and_strength_keeps_the_current_one() -> None:
    """全平 → 保留先到的那条（后来的不替换已有的）。"""
    first = _item("L3", "我的生日是 1990 年 1 月 1 日", memory_id="m1", score=0.5, valid_at=NOW)
    second = _item("L3", "我的生日是 1990 年 1 月 1 日", memory_id="m2", score=0.5, valid_at=NOW)
    assert _prefer_item(second, first, NOW) is False


def test_the_strength_breaks_a_tie_on_score_and_valid_at() -> None:
    """分数与生效时间都相同 → 比强度。"""
    fresh = _item(
        "L3",
        "我的生日是 1990 年 1 月 1 日",
        memory_id="m1",
        score=0.5,
        recall_count=5,
        last_recalled_at=ANCIENT - timedelta(hours=1),
        valid_at=NOW,
    )
    baseline = _item("L3", "我的生日是 1990 年 1 月 1 日", memory_id="m2", score=0.5, valid_at=NOW)
    assert _prefer_item(fresh, baseline, ANCIENT) is True
    assert _prefer_item(baseline, fresh, ANCIENT) is False


def test_only_one_memory_per_slot_survives() -> None:
    """同槽位的多条记忆只留一条 —— 旧值与新值同时进包比重复更严重。"""
    old = _item(
        "L3", "我的生日是 1990 年 1 月 1 日", memory_id="m1", valid_at=NOW - timedelta(days=1)
    )
    new = _item("L3", "我的生日是 1991 年 2 月 2 日", memory_id="m2", valid_at=NOW)
    deduped = {"old": old, "new": new}
    resolved = _resolve_slot_conflicts(deduped, NOW)
    assert list(resolved) == ["new"]


def test_a_non_l3_item_does_not_block_the_slot_arbitration() -> None:
    """非 L3 不参与槽位仲裁 —— 中途的 continue 不能把后面的 L3 一起吃掉。"""
    l1 = _item("L1", "我的生日是 1990 年 1 月 1 日")
    old = _item(
        "L3", "我的生日是 1990 年 1 月 1 日", memory_id="m1", valid_at=NOW - timedelta(days=1)
    )
    new = _item("L3", "我的生日是 1991 年 2 月 2 日", memory_id="m2", valid_at=NOW)
    resolved = _resolve_slot_conflicts({"l1": l1, "old": old, "new": new}, NOW)
    assert set(resolved) == {"l1", "new"}


def test_three_memories_in_one_slot_still_resolve_to_one() -> None:
    """三条同槽位记忆 —— 中间那条被替换后，后续比较不能崩。"""
    first = _item(
        "L3", "我的生日是 1990 年 1 月 1 日", memory_id="m1", valid_at=NOW - timedelta(days=2)
    )
    second = _item(
        "L3", "我的生日是 1990 年 1 月 1 日", memory_id="m2", valid_at=NOW - timedelta(days=1)
    )
    third = _item("L3", "我的生日是 1990 年 1 月 1 日", memory_id="m3", valid_at=NOW)
    resolved = _resolve_slot_conflicts({"a": first, "b": second, "c": third}, NOW)
    assert list(resolved) == ["c"]


# ── 正文去重 ───────────────────────────────────────────────────────────────


def test_identical_content_is_deduped_across_layers() -> None:
    """同一条事实在三条通道里只留一条 —— 层优先级高的胜出。"""
    l1 = _item("L1", "用户喜欢喝美式咖啡")
    l3 = _item("L3", "用户喜欢喝美式咖啡。", memory_id="m1", score=0.5)
    kept = dedupe_and_clip([l1, l3], token_budget=1000, now=NOW)
    assert len(kept) == 1
    assert kept[0].layer == "L3"


def test_the_same_layer_keeps_the_stronger_one() -> None:
    """同层同正文 → 比强度。"""
    strong = _item(
        "L3",
        "同一个内容",
        memory_id="m1",
        score=0.5,
        recall_count=5,
        last_recalled_at=ANCIENT - timedelta(hours=1),
    )
    weak = _item("L3", "同一个内容", memory_id="m2", score=0.5, recall_count=0)
    kept = dedupe_and_clip([weak, strong], token_budget=1000, now=ANCIENT)
    assert len(kept) == 1
    assert kept[0].memory_id == "m1"


def test_the_same_layer_breaks_a_strength_tie_by_score() -> None:
    """强度相同 → 比分数。"""
    high = _item("L3", "同一个内容", memory_id="m1", score=0.9)
    low = _item("L3", "同一个内容", memory_id="m2", score=0.2)
    kept = dedupe_and_clip([low, high], token_budget=1000, now=NOW)
    assert len(kept) == 1
    assert kept[0].memory_id == "m1"


def test_an_equal_score_and_strength_keeps_the_first_one() -> None:
    """分数与强度都相同 → 保留先到的那条。"""
    first = _item("L3", "同一个内容", memory_id="m1", score=0.5)
    second = _item("L3", "同一个内容", memory_id="m2", score=0.5)
    kept = dedupe_and_clip([first, second], token_budget=1000, now=NOW)
    assert len(kept) == 1
    assert kept[0].memory_id == "m1"


def test_an_item_with_an_unknown_layer_never_beats_a_known_one() -> None:
    """未知层不在优先级表里 → 优先级 0（最低）。"""
    known = _item("L1", "同一个内容")
    unknown = _item("L9", "同一个内容", score=0.9)
    kept = dedupe_and_clip([known, unknown], token_budget=1000, now=NOW)
    assert len(kept) == 1
    assert kept[0].layer == "L1"


def test_an_item_with_an_empty_content_is_dropped() -> None:
    """空正文没有去重键 → 跳过；但不能把后面的条目一起吃掉。"""
    empty = _item("L1", "")
    real = _item("L1", "有内容")
    kept = dedupe_and_clip([empty, real], token_budget=1000, now=NOW)
    assert [item.content for item in kept] == ["有内容"]


# ── 层间配额 ───────────────────────────────────────────────────────────────


def test_a_single_layer_gets_the_whole_budget() -> None:
    """只有一层时不拆分 —— 整份预算给这一层。"""
    items = [_item("L3", _cjk(30), memory_id="m1", score=0.5)]
    kept = _clip_by_layer(items, 100)
    assert len(kept) == 1


def test_the_layer_allocation_gives_each_layer_its_share() -> None:
    """层间配额：L1 保底、L3 为主。

    L1/L2/L3 各要 30/20/50，正好把 100 的预算占满 —— 哪一层多拿都会挤掉
    另一层，所以这条断言同时钉住了三个份额。
    """
    l1 = _item("L1", _cjk(30, "甲"))
    l2 = _item("L2", _cjk(20, "乙"))
    l3 = _item("L3", _cjk(50, "丙"), memory_id="m1", score=0.5)
    kept = dedupe_and_clip([l1, l2, l3], token_budget=100, now=NOW)
    contents = [item.content for item in kept]
    assert _cjk(30, "甲") in contents
    assert _cjk(20, "乙") in contents
    assert _cjk(50, "丙") in contents


def test_an_item_that_exceeds_its_layer_share_is_dropped() -> None:
    """层的份额装不下那条记忆时，那条被丢掉（不是整层清空）。

    L3 的份额是 50，它要 100 —— 剩下的 50 已经被 L1/L2 的份额占掉，
    补位也补不回来（全局余额只剩 0），所以那条必须掉。
    """
    l1 = _item("L1", _cjk(30, "甲"))
    l2 = _item("L2", _cjk(20, "乙"))
    l3 = _item("L3", _cjk(100, "丙"), memory_id="m1", score=0.5)
    kept = dedupe_and_clip([l1, l2, l3], token_budget=100, now=NOW)
    contents = [item.content for item in kept]
    assert _cjk(100, "丙") not in contents
    assert _cjk(30, "甲") in contents


def test_every_layer_keeps_at_least_one_item_when_the_budget_allows() -> None:
    """层配额装不下任何一条时，用全局余额给空层补一条。

    预算最紧时（L1/L3 各一条 60 token，budget=100）曾出现「整层被清空 →
    召回返回空包、预算一分未用」。契约要求不返回空包。
    """
    l1 = _item("L1", _cjk(40, "甲"))
    l2 = _item("L2", _cjk(25, "乙"))
    l3 = _item("L3", _cjk(50, "丙"), memory_id="m1", score=0.5)
    kept = dedupe_and_clip([l1, l2, l3], token_budget=100, now=NOW)
    contents = [item.content for item in kept]
    assert _cjk(50, "丙") in contents, "L3 的份额装得下 50 token 的事实"
    assert _cjk(40, "甲") in contents, "L1 应当被补一条"
    assert _cjk(25, "乙") not in contents, "全局余额不够时，L2 应当让位"


def test_the_fallback_picks_an_item_that_exactly_fits_the_remaining_budget() -> None:
    """补位时成本正好等于余额 → 装得下（``>`` 不是 ``>=``）。"""
    l1 = _item("L1", _cjk(50, "甲"))
    l2 = _item("L2", _cjk(25, "乙"))
    l3 = _item("L3", _cjk(25, "丙"), memory_id="m1", score=0.5)
    kept = dedupe_and_clip([l1, l2, l3], token_budget=100, now=NOW)
    contents = [item.content for item in kept]
    assert _cjk(50, "甲") in contents
    assert _cjk(25, "乙") in contents


def test_the_fallback_skips_an_item_that_does_not_fit_and_tries_the_next() -> None:
    """补位时第一条装不下 → 继续看后面装得下的（不能直接放弃整层）。"""
    l1 = _item("L1", _cjk(10, "甲"))
    big = _item("L3", _cjk(95, "乙"), memory_id="m1", score=0.9)
    small = _item("L3", _cjk(60, "丙"), memory_id="m2", score=0.8)
    kept = dedupe_and_clip([l1, big, small], token_budget=100, now=NOW)
    contents = [item.content for item in kept]
    assert _cjk(60, "丙") in contents, "应当跳过装不下的 95 token 那条"
    assert _cjk(95, "乙") not in contents


def test_the_fallback_reaches_the_l3_layer_when_it_is_empty() -> None:
    """L3 整层被份额挤空时，补位要能轮到 L3。"""
    l1 = _item("L1", _cjk(10, "甲"))
    l3 = _item("L3", _cjk(60, "乙"), memory_id="m1", score=0.5)
    kept = dedupe_and_clip([l1, l3], token_budget=100, now=NOW)
    contents = [item.content for item in kept]
    assert _cjk(60, "乙") in contents


def test_an_item_that_does_not_fit_its_layer_does_not_empty_the_layer() -> None:
    """一条超长内容不该把整层清空 —— 后面的条目同样相关，只是更短。

    L3 的份额正好是 50（预算被 L1/L2 的份额占满，没有回流）：
    60 token 的那条装不下，但 20 token 的两条装得下。
    """
    l1 = _item("L1", _cjk(30, "甲"))
    l2 = _item("L2", _cjk(20, "乙"))
    long_item = _item("L3", _cjk(60, "丙"), memory_id="m1", score=0.9)
    short_a = _item("L3", _cjk(20, "丁"), memory_id="m2", score=0.8)
    short_b = _item("L3", _cjk(20, "戊"), memory_id="m3", score=0.7)
    kept = dedupe_and_clip([l1, l2, long_item, short_a, short_b], token_budget=100, now=NOW)
    l3_contents = [item.content for item in kept if item.layer == "L3"]
    assert _cjk(60, "丙") not in l3_contents
    assert _cjk(20, "丁") in l3_contents
    assert _cjk(20, "戊") in l3_contents, "两条短的都该留下"


# ── 类型级上限 ─────────────────────────────────────────────────────────────


def test_a_type_cap_stops_the_second_item_of_that_type() -> None:
    """某一类记忆不能吃掉整份 L3 额度。"""
    p1 = _item("L3", "a", memory_id="m1", score=0.3, metadata={"memory_type": "profile"})
    p2 = _item("L3", "b", memory_id="m2", score=0.2, metadata={"memory_type": "profile"})
    p3 = _item("L3", "c", memory_id="m3", score=0.1, metadata={"memory_type": "profile"})
    kept = dedupe_and_clip([p1, p2, p3], token_budget=1000, type_caps={"profile": 2}, now=NOW)
    assert [item.content for item in kept] == ["a", "b"]


def test_a_type_cap_of_exactly_the_item_cost_still_keeps_the_item() -> None:
    """上限正好等于这条的成本 → 装得下（``>`` 不是 ``>=``）。"""
    p1 = _item("L3", "a", memory_id="m1", score=0.3, metadata={"memory_type": "profile"})
    p2 = _item("L3", "b", memory_id="m2", score=0.2, metadata={"memory_type": "profile"})
    other = _item("L3", "d", memory_id="m3", score=0.1, metadata={"memory_type": "event"})
    kept = dedupe_and_clip([p1, p2, other], token_budget=1000, type_caps={"profile": 1}, now=NOW)
    assert [item.content for item in kept] == ["a", "d"]


def test_a_type_cap_does_not_stop_the_other_types() -> None:
    """某一类用满额度后，别的类型照样能进上下文包。"""
    p1 = _item("L3", "a", memory_id="m1", score=0.3, metadata={"memory_type": "profile"})
    p2 = _item("L3", "b", memory_id="m2", score=0.2, metadata={"memory_type": "profile"})
    other = _item("L3", "d", memory_id="m3", score=0.1, metadata={"memory_type": "event"})
    kept = dedupe_and_clip([p1, p2, other], token_budget=1000, type_caps={"profile": 1}, now=NOW)
    contents = [item.content for item in kept]
    assert "a" in contents
    assert "d" in contents
    assert "b" not in contents


def test_without_type_caps_every_type_is_unlimited() -> None:
    """没配上限 → 类型不设限。"""
    items = [
        _item("L3", "a", memory_id="m1", score=0.3, metadata={"memory_type": "profile"}),
        _item("L3", "b", memory_id="m2", score=0.2, metadata={"memory_type": "profile"}),
    ]
    kept = dedupe_and_clip(items, token_budget=1000, now=NOW)
    assert len(kept) == 2


def test_the_type_caps_reach_the_clipping_stage() -> None:
    """类型上限必须一路传到裁剪阶段 —— 中途丢掉就等于没设上限。"""
    items = [
        _item("L3", "a", memory_id="m1", score=0.3, metadata={"memory_type": "profile"}),
        _item("L3", "b", memory_id="m2", score=0.2, metadata={"memory_type": "profile"}),
        _item("L3", "c", memory_id="m3", score=0.1, metadata={"memory_type": "profile"}),
    ]
    kept = dedupe_and_clip(items, token_budget=1000, type_caps={"profile": 1}, now=NOW)
    assert len(kept) == 1


# ── 补位与类型上限 ─────────────────────────────────────────────────────────
#
# 下面这组场景共用同一个骨架：某层被自己的份额挤空 → 走「每层至少留一条」
# 的补位逻辑。补位用的是**全局余额**，不是层份额 —— 于是补位路径上的
# 类型上限、余额边界、类型累计都是独立于主循环的一套判定，主循环的用例
# 够不着它们。
#
# 骨架：预算 100；L1 那条 150 token（装不下任何层的份额，补位时也装不下
# 全局余额 100）→ L1 彻底空掉、全局余额没被它占用；L3 两条 120 / 80
# token（都超过 L3 的份额，装不进主循环）→ 补位时 120 装不下全局余额、
# 80 装得下。


def _fallback_skeleton() -> list[MemoryItem]:
    return [
        _item("L1", _cjk(150, "甲")),
        _item("L3", _cjk(120, "乙"), memory_id="m1", score=0.9),
        _item("L3", _cjk(80, "丙"), memory_id="m2", score=0.8),
    ]


def test_the_fallback_picks_the_item_that_fits_after_skipping_the_oversized_one() -> None:
    """补位时第一条装不下 → 继续看后面装得下的（不能直接放弃整层）。"""
    kept = dedupe_and_clip(_fallback_skeleton(), token_budget=100, now=NOW)
    contents = [item.content for item in kept]
    assert _cjk(80, "丙") in contents, "应当跳过装不下的 120 token 那条"
    assert _cjk(120, "乙") not in contents


def test_the_fallback_respects_the_type_cap() -> None:
    """补位的那条也要受类型上限约束 —— 上限之内才补得进来。"""
    items = _fallback_skeleton()
    items[1] = _item(
        "L3", _cjk(120, "乙"), memory_id="m1", score=0.9, metadata={"memory_type": "profile"}
    )
    items[2] = _item(
        "L3", _cjk(80, "丙"), memory_id="m2", score=0.8, metadata={"memory_type": "profile"}
    )
    kept = dedupe_and_clip(items, token_budget=100, type_caps={"profile": 90}, now=NOW)
    contents = [item.content for item in kept]
    assert _cjk(80, "丙") in contents, "上限之内的那条应当被补上"
    assert _cjk(120, "乙") not in contents


def test_the_fallback_is_blocked_when_the_item_exceeds_the_type_cap() -> None:
    """补位的那条超过类型上限时也不能进包 —— 上限不只管主循环。"""
    items = _fallback_skeleton()
    items[1] = _item(
        "L3", _cjk(120, "乙"), memory_id="m1", score=0.9, metadata={"memory_type": "profile"}
    )
    items[2] = _item(
        "L3", _cjk(80, "丙"), memory_id="m2", score=0.8, metadata={"memory_type": "profile"}
    )
    kept = dedupe_and_clip(items, token_budget=100, type_caps={"profile": 70}, now=NOW)
    contents = [item.content for item in kept]
    assert _cjk(80, "丙") not in contents, "超出类型上限的条目不该被补进来"


def test_the_fallback_takes_an_item_whose_cost_equals_the_type_cap() -> None:
    """补位条目的成本正好等于上限 → 装得下（``>`` 不是 ``>=``）。"""
    items = _fallback_skeleton()
    items[1] = _item(
        "L3", _cjk(120, "乙"), memory_id="m1", score=0.9, metadata={"memory_type": "profile"}
    )
    items[2] = _item(
        "L3", _cjk(80, "丙"), memory_id="m2", score=0.8, metadata={"memory_type": "profile"}
    )
    kept = dedupe_and_clip(items, token_budget=100, type_caps={"profile": 80}, now=NOW)
    contents = [item.content for item in kept]
    assert _cjk(80, "丙") in contents


def test_the_fallback_takes_an_item_whose_cost_equals_the_remaining_budget() -> None:
    """补位条目的成本正好等于全局余额 → 装得下（``>`` 不是 ``>=``）。"""
    items = [
        _item("L1", _cjk(150, "甲")),
        _item("L3", _cjk(100, "乙"), memory_id="m1", score=0.9),
    ]
    kept = dedupe_and_clip(items, token_budget=100, now=NOW)
    contents = [item.content for item in kept]
    assert _cjk(100, "乙") in contents, "成本正好等于余额 → 装得下"


def test_the_fallback_prefers_l1_then_l2_then_l3() -> None:
    """补位顺序是 L1 → L2 → L3：当前对话先保底，摘要次之，长期事实最后。"""
    l1 = _item("L1", _cjk(90, "甲"))
    l2 = _item("L2", _cjk(90, "乙"))
    l3 = _item("L3", _cjk(90, "丙"), memory_id="m1", score=0.5)
    kept = dedupe_and_clip([l1, l2, l3], token_budget=100, now=NOW)
    contents = [item.content for item in kept]
    assert _cjk(90, "甲") in contents, "L1 是当前对话，补位优先保它"
    assert _cjk(90, "乙") not in contents, "全局余额不够时，L2 让位给 L1"
    assert _cjk(90, "丙") not in contents, "全局余额不够时，L3 让位给 L1"


# ── 仲裁必须用传入的 now ───────────────────────────────────────────────────


def test_the_dedup_tie_break_uses_the_given_now() -> None:
    """同分同时层时用强度仲裁 —— 强度按传入的 now 算，不是当前时间。

    ``strong`` 按给定的 now 算还有强度，``baseline`` 永远是 0.5 基线；
    若实现偷偷用当前时间，strong 的强度会衰减到 0，胜方就反了。
    """
    strong = _item(
        "L3",
        "同一个内容",
        memory_id="m1",
        score=0.5,
        recall_count=5,
        last_recalled_at=ANCIENT - timedelta(hours=1),
    )
    baseline = _item("L3", "同一个内容", memory_id="m2", score=0.5, recall_count=0)
    kept = dedupe_and_clip([strong, baseline], token_budget=1000, now=ANCIENT)
    assert len(kept) == 1
    assert kept[0].memory_id == "m1"


def test_the_slot_tie_break_uses_the_given_now() -> None:
    """同槽位、同分、同生效时间 → 用强度仲裁 —— 同样按传入的 now 算。"""
    strong = _item(
        "L3",
        "我的生日是 1990 年 1 月 1 日",
        memory_id="m1",
        score=0.5,
        recall_count=5,
        last_recalled_at=ANCIENT - timedelta(hours=1),
        valid_at=NOW,
    )
    baseline = _item("L3", "我的生日是 1991 年 2 月 2 日", memory_id="m2", score=0.5, valid_at=NOW)
    resolved = _resolve_slot_conflicts({"a": strong, "b": baseline}, ANCIENT)
    assert list(resolved) == ["a"]


def test_strength_reads_the_clock_as_utc_when_now_is_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不传 now 时用 UTC 当前时间 —— 本地时间被当成 UTC 会差一个时区偏移。

    这里把时钟钉死：不传 now 时取 12:00 UTC，若实现改成读本地时间
    （这里是 20:00），强度会按少 8 小时算，差得很明显。
    """
    import thinkback.domain.recall_policy as recall_policy

    class _FakeDatetime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            if tz is None:
                return datetime(2026, 10, 28, 20, 0, 0)
            return datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC)

    monkeypatch.setattr(recall_policy, "datetime", _FakeDatetime)
    item = _item(
        "L3",
        memory_id="m1",
        recall_count=3,
        last_recalled_at=datetime(2026, 10, 28, 0, 0, tzinfo=UTC),
    )
    assert _strength(item) == pytest.approx(0.5 ** (12 / 72) * (1 + math.log1p(3)))


def test_an_l3_item_with_recalls_but_no_timestamp_gets_the_baseline() -> None:
    """有召回次数但没有召回时刻 → 算不出衰减 → 0.5 基线。"""
    item = _item("L3", memory_id="m1", recall_count=3, last_recalled_at=None)
    assert _strength(item, now=NOW) == 0.5


# ── 缺失值的默认口径 ───────────────────────────────────────────────────────


def test_an_unknown_layer_never_beats_l1_on_the_same_content() -> None:
    """未知层的优先级是 0（最低）—— 哪怕 L1 那条分数更低，L1 也该赢。"""
    known = _item("L1", "同一个内容", score=0.1)
    unknown = _item("L9", "同一个内容", score=0.9)
    kept = dedupe_and_clip([unknown, known], token_budget=1000, now=NOW)
    assert len(kept) == 1
    assert kept[0].layer == "L1"


def test_a_missing_score_loses_the_tie_break() -> None:
    """分数缺失按 0 算 —— 不能按 1 算（那会让无分记忆顶掉有分的）。"""
    existing = _item("L3", "同一个内容", memory_id="m1", score=0.5)
    candidate = _item("L3", "同一个内容", memory_id="m2", score=None)
    kept = dedupe_and_clip([existing, candidate], token_budget=1000, now=NOW)
    assert len(kept) == 1
    assert kept[0].memory_id == "m1"


def test_a_missing_score_on_the_existing_item_loses_the_tie_break() -> None:
    """已存在的那条分数缺失也按 0 算 —— 候选该赢还是要赢。"""
    existing = _item("L3", "同一个内容", memory_id="m1", score=None)
    candidate = _item("L3", "同一个内容", memory_id="m2", score=0.5)
    kept = dedupe_and_clip([existing, candidate], token_budget=1000, now=NOW)
    assert len(kept) == 1
    assert kept[0].memory_id == "m2"


# ── 槽位仲裁的遍历顺序 ─────────────────────────────────────────────────────


def test_an_item_without_a_slot_does_not_stop_the_slot_arbitration() -> None:
    """没命中 P0 槽位的条目不参与仲裁 —— 但也不能把后面的仲裁一起吃掉。"""
    plain = _item("L3", "普通的一条事实", memory_id="m0")
    old = _item(
        "L3", "我的生日是 1990 年 1 月 1 日", memory_id="m1", valid_at=NOW - timedelta(days=1)
    )
    new = _item("L3", "我的生日是 1991 年 2 月 2 日", memory_id="m2", valid_at=NOW)
    resolved = _resolve_slot_conflicts({"p": plain, "old": old, "new": new}, NOW)
    assert set(resolved) == {"p", "new"}


def test_the_dedupe_pipeline_runs_the_slot_arbitration_with_the_given_now() -> None:
    """整条流水线（去重 → 槽位仲裁 → 排序 → 裁剪）都要用传入的 now。

    这里用同槽位、不同正文的两条：正文去重放它们过去，槽位仲裁才有机会
    按强度分胜负 —— 而强度必须按给定的 now 算。
    """
    strong = _item(
        "L3",
        "我的生日是 1990 年 1 月 1 日",
        memory_id="m1",
        score=0.5,
        recall_count=5,
        last_recalled_at=ANCIENT - timedelta(hours=1),
        valid_at=NOW,
    )
    baseline = _item("L3", "我的生日是 1991 年 2 月 2 日", memory_id="m2", score=0.5, valid_at=NOW)
    kept = dedupe_and_clip([strong, baseline], token_budget=1000, now=ANCIENT)
    assert [item.memory_id for item in kept] == ["m1"]


def test_the_dedupe_pipeline_orders_by_strength_with_the_given_now() -> None:
    """排序也要用传入的 now —— 同分同质量时靠强度分胜负。"""
    strong = _item(
        "L3",
        "事实甲",
        memory_id="m1",
        score=0.5,
        recall_count=5,
        last_recalled_at=ANCIENT - timedelta(hours=1),
    )
    baseline = _item("L3", "事实乙", memory_id="m2", score=0.5)
    kept = dedupe_and_clip([strong, baseline], token_budget=1000, now=ANCIENT)
    assert [item.memory_id for item in kept] == ["m1", "m2"]


def test_the_fallback_skips_a_type_capped_item_and_tries_the_next() -> None:
    """补位时某条被类型上限挡住 → 继续看后面装得下的（不能直接放弃整层）。"""
    items = [
        _item("L1", _cjk(150, "甲")),
        _item("L3", _cjk(120, "乙"), memory_id="m1", score=0.9),
        _item(
            "L3",
            _cjk(90, "丙"),
            memory_id="m2",
            score=0.8,
            metadata={"memory_type": "profile"},
        ),
        _item("L3", _cjk(80, "丁"), memory_id="m3", score=0.7),
    ]
    kept = dedupe_and_clip(items, token_budget=100, type_caps={"profile": 70}, now=NOW)
    contents = [item.content for item in kept]
    assert _cjk(80, "丁") in contents, "被类型上限挡住的那条不该让整层空掉"
    assert _cjk(90, "丙") not in contents
