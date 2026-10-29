"""召回预算：层保底 + 类型上限 + 未用配额回流。

三件事要钉住：
- 某一类记忆不能吃掉整份 L3 额度（类型上限来自注册表的 recall.budget）；
- 某类型没用完的额度仍能给别的类型用（配额回流）；
- 层保底不受类型上限影响：类型把额度用满时，L1/L2 仍然拿得到自己的份额。
"""

from __future__ import annotations

from thinkback.domain.recall_policy import dedupe_and_clip, estimate_tokens
from thinkback.memory.schemas import MemoryItem


def _l3(content: str, *, memory_type: str, score: float = 0.9) -> MemoryItem:
    return MemoryItem(
        layer="L3",
        content=content,
        source="mem0",
        score=score,
        metadata={"memory_as_data": True, "memory_type": memory_type},
    )


def test_type_cap_limits_one_type() -> None:
    items = [
        _l3("画像条目一的内容比较长一些", memory_type="profile", score=0.9),
        _l3("画像条目二的内容也比较长一些", memory_type="profile", score=0.8),
        _l3("画像条目三的内容同样不短", memory_type="profile", score=0.7),
    ]
    budget = estimate_tokens("画像条目一的内容比较长一些")

    kept = dedupe_and_clip(items, 1000, type_caps={"profile": budget})

    assert len(kept) == 1, "类型上限用满后，同类型的条目不再进上下文"


def test_unused_type_quota_flows_to_other_types() -> None:
    items = [
        _l3("画像条目一的内容比较长一些", memory_type="profile", score=0.9),
        _l3("事实条目一的内容比较长一些", memory_type="event", score=0.8),
    ]

    kept = dedupe_and_clip(items, 1000, type_caps={"profile": 10, "event": 1000})

    types = [str(item.metadata.get("memory_type")) for item in kept]
    assert "event" in types, "画像没用完的额度要留给别的类型"


def test_types_without_cap_are_unlimited() -> None:
    items = [
        _l3("事实条目一的内容比较长一些", memory_type="event", score=0.9),
        _l3("事实条目二的内容比较长一些", memory_type="event", score=0.8),
    ]

    kept = dedupe_and_clip(items, 1000, type_caps={"profile": 10})

    assert len(kept) == 2, "没配上限的类型不受限"


def test_type_cap_does_not_starve_other_layers() -> None:
    """类型上限只作用在 L3：L1（当前对话）不能被 L3 的类型额度挤掉。"""

    items = [
        _l3("画像条目一的内容比较长一些", memory_type="profile", score=0.9),
        _l3("画像条目二的内容比较长一些", memory_type="profile", score=0.8),
        MemoryItem(layer="L1", content="user: 最近这轮对话", source="round-1"),
    ]

    kept = dedupe_and_clip(items, 60, type_caps={"profile": 20})

    layers = {item.layer for item in kept}
    assert "L1" in layers


def test_layer_quota_below_single_item_cost_must_not_empty_the_package() -> None:
    """层配额比该层任何一条都小时，不能整层清空成空包。

    budget=100 时 L1 配额 30→48、L3 配额 50→51，而两层各有一条 60 token 的
    内容：两条都装不下 → 召回返回空包、预算一分未用。契约要求「不返回空包」
    （插件化设计 §6.4）。
    """
    items = [
        MemoryItem(layer="L1", content="甲" * 60, source="round-1"),
        _l3("乙" * 60, memory_type="event", score=0.9),
    ]

    kept = dedupe_and_clip(items, 100)

    assert kept, "预算有余额时不得返回空包"
    assert estimate_tokens(kept[0].content.strip()) <= 100


def test_empty_layer_gets_one_item_from_the_global_budget() -> None:
    """层配额装不下任何一条时，用全局余额给该层补一条（保底顺序 L1 优先）。"""
    items = [
        MemoryItem(layer="L1", content="甲" * 400, source="round-1"),
        _l3("乙" * 600, memory_type="event", score=0.9),
    ]

    kept = dedupe_and_clip(items, 1200)

    layers = {item.layer for item in kept}
    assert "L1" in layers, "L1 是保底层，层配额不够也要留下一条"
    assert sum(estimate_tokens(item.content.strip()) for item in kept) <= 1200
