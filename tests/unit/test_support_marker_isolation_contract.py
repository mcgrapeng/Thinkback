"""``domain/slots/support.py`` 的**逐条隔离**用例（support.py 进变异范围后的补齐）。

前面的用例（``test_source_marker_vocabulary.py`` /
``test_source_support_gap_contract.py``）证明的是「每类事实至少有一个依据词」。
变异审计要的是**每一个词**：少一个词 → 那一类真实事实被静默丢弃；
多一个词 → 凭空捏造的事实被放行。两种都是「功能正常、一切绿」的错误。

所以这里的每个用例都让**唯一一个** marker 起作用：源文本里出现它，
别的依据词一个都不出现，且不落在「兜底词表」里 —— 兜底词表能兜住的话，
改掉方向词也看不出差别（那属于「杀不掉的等价变异」，登记成存活）。

同理，判定函数的用例断言**精确值**（排序键的每一档、字节阈值的等号），
而不是「返回 True / False」—— 只断言布尔值，分支写歪了也照样绿。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from thinkback.domain.entities import JournalEntry
from thinkback.domain.slots.support import (
    is_same_source_local_p0_memory,
    memory_supported_by_source,
    round_order,
    should_extract_to_l3,
    source_cursor,
)
from thinkback.memory.repositories import InMemoryMemoryRepository

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _supported(memory_text: str, source_text: str) -> bool:
    return memory_supported_by_source(memory_text, {"source_text": source_text})


def _entry(
    *,
    round_id: str = "r1",
    user_id: str = "user-1",
    session_id: str = "s1",
    source_timestamp: datetime = NOW,
    round_index: int | None = None,
) -> JournalEntry:
    return JournalEntry(
        journal_id=round_id,
        user_id=user_id,
        memory_scope_id=session_id,
        session_id=session_id,
        round_id=round_id,
        messages=[],
        source_timestamp=source_timestamp,
        round_fingerprint="fp",
        round_index=round_index,
    )


def _save(repository: InMemoryMemoryRepository, entry: JournalEntry) -> None:
    repository.journal_by_round[entry.round_id] = entry


# ---------------------------------------------------------------------------
# 沟通偏好：焦虑方向的「记忆侧」词表
#
# 记忆侧词表决定「这条记忆算不算焦虑方向」。改掉其中一个词，那条记忆就会
# 掉进别的分支 —— 而兜底词表里没有 anxiety / break down / breaking down，
# 所以掉进去之后源文本的证据也就用不上了。
# ---------------------------------------------------------------------------

ANXIETY_MEMORY_SIDE_CASES = [
    # (记忆文本, 源文本) —— 源文本里只有源侧词表里的一个词，
    # 且那个词不在兜底词表里（兜底词表能兜住就看不出差别）。
    ("User communication preference: I have anxiety", "I break down sometimes"),
    ("User communication preference: I get anxious", "I break down sometimes"),
    ("User communication preference: I break down a lot", "I have anxiety"),
    ("User communication preference: I am breaking down", "I have anxiety"),
    ("User communication preference: 我容易焦虑", "我需要先拆解一下"),
]


@pytest.mark.parametrize(("memory_text", "source_text"), ANXIETY_MEMORY_SIDE_CASES)
def test_each_anxiety_memory_side_marker_is_the_one_that_decides(
    memory_text: str, source_text: str
) -> None:
    """焦虑方向的**记忆侧**每个词都要能单独把这条记忆归到焦虑档。

    掉档的后果：源文本里的证据被当成别的方向的证据，或者干脆找不到证据，
    用户真说过的话就被静默丢弃。
    """
    assert _supported(memory_text, source_text) is True


# ---------------------------------------------------------------------------
# 沟通偏好：焦虑方向的「源侧」词表
# ---------------------------------------------------------------------------

ANXIETY_SOURCE_SIDE_CASES = [
    ("User communication preference: 我容易焦虑", "I have anxiety"),
    ("User communication preference: 我容易焦虑", "I am breaking down"),
]


@pytest.mark.parametrize(("memory_text", "source_text"), ANXIETY_SOURCE_SIDE_CASES)
def test_each_anxiety_source_side_marker_is_evidence(memory_text: str, source_text: str) -> None:
    """焦虑方向的**源侧**每个词都要能单独作为依据。

    少一个 → 用户在源文本里说的是「anxiety」，系统却认为没依据。
    """
    assert _supported(memory_text, source_text) is True


# ---------------------------------------------------------------------------
# 沟通偏好：简洁方向
# ---------------------------------------------------------------------------

CONCISE_CASES = [
    # 记忆侧的 concise / direct / 简洁
    ("User communication preference: keep it concise", "say it direct"),
    ("User communication preference: be direct", "keep it concise"),
    ("User communication preference: 喜欢简洁", "回答要直接"),
    # 源侧的 concise
    ("User communication preference: 喜欢简洁", "keep it concise"),
]


@pytest.mark.parametrize(("memory_text", "source_text"), CONCISE_CASES)
def test_each_concise_marker_is_the_one_that_decides(memory_text: str, source_text: str) -> None:
    """简洁方向的两个词表里，每一个词都要能单独定方向。

    掉到兜底分支也看不出差别 —— 兜底词表里没有 concise / direct / 简洁 /
    直接，所以源文本里的证据用不上。
    """
    assert _supported(memory_text, source_text) is True


# ---------------------------------------------------------------------------
# 沟通偏好：兜底词表（给建议那一档）
# ---------------------------------------------------------------------------

FALLBACK_CASES = [
    ("User communication preference: 沟通时给建议", "我们沟通一下"),
    ("User communication preference: 沟通时给建议", "我需要一些安慰"),
    ("User communication preference: 沟通时给建议", "我最近很焦虑"),
    ("User communication preference: 沟通时给建议", "I need reassurance"),
    ("User communication preference: 沟通时给建议", "I feel anxious"),
]


@pytest.mark.parametrize(("memory_text", "source_text"), FALLBACK_CASES)
def test_each_fallback_marker_is_evidence(memory_text: str, source_text: str) -> None:
    """兜底方向的每个词都要能单独作为依据。

    兜底是「匹配不上具体方向时」的最后一档；少一个词 → 那一类真实的
    沟通偏好被当成「没依据」，用户说过的话不进 /items。
    """
    assert _supported(memory_text, source_text) is True


# ---------------------------------------------------------------------------
# 宠物：中文 marker 的正则分支
# ---------------------------------------------------------------------------


def test_the_holding_word_branch_alone_is_enough_evidence() -> None:
    """「持有关系词 + marker」这一条命中就成立。

    另外两条分支（``叫X + marker`` 与 ``marker + 叫``）都不命中 —— 源文本
    里没有「叫」。三条分支是**或**的关系：写成「与」的话，只有第一条
    命中时就判不出依据，用户的猫名就被丢掉。
    """
    assert _supported("User has a cat named Tom", "我养了一只猫Tom") is True


def test_a_pet_source_with_a_different_pet_kind_is_not_evidence() -> None:
    """源文本里只有狗 → 猫的记忆不能靠源文本成立。

    这条钉的是「先看源文本里的宠物种类」这条判据：源文本里根本没有猫，
    那就不该判成有依据。
    """
    assert _supported("User has a cat named Tom", "我养了一只狗Tom") is False


def test_the_holding_word_must_be_the_whole_word() -> None:
    """持有关系词是「养了」，不是「养」。

    「养了?」里的可选号要去掉：留着它，「我养猫」也会被判成依据 ——
    而那句话说的是「养」，不是「养了」，语义上不是同一件事。
    """
    assert _supported("User has a cat named Tom", "我养猫Tom") is False


def test_the_holding_word_branch_matches_a_kept_pet() -> None:
    """持有关系词「养了」命中 → 成立。

    与上一条成对：一条钉住「养了」要命中，一条钉住「养」不算命中。
    """
    assert _supported("User has a cat named Tom", "我养了猫Tom") is True


def test_the_call_branch_alone_is_enough_evidence() -> None:
    """「叫X + marker」这一条命中就成立。

    持有关系词那条不命中（源文本里「叫」前面没有持有词），marker + 叫
    那条也不命中（marker 后面不是「叫」）。三条分支是**或**的关系。
    """
    assert _supported("User has a cat named Tom", "他叫小猫咪Tom") is True


def test_the_name_branch_alone_is_enough_evidence() -> None:
    """「marker + 的名字」这一条命中就成立。

    这是三条分支里唯一能命中这个源文本的：marker 后面跟着「的名字」。
    """
    assert _supported("User has a cat named Tom", "猫的名字是Tom") is True


def test_a_source_whose_slot_differs_from_the_memory_is_not_evidence() -> None:
    """源文本讲的是生日，记忆讲的是住址 → 不算依据。

    源文本里能解析出的槽位与记忆的槽位对不上时，这条记忆就是**别的**
    事实 —— 放行它等于凭空造一条「住址」记忆。
    """
    assert _supported("User lives in 杭州", "我的生日是1990年1月1日") is False


def test_a_reassurance_memory_is_not_supported_by_an_unrelated_source() -> None:
    """记忆说「需要安慰」，源文本只说「沟通」→ 不算依据。

    记忆侧的安慰分支有它自己的源侧词表（安慰 / reassurance / comfort）；
    落不到那个表里就不能算依据 —— 兜底分支的词表更宽，不能拿来顶替。
    """
    assert _supported("User communication preference: I need reassurance", "我们沟通一下") is False


# ---------------------------------------------------------------------------
# 排序游标：每一档的**精确值**
# ---------------------------------------------------------------------------


def test_a_round_without_a_number_sorts_by_its_normalized_id() -> None:
    """没有数字后缀的 round_id → 第 0 档，第二位是 0.0，第三位是小写 id。

    第三位是小写而不是大写：比较的是 round_id 本身，大小写不同的两个
    id 必须归一到同一个值，否则同一轮会被当成两轮。
    """
    repository = InMemoryMemoryRepository()

    order = round_order(repository, "user-1", "s1", "round-alpha")

    assert order == (0, 0.0, "round-alpha")


def test_a_numeric_round_id_sorts_by_its_number() -> None:
    """带数字后缀 → 第 1 档，第二位是那个数字。

    第三位带零填充的数字前缀，是为了让「round-9」排在「round-10」前面
    —— 直接比字符串会排反。
    """
    repository = InMemoryMemoryRepository()

    order = round_order(repository, "user-1", "s1", "round-42")

    assert order[0] == 1
    assert order[1] == 42.0
    assert order[2].startswith("00000000000000000042:")


def test_a_round_with_a_source_timestamp_outranks_the_round_id_number() -> None:
    """库里有来源时间 → 第 3 档，第三位带 round_index 的零填充值。

    round_index 缺失时用 -1.0（不是 0 也不是 +1）：负数让「没有序号」的
    回合排在「有序号」的后面。
    """
    repository = InMemoryMemoryRepository()
    _save(repository, _entry(round_id="r1", round_index=None))

    order = round_order(repository, "user-1", "s1", "r1")

    assert order[0] == 3
    assert order[1] == NOW.timestamp()
    assert order[2].startswith(f"{-1.0:020.6f}:")


def test_the_round_index_is_part_of_the_source_timestamp_tier() -> None:
    """库里有来源时间**且**有 round_index → 第三位带的是真的 round_index。

    与上一条成对：round_index 缺失时用 -1.0，有值时必须用真值 ——
    写成「一律 -1.0」的话，有序号的回合与没序号的回合就分不出先后了。
    """
    repository = InMemoryMemoryRepository()
    _save(repository, _entry(round_id="r1", round_index=7))

    order = round_order(repository, "user-1", "s1", "r1")

    assert order[0] == 3
    assert order[2].startswith(f"{7.0:020.6f}:")


def test_the_round_index_is_used_when_the_source_timestamp_is_missing() -> None:
    """库里有轮次但没有来源时间 → 第 2 档，第二位是 round_index。"""
    repository = InMemoryMemoryRepository()
    entry = _entry(round_id="r1", round_index=7)
    entry.source_timestamp = None  # type: ignore[assignment]
    _save(repository, entry)

    order = round_order(repository, "user-1", "s1", "r1")

    assert order[0] == 2
    assert order[1] == 7.0


def test_a_round_of_another_user_does_not_use_the_library_entry() -> None:
    """库里的轮次属于别人 → 不得借用它的来源时间。

    借了的话，别人的轮次会把这一轮顶到「最新」，仲裁结果直接错。
    """
    repository = InMemoryMemoryRepository()
    _save(repository, _entry(round_id="r1", user_id="other-user", round_index=7))

    order = round_order(repository, "user-1", "s1", "r1")

    # 第 1 档（round_id 里的数字），不是第 3 档（库内来源时间）
    assert order[0] == 1
    assert order[1] == 1.0


# ---------------------------------------------------------------------------
# 抽取前的廉价闸门：字节阈值
# ---------------------------------------------------------------------------


def test_a_message_exactly_at_the_byte_threshold_is_extracted() -> None:
    """正好 24 字节 → 放行。

    阈值写成 ``>`` 的话，恰好到阈值的一轮会被丢掉 —— 用户明确说了一件
    有价值的事，却因为长度「刚好不够」没进 L3。
    """
    messages = [{"role": "user", "content": "x" * 24}]

    assert (
        should_extract_to_l3(messages=messages, metadata=None, allowed_slots=["birthday"]) is True
    )


def test_a_message_below_the_byte_threshold_is_not_extracted() -> None:
    """23 字节 → 不放行（闸门开着时也要守阈值）。"""
    messages = [{"role": "user", "content": "x" * 23}]

    assert (
        should_extract_to_l3(messages=messages, metadata=None, allowed_slots=["birthday"]) is False
    )


def test_the_user_messages_are_joined_with_a_single_space() -> None:
    """多条 user 消息之间只隔一个空格 —— 拼接符变了会让字节数跟着变。

    两条 11 字节的消息：正常拼接 23 字节（不够阈值），拼接符变长就够阈值了。
    """
    messages = [
        {"role": "user", "content": "x" * 11},
        {"role": "user", "content": "y" * 11},
    ]

    assert (
        should_extract_to_l3(messages=messages, metadata=None, allowed_slots=["birthday"]) is False
    )


def test_a_message_without_content_counts_as_empty() -> None:
    """没有 content 字段的消息按**空**算，不按 "None" 算。

    按 "None" 算会给字节数凭空加 4 —— 阈值附近的判断跟着漂。
    """
    messages = [
        {"role": "user", "content": "x" * 22},
        {"role": "user"},
    ]

    assert (
        should_extract_to_l3(messages=messages, metadata=None, allowed_slots=["birthday"]) is False
    )


def test_only_user_messages_count_towards_the_threshold() -> None:
    """只有 user 角色的消息进源文本 —— 助手的话不算用户的输入。"""
    messages = [
        {"role": "user", "content": "x" * 10},
        {"role": "assistant", "content": "y" * 50},
    ]

    assert (
        should_extract_to_l3(messages=messages, metadata=None, allowed_slots=["birthday"]) is False
    )


# ---------------------------------------------------------------------------
# 同源判定：既有记忆缺字段时的兜底
# ---------------------------------------------------------------------------


class _BareMemory:
    """一个**没有**任何可选字段的既有记忆。"""

    backend_memory_id = "local-p0:1"


class _NoBackendIdMemory:
    """一个连 backend_memory_id 都没有的既有记忆。"""

    memory_text = "User lives in 杭州"
    source_refs: list[dict[str, str]] = []


def test_a_memory_without_a_backend_id_is_not_a_local_p0_memory() -> None:
    """缺 backend_memory_id 的记忆不是本地 P0 记忆 → 不是同源。

    缺字段时应当按「没有本地 ID」处理，而不是崩掉或把别的值当 ID。
    """
    memory = _NoBackendIdMemory()

    assert (
        is_same_source_local_p0_memory(
            memory,
            memory_text="User lives in 杭州",
            l3_metadata={"source_refs": [{"session_id": "s1", "round_id": "r1"}]},
        )
        is False
    )


def test_a_memory_without_a_text_is_not_the_same_source() -> None:
    """既有记忆没有 memory_text → 槽位取不到 → 不是同源。

    缺字段时把默认值当成「有内容」去比，会把不相干的记忆判成同源，
    新事实就被跳过写入了。
    """
    memory = _BareMemory()

    assert (
        is_same_source_local_p0_memory(
            memory,
            memory_text="User lives in 杭州",
            l3_metadata={"source_refs": [{"session_id": "s1", "round_id": "r1"}]},
        )
        is False
    )


def test_a_memory_without_source_refs_is_not_the_same_source() -> None:
    """既有记忆没有 source_refs → 没有可相交的引用 → 不是同源。"""
    memory = _BareMemory()
    memory.memory_text = "User lives in 杭州"  # type: ignore[attr-defined]

    assert (
        is_same_source_local_p0_memory(
            memory,
            memory_text="User lives in 杭州",
            l3_metadata={"source_refs": [{"session_id": "s1", "round_id": "r1"}]},
        )
        is False
    )


# ---------------------------------------------------------------------------
# 来源游标：user_id 参与库内轮次查找
# ---------------------------------------------------------------------------


def test_the_cursor_uses_the_library_entry_of_the_given_user() -> None:
    """游标要按 user_id 查库里的轮次 —— 查别人的轮次会把排序档位抬高。

    档位从 0（无库内轮次）抬到 3（用库内来源时间）意味着这一轮被当成
    「比库里更新」，同槽位仲裁会选错。
    """
    repository = InMemoryMemoryRepository()
    _save(repository, _entry(round_id="r1", user_id="user-1", round_index=3))

    cursor = source_cursor(
        repository,
        "user-1",
        {"source_refs": [{"session_id": "s1", "round_id": "r1"}]},
    )

    assert cursor is not None
    assert cursor[0][0] == 3


# ---------------------------------------------------------------------------
# 兜底放行：认得出是 P0 槽位、但抽不出具体值的那一类
# ---------------------------------------------------------------------------


def test_a_nickname_slot_without_an_extractable_value_is_still_allowed() -> None:
    """槽位认得出、值抽不出 → **放行**，不是拒。

    ``memory_supported_by_source`` 的一串交叉校验只覆盖有具体取值的
    槽位（生日 / 住址 / 工作状态 / 沟通偏好 / 饮品 / 食物 / 睡眠提醒）。
    昵称与宠物名走的是**抽取器自身**那条路：抽不出值就不进那两段校验，
    于是流程落到函数末尾的兜底 ``return True``。

    兜底写成 ``return False`` 会把这一整类记忆静默丢掉：模型抽出
    「用户的 preferred name 还待确认」这类**提到了槽位但没有值**的记忆，
    进了本地索引就被抹掉（/items 看不到），而源文本里明明有。

    「认得出槽位」与「抽得出值」是两件事：前者决定这条记忆**要不要
    跟源文本对账**，对不上就拒；后者只影响要不要走具体槽位的校验。
    """
    assert _supported("以后叫我（还没定）", "some source text") is True
    assert _supported("changed preferred name", "some source text") is True
