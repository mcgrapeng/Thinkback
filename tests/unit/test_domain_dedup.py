"""跨通道去重的领域口径测试（domain/dedup.py + domain/recall_policy.py）。

覆盖三件事：

1. 正文去重键：语义等价的措辞（大小写 / 空白 / 标点 / 说话人前缀）落同一个键；
2. 跨通道去重：L1 回合、L2 摘要、L3 记忆里的同一件事只注入一次；
3. 槽位冲突消解：同一个 P0 槽位（旧值 + 新值）只保留 valid_at 最新的那条。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from thinkback.domain.dedup import content_dedup_key, normalize_text, slot_dedup_key
from thinkback.domain.recall_policy import dedupe_and_clip
from thinkback.memory.schemas import MemoryItem


def _now() -> datetime:
    return datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


def test_normalize_text_strips_punctuation_and_case() -> None:
    assert normalize_text("User lives in Shanghai.") == "userlivesinshanghai"
    assert normalize_text("user:  我住在上海") == "user我住在上海"
    assert normalize_text("我住在上海。") == "我住在上海"
    assert normalize_text("   ") == ""


def test_content_dedup_key_is_stable_across_equivalent_wording() -> None:
    assert content_dedup_key("User lives in Shanghai.") == content_dedup_key(
        "user lives in  shanghai"
    )
    assert content_dedup_key("User lives in Shanghai") != content_dedup_key(
        "User lives in Hangzhou"
    )


def test_slot_dedup_key_uses_p0_slot() -> None:
    assert slot_dedup_key("User lives in Shanghai") == "slot:current_location"
    assert slot_dedup_key("User lives in Hangzhou") == "slot:current_location"
    assert slot_dedup_key("用户今天心情不错") is None


def test_cross_channel_duplicate_keeps_only_the_highest_layer() -> None:
    """L1 回合与 L3 记忆说的是同一件事（只差标点/大小写）→ 只注入 L3。"""

    items = [
        MemoryItem(layer="L1", content="User lives in Shanghai.", source="round-1"),
        MemoryItem(
            layer="L3",
            content="user lives in  shanghai",
            source="mem0",
            memory_id="memory-1",
        ),
    ]
    clipped = dedupe_and_clip(items, token_budget=1200, now=_now())
    assert [item.layer for item in clipped] == ["L3"]


def test_same_slot_conflicting_facts_keep_the_newest_valid_at() -> None:
    """同一槽位的旧值与新值同时命中 → 只保留事实生效时间更新的那条。"""

    now = _now()
    items = [
        MemoryItem(
            layer="L3",
            content="User lives in Hangzhou",
            source="mem0",
            memory_id="memory-old",
            score=0.9,
            valid_at=now - timedelta(days=30),
        ),
        MemoryItem(
            layer="L3",
            content="User lives in Shanghai",
            source="mem0",
            memory_id="memory-new",
            score=0.4,
            valid_at=now - timedelta(days=1),
        ),
    ]
    clipped = dedupe_and_clip(items, token_budget=1200, now=now)
    assert [item.memory_id for item in clipped] == ["memory-new"]


def test_same_slot_conflict_falls_back_to_score_when_valid_at_missing() -> None:
    now = _now()
    items = [
        MemoryItem(
            layer="L3",
            content="User lives in Hangzhou",
            source="mem0",
            memory_id="memory-old",
            score=0.9,
        ),
        MemoryItem(
            layer="L3",
            content="User lives in Shanghai",
            source="mem0",
            memory_id="memory-new",
            score=0.4,
        ),
    ]
    clipped = dedupe_and_clip(items, token_budget=1200, now=now)
    assert [item.memory_id for item in clipped] == ["memory-old"]


def test_different_slots_are_both_kept() -> None:
    items = [
        MemoryItem(
            layer="L3",
            content="User lives in Shanghai",
            source="mem0",
            memory_id="memory-1",
        ),
        MemoryItem(
            layer="L3",
            content="User birthday: March 3rd",
            source="mem0",
            memory_id="memory-2",
        ),
    ]
    clipped = dedupe_and_clip(items, token_budget=1200, now=_now())
    assert {item.memory_id for item in clipped} == {"memory-1", "memory-2"}


def test_l1_and_l2_duplicate_keeps_l2() -> None:
    items = [
        MemoryItem(layer="L1", content="我住在上海", source="round-1"),
        MemoryItem(layer="L2", content="我住在上海。", source="summary-1"),
    ]
    clipped = dedupe_and_clip(items, token_budget=1200, now=_now())
    assert [item.layer for item in clipped] == ["L2"]


def test_empty_content_is_dropped() -> None:
    items = [MemoryItem(layer="L1", content="   ", source="round-1")]
    assert dedupe_and_clip(items, token_budget=1200, now=_now()) == []
