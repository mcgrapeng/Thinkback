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

from datetime import UTC, datetime

from thinkback.domain.entities import JournalEntry
from thinkback.domain.slots.canonical import memory_conflict_slot
from thinkback.domain.slots.query_slots import query_conflict_slot
from thinkback.domain.slots.support import (
    is_same_source_local_p0_memory,
    memory_supported_by_source,
    round_order,
    should_extract_to_l3,
    source_cursor,
    source_cursor_from_refs,
)
from thinkback.memory.repositories import InMemoryMemoryRepository


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


# ---------------------------------------------------------------------------
# 来源排序游标与同源判定（此前整块 0 覆盖）
# ---------------------------------------------------------------------------
#
# round_order / source_cursor 是「哪个来源更新」的仲裁口径：同槽位冲突时
# 事实生效时间最新的胜出、重建时按来源新旧合并，都靠它。这几个函数此前在
# 整个测试树里一次都没出现过 —— 顺序方向写反了（旧的当新的）不会让任何
# 既有���例变红，而线上表现是「用户改了偏好，系统还在用旧的那条」。


def _journal_entry(
    *,
    round_id: str,
    user_id: str = "user-1",
    session_id: str = "session-1",
    source_timestamp: datetime | None = datetime(2026, 9, 7, 10, 0, tzinfo=UTC),
    round_index: int | None = None,
) -> JournalEntry:
    return JournalEntry(
        journal_id=f"j-{round_id}",
        user_id=user_id,
        memory_scope_id=session_id,
        session_id=session_id,
        round_id=round_id,
        messages=[],
        source_timestamp=source_timestamp,  # type: ignore[arg-type]
        round_fingerprint="fp",
        round_index=round_index,
    )


class _Memory:
    """``is_same_source_local_p0_memory`` 只看三个字段的最小替身。"""

    def __init__(self, backend_memory_id: str, memory_text: str, source_refs: list[dict[str, str]]):
        self.backend_memory_id = backend_memory_id
        self.memory_text = memory_text
        self.source_refs = source_refs


# ── 回合全序 ───────────────────────────────────────────────────────────


def test_round_order_prefers_the_caller_supplied_source_timestamp() -> None:
    """调用方手里有来源时间时，以它为准 —— 不看 round_id 猜新旧。

    外部历史回放时库里根本没有这一轮，只能靠调用方给的来源时间定新旧。
    """
    repository = InMemoryMemoryRepository()
    repository.journal_by_round["round-2"] = _journal_entry(round_id="round-2", round_index=2)

    order = round_order(
        repository,
        "user-1",
        "session-1",
        "round-2",
        source_timestamp=datetime(2026, 9, 8, 9, 0, tzinfo=UTC),
    )

    assert order[0] == 4, "给了来源时间就必须用它，不能退回 round_id 猜"


def test_round_order_uses_the_stored_source_timestamp_when_no_caller_one_is_given() -> None:
    repository = InMemoryMemoryRepository()
    repository.journal_by_round["round-2"] = _journal_entry(
        round_id="round-2", source_timestamp=datetime(2026, 9, 7, 10, 0, tzinfo=UTC), round_index=2
    )

    order = round_order(repository, "user-1", "session-1", "round-2")

    assert order[0] == 3


def test_round_order_falls_back_to_the_stored_round_index() -> None:
    """库里这一轮没有来源时间，但有 round_index → 用 round_index 排序。

    这是「有回合但没有时间戳」的那档：没有它就只能退回 round_id 数字，
    而 round_id 的字典序与事实新旧无关。
    """
    repository = InMemoryMemoryRepository()
    repository.journal_by_round["round-2"] = _journal_entry(
        round_id="round-2", source_timestamp=None, round_index=7
    )

    order = round_order(repository, "user-1", "session-1", "round-2")

    assert order[0] == 2
    assert order[1] == 7.0


def test_round_order_falls_back_to_the_round_id_number_when_the_round_is_unknown() -> None:
    repository = InMemoryMemoryRepository()

    order = round_order(repository, "user-1", "session-1", "round-12")

    assert order[0] == 1
    assert order[1] == 12.0


def test_round_order_without_any_number_is_the_lowest_tier() -> None:
    """round_id 里没有数字 → 最低档：谁都比它「新」，但它不会自己新过谁。"""
    repository = InMemoryMemoryRepository()

    order = round_order(repository, "user-1", "session-1", "round-alpha")

    assert order[0] == 0


def test_round_order_ignores_a_round_belonging_to_another_user() -> None:
    """别的用户/别的会话的回合不能拿来当排序依据 —— 那是别人的数据。"""
    repository = InMemoryMemoryRepository()
    repository.journal_by_round["round-2"] = _journal_entry(
        round_id="round-2", user_id="user-2", session_id="session-9", round_index=99
    )

    order = round_order(repository, "user-1", "session-1", "round-2")

    assert order[0] == 1, "别人的回合被当成了排序依据"
    assert order[1] == 2.0


# ── 来源游标 ───────────────────────────────────────────────────────────


def test_source_cursor_picks_the_newest_ref() -> None:
    repository = InMemoryMemoryRepository()

    cursor = source_cursor_from_refs(
        repository,
        "user-1",
        [
            {"session_id": "s1", "round_id": "round-1"},
            {"session_id": "s2", "round_id": "round-2"},
        ],
    )

    assert cursor is not None
    assert cursor[2] == "round-2", "游标必须指向更新的那一轮"


def test_source_cursor_skips_refs_without_a_round_id() -> None:
    """缺 round_id 的引用没法排序 → 跳过，不是崩。

    引用是从外部 metadata 来的，形状不可控；一条脏引用不该让整批失效。
    """
    repository = InMemoryMemoryRepository()

    assert source_cursor_from_refs(repository, "user-1", [{"session_id": "s1"}]) is None
    assert (
        source_cursor_from_refs(repository, "user-1", [{"session_id": "s1", "round_id": "round-1"}])
        is not None
    )


def test_source_cursor_skips_refs_that_are_not_dicts() -> None:
    repository = InMemoryMemoryRepository()

    assert source_cursor_from_refs(repository, "user-1", ["not-a-dict"]) is None  # type: ignore[list-item]


def test_source_cursor_returns_none_without_source_refs() -> None:
    repository = InMemoryMemoryRepository()

    assert source_cursor(repository, "user-1", {}) is None
    assert source_cursor(repository, "user-1", {"source_refs": "not-a-list"}) is None  # type: ignore[dict-item]


def test_source_cursor_ignores_an_unparsable_source_timestamp() -> None:
    """来源时间解析不了 → 退回 round_id 排序，而不是让整批引用失效。"""
    repository = InMemoryMemoryRepository()

    cursor = source_cursor(
        repository,
        "user-1",
        {
            "source_refs": [{"session_id": "s1", "round_id": "round-3"}],
            "source_timestamp": "not-a-timestamp",
        },
    )

    assert cursor is not None
    assert cursor[0][0] == 1, "解析失败时应退回 round_id 档位"


def test_source_cursor_treats_a_naive_source_timestamp_as_utc() -> None:
    repository = InMemoryMemoryRepository()

    cursor = source_cursor(
        repository,
        "user-1",
        {
            "source_refs": [{"session_id": "s1", "round_id": "round-3"}],
            "source_timestamp": "2026-09-07T10:00:00",
        },
    )

    assert cursor is not None
    assert cursor[0][0] == 4, "naive 时间应按 UTC 处理"


# ── 同源判定 ───────────────────────────────────────────────────────────


def test_same_source_memory_requires_the_same_slot() -> None:
    """槽位不同就不是同一条事实的两次提及。"""
    memory = _Memory(
        "local-p0:1",
        "叫我小明就行",
        [{"session_id": "s1", "round_id": "r1"}],
    )

    assert not is_same_source_local_p0_memory(
        memory,
        memory_text="我的生日是1990年1月1日",
        l3_metadata={"source_refs": [{"session_id": "s1", "round_id": "r1"}]},
    )


def test_same_source_memory_requires_the_refs_to_intersect() -> None:
    """槽位相同但来源不同 → 不是同源。"""
    memory = _Memory(
        "local-p0:1",
        "叫我小明就行",
        [{"session_id": "s1", "round_id": "r1"}],
    )

    assert not is_same_source_local_p0_memory(
        memory,
        memory_text="叫我小明就行",
        l3_metadata={"source_refs": [{"session_id": "s2", "round_id": "r2"}]},
    )


def test_same_source_memory_rejects_metadata_without_a_ref_list() -> None:
    """metadata 里没有 source_refs 列表 → 无法判定同源 → 不认。"""
    memory = _Memory(
        "local-p0:1",
        "叫我小明就行",
        [{"session_id": "s1", "round_id": "r1"}],
    )

    assert not is_same_source_local_p0_memory(memory, memory_text="叫我小明就行", l3_metadata={})


def test_same_source_memory_is_recognized() -> None:
    """同槽位 + 来源相交 → 同源。"""
    memory = _Memory(
        "local-p0:1",
        "叫我小明就行",
        [{"session_id": "s1", "round_id": "r1"}],
    )

    assert is_same_source_local_p0_memory(
        memory,
        memory_text="叫我小明就行",
        l3_metadata={"source_refs": [{"session_id": "s1", "round_id": "r1"}]},
    )


def test_a_backend_managed_memory_is_never_a_local_p0_memory() -> None:
    """mem0 后端管的记忆不是本地合成的 P0 行。"""
    memory = _Memory(
        "mem0-abc",
        "叫我小明就行",
        [{"session_id": "s1", "round_id": "r1"}],
    )

    assert not is_same_source_local_p0_memory(
        memory,
        memory_text="叫我小明就行",
        l3_metadata={"source_refs": [{"session_id": "s1", "round_id": "r1"}]},
    )


# ── 抽取闸门与交叉校验的剩余分支 ───────────────────────────────────────


def test_a_query_slot_hit_triggers_extraction() -> None:
    """问句里带槽位（「我住哪」）也要抽 —— 但它不是记忆侧的槽位命中。

    记忆侧判槽位认的是陈述句；问句只走 query 侧。不走这一支的话，
    「我住哪」这种短问句会被长度闸门挡掉，用户的居住地永远抽不出来。
    """
    text = "我住哪"
    assert memory_conflict_slot(text) is None, "这句话不该命中记忆侧槽位"
    assert query_conflict_slot(text) == "current_location"

    assert (
        should_extract_to_l3(messages=_messages(text), metadata=None, allowed_slots=["birthday"])
        is True
    )


def test_communication_preference_needs_anxiety_evidence_in_the_source() -> None:
    """「焦虑时先拆解」这条沟通偏好，源里必须有焦虑/拆解的证据。"""
    memory_text = "沟通偏好：焦虑时先拆解"

    assert memory_supported_by_source(memory_text, _meta("我焦虑的时候希望有人帮我拆解"))
    assert not memory_supported_by_source(memory_text, _meta("今天天气不错"))


def test_sleep_reminder_preference_needs_sleep_evidence_in_the_source() -> None:
    """「接受温和的睡眠提醒」这条偏好，源里必须有睡/提醒的证据。"""
    memory_text = "睡眠提醒：接受温和提醒睡觉"

    assert memory_supported_by_source(memory_text, _meta("我接受温和的睡眠提醒"))
    assert not memory_supported_by_source(memory_text, _meta("今天天气不错"))
