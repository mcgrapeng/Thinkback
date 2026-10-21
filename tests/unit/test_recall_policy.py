"""S1 regression tests: dedupe_and_clip uses Ebbinghaus strength (recency × frequency).

G1 from comparison report: langmem 的 strength 排序是 2026 共识；本仓
``recall_count`` / ``last_recalled_at`` 已落库但 ``dedupe_and_clip`` 不消费。
本测试钉死新的排序优先级 —— 同层按 strength，strength = log1p(recall) × 0.5**(hours/half_life)。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from thinkback.domain.content_quality import intrinsic_quality
from thinkback.domain.recall_policy import _strength, dedupe_and_clip
from thinkback.memory.schemas import MemoryItem


def _now() -> datetime:
    return datetime(2026, 10, 5, 12, 0, 0, tzinfo=UTC)


def test_strength_is_zero_baseline_for_non_l3() -> None:
    item_l1 = MemoryItem(layer="L1", content="x")
    item_l2 = MemoryItem(layer="L2", content="x")
    assert _strength(item_l1, now=_now()) == 0.5
    assert _strength(item_l2, now=_now()) == 0.5


def test_strength_is_zero_baseline_for_l3_with_no_recalls() -> None:
    item = MemoryItem(layer="L3", content="x", memory_id="m1", recall_count=0)
    assert _strength(item, now=_now()) == 0.5


def test_strength_favors_recently_recalled_high_frequency() -> None:
    now = _now()
    fresh = MemoryItem(
        layer="L3",
        content="x",
        memory_id="m-fresh",
        recall_count=10,
        last_recalled_at=now - timedelta(hours=1),
    )
    stale = MemoryItem(
        layer="L3",
        content="x",
        memory_id="m-stale",
        recall_count=10,
        last_recalled_at=now - timedelta(days=14),
    )
    assert _strength(fresh, now=now) > _strength(stale, now=now)


def test_strength_favors_high_recall_count_when_recency_equal() -> None:
    now = _now()
    high = MemoryItem(
        layer="L3",
        content="x",
        memory_id="m-high",
        recall_count=10,
        last_recalled_at=now - timedelta(hours=12),
    )
    low = MemoryItem(
        layer="L3",
        content="x",
        memory_id="m-low",
        recall_count=1,
        last_recalled_at=now - timedelta(hours=12),
    )
    assert _strength(high, now=now) > _strength(low, now=now)


def test_dedupe_and_clip_orders_l3_by_strength_within_layer() -> None:
    """同层同内容去重按 strength 仲裁；强度高的胜。"""
    now = _now()
    strong = MemoryItem(
        layer="L3",
        content="User lives in 杭州",
        memory_id="m-strong",
        score=0.5,
        recall_count=20,
        last_recalled_at=now - timedelta(hours=2),
    )
    weak = MemoryItem(
        layer="L3",
        content="User lives in 杭州",
        memory_id="m-weak",
        score=0.9,  # higher score but much weaker signal
        recall_count=1,
        last_recalled_at=now - timedelta(days=30),
    )
    result = dedupe_and_clip([weak, strong], token_budget=1200, now=now)
    assert len(result) == 1
    assert result[0].memory_id == "m-strong"


def test_dedupe_and_clip_keeps_layer_priority_over_strength() -> None:
    """层优先 > 强度：L2 不应被 L3 强度高而反超。"""
    now = _now()
    l3_strong = MemoryItem(
        layer="L3",
        content="User likes 咖啡",
        memory_id="m-l3",
        score=0.9,
        recall_count=20,
        last_recalled_at=now - timedelta(hours=1),
    )
    l2 = MemoryItem(layer="L2", content="User likes 咖啡", source="summary-1")
    result = dedupe_and_clip([l2, l3_strong], token_budget=1200, now=now)
    assert len(result) == 1
    assert result[0].layer == "L3"


# ---------------------------------------------------------------------------
# S6 (G4): L2 summary structured_sections 字段
# ---------------------------------------------------------------------------


def test_summary_entry_defaults_structured_sections_to_empty_dict() -> None:
    """S6: SummaryEntry 默认 structured_sections={}（concat 降级版保持空）。"""
    from thinkback.domain.entities import SummaryEntry
    from thinkback.domain.enums import SummaryState

    entry = SummaryEntry(
        summary_id="s1",
        user_id="u",
        memory_scope_id="sc",
        summary_text="",
        summary_cursor_round=None,
        latest_source_round_id=None,
        latest_source_timestamp=None,
        summary_state=SummaryState.ACTIVE,
    )
    assert entry.structured_sections == {}


def test_sections_to_text_renders_structured_sections_view() -> None:
    """S6: sections_to_text 把 4 段画像拼成向后兼容 summary_text 视图。"""
    from thinkback.domain.summarization import sections_to_text

    sections = {
        "主题": "用户在聊咖啡",
        "进行中事项": "等新品上市",
        "行为偏好": "无",
        "近期状态": "心情不错",
    }
    view = sections_to_text(sections)
    assert "【主题】用户在聊咖啡" in view
    assert "【行为偏好】无" not in view  # "无" 不渲染（视作空段）
    assert "【近期状态】心情不错" in view


def test_strength_handles_naive_datetime_inputs() -> None:
    """S1-P2: asyncpg 默认可能返回 naive datetime；_strength 必须两侧归一化 UTC
    后再做时间差，否则 "can't subtract naive from aware" TypeError。
    """

    naive = _now().replace(tzinfo=None)
    item = MemoryItem(
        layer="L3",
        content="x",
        memory_id="m-naive",
        recall_count=5,
        last_recalled_at=naive,
    )
    # naive now
    result_naive_now = _strength(item, now=_now().replace(tzinfo=None))
    # aware now
    result_aware_now = _strength(item, now=_now())
    assert result_naive_now > 0
    assert result_aware_now > 0
    # 差异应在 ±1s 内（同一 last_recalled_at 与 now 不同 tz 表示）
    assert abs(result_naive_now - result_aware_now) < 0.01


def test_parse_summary_sections_filters_unknown_keys() -> None:
    """S6-P2: parse_summary_sections 必须只接受 4 段 known_keys，其他 key
    一律丢弃（防止 prompt 漂移引入噪声字段污染下游机器消费）。"""
    from thinkback.memory.l2_refresh import parse_summary_sections

    parsed = parse_summary_sections(
        '{"主题": "x", "进行中事项": "y", "行为偏好": "z", "近期状态": "w", '
        '"noise_key": "drop_me", "injected": "drop_me_too"}'
    )
    assert "noise_key" not in parsed
    assert "injected" not in parsed
    assert parsed == {
        "主题": "x",
        "进行中事项": "y",
        "行为偏好": "z",
        "近期状态": "w",
    }


def test_parse_summary_sections_rejects_non_string_values() -> None:
    """S6-P2: 非字符串 value（json 数字/null）必须丢弃，否则下游消费者拿到 "123"/"None" 噪声。"""
    from thinkback.memory.l2_refresh import parse_summary_sections

    parsed = parse_summary_sections('{"主题": 123, "进行中事项": null}')
    assert "主题" not in parsed
    assert "进行中事项" not in parsed


def test_parse_summary_sections_falls_back_to_regex_when_json_only_has_unknown_keys() -> None:
    """S6-P2: JSON 成功但全是噪声字段时，回退到【】regex 而不是丢弃。"""
    from thinkback.memory.l2_refresh import parse_summary_sections

    parsed = parse_summary_sections('{"unknown": "x"}\n【主题】hello\n【行为偏好】foo')
    assert parsed == {"主题": "hello", "行为偏好": "foo"}


def test_touch_recalled_entries_handles_naive_last_recalled_at() -> None:
    """S3-P3: _touch_recalled_entries 接受 naive datetime（asyncpg 默认行为），
    不应抛 can't compare naive/aware。
    """
    from dataclasses import dataclass
    from datetime import UTC, datetime, timedelta

    from thinkback.memory.service import MemoryService

    @dataclass
    class FakeMemory:
        memory_id: str
        last_recalled_at: datetime | None

    # 一些项 naive（来自 PG），一些 aware
    naive = FakeMemory(
        memory_id="m-naive",
        last_recalled_at=(datetime.now(UTC) - timedelta(seconds=7200)).replace(tzinfo=None),
    )
    service = MemoryService(
        repository=__import__(
            "thinkback.memory.repositories", fromlist=["InMemoryMemoryRepository"]
        ).InMemoryMemoryRepository(),
        backend=__import__(
            "thinkback.memory.backends", fromlist=["FakeMemoryBackend"]
        ).FakeMemoryBackend(),
    )
    # 不应抛 TypeError
    service._touch_recalled_entries([naive])  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# 内容质量接入排序键（六维评分落地后）
# ---------------------------------------------------------------------------

LOW_QUALITY = "他最近不太好"
HIGH_QUALITY = "2024 年 3 月我入职了杭州阿里巴巴云智能团队，负责 ToB 业务方向"


def _now() -> datetime:
    return datetime(2026, 10, 5, 12, 0, 0, tzinfo=UTC)


def _l3(content: str, memory_id: str, **kwargs: Any) -> MemoryItem:
    return MemoryItem(layer="L3", content=content, memory_id=memory_id, **kwargs)


def test_same_layer_prefers_higher_content_quality() -> None:
    """同层同强度：内容能被模型接住的先排前面。"""
    low = _l3(LOW_QUALITY, "m-low", recall_count=0)
    high = _l3(HIGH_QUALITY, "m-high", recall_count=0)

    result = dedupe_and_clip([low, high], token_budget=1200, now=_now())

    assert [item.memory_id for item in result] == ["m-high", "m-low"]


def test_layer_priority_still_wins_over_quality() -> None:
    """层优先级不可破：质量差的 L3 仍然压过质量好的 L2。"""
    l2 = MemoryItem(layer="L2", content=HIGH_QUALITY, source="summary-1")
    l3 = _l3(LOW_QUALITY, "m-l3", recall_count=0)

    result = dedupe_and_clip([l2, l3], token_budget=1200, now=_now())

    assert [item.layer for item in result] == ["L3", "L2"]


def test_quality_survives_token_budget_clipping() -> None:
    """预算裁剪发生在排序之后：预算只够一条时，留下的是高质量那条。"""
    low = _l3(LOW_QUALITY, "m-low", recall_count=0)
    high = _l3(HIGH_QUALITY, "m-high", recall_count=0)

    # token 成本 = len(content) // 2：高质量条目成本 18，低质量条目成本 3。
    result = dedupe_and_clip([low, high], token_budget=18, now=_now())

    assert [item.memory_id for item in result] == ["m-high"]


def test_strength_breaks_tie_when_quality_is_equal() -> None:
    """质量相同则强度高的先排（原有 strength 语义保留）。"""
    now = _now()
    fresh = _l3(
        "2024 年 3 月入职杭州阿里巴巴",
        "m-fresh",
        recall_count=20,
        last_recalled_at=now - timedelta(hours=1),
    )
    stale = _l3(
        "2025 年 4 月入职上海字节跳动",
        "m-stale",
        recall_count=1,
        last_recalled_at=now - timedelta(days=30),
    )

    # 两条内容质量必须严格相等，否则这条用例测的就不是"强度仲裁"。
    assert intrinsic_quality(fresh.content) == intrinsic_quality(stale.content)

    result = dedupe_and_clip([stale, fresh], token_budget=1200, now=now)

    assert [item.memory_id for item in result] == ["m-fresh", "m-stale"]


def test_quality_does_not_change_item_count_or_layers() -> None:
    """排序变化不改变返回条数与层集合（预算充足时）。"""
    items = [
        _l3(LOW_QUALITY, "m-low", recall_count=0),
        _l3(HIGH_QUALITY, "m-high", recall_count=0),
        MemoryItem(layer="L1", content="用户：你今天怎么样？", source="round-1"),
    ]

    result = dedupe_and_clip(items, token_budget=1200, now=_now())

    assert len(result) == 3
    assert {item.layer for item in result} == {"L3", "L1"}
