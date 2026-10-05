"""S5 regression tests: should_extract_to_l3 pre-write filter (G2 路线图).

闸门策略：
- 默认（allowed_slots 空）→ 不闸门，所有轮都送 mem0（向后兼容）
- 闸门启用（allowed_slots 非空）→ slot 命中 OR 长消息 OR force_extract 才送
- 闸门启用 + 非上述条件 → 只落 L1/L2，避免 mem0 沉淀不可召回的暗数据
"""

from __future__ import annotations

from thinkback.domain.slots.support import should_extract_to_l3


def test_default_passthrough_when_allowed_slots_empty() -> None:
    """S5 向后兼容：allowed_slots 空时不闸门，所有轮都送 mem0。"""
    short_msg = [{"role": "user", "content": "我喜欢猫"}]
    assert should_extract_to_l3(messages=short_msg, metadata=None) is True
    assert should_extract_to_l3(messages=short_msg, metadata=None, allowed_slots=None) is True
    assert should_extract_to_l3(messages=short_msg, metadata=None, allowed_slots=[]) is True


def test_force_extract_metadata_overrides_filter() -> None:
    """S5: force_extract metadata 始终 True，不论 allowlist 与长度。"""
    assert (
        should_extract_to_l3(
            messages=[{"role": "user", "content": "x"}],
            metadata={"force_extract": True},
            allowed_slots=["preferred_nickname"],
        )
        is True
    )


def test_allowlist_blocks_short_unrelated_message() -> None:
    """S5: allowlist 启用 + 短消息 + 无 slot 命中 → 闸门拒绝。"""
    assert (
        should_extract_to_l3(
            messages=[{"role": "user", "content": "我喜欢猫"}],
            metadata=None,
            allowed_slots=["preferred_nickname"],
        )
        is False
    )


def test_allowlist_allows_slot_match() -> None:
    """S5: allowlist 启用 + slot 命中（"我的狗叫豆包"） → 闸门放行。"""
    assert (
        should_extract_to_l3(
            messages=[{"role": "user", "content": "我的狗叫豆包"}],
            metadata=None,
            allowed_slots=["preferred_nickname"],
        )
        is True
    )


def test_allowlist_allows_long_message_even_without_slot() -> None:
    """S5: allowlist 启用 + 长消息 → 闸门放行（>= 24 字节启发式）。"""
    long_msg = "我昨天和朋友去了西湖边上的星巴克点了拿铁咖啡味道不错推荐"
    assert (
        should_extract_to_l3(
            messages=[{"role": "user", "content": long_msg}],
            metadata=None,
            allowed_slots=["preferred_nickname"],
        )
        is True
    )
