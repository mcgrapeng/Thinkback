"""P0 记忆的「源文本交叉校验」闸门。

``domain/slots/support.py`` 是防捏造的最后一道确定性闸门：mem0 抽出的
L3 事实要进本地可见表（``/items``）之前，必须能在**原始对话文本**里
找到依据。判据松了 → 模型凭空写出的「用户养了一只猫」会出现在用户
的可见记忆里（用户会当真）；判据紧了 → 真事实被丢 silently。

这些分支此前几乎没被覆盖（90% 行覆盖，缺的正是各 slot 的交叉校验）。

另外两个已修过的真实缺陷也在这里钉住：

- **H-1**：``"cat"`` 不能再命中 ``"catastrophe"``、``"猫"`` 不能再命中
  ``"招财猫"`` —— 裸 substring 匹配会把无关文本当成依据。
- **M-2**：``"system nickname policy"`` 这类元问题不应被当成
  「用户偏好昵称」的槽位证据。
"""

from __future__ import annotations

from thinkback.domain.slots.support import (
    memory_supported_by_source,
    should_extract_to_l3,
)


def _meta(source_text: str) -> dict[str, object]:
    return {"source_text": source_text}


def _messages(content: str) -> list[dict[str, object]]:
    return [{"role": "user", "content": content}]


# ---------------------------------------------------------------------------
# 抽取前的廉价过滤
# ---------------------------------------------------------------------------


def test_force_extract_metadata_always_extracts() -> None:
    """显式强制优先于一切闸门 —— 调用方说了要抽，就抽。"""
    assert (
        should_extract_to_l3(
            messages=_messages("短"),
            metadata={"force_extract": True},
            allowed_slots=["birthday"],
        )
        is True
    )


def test_an_empty_slot_list_disables_the_gate() -> None:
    """白名单留空 = 闸门关闭 = 所有轮都过 mem0（向后兼容）。"""
    assert should_extract_to_l3(messages=_messages("短"), metadata=None) is True
    assert should_extract_to_l3(messages=_messages("短"), metadata=None, allowed_slots=[]) is True


def test_a_slot_hit_extracts_even_when_short() -> None:
    assert (
        should_extract_to_l3(
            messages=_messages("我的生日是1990年1月1日"),
            metadata=None,
            allowed_slots=["birthday"],
        )
        is True
    )


def test_a_long_message_extracts_even_without_a_slot() -> None:
    """长消息（约 24 字节以上）也可能带出 P0 事实，不靠 slot 也要抽。"""
    long_text = "这是一段比较长的对话内容用来触发长度闸门"
    assert (
        should_extract_to_l3(
            messages=_messages(long_text),
            metadata=None,
            allowed_slots=["birthday"],
        )
        is True
    )


def test_a_short_message_without_a_slot_is_not_extracted() -> None:
    """闸门启用时，短消息且无槽位 → 不进 mem0（只落 L1/L2）。"""
    assert (
        should_extract_to_l3(
            messages=_messages("好的"),
            metadata=None,
            allowed_slots=["birthday"],
        )
        is False
    )


def test_only_user_messages_count_toward_the_length_gate() -> None:
    """assistant 的话不是「用户说过的事」，不该单独触发抽取。"""
    assert (
        should_extract_to_l3(
            messages=[
                {"role": "assistant", "content": "这是一段比较长的回答内容用来触发长度闸门"},
            ],
            metadata=None,
            allowed_slots=["birthday"],
        )
        is False
    )


# ---------------------------------------------------------------------------
# 源文本交叉校验
# ---------------------------------------------------------------------------


def test_a_memory_without_source_text_is_accepted() -> None:
    """没有源文本就没法交叉校验 —— 不因此把记忆丢掉。"""
    assert memory_supported_by_source("User birthday: 1990-01-01", {}) is True


def test_a_slot_outside_the_whitelist_is_rejected() -> None:
    """白名单只放行列出的 slot —— 其它记忆仍写 mem0，但不入本地可见表。"""
    assert (
        memory_supported_by_source(
            "User birthday: 1990-01-01",
            _meta("我的生日是1990年1月1日"),
            allowed_slots=["current_location"],
        )
        is False
    )


def test_a_birthday_memory_needs_a_birthday_mention_in_the_source() -> None:
    assert memory_supported_by_source("User birthday: 1990-01-01", _meta("我的生日是1990年1月1日"))
    assert not memory_supported_by_source("User birthday: 1990-01-01", _meta("今天天气不错"))


def test_a_location_memory_needs_a_location_mention_in_the_source() -> None:
    assert memory_supported_by_source("User lives in 杭州", _meta("我现在住在杭州"))
    assert not memory_supported_by_source("User lives in 杭州", _meta("今天天气不错"))


def test_a_work_status_memory_needs_a_work_mention_in_the_source() -> None:
    assert memory_supported_by_source("User current work status: 在职", _meta("我最近换了工作"))
    assert not memory_supported_by_source("User current work status: 在职", _meta("今天天气不错"))


def test_a_drink_preference_needs_a_drink_mention_in_the_source() -> None:
    assert memory_supported_by_source("User favorite drink: 咖啡", _meta("我平时喜欢喝美式咖啡"))
    assert not memory_supported_by_source("User favorite drink: 咖啡", _meta("今天天气不错"))


def test_a_food_preference_needs_a_food_mention_in_the_source() -> None:
    assert memory_supported_by_source("User favorite food: 川菜", _meta("我最爱吃川菜"))
    assert not memory_supported_by_source("User favorite food: 川菜", _meta("今天天气不错"))


def test_a_sleep_reminder_needs_a_sleep_mention_in_the_source() -> None:
    assert memory_supported_by_source(
        "User sleep reminder preference: 睡前提醒", _meta("我希望有人提醒我睡觉")
    )
    assert not memory_supported_by_source(
        "User sleep reminder preference: 睡前提醒", _meta("今天天气不错")
    )


def test_a_communication_preference_needs_a_matching_marker() -> None:
    """沟通偏好的方向要对上：记忆说「要直接建议」，源里也是直接建议。"""
    assert memory_supported_by_source(
        "User communication preference: 喜欢直接建议", _meta("沟通时希望直接给建议")
    )
    assert not memory_supported_by_source(
        "User communication preference: 喜欢直接建议", _meta("沟通时希望先安慰我")
    )


def test_a_reassurance_preference_needs_a_reassurance_mention() -> None:
    assert memory_supported_by_source(
        "User communication preference: 喜欢安慰", _meta("我难受的时候希望有人安慰我")
    )
    assert not memory_supported_by_source(
        "User communication preference: 喜欢安慰", _meta("今天天气不错")
    )


# ---------------------------------------------------------------------------
# H-1：宠物名匹配必须带边界
# ---------------------------------------------------------------------------


def test_the_english_word_cat_does_not_match_catastrophe() -> None:
    """``catastrophe`` 里没有「猫」—— 裸 substring 会误判成依据。"""
    assert not memory_supported_by_source(
        "User has a cat named Tom", _meta("the catastrophe in the news")
    )


def test_the_chinese_character_for_cat_does_not_match_a_decoration_word() -> None:
    """「招财猫」不是「养了一只猫」—— 中文裸 substring 会误判。"""
    assert not memory_supported_by_source("User has a cat named 招财", _meta("我买了个招财猫摆件"))


def test_a_real_pet_mention_is_accepted() -> None:
    assert memory_supported_by_source("User has a cat named 咪咪", _meta("我家养了只猫叫咪咪"))


def test_the_pet_kind_must_match_the_source() -> None:
    """源里是狗，记忆说是猫 —— 种类对不上不算依据。"""
    assert not memory_supported_by_source("User has a cat named Tom", _meta("我养了只狗叫Tom"))


def test_a_nickname_needs_both_the_name_and_a_nickname_marker() -> None:
    assert memory_supported_by_source("User prefers to be called 小明", _meta("叫我小明就行"))
    assert not memory_supported_by_source(
        "User prefers to be called 小明", _meta("小明是我同事的名字")
    )


def test_a_meta_question_is_not_evidence_for_a_nickname() -> None:
    """M-2：元问题（系统怎么称呼用户）不是用户偏好昵称的证据。"""
    assert not memory_supported_by_source(
        "User prefers to be called 小明", _meta("system nickname policy")
    )
