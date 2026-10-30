"""防捏造闸门的两处词表盲区：nickname 与沟通偏好的依据词。

``memory_supported_by_source`` 判据是「记忆里认出的槽位，能不能在**原始
对话文本**里找到依据」。词表少一个词 → 那一类真实事实被静默丢弃；词表多
一个词 → 凭空捏造的事实被放行。两种都是「功能正常、一切绿」的错误。

这里补的是两处**无法用「源文本里只出现一个 marker」隔离**的地方：

- nickname 的 9 个 marker：昵称本身必须出现在源文本里，于是源文本天然
  含有别的 marker（「称呼」⊂「称呼我」），要把一个 marker 单独钉死就得
  换一个只含它自己的源文本。
- 沟通偏好的三个子方向：焦虑方向的记忆里同时有「焦虑」和「拆解」，简洁
  方向同时有「简洁」和「直接」—— 只改其中一个，另一个仍然成立，测不出
  来。要钉死就得让记忆里只出现一个方向词。
"""

from __future__ import annotations

import pytest

from thinkback.domain.slots.support import memory_supported_by_source

#: (记忆文本, 源文本) —— 源文本里只有该 nickname marker 出现（昵称本身另算）。
NICKNAME_CASES = [
    ("User prefers to be called 小明", "叫我小明"),
    ("User prefers to be called 小明", "称呼我小明"),
    ("User prefers to be called 小明", "叫用户小明"),
    ("User prefers to be called 小明", "称呼小明"),
    ("User prefers to be called 小明", "preferred name 小明"),
    ("User prefers to be called 小明", "name should be 小明"),
    ("User prefers to be called 小明", "name is 小明"),
    ("User prefers to be called 小明", "called 小明"),
    ("User prefers to be called 小明", "addressed as 小明"),
]


@pytest.mark.parametrize(("memory_text", "source_text"), NICKNAME_CASES)
def test_each_nickname_marker_is_evidence(memory_text: str, source_text: str) -> None:
    """昵称的每个依据词都要能单独成立。

    少一个 → 用户说过「怎么称呼我」但昵称没进白名单 → /items 里看不到
    自己说过的话；多一个 → 凭空捏造的昵称被放行。
    """
    assert memory_supported_by_source(memory_text, {"source_text": source_text}) is True


def test_a_nickname_without_a_marker_in_the_source_is_not_evidence() -> None:
    """对照组：源里只有昵称、没有依据词 → 不算依据。"""
    assert (
        memory_supported_by_source(
            "User prefers to be called 小明", {"source_text": "小明今天很高兴"}
        )
        is False
    )


#: (记忆文本, 源文本) —— 记忆里只出现该方向词，源文本里也只有它。
COMMUNICATION_DIRECTION_CASES = [
    # 焦虑方向：「焦虑」与「拆解」是两个词，记忆里只出现一个才隔离得开
    ("User communication preference: 我容易焦虑", "我最近很焦虑"),
    ("User communication preference: 需要先拆解", "我需要先拆解"),
    ("User communication preference: I get anxious", "I get anxious"),
    # 简洁方向：「简洁」与「直接」同理
    ("User communication preference: 喜欢简洁", "沟通时请简洁"),
    ("User communication preference: 请直接一点", "沟通时请直接一点"),
    # 安慰方向
    ("User communication preference: 我需要安慰", "我需要安慰"),
    ("User communication preference: I need reassurance", "I need reassurance"),
    ("User communication preference: I need comfort", "I need comfort"),
]


@pytest.mark.parametrize(("memory_text", "source_text"), COMMUNICATION_DIRECTION_CASES)
def test_each_communication_direction_marker_is_evidence(
    memory_text: str, source_text: str
) -> None:
    """沟通偏好的三个子方向，每个方向词都要能单独成为依据。

    方向必须对上：记忆说焦虑，源里只说「简洁」不算依据（另有用例钉住）。
    """
    assert memory_supported_by_source(memory_text, {"source_text": source_text}) is True


#: (记忆文本, 源文本) —— 兜底方向的依据词（记忆里不带任何子方向词）。
COMMUNICATION_FALLBACK_CASES = [
    ("User communication preference: 沟通时说教", "说教"),
    ("User communication preference: communication matters", "communication"),
    ("User communication preference: 安慰", "安慰"),
    ("User communication preference: anxious", "anxious"),
]


@pytest.mark.parametrize(("memory_text", "source_text"), COMMUNICATION_FALLBACK_CASES)
def test_each_communication_fallback_marker_is_evidence(memory_text: str, source_text: str) -> None:
    """兜底方向的依据词：记忆不带子方向词时，源里的这些词仍然算依据。"""
    assert memory_supported_by_source(memory_text, {"source_text": source_text}) is True
