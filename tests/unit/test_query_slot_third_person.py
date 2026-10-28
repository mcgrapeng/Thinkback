"""提问侧的槽位识别必须认得治理台口径的第三人称问法。

真实栈证据：质量评测的提问都是第三人称（「用户希望你怎么给建议？」），
而沟通偏好的自指判定只认「我」，于是这个提问落不到任何槽位：召回不会做
槽位回填，只在本地索引里的 P0 规范化记忆（沟通偏好：简洁直接的建议）
永远召不回来 —— 用户明明说过，助手却看不到。
"""

from __future__ import annotations

import pytest

from thinkback.domain.slots.query_slots import query_conflict_slots


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("用户希望你怎么给建议？", "communication_preference"),
        ("怎么给我建议比较合适？", "communication_preference"),
        ("用户希望怎么沟通？", "communication_preference"),
        ("用户的沟通偏好是什么？", "communication_preference"),
        ("用户现在住在哪？", "current_location"),
        ("用户的猫叫什么？", "pet_name:cat"),
        ("现在应该怎么称呼用户？", "preferred_nickname"),
    ],
)
def test_query_slots_recognise_third_person_questions(query: str, expected: str) -> None:
    assert expected in query_conflict_slots(query)


def test_meta_question_still_not_a_slot() -> None:
    """元问题（没有自指主语）不该触发槽位：避免把元讨论当用户偏好。"""
    assert "communication_preference" not in query_conflict_slots("how to communicate politely")
