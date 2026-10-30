"""槽位规范化的契约用例（domain/slots/canonical.py 进变异范围后的补齐）。

canonical.py 是「什么算同一条事实」的共用口径：冲突仲裁、去重、落库文本
都拿它的输出当比较输入。口径错了，两条本该合并的记忆就会各自留存 ——
用户改了昵称，旧的还在生效。

这里每条用例都对应变异审计里活下来的一个变异体：事实计数、源侧规范化、
宠物名跨源覆盖、大小写不敏感、以及长度阈值的边界。
"""

from __future__ import annotations

import pytest

from thinkback.domain.slots.canonical import (
    canonical_memory_text,
    canonical_memory_text_from_source,
    count_p0_facts,
    is_correction_text,
    local_index_memory_text,
    source_canonical_memory_text,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("这是一条普通记忆", 0),
        ("叫我小明", 1),
        ("我家养了只猫叫Tom", 1),
        ("我搬到上海了", 1),
        ("工作状态是芯片整合", 1),
        ("沟通偏好是希望得到直接建议", 1),
        ("我的生日是1990年1月1日", 1),
        ("我最喜欢喝美式咖啡", 1),
        ("我接受温和提醒睡觉", 1),
        ("叫我小明，我搬到上海了", 2),
        ("叫我小明，我搬到上海了，我的生日是1990年1月1日", 3),
        (
            "叫我小明，我家养了只猫叫Tom，我搬到上海了，工作状态是芯片整合，"
            "沟通偏好是希望得到直接建议，我的生日是1990年1月1日，"
            "我最喜欢喝美式咖啡，我接受温和提醒睡觉",
            8,
        ),
    ],
)
def test_count_p0_facts_counts_every_matched_slot(text: str, expected: int) -> None:
    """命中的 P0 槽位数量要逐档算准。

    这个数字决定「这条记忆是不是多事实」：多事实保留原文，规范化会把其余
    事实整段吃掉。少算一档 → 生日和雇主跟着 canonical 一起消失；多算一档 →
    单槽位记忆被当成多事实，规范化永远不生效。

    最后那条八槽位齐发的样例是逐档加法的护栏：八档各自 +1，只要有一档
    写成 =1 / -=1 / +=2，总数就凑不出 8。
    """
    assert count_p0_facts(text) == expected


def test_the_sleep_reminder_slot_renders_its_own_canonical_text() -> None:
    """睡觉提醒偏好是一档独立槽位，规范化文本要单独占一条。

    少了这一档，「我接受温和提醒睡觉」会被当成普通记忆：不进槽位 →
    冲突仲裁不认它 → 用户改了提醒偏好，旧的继续生效。
    """
    assert (
        canonical_memory_text("我接受温和提醒睡觉")
        == "User sleep reminder preference: okay with gentle sleep reminders"
    )


def test_the_source_side_canonical_text_is_returned_verbatim() -> None:
    """源文本侧能给出同槽位规范化文本时，必须原样返回它。

    这是「源文本是权威」这条规则的落点：返回 None 的话，调用方只能退回
    去用 mem0 抽出来的噪声文本做比较，源里那条就白留了。
    """
    assert (
        source_canonical_memory_text("User lives in Hangzhou", {"source_text": "我搬到上海了"})
        == "User lives in 上海"
    )


def test_the_source_side_canonical_text_wins_over_the_memory_text() -> None:
    """记忆说杭州、源文本说上海时，比较口径用源文本的上海。

    源文本是原文，mem0 抽出来的是二次加工 —— 拿抽取结果比，就会把
    「用户已经搬到上海」当成「用户还住杭州」。
    """
    assert (
        canonical_memory_text_from_source("User lives in Hangzhou", {"source_text": "我搬到上海了"})
        == "User lives in 上海"
    )


def test_the_source_pet_name_wins_when_the_memory_text_is_also_a_nickname() -> None:
    """记忆文本同时是昵称和宠物时，源文本里的宠物名仍然说了算。

    这条走的是「源侧同槽位对不上、退到宠物名覆盖」那条路：记忆里的宠物名
    可能是抽取噪声，源文本里的才是原文。
    """
    assert (
        canonical_memory_text_from_source(
            "User is addressed as Max and has a cat named Tom",
            {"source_text": "我家养了只猫叫Tommy"},
        )
        == "User has a cat named Tommy"
    )


def test_a_different_pet_kind_in_the_source_does_not_replace_the_memory() -> None:
    """源里是猫、记忆里是狗时，不能拿源里的猫名去覆盖狗。

    覆盖的前提是「两边说的是同一只宠物」：种类不同就是两条事实，覆盖了
    等于把用户没养过的猫写进画像。
    """
    assert (
        canonical_memory_text_from_source(
            "User has a dog named Rex", {"source_text": "我家养了只猫叫Tommy"}
        )
        == "User has a dog named Rex"
    )


def test_a_correction_is_detected_in_any_case() -> None:
    """更正措辞要大小写不敏感：mem0 抽出的文本大小写不固定。

    「CORRECTED」认不出来 → 这条被当成普通记忆 → 被取代的旧值继续躺在
    画像里。
    """
    assert is_correction_text("User CORRECTED her name") is True


def test_the_canonical_loss_threshold_boundary_keeps_the_canonical_text() -> None:
    """canonical 正好短 12 个字符时仍算「没丢事实」，落库用 canonical。

    阈值是「短**超过** 12 个字符」：正好 12 不算丢。把边界写成 >= 会让
    只差 12 个字符的规范化白白丢掉原文。
    """
    original = "User lives in Shanghai" + "!" * 12
    canonical = "User lives in Shanghai"

    assert local_index_memory_text(original, canonical) == canonical
