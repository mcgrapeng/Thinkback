"""召回后处理策略的边界与仲裁顺序。

``domain/recall_policy.py`` 的仲裁顺序是**一条链**：层优先级 → 内容质量 →
引擎分 → 事实生效时间 → 强度。链上每一环的**比较方向**写反了，都不会让
任何既有用例变红，只会让「旧值污染新值」在生产上出现。

这里钉的都是「翻转了也不红」的那类：

- ``score`` 缺失时当 0.0 还是 1.0 —— 缺失值参与仲裁的默认口径；
- 同分时是继续比强度还是直接判平 —— 决定「谁胜」；
- 强度里 ``recall_count=0`` 与「从没被召回」的区分；
- ``valid_at`` 一个带时区一个不带 —— 混着比会抛异常；
- 预算**刚好装满**时，「每层保底一条」还能不能兜住；
- CJK 区间端点与 ASCII 边界字符的归类。

这些分支此前只有变异审计看得见（157 个存活变异体），属性测试够不着 ——
属性测试要的是「随机输入下不变量成立」，而这里是「某个特定输入下谁胜」。
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from thinkback.domain.recall_policy import (
    _allocate_by_layer,
    _strength,
    dedupe_and_clip,
    estimate_tokens,
)
from thinkback.memory.schemas import MemoryItem

_NOW = datetime(2026, 10, 5, 12, 0, 0, tzinfo=UTC)

_SLOT_A = "User birthday: 1990-01-01"
_SLOT_B = "User birthday: 1991-02-02"


def _l3(
    content: str,
    *,
    memory_id: str,
    score: float | None = None,
    recall_count: int = 0,
    hours_ago: int = 0,
    valid_at: datetime | None = None,
) -> MemoryItem:
    return MemoryItem(
        layer="L3",
        content=content,
        memory_id=memory_id,
        score=score,
        recall_count=recall_count,
        last_recalled_at=_NOW - timedelta(hours=hours_ago),
        valid_at=valid_at,
    )


# ---------------------------------------------------------------------------
# 缺失 score 的默认口径
# ---------------------------------------------------------------------------


def test_a_missing_score_counts_as_zero_when_arbitrating() -> None:
    """缺失 score 当 0.0 —— 不能当 1.0，否则「没分」的记忆会压过「有分」的。"""
    scored = _l3(_SLOT_A, memory_id="m-scored", score=0.5, valid_at=_NOW)
    unscored = _l3(_SLOT_B, memory_id="m-unscored", score=None, valid_at=_NOW)
    kept = dedupe_and_clip([scored, unscored], token_budget=4000, now=_NOW)
    assert [item.memory_id for item in kept] == ["m-scored"]


def test_a_missing_score_loses_against_a_scored_candidate() -> None:
    """候选缺失 score 时同样按 0.0 算。"""
    scored = _l3(_SLOT_A, memory_id="m-scored", score=0.5, valid_at=_NOW)
    unscored = _l3(_SLOT_B, memory_id="m-unscored", score=None, valid_at=_NOW)
    kept = dedupe_and_clip([unscored, scored], token_budget=4000, now=_NOW)
    assert [item.memory_id for item in kept] == ["m-scored"]


def test_l3_items_are_ordered_by_engine_score() -> None:
    """引擎相关度主导 L3 内部顺序 —— 分数高的先注入。"""
    high = _l3("内容甲甲甲甲", memory_id="m-high", score=0.9)
    low = _l3("内容乙乙乙乙", memory_id="m-low", score=0.1)
    kept = dedupe_and_clip([low, high], token_budget=4000, now=_NOW)
    assert [item.memory_id for item in kept] == ["m-high", "m-low"]


# ---------------------------------------------------------------------------
# 同分时的仲裁：继续比强度，而不是判平
# ---------------------------------------------------------------------------


def test_equal_scores_fall_through_to_strength() -> None:
    """同分时**继续**比强度 —— 不是「后来者直接判胜」。"""
    strong = _l3(_SLOT_A, memory_id="m-strong", score=0.5, recall_count=50, hours_ago=1)
    weak = _l3(_SLOT_B, memory_id="m-weak", score=0.5, recall_count=1, hours_ago=24 * 30)
    kept = dedupe_and_clip([strong, weak], token_budget=4000, now=_NOW)
    assert [item.memory_id for item in kept] == ["m-strong"]


# ---------------------------------------------------------------------------
# 强度的边界
# ---------------------------------------------------------------------------


def test_an_l3_item_never_recalled_has_the_baseline_strength() -> None:
    """``recall_count=0`` 是「从没被召回」，不是「召回过一次但算不出强度」。"""
    item = _l3("内容甲甲甲甲", memory_id="m-0", recall_count=0, hours_ago=24 * 30)
    assert _strength(item, now=_NOW) == 0.5


def test_an_l3_item_recalled_once_has_a_measurable_strength() -> None:
    """``recall_count=1`` 已经算「被召回过」—— 不能还按基线 0.5 处理。"""
    item = _l3("内容甲甲甲甲", memory_id="m-1", recall_count=1, hours_ago=1)
    assert _strength(item, now=_NOW) > 0.5


def test_a_future_last_recalled_at_is_clamped_to_now() -> None:
    """时钟回拨或并发写入可能让 ``last_recalled_at`` 落在未来 —— 差值不能为负。"""
    item = _l3(
        "内容甲甲甲甲",
        memory_id="m-future",
        recall_count=4,
        hours_ago=-24,
    )
    expected = 1.0 + math.log1p(4)  # 差值被截到 0 → 新鲜度因子为 1.0
    assert _strength(item, now=_NOW) == expected


# ---------------------------------------------------------------------------
# valid_at 的时区
# ---------------------------------------------------------------------------


def test_a_naive_valid_at_is_compared_as_utc() -> None:
    """一个带时区一个不带 —— 混着比会抛 TypeError，必须先归一。"""
    naive = _l3(
        _SLOT_A,
        memory_id="m-naive",
        score=0.5,
        valid_at=datetime(2026, 10, 1, 12, 0, 0),
    )
    aware = _l3(
        _SLOT_B,
        memory_id="m-aware",
        score=0.5,
        valid_at=datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC),
    )
    kept = dedupe_and_clip([aware, naive], token_budget=4000, now=_NOW)
    assert [item.memory_id for item in kept] == ["m-naive"]


# ---------------------------------------------------------------------------
# 预算刚好装满时的「每层保底一条」
# ---------------------------------------------------------------------------


def test_a_layer_is_still_filled_when_its_item_exactly_fits_the_remaining_budget() -> None:
    """剩余额度**刚好**等于条目成本时，那一层仍要兜住一条。

    「每层保底一条」用全局余额补空层。余额算错一个 token，这里就会返回
    空包或漏掉一条正好装得下的记忆。
    """
    l1 = MemoryItem(layer="L1", content="a" * 16)  # 4 token
    l3 = _l3("b" * 24, memory_id="m-l3")  # 6 token
    kept = dedupe_and_clip([l1, l3], token_budget=10, now=_NOW)
    assert {item.content for item in kept} == {l1.content, l3.content}
    assert sum(estimate_tokens(item.content.strip()) for item in kept) == 10


# ---------------------------------------------------------------------------
# 层间分配：单层时额度要全额给它
# ---------------------------------------------------------------------------


def test_a_single_layer_gets_the_whole_budget() -> None:
    """只有一层时，别的层没占的额度要全额回流给它。"""
    allocation = _allocate_by_layer({"L3": 100}, 100)
    assert allocation["L3"] == 100


def test_a_single_layer_with_a_small_need_gets_what_it_needs() -> None:
    allocation = _allocate_by_layer({"L3": 30}, 100)
    assert allocation["L3"] == 30


# ---------------------------------------------------------------------------
# 字符集边界
# ---------------------------------------------------------------------------


def test_the_first_character_of_the_cjk_range_is_cjk() -> None:
    """U+4E00 是 CJK 基本区的**头** —— 边界判错会让最常用的汉字被当别的。"""
    assert estimate_tokens("一" * 2) == 2


def test_the_last_character_of_the_cjk_range_is_cjk() -> None:
    """U+9FFF 是 CJK 基本区的**尾**。"""
    assert estimate_tokens("鿿" * 2) == 2


def test_the_character_just_below_ascii_is_not_ascii() -> None:
    """U+00A0 不是 ASCII —— 归错档会让混排正文的预算算错。"""
    assert estimate_tokens(" " * 3) == 2


# ---------------------------------------------------------------------------
# 兜底分支（每层至少留一条）
# ---------------------------------------------------------------------------


def test_the_fallback_fills_a_layer_whose_item_exactly_fits_the_budget() -> None:
    """主循环一条都没选中时，兜底分支要用**全局余额**给空层补一条。

    这两条都装得下（各 10 token，预算 10），但层配额（L1 拿 3、L3 拿 5）
    都装不下 —— 只有兜底分支能救。兜底的余额算错一个 token，这里就会
    返回**空包**（契约要求「不返回空包」）。
    """
    l1 = MemoryItem(layer="L1", content="a" * 40)  # 10 token
    l3 = _l3("b" * 40, memory_id="m-l3")  # 10 token
    kept = dedupe_and_clip([l1, l3], token_budget=10, now=_NOW)
    assert kept, "装得下却返回空包"
    assert l1.content in {item.content for item in kept}


def test_the_fallback_prefers_the_lowest_layer_when_several_fit() -> None:
    """兜底顺序是 L1 → L2 → L3 —— L1 是当前对话，份额本来就是保底。"""
    l1 = MemoryItem(layer="L1", content="a" * 40)
    l3 = _l3("b" * 40, memory_id="m-l3")
    kept = dedupe_and_clip([l1, l3], token_budget=10, now=_NOW)
    assert [item.layer for item in kept] == ["L1"]


# ---------------------------------------------------------------------------
# 层间分配：三层的份额与回流
# ---------------------------------------------------------------------------


def test_a_layer_missing_from_the_needs_does_not_starve_the_others() -> None:
    """中间层缺席时，后面的层仍要拿到自己的份额。

    分配循环里 ``continue`` 写成 ``break``，后面的层就分不到初始份额 ——
    而 L3 的份额（0.5）是最大的那份，饿死它等于长期事实被摘摘要挤掉。
    """
    allocation = _allocate_by_layer({"L1": 100, "L3": 100}, 100)
    assert allocation["L3"] > allocation["L1"]


# ---------------------------------------------------------------------------
# 缺失 now 时的一致性
# ---------------------------------------------------------------------------


def test_the_strength_uses_the_real_clock_when_no_now_is_given() -> None:
    """不传 now 时用真实时钟 —— 且那个时钟**带时区**。

    ``last_recalled_at`` 是 aware 的；时钟一旦退化成 naive，两者相减直接
    抛 ``TypeError: can't subtract offset-naive and offset-aware datetimes``。
    """
    item = _l3(
        "内容甲甲甲甲",
        memory_id="m-1",
        recall_count=3,
        hours_ago=1,
    )
    assert isinstance(_strength(item), float)


def test_a_future_last_recalled_at_outranks_a_stale_one_at_equal_scores() -> None:
    """同分同生效时间时，比的是强度；而强度依赖传入的 now。

    ``last_recalled_at`` 落在未来（时钟回拨 / 并发写入）时，相对 ``now``
    的差值被截到 0 → 强度最高。换成真实时钟后这条关系会反过来 ——
    所以「用哪个 now」是可观察的，不能随手改。
    """
    fresh = _l3(_SLOT_A, memory_id="m-fresh", score=0.5, recall_count=1, hours_ago=-1)
    stale = _l3(_SLOT_B, memory_id="m-stale", score=0.5, recall_count=10, hours_ago=100)
    kept = dedupe_and_clip([stale, fresh], token_budget=4000, now=_NOW)
    assert [item.memory_id for item in kept] == ["m-fresh"]


# ---------------------------------------------------------------------------
# 正文去重时的分值仲裁
# ---------------------------------------------------------------------------


def test_a_missing_score_loses_in_the_content_dedup_path() -> None:
    """**同一条正文**（去重键相同）的两条记忆按 score 仲裁时，缺失当 0.0。"""
    from thinkback.memory.schemas import MemoryItem as _Item

    scored = _Item(layer="L3", content="同一条正文", memory_id="m-scored", score=0.5)
    unscored = _Item(layer="L3", content="同一条正文", memory_id="m-unscored", score=None)
    kept = dedupe_and_clip([scored, unscored], token_budget=4000, now=_NOW)
    assert [item.memory_id for item in kept] == ["m-scored"]


def test_a_l3_item_with_recalls_but_no_timestamp_keeps_the_baseline_strength() -> None:
    """「召回过但没记时间」→ 按基线 0.5 处理，不是「最新鲜」。

    ``recall_count`` 有值而 ``last_recalled_at`` 为空，说明召回信号写了一半。
    把它当成最新鲜（强度最大化），会让这类残缺记录压过真正新鲜的记忆。
    """
    from thinkback.memory.schemas import MemoryItem as _Item

    item = _Item(layer="L3", content="内容甲甲甲甲", memory_id="m-1", recall_count=5)
    assert item.last_recalled_at is None
    assert _strength(item, now=_NOW) == 0.5


def test_a_missing_score_loses_against_a_scored_candidate_in_the_dedup_path() -> None:
    """同一条正文去重时，**先来的**那条缺失 score 也按 0.0 算。

    去重的仲裁顺序是「层优先级 → 强度 → score」，所以缺失 score 的那条
    先占位时，后来的有分值者必须能把它顶掉。
    """
    from thinkback.memory.schemas import MemoryItem as _Item

    unscored = _Item(layer="L3", content="同一条正文", memory_id="m-unscored", score=None)
    scored = _Item(layer="L3", content="同一条正文", memory_id="m-scored", score=0.5)
    kept = dedupe_and_clip([unscored, scored], token_budget=4000, now=_NOW)
    assert [item.memory_id for item in kept] == ["m-scored"]


# ---------------------------------------------------------------------------
# 兜底分支上的类型级上限
# ---------------------------------------------------------------------------


def test_the_fallback_respects_the_type_cap() -> None:
    """兜底分支（每层至少留一条）也要守类型级上限。

    主循环一条都没选中时，兜底用全局余额补一条 —— 但补进来的那条同样要
    受类型级上限约束，否则某一类可以借兜底把整份预算吃掉。
    """
    capped = MemoryItem(layer="L1", content="a" * 40, metadata={"memory_type": "profile"})
    other = _l3("b" * 40, memory_id="m-l3")
    kept = dedupe_and_clip([capped, other], token_budget=10, now=_NOW, type_caps={"profile": 5})
    assert capped.content not in {item.content for item in kept}


def test_the_fallback_allows_a_type_exactly_at_its_cap() -> None:
    """类型用量**正好等于**上限时仍然放行 —— 判据是「超出」而不是「达到」。"""
    capped = MemoryItem(layer="L1", content="a" * 40, metadata={"memory_type": "profile"})
    other = _l3("b" * 40, memory_id="m-l3")
    kept = dedupe_and_clip([capped, other], token_budget=10, now=_NOW, type_caps={"profile": 10})
    assert capped.content in {item.content for item in kept}


def test_the_fallback_keeps_looking_after_an_item_that_does_not_fit() -> None:
    """兜底遇到装不下的条目要**继续找下一条**，而不是放弃整层。

    层配额被前面的层分走时，这一层的条目可能都超过配额（主循环全跳过），
    但兜底用的是全局余额 —— 后面那条更短的条目仍然装得下。
    """
    fits = MemoryItem(layer="L1", content="c" * 20)  # 5 token
    too_big = MemoryItem(layer="L1", content="d" * 48)  # 12 token
    l2 = MemoryItem(layer="L2", content="e" * 12)  # 3 token
    l3 = _l3("f" * 12, memory_id="m-l3")  # 3 token
    kept = dedupe_and_clip([fits, too_big, l2, l3], token_budget=10, now=_NOW)
    assert fits.content in {item.content for item in kept}


def test_the_main_loop_skips_an_item_over_its_type_cap_and_keeps_the_next_one() -> None:
    """主循环里某类型的额度用满后要**跳过这一条继续看下一条**。

    直接放弃整层的话，同类型的下一条（更短、装得下）也会被丢掉。
    """
    over_cap = _l3("a" * 24, memory_id="m-over")  # 6
    over_cap.metadata["memory_type"] = "profile"
    fits = _l3("b" * 12, memory_id="m-fits")  # 3
    fits.metadata["memory_type"] = "profile"
    kept = dedupe_and_clip([over_cap, fits], token_budget=20, now=_NOW, type_caps={"profile": 5})
    assert [item.memory_id for item in kept] == ["m-fits"]


def test_the_main_loop_skips_an_item_over_the_layer_limit_and_keeps_the_next_one() -> None:
    """主循环里装不下的条目要跳过继续看下一条 —— 放弃整层会白丢一条。"""
    too_big = _l3("a" * 48, memory_id="m-big")  # 12
    fits = _l3("b" * 12, memory_id="m-fits")  # 3
    l1 = MemoryItem(layer="L1", content="c" * 12)  # 3
    kept = dedupe_and_clip([too_big, fits, l1], token_budget=5, now=_NOW)
    assert "m-fits" in {item.memory_id for item in kept}


def test_the_fallback_reaches_the_last_layer_even_when_earlier_layers_are_skipped() -> None:
    """兜底要一层一层往下走 —— 前面几层跳过不能把最后一层也搭进去。"""
    l1 = MemoryItem(layer="L1", content="a" * 12)  # 3
    l2 = MemoryItem(layer="L2", content="b" * 48)  # 12 —— 永远装不下
    l3 = _l3("c" * 24, memory_id="m-l3")  # 6
    kept = dedupe_and_clip([l1, l2, l3], token_budget=10, now=_NOW)
    assert "m-l3" in {item.memory_id for item in kept}


def test_the_fallback_gives_the_budget_to_the_lowest_layer_first() -> None:
    """兜底顺序 L1 → L2 → L3：L2 装得下时就不该轮到 L3。"""
    l1 = MemoryItem(layer="L1", content="a" * 12)  # 3
    l2 = MemoryItem(layer="L2", content="b" * 16)  # 4
    l3 = _l3("c" * 24, memory_id="m-l3")  # 6
    kept = dedupe_and_clip([l1, l2, l3], token_budget=10, now=_NOW)
    contents = {item.content for item in kept}
    assert l2.content in contents
    assert l3.content not in contents


def test_the_fallback_gives_the_budget_to_l1_before_l3() -> None:
    """L1 是当前对话，份额本来就是保底 —— 兜底也要先给它。"""
    l1 = MemoryItem(layer="L1", content="a" * 40)  # 10
    l3 = _l3("b" * 24, memory_id="m-l3")  # 6
    kept = dedupe_and_clip([l1, l3], token_budget=10, now=_NOW)
    contents = {item.content for item in kept}
    assert l1.content in contents
    assert l3.content not in contents
