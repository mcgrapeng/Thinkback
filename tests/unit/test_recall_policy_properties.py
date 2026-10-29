"""召回后处理策略的属性测试（hypothesis）。

``domain/recall_policy.py`` 是记忆系统里逻辑最密的一块纯函数：正文去重、
P0 槽位冲突消解、层间 token 预算分配、层内裁剪、每层保底一条。
此前只有示例测试（``test_recall_policy.py``），**且不在变异审计范围内** ——
而这正是「判定方向写反了也不让任何既有用例变红」的地方：

- 预算分配里 ``<=`` 写成 ``<``：某层多拿/少拿一点预算，示例用例全绿。
- 槽位仲裁里 ``valid_at`` 比较方向翻转：旧值污染回答，示例用例全绿。
- 「每层保底一条」被删掉：预算极紧时召回返回**空包**，示例用例全绿。

示例测试只能覆盖「明显老」和「明显新」两个样本，离阈值差一点也不红。
属性测试用随机输入（含贴边界的偏移）钉住不变量。

``deadline=None``：这些用例是纯本地计算，但槽位抽取走正则，单次执行可能
超过 hypothesis 默认的 200ms 预算 —— 那不是被测代码变慢，是 deadline 太紧，
会变成随机 flaky。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hypothesis import assume, given
from hypothesis import strategies as st

from thinkback.domain.dedup import content_dedup_key, slot_dedup_key
from thinkback.domain.recall_policy import (
    LAYER_PRIORITY,
    _allocate_by_layer,
    _strength,
    dedupe_and_clip,
    estimate_tokens,
)
from thinkback.memory.schemas import MemoryItem

_NOW = datetime(2026, 10, 5, 12, 0, 0, tzinfo=UTC)
_LAYERS = ("L1", "L2", "L3")

# 正文只取「实义字符」：``content_dedup_key`` 会剔掉标点，
# 若正文全是标点则去重键为空串、条目被整体丢弃 —— 那是另一条契约，
# 这里单独用示例用例覆盖，不混进属性测试的噪声里。
_WORDS = st.text(
    alphabet=st.characters(
        blacklist_categories=("Cs", "Cc"),
        blacklist_characters="，。！？；：、（）【】《》“”‘’",
    ),
    min_size=1,
    max_size=24,
).filter(lambda text: bool(content_dedup_key(text)))

_HOURS_AGO = st.integers(min_value=0, max_value=24 * 365)
_RECALLS = st.integers(min_value=0, max_value=5_000)
_SCORE = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)
_BUDGET = st.integers(min_value=0, max_value=800)

# 三个互不相干的字符集：ASCII / CJK / 其余非 ASCII。
_ASCII = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126), min_size=1, max_size=60
)
_CJK = st.text(alphabet="中文漢字ひらがなカタカナ", min_size=1, max_size=30)
_OTHER = st.text(alphabet="Приветαβγ", min_size=1, max_size=30)

# 命中同一个 P0 槽位（生日）的两段正文：用于钉「同槽位至多留一条」。
_SLOT_A = "User birthday: 1990-01-01"
_SLOT_B = "User birthday: 1991-02-02"


def _l3_item(
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
# token 估算
# ---------------------------------------------------------------------------


@given(text=st.text())
def test_token_estimate_is_never_below_one(text: str) -> None:
    """空串也算 1 token —— 空正文也要占位，不能估成 0 让预算算错。"""
    assert estimate_tokens(text) >= 1


@given(text=st.text(), suffix=st.text())
def test_token_estimate_is_monotone_under_concatenation(text: str, suffix: str) -> None:
    """追加内容不会让估算变小。

    ``estimate_tokens`` 按字符集分档 ceil，追加只会让某档变多或不变；
    若改成「重新分档」或「取平均」，这条不变量就破了 —— 预算会随内容
    变长而变小，裁剪行为随之漂移。
    """
    assert estimate_tokens(text + suffix) >= estimate_tokens(text)


@given(ascii_text=_ASCII)
def test_ascii_costs_one_token_per_four_characters(ascii_text: str) -> None:
    """ASCII 约四字符一个 token。"""
    assert estimate_tokens(ascii_text) == -(-len(ascii_text) // 4)


@given(cjk_text=_CJK)
def test_cjk_costs_one_token_per_character(cjk_text: str) -> None:
    """中文一字约一个 token。"""
    assert estimate_tokens(cjk_text) == len(cjk_text)


@given(other_text=_OTHER)
def test_other_non_ascii_costs_one_token_per_two_characters(other_text: str) -> None:
    """其余非 ASCII（西里尔/希腊文等）约两字符一个 token。"""
    assert estimate_tokens(other_text) == -(-len(other_text) // 2)


@given(ascii_text=_ASCII, cjk_text=_CJK, other_text=_OTHER)
def test_charset_buckets_are_summed_independently(
    ascii_text: str, cjk_text: str, other_text: str
) -> None:
    """三种字符集的额度**各自**向上取整后相加，不是合起来取整。

    合起来取整（先拼总长再分档）会让「1 个 ASCII + 1 个 CJK」算成 1 token
    而不是 2 —— 混排正文（中文里夹英文术语，最常见的情形）被系统性低估，
    预算裁剪形同虚设。
    """
    assert estimate_tokens(ascii_text + cjk_text + other_text) == (
        estimate_tokens(ascii_text) + estimate_tokens(cjk_text) + estimate_tokens(other_text)
    )


# ---------------------------------------------------------------------------
# 强度
# ---------------------------------------------------------------------------


@given(hours_a=_HOURS_AGO, hours_b=_HOURS_AGO)
def test_strength_is_monotone_in_recency(hours_a: int, hours_b: int) -> None:
    """越久没被召回，强度越低。

    半衰期 72h 写反会让「陈旧但高频」的记忆压过「新鲜」的记忆 ——
    方向翻转只在生产上暴露，示例用例离阈值几十天也照样绿。
    """
    assume(hours_a < hours_b)
    newer = _l3_item("x", memory_id="m-new", recall_count=5, hours_ago=hours_a)
    older = _l3_item("x", memory_id="m-old", recall_count=5, hours_ago=hours_b)
    assert _strength(newer, now=_NOW) > _strength(older, now=_NOW)


@given(count_a=_RECALLS, count_b=_RECALLS)
def test_strength_is_monotone_in_recall_count(count_a: int, count_b: int) -> None:
    """同样新鲜时，被召回更多次的记忆强度更高。"""
    assume(1 <= count_a < count_b)
    few = _l3_item("x", memory_id="m-few", recall_count=count_a, hours_ago=10)
    many = _l3_item("x", memory_id="m-many", recall_count=count_b, hours_ago=10)
    assert _strength(few, now=_NOW) < _strength(many, now=_NOW)


@given(layer=st.sampled_from(["L1", "L2"]))
def test_strength_ignores_non_l3_layers(layer: str) -> None:
    """非 L3 没有召回次数可言，强度恒为基线 0.5，不参与排序。"""
    item = MemoryItem(layer=layer, content="x")
    assert _strength(item, now=_NOW) == 0.5


# ---------------------------------------------------------------------------
# 层间预算分配
# ---------------------------------------------------------------------------


@given(
    needs=st.dictionaries(
        st.sampled_from(_LAYERS),
        st.integers(min_value=0, max_value=600),
        min_size=1,
        max_size=3,
    ),
    budget=_BUDGET,
)
def test_layer_allocation_never_exceeds_needs(needs: dict[str, int], budget: int) -> None:
    """分到的额度不会超过该层真实需要量。"""
    allocation = _allocate_by_layer(needs, budget)
    assert set(allocation) == set(needs)
    for layer, want in needs.items():
        assert 0 <= allocation[layer] <= want


@given(
    needs=st.dictionaries(
        st.sampled_from(_LAYERS),
        st.integers(min_value=0, max_value=600),
        min_size=1,
        max_size=3,
    ),
    budget=_BUDGET,
)
def test_layer_allocation_never_exceeds_the_total_budget(
    needs: dict[str, int], budget: int
) -> None:
    """三层分到的额度加起来不超过总预算。

    ``LAYER_BUDGET_SHARES`` 之和是 1.0，回流循环再分一次剩余额度 ——
    若回流时把 ``remaining`` 算错（多扣或漏扣），总额会超预算，
    注入模型的内容就超了调用方给的 token 上限。
    """
    allocation = _allocate_by_layer(needs, budget)
    assert sum(allocation.values()) <= budget


@given(budget=st.integers(min_value=1, max_value=800))
def test_unused_budget_flows_back_to_layers_that_need_it(budget: int) -> None:
    """某层没用完的额度回流给还需要的层，不被白白浪费。"""
    needs = {"L1": 0, "L2": 0, "L3": budget}
    allocation = _allocate_by_layer(needs, budget)
    assert allocation["L3"] == budget


# ---------------------------------------------------------------------------
# dedupe_and_clip 的整体不变量
# ---------------------------------------------------------------------------


def _items_strategy() -> st.SearchStrategy[MemoryItem]:
    return st.builds(
        MemoryItem,
        layer=st.sampled_from(_LAYERS),
        content=_WORDS,
        memory_id=st.one_of(st.none(), st.text(min_size=1, max_size=12)),
        score=st.one_of(st.none(), _SCORE),
        recall_count=_RECALLS,
        last_recalled_at=st.one_of(st.none(), st.just(_NOW - timedelta(hours=24))),
        valid_at=st.one_of(st.none(), st.just(_NOW)),
    )


@given(items=st.lists(_items_strategy(), max_size=8), budget=_BUDGET)
def test_output_never_exceeds_the_token_budget(items: list[MemoryItem], budget: int) -> None:
    """输出内容占用的 token 永不超过预算。

    这是召回侧最硬的契约：调用方按 token 预算付费，超了就是真金白银的账单，
    而且会挤掉上下文窗口里的别的东西。
    """
    kept = dedupe_and_clip(items, token_budget=budget, now=_NOW)
    total = sum(estimate_tokens(item.content.strip()) for item in kept)
    assert total <= budget


@given(items=st.lists(_items_strategy(), max_size=8), budget=_BUDGET)
def test_output_is_ordered_l3_then_l2_then_l1(items: list[MemoryItem], budget: int) -> None:
    """输出顺序恒为 L3 → L2 → L1。

    层优先级是架构承诺（长期事实 > 摘要 > 当前对话）。排序环节一旦写反，
    摘要会压过长期事实，模型拿到的是过期概括而不是事实本身。
    """
    kept = dedupe_and_clip(items, token_budget=budget, now=_NOW)
    rank = {"L3": 0, "L2": 1, "L1": 2}
    ranks = [rank[item.layer] for item in kept]
    assert ranks == sorted(ranks)


@given(items=st.lists(_items_strategy(), max_size=8), budget=_BUDGET)
def test_dedupe_is_a_fixed_point(items: list[MemoryItem], budget: int) -> None:
    """再跑一次不会变多，也不会引入新内容。

    去重必须是幂等的：否则「重放一次同一次召回」会让上下文包里
    出现两份同一条事实。
    """
    once = dedupe_and_clip(items, token_budget=budget, now=_NOW)
    twice = dedupe_and_clip(once, token_budget=budget, now=_NOW)
    assert len(twice) <= len(once)
    assert {item.content for item in twice} <= {item.content for item in once}


@given(items=st.lists(_items_strategy(), max_size=8), budget=_BUDGET)
def test_duplicate_content_keeps_only_the_highest_layer(
    items: list[MemoryItem], budget: int
) -> None:
    """同一段正文在多层各出现一次时，只保留层优先级最高的那条。"""
    kept = dedupe_and_clip(items, token_budget=budget, now=_NOW)
    highest_by_key: dict[str, int] = {}
    for item in items:
        key = content_dedup_key(item.content)
        if not key:
            continue
        priority = LAYER_PRIORITY.get(item.layer, 0)
        highest_by_key[key] = max(highest_by_key.get(key, 0), priority)
    for item in kept:
        key = content_dedup_key(item.content)
        assert LAYER_PRIORITY.get(item.layer, 0) == highest_by_key[key]


@given(items=st.lists(_items_strategy(), max_size=8), budget=_BUDGET)
def test_at_most_one_l3_memory_per_p0_slot_survives(items: list[MemoryItem], budget: int) -> None:
    """同一个 P0 槽位（生日 / 现居城市 / 宠物名……）在上下文包里至多一条。

    两条同槽位记忆同时进包，模型会同时看到旧值与新值 —— 这比「重复」
    更严重：旧值会污染回答。写入期用 supersede 保证过，但并发写入、
    非本副本写入、重建路径都可能留下多条 ACTIVE，召回期要兜住。
    """
    kept = dedupe_and_clip(items, token_budget=budget, now=_NOW)
    slots = [slot_dedup_key(item.content) for item in kept if item.layer == "L3"]
    real_slots = [slot for slot in slots if slot]
    assert len(real_slots) == len(set(real_slots))


@given(items=st.lists(_items_strategy(), max_size=8), budget=_BUDGET)
def test_output_is_never_empty_when_something_fits(items: list[MemoryItem], budget: int) -> None:
    """装得下就至少给一条 —— 不返回空包。

    契约（插件化设计 §6.4）：召回不能空转。预算极紧时每层配额可能小于
    该层任何一条的成本，实现用全局余额给空层补一条；补的逻辑被删掉时
    这里就会返回空包。
    """
    fitting = [
        item
        for item in items
        if content_dedup_key(item.content) and estimate_tokens(item.content.strip()) <= budget
    ]
    assume(fitting)
    kept = dedupe_and_clip(items, token_budget=budget, now=_NOW)
    assert kept


def test_same_slot_keeps_the_newest_fact() -> None:
    """同槽位冲突：``valid_at`` 最新的那条胜出（旧值不污染回答）。"""
    older = _l3_item(
        _SLOT_A,
        memory_id="m-old",
        score=0.99,
        valid_at=_NOW - timedelta(days=30),
    )
    newer = _l3_item(
        _SLOT_B,
        memory_id="m-new",
        score=0.10,
        valid_at=_NOW,
    )
    kept = dedupe_and_clip([older, newer], token_budget=4000, now=_NOW)
    assert [item.memory_id for item in kept] == ["m-new"]


def test_same_slot_breaks_ties_by_score_then_strength() -> None:
    """``valid_at`` 相同（或都缺失）时依次比 score、再比强度。"""
    low_score = _l3_item(_SLOT_A, memory_id="m-low", score=0.2, valid_at=_NOW)
    high_score = _l3_item(_SLOT_B, memory_id="m-high", score=0.8, valid_at=_NOW)
    kept = dedupe_and_clip([low_score, high_score], token_budget=4000, now=_NOW)
    assert [item.memory_id for item in kept] == ["m-high"]


def test_same_slot_without_valid_at_keeps_the_stronger_one() -> None:
    """``valid_at`` 缺失视为最旧；两条都缺失时比强度。"""
    weak = _l3_item(_SLOT_A, memory_id="m-weak", score=0.5, recall_count=1, hours_ago=24 * 30)
    strong = _l3_item(_SLOT_B, memory_id="m-strong", score=0.5, recall_count=50, hours_ago=1)
    kept = dedupe_and_clip([weak, strong], token_budget=4000, now=_NOW)
    assert [item.memory_id for item in kept] == ["m-strong"]


def test_l1_keeps_the_newest_turns_when_the_budget_is_tight() -> None:
    """预算紧时 L1 保留最新对话，而不是最旧；输出仍按时间顺序。"""
    items = [
        MemoryItem(layer="L1", content=f"turn-{index}", source=f"round-{index}")
        for index in range(5)
    ]
    kept = dedupe_and_clip(items, token_budget=4, now=_NOW)
    assert [item.content for item in kept] == ["turn-3", "turn-4"]
