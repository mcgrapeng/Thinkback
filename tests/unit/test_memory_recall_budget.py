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
