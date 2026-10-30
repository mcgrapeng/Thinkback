"""防捏造闸门的**依据词表**：每个 marker 都要能独立成为证据。

``domain/slots/support.py`` 的 ``memory_supported_by_source`` 判据是
「记忆里认出的槽位，能不能在**原始对话文本**里找到依据」。依据词表就是
这套判据的规格：

- 词表里少一个词 → 那一类真实事实被静默丢弃（用户在 /items 里看不到自己说过的话）；
- 词表里多一个词 → 凭空捏造的事实被放行（用户会当真）。

两种都是「功能正常、一切绿」的错误，只能靠**逐词钉死**杀掉。

每条用例让**唯一一个** marker 出现在源文本里：这样改坏任何一个 marker，
对应的那条用例立刻不成立。词表冗余的条目（``live`` ⊃ ``lived``、
``工作`` ⊂ ``换工作``）无法隔离 —— 它们是**等价变异**，登记为存活。
"""

from __future__ import annotations

import pytest

from thinkback.domain.slots.support import memory_supported_by_source


def _supported(memory_text: str, source_text: str) -> bool:
    return memory_supported_by_source(memory_text, {"source_text": source_text})


# ---------------------------------------------------------------------------
# 宠物：ASCII marker（整词匹配）与中文 marker（持有关系词前缀）
# ---------------------------------------------------------------------------

#: (记忆文本, 源文本) —— 源文本里只有该 kind 的一个 ASCII marker 出现。
PET_ASCII_CASES = [
    ("User has a cat named Tom", "Tom saw a cat on the street today"),
    ("User has a dog named Tom", "Tom walks a dog every evening"),
    ("User has a bird named Tom", "Tom watches a bird in the park"),
    ("User has a bird named Tom", "Tom saw a parrot at the zoo"),
    ("User has a rabbit named Tom", "Tom saw a rabbit in the field"),
    ("User has a rabbit named Tom", "Tom saw a bunny in the field"),
]

#: (记忆文本, 源文本) —— 中文 marker 前带持有关系词（「我有一只…」）。
PET_CJK_CASES = [
    ("User has a cat named Tom", "我有一只猫，Tom 是朋友"),
    ("User has a dog named Tom", "我有一只狗，Tom 是朋友"),
    ("User has a bird named Tom", "我有一只鸟，Tom 是朋友"),
    ("User has a bird named Tom", "我有一只鹦鹉，Tom 是朋友"),
    ("User has a rabbit named Tom", "我有一只兔，Tom 是朋友"),
    ("User has a rabbit named Tom", "我有一只兔子，Tom 是朋友"),
]


@pytest.mark.parametrize(("memory_text", "source_text"), PET_ASCII_CASES)
def test_each_ascii_pet_marker_is_evidence(memory_text: str, source_text: str) -> None:
    """英文宠物依据词：cat / dog / bird / parrot / rabbit / bunny 各自都算依据。"""

    assert _supported(memory_text, source_text) is True


@pytest.mark.parametrize(("memory_text", "source_text"), PET_CJK_CASES)
def test_each_chinese_pet_marker_is_evidence(memory_text: str, source_text: str) -> None:
    """中文宠物依据词：猫 / 狗 / 鸟 / 鹦鹉 / 兔 / 兔子 各自都算依据。"""

    assert _supported(memory_text, source_text) is True


def test_a_pet_source_without_any_pet_marker_is_not_evidence() -> None:
    """对照组：源里没有宠物词 → 不算依据（否则这张表就是摆设）。"""

    assert _supported("User has a cat named Tom", "今天 Tom 很忙") is False


# ---------------------------------------------------------------------------
# 槽位依据词：每个 slot 的 marker 词表
# ---------------------------------------------------------------------------

#: (slot 记忆文本, [(marker, 源文本), ...]) —— 每个源文本里只出现该 marker。
SLOT_MARKER_CASES: list[tuple[str, list[tuple[str, str]]]] = [
    (
        "User birthday: 1990-01-01",
        [
            ("生日", "我的生日是1990年1月1日"),
            ("birthday", "my birthday is 1990-01-01"),
        ],
    ),
    (
        "User lives in 杭州",
        [
            ("住", "我住在杭州"),
            ("搬", "我搬到杭州"),
            ("城市", "我的城市是杭州"),
            ("location", "my location is Hangzhou"),
            ("live", "I live in Hangzhou"),
            ("moving", "I am moving to Hangzhou"),
            ("moved", "I moved to Hangzhou"),
            ("relocated", "I relocated to Hangzhou"),
        ],
    ),
    (
        "User current work status: 在职",
        [
            ("工作", "我最近工作很忙"),
            ("offer", "I got an offer"),
            ("job", "my new job is here"),
            ("moonshot", "at moonshot now"),
        ],
    ),
    (
        "User favorite drink: 咖啡",
        [
            ("饮品", "我平时的饮品是美式"),
            ("饮料", "我平时的饮料是美式"),
            ("喝", "我每天喝美式"),
            ("咖啡", "我最爱美式咖啡"),
            ("茶", "我最爱绿茶"),
            ("drink", "my drink of choice is espresso"),
            ("beverage", "my beverage of choice is espresso"),
            ("coffee", "my morning coffee is espresso"),
            ("tea", "my afternoon tea is green"),
        ],
    ),
    (
        "User favorite food: 川菜",
        [
            ("食物", "我的食物偏好是川菜"),
            ("吃", "我喜欢吃川菜"),
            ("food", "my food preference is Sichuan"),
        ],
    ),
    (
        "User sleep reminder preference: 睡前提醒",
        [
            ("睡", "我睡觉前要有人陪我"),
            ("提醒", "我希望有人提醒我"),
            ("sleep", "I need a sleep nudge"),
            ("reminder", "I need a reminder"),
        ],
    ),
]


@pytest.mark.parametrize(
    ("memory_text", "marker", "source_text"),
    [
        (memory_text, marker, source_text)
        for memory_text, pairs in SLOT_MARKER_CASES
        for marker, source_text in pairs
    ],
)
def test_each_slot_marker_is_evidence(memory_text: str, marker: str, source_text: str) -> None:
    """每个 slot 的每个依据词都必须能独立撑起这条记忆。"""

    assert _supported(memory_text, source_text) is True, f"marker {marker!r} 不再被当作依据"


# ---------------------------------------------------------------------------
# 沟通偏好：三个子方向各自的依据词
# ---------------------------------------------------------------------------

COMMUNICATION_CASES = [
    # 焦虑方向：记忆说「焦虑 / 拆解」，源里出现对应词才成立
    ("User communication preference: 我容易焦虑，需要先拆解", "我最近很焦虑，希望先拆解"),
    ("User communication preference: 我容易焦虑，需要先拆解", "我最近很anxious"),
    ("User communication preference: 我容易焦虑，需要先拆解", "我经常break down"),
    # 简洁方向
    ("User communication preference: 喜欢简洁直接的沟通", "沟通时请简洁"),
    ("User communication preference: 喜欢简洁直接的沟通", "请direct一点"),
    # 安慰方向
    ("User communication preference: 需要安慰和安抚", "我需要安慰"),
    ("User communication preference: 需要安慰和安抚", "I need reassurance"),
    ("User communication preference: 需要安慰和安抚", "I need comfort"),
    # 兜底方向：给建议
    ("User communication preference: 沟通时给建议", "沟通时给建议"),
    ("User communication preference: 沟通时给建议", "给点建议"),
    ("User communication preference: 沟通时给建议", "give me advice"),
    ("User communication preference: 沟通时给建议", "give me suggestions"),
]


@pytest.mark.parametrize(("memory_text", "source_text"), COMMUNICATION_CASES)
def test_each_communication_marker_is_evidence(memory_text: str, source_text: str) -> None:
    """沟通偏好的三个子方向与兜底方向的依据词。"""

    assert _supported(memory_text, source_text) is True


def test_a_communication_preference_without_a_matching_marker_is_not_evidence() -> None:
    """对照组：源里与该方向无关 → 不算依据。"""

    assert _supported("User communication preference: 喜欢简洁直接的沟通", "今天天气不错") is False


def test_an_anxiety_memory_is_not_supported_by_a_concise_source() -> None:
    """方向必须对上：记忆说焦虑，源里只说「简洁」不算依据。"""

    assert (
        _supported("User communication preference: 我容易焦虑，需要先拆解", "请简洁一点") is False
    )


def test_a_concise_memory_is_not_supported_by_an_anxiety_source() -> None:
    assert _supported("User communication preference: 喜欢简洁直接的沟通", "我最近很焦虑") is False
