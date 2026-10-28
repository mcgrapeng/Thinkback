"""中文昵称槽位必须认得模型真实产出的措辞。

真实栈证据（zhiman38_27b 抽取）：纠正昵称后，mem0 抽出的旧昵称文本是
「用户喜欢别人叫他阿鹏」——不是「叫我阿鹏」。旧措辞只认「叫我/称呼」，
于是这条记忆拿不到 preferred_nickname 槽位，纠正时不会被 supersede，
旧昵称继续 ACTIVE、继续被召回（真实质量评测因此判失败）。
"""

from __future__ import annotations

import pytest

from thinkback.domain.slots.canonical import memory_conflict_slot
from thinkback.domain.slots.extractors import extract_current_nickname


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # 模型真实产出的第三人称中文措辞
        ("用户喜欢别人叫他阿鹏", "阿鹏"),
        ("用户希望别人叫他小鹏", "小鹏"),
        ("用户喜欢别人叫我豆包", "豆包"),
        # 已有口径不能被破坏
        ("以后请叫我小朋吧，这是我的昵称。", "小朋"),
        ("称呼我阿花呀", "阿花"),
        ("User prefers to be called Xiaopeng", "Xiaopeng"),
    ],
)
def test_extract_current_nickname_handles_third_person_chinese(text: str, expected: str) -> None:
    assert extract_current_nickname(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "用户喜欢别人叫他阿鹏",
        "用户希望别人叫他小鹏",
        "用户喜欢别人叫我豆包",
    ],
)
def test_third_person_chinese_maps_to_nickname_slot(text: str) -> None:
    """同槽位才能被冲突仲裁 supersede。"""
    assert memory_conflict_slot(text) == "preferred_nickname"


def test_plain_call_verb_is_not_a_nickname() -> None:
    """「叫他去开会」不是昵称：没有偏好语境就不能当成槽位事实。"""
    assert extract_current_nickname("叫他去开会") is None
    assert memory_conflict_slot("叫他去开会") is None
