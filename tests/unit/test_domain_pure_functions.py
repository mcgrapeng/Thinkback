"""领域纯函数契约：输出格式/摘要算法/排序键的精确行为。

变异测试审计（mutmut）发现这些纯函数此前只被间接覆盖，等价变异大量存活。
指纹会随行持久化（幂等判断），摘要格式是 L2/UI 契约——都必须钉死。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tests.unit.test_memory_service import make_append
from thinkback.domain.entities import JournalEntry
from thinkback.domain.keys import fingerprint, request_fingerprint, round_sort_key
from thinkback.domain.slots.canonical import (
    canonical_memory_text,
    canonical_memory_text_from_source,
    count_p0_facts,
    is_correction_text,
    local_index_memory_text,
    memory_conflict_slot,
    p0_canonical_memories_from_source,
)
from thinkback.domain.summarization import (
    StructuredSummary,
    build_structured_summary_user_prompt,
    sections_to_text,
    summarize_rounds,
)


def _entry(round_id: str, *, round_index: int | None = None) -> JournalEntry:
    return JournalEntry(
        journal_id=f"journal-{round_id}",
        user_id="user-1",
        memory_scope_id="session-1",
        session_id="session-1",
        round_id=round_id,
        round_index=round_index,
        messages=[{"role": "user", "content": f"内容-{round_id}"}],
        source_timestamp=datetime(2026, 5, 4, 10, 0, 0, tzinfo=UTC),
        round_fingerprint="fp",
    )


def test_local_index_memory_text_keeps_original_when_canonical_collapses_content() -> None:
    """canonical 只命中一个槽位、却折叠掉大量内容时，落库文本必须保留原文。

    回归缺陷（Task 0.5）：只用「命中多个 P0 槽位」判定多事实会漏掉这条 ——
    「搬到杭州 + 2024 年去阿里巴巴上班 + 离开老团队」只命中 location 一个槽位，
    但 canonical 把它截成「User lives in Hangzhou」，雇主、年份、团队全丢。
    """
    original = "User moved to Hangzhou for a new job at Alibaba in 2024, leaving her old team."
    canonical = canonical_memory_text(original)

    assert canonical == "User lives in Hangzhou"
    assert local_index_memory_text(original, canonical) == original


def test_local_index_memory_text_prefers_correction_canonical_over_original() -> None:
    """更正事件必须用 canonical —— 丢的是陈旧值，留着会让旧值继续躺在画像里。"""
    original = "User moved from Hangzhou to Shanghai"
    canonical = canonical_memory_text(original)

    assert canonical == "User lives in Shanghai"
    assert local_index_memory_text(original, canonical) == canonical


def test_local_index_memory_text_prefers_source_derived_canonical() -> None:
    """源文本侧给出的 canonical 更权威 —— mem0 抽取文本里的同槽位噪声应丢掉。"""
    original = "User communication preference: to have anxiety situations handled slowly"
    canonical = "User communication preference: 简洁直接的建议"

    assert local_index_memory_text(original, canonical, canonical_from_source=True) == canonical


def test_local_index_memory_text_keeps_multi_fact_even_when_it_sounds_like_a_correction() -> None:
    """既多事实又带更正措辞时，必须保留原文 —— 更正判据不能压过多事实判据。

    回归缺陷：更正判据排在前面时，这条会落成 canonical，
    生日、雇主、年份一起丢（不可逆）。
    """
    original = (
        "User birthday is March 3rd, and she moved from Hangzhou to Shanghai "
        "for a new job at Alibaba in 2024"
    )
    canonical = canonical_memory_text(original)

    assert count_p0_facts(original) > 1
    assert is_correction_text(original)
    assert local_index_memory_text(original, canonical) == original


def test_local_index_memory_text_uses_canonical_when_nothing_is_lost() -> None:
    """单槽位记忆且 canonical 没吃掉内容时，落库用 canonical（模板形式更准）。"""
    original = "User favorite drink is tea"
    canonical = canonical_memory_text(original)

    assert canonical != original
    assert local_index_memory_text(original, canonical) == canonical


def test_local_index_memory_text_keeps_multi_fact_original() -> None:
    """命中多个 P0 槽位的原文必须原样保留。"""
    original = "User's birthday is March 3rd, and her sister Lily lives in Boston."
    canonical = canonical_memory_text(original)

    assert local_index_memory_text(original, canonical) == original


class TestFingerprint:
    def test_digest_is_stable_and_pinned(self) -> None:
        """指纹算法是 canonical JSON（sort_keys + 紧凑分隔符）。

        旧的 `|` 拼接算法会让内容里的 `|` 参与编码，两组不同消息可能撞 hash；
        改成 canonical JSON 后编码不含分隔符歧义。旧 hash 会失效（服务未上线，
        无生产数据需要迁移）。
        """
        import hashlib
        import json

        def expected(messages: list[dict[str, str]]) -> str:
            payload = json.dumps(
                messages, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            return hashlib.sha256(payload.encode("utf-8")).hexdigest()

        assert fingerprint([{"role": "user", "content": "a"}]) == expected(
            [{"role": "user", "content": "a"}]
        )
        assert fingerprint(
            [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
        ) == expected([{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}])

    def test_message_boundaries_and_content_sensitivity(self) -> None:
        one = fingerprint([{"role": "user", "content": "a"}])
        split = fingerprint(
            [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
        )
        assert one != split
        assert fingerprint([{"role": "user", "content": "ab"}]) != one
        assert fingerprint([{"role": "assistant", "content": "a"}]) != one

    def test_request_fingerprint_uses_role_and_content_only(self) -> None:
        base = make_append(round_id="round-1", content="hi")
        renamed = make_append(round_id="round-2", content="hi")
        changed = make_append(round_id="round-1", content="bye")
        assert request_fingerprint(base) == request_fingerprint(renamed)
        assert request_fingerprint(base) != request_fingerprint(changed)

    def test_request_fingerprint_depends_on_behavior_metadata_values(self) -> None:
        """影响行为的 metadata 取值必须进指纹：类型变了就是另一轮。"""
        state = make_append(round_id="round-1", content="hi", metadata={"memory_type": "state"})
        event = make_append(round_id="round-1", content="hi", metadata={"memory_type": "event"})
        assert request_fingerprint(state) != request_fingerprint(event)


class TestRoundSortKey:
    def test_null_round_index_sorts_before_numeric(self) -> None:
        null_key = round_sort_key(_entry("r-null", round_index=None))
        zero_key = round_sort_key(_entry("r-0", round_index=0))
        assert null_key[1] == -1
        assert null_key < zero_key

    def test_key_orders_by_timestamp_then_index_then_round_id(self) -> None:
        early = round_sort_key(_entry("r-2", round_index=2))
        same_time_late = round_sort_key(_entry("r-10", round_index=10))
        assert early < same_time_late


class TestSummarizeRounds:
    def test_empty_rounds_returns_empty_text_and_no_latest(self) -> None:
        assert summarize_rounds([]) == ("", None)

    def test_concatenates_user_messages_of_last_ten_rounds_only(self) -> None:
        rounds = [_entry(f"r-{index:02d}") for index in range(12)]
        text, latest = summarize_rounds(rounds)
        assert latest is not None
        assert latest.round_id == "r-11"
        assert text.split("；") == [f"内容-r-{index:02d}" for index in range(2, 12)]


class TestSectionsToText:
    def test_renders_sections_in_fixed_order_skipping_empty_and_placeholder(self) -> None:
        assert (
            sections_to_text({"主题": "A", "进行中事项": "无", "行为偏好": "B", "近期状态": ""})
            == "【主题】A\n【行为偏好】B"
        )

    def test_all_four_sections_render_in_canonical_order(self) -> None:
        assert (
            sections_to_text({"主题": "T", "进行中事项": "I", "行为偏好": "P", "近期状态": "S"})
            == "【主题】T\n【进行中事项】I\n【行为偏好】P\n【近期状态】S"
        )

    def test_empty_sections_render_placeholder(self) -> None:
        assert sections_to_text({}) == "无"

    def test_accepts_bracket_prefixed_keys(self) -> None:
        assert sections_to_text({"【主题】": "A"}) == "【主题】A"


class TestStructuredSummary:
    def test_to_dict_covers_all_four_sections(self) -> None:
        assert StructuredSummary(主题="t", 进行中事项="i").to_dict() == {
            "主题": "t",
            "进行中事项": "i",
            "行为偏好": "",
            "近期状态": "",
        }


class TestStructuredSummaryPrompt:
    def test_prompt_contains_previous_summary_and_user_lines(self) -> None:
        prompt = build_structured_summary_user_prompt(
            [_entry("r-1"), _entry("r-2")], previous_summary="上一版"
        )
        assert "上一版摘要" in prompt
        assert "user: 内容-r-1" in prompt
        assert "user: 内容-r-2" in prompt

    def test_prompt_without_previous_summary_omits_block(self) -> None:
        prompt = build_structured_summary_user_prompt([_entry("r-1")], previous_summary=None)
        assert "上一版摘要" not in prompt
        assert "user: 内容-r-1" in prompt


def test_request_fingerprint_distinguishes_embedded_separators() -> None:
    """消息里含 | 的两组不同对话，指纹不得相同（否则幂等误判为同一轮）。"""
    from thinkback.domain.keys import fingerprint

    a = [
        {"role": "user", "content": "a|assistant:b"},
        {"role": "assistant", "content": "c"},
    ]
    b = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b|assistant:c"},
    ]
    assert fingerprint(a) != fingerprint(b)


def test_request_fingerprint_covers_behavior_changing_metadata() -> None:
    """force_extract 会改变是否抽取，不能在同 round 下被当成同一次写入。"""
    from tests.unit.test_memory_service import make_append
    from thinkback.domain.keys import request_fingerprint

    base = make_append(round_id="r-behavior")
    forced = base.model_copy(update={"metadata": {"force_extract": True}})
    assert request_fingerprint(base) != request_fingerprint(forced)


# ---------------------------------------------------------------------------
# 槽位词表与规范化口径（domain/slots/canonical.py 进变异范围后暴露的盲区）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "marker",
    [
        "叫我",
        "称呼",
        "别人叫",
        "preferred name",
        "prefers to be called",
        "prefers to be addressed as",
        "addressed as",
        "changed preferred name",
    ],
)
def test_every_nickname_marker_yields_the_nickname_slot(marker: str) -> None:
    """昵称词表里的每一个 marker 都要能把记忆判成 preferred_nickname 槽位。

    少了任何一个，用户说了那种话就会被当成普通记忆：不进槽位 → 冲突仲裁
    不认它 → 用户改了昵称，旧的那条继续生效。
    """
    assert memory_conflict_slot(marker) == "preferred_nickname"


@pytest.mark.parametrize(
    "marker",
    [
        "communication preference",
        "reassurance",
        "direct advice",
        "before advice",
        "instead of reassurance",
        "沟通偏好",
        "直接建议",
        "先安慰",
        "说教",
    ],
)
def test_every_communication_marker_yields_the_communication_slot(marker: str) -> None:
    """沟通偏好词表里的每一个 marker 都要能把记忆判成 communication_preference 槽位。

    这一档决定「用户说『我需要直接建议』」算不算一条沟通偏好事实。不进
    槽位 → 冲突仲裁不认 → 旧的偏好继续生效，用户改了沟通方式却没人理。
    """
    assert memory_conflict_slot(marker) == "communication_preference"


def test_the_work_status_is_canonicalized_from_the_source_text() -> None:
    """源文本里的工作状态要能规范化成一条记忆。

    规范化后的文本是槽位比较的输入：源里有工作状态、规范化里没有，
    就会和源里那条同槽位记忆并存成两条事实。
    """
    assert "User current work status: 芯片整合" in p0_canonical_memories_from_source(
        "工作状态是芯片整合"
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("叫我小明", "User prefers to be called 小明"),
        ("我家养了只猫叫Tom", "User has a cat named Tom"),
        ("我搬到上海了", "User lives in 上海"),
        ("工作状态是芯片整合", "User current work status: 芯片整合"),
        ("沟通偏好是希望得到直接建议", "User communication preference: 得到直接建议"),
        ("我的生日是1990年1月1日", "User birthday: 1990年1月1日"),
        ("我最喜欢喝美式咖啡", "User favorite drink: 美式咖啡"),
        ("我接受温和提醒睡觉", "User sleep reminder preference: okay with gentle sleep reminders"),
    ],
)
def test_every_slot_is_canonicalized_from_the_source_text(text: str, expected: str) -> None:
    """源文本里每一档 P0 槽位都要被规范化成对应的那一条记忆。

    少一档就会出问题：源里有昵称、规范化里没有 → mem0 抽出来的那条
    昵称记忆与源里那条变成两条并存的事实，冲突仲裁认不出它们是同一件事。
    """
    assert expected in p0_canonical_memories_from_source(text)


def test_the_source_side_canonical_wins_over_the_memory_text() -> None:
    """源侧规范化文本优先：mem0 抽取结果里的噪声不进比较口径。"""
    canonical = canonical_memory_text_from_source(
        "User has a cat named Tom",
        {
            "source_text": "我家养了只猫叫Tom",
            "canonical_memory_text": "User has a cat named Tom",
        },
    )

    assert canonical == "User has a cat named Tom"


def test_a_pet_name_is_canonicalized_from_the_source_side() -> None:
    """记忆文本与源文本里的宠物种类一致时，规范化用源文本里的名字。

    源文本才是原文；mem0 抽出来的名字可能有噪声（截断、别名），比较时
    用源侧的更稳。
    """
    canonical = canonical_memory_text_from_source(
        "User has a cat named Tom",
        {"source_text": "我家养了只猫叫Tommy"},
    )

    assert canonical == "User has a cat named Tommy"


def test_a_pet_name_without_a_pet_in_the_source_falls_back_to_the_memory_text() -> None:
    """源文本里没有宠物时，用记忆文本自己的规范化结果。

    这一条同时钉住「源侧有宠物才用源侧名字」这个前提：把它写成「只要
    有一边有宠物就用源侧」的话，源侧没有宠物时会拿空值去取名字 → 直接
    抛异常，整条 L3 抽取失败。
    """
    canonical = canonical_memory_text_from_source(
        "User has a cat named Tom",
        {"source_text": "今天天气不错"},
    )

    assert canonical == "User has a cat named Tom"


def test_empty_text_is_not_a_correction() -> None:
    """空文本不是「更正」—— 空字符串不该触发更正口径。"""
    assert is_correction_text("") is False


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("叫我小明", "User prefers to be called 小明"),
        ("我家养了只猫叫Tom", "User has a cat named Tom"),
        ("我搬到上海了", "User lives in 上海"),
        ("工作状态是芯片整合", "User current work status: 芯片整合"),
        ("沟通偏好是希望得到直接建议", "User communication preference: 得到直接建议"),
        ("我的生日是1990年1月1日", "User birthday: 1990年1月1日"),
        ("我最喜欢喝美式咖啡", "User favorite drink: 美式咖啡"),
        ("这是一条普通记忆", "这是一条普通记忆"),
    ],
)
def test_canonical_memory_text_renders_every_slot(text: str, expected: str) -> None:
    """每一档槽位的规范化文本都要钉死。

    规范化文本是**槽位比较的输入**：两条记忆要判成「同一条事实」，比的就是
    它。哪一档渲染错了，冲突仲裁就会把「昵称从小明改成阿花」当成两条不同
    的事实并存 —— 两条都在召回里，旧的那条还不肯让位。
    """
    assert canonical_memory_text(text) == expected


def test_an_empty_canonical_keeps_the_original_text() -> None:
    """规范化结果为空时保留原文：没得规范化不等于该丢内容。"""
    assert local_index_memory_text("原文", "") == "原文"


def test_an_empty_original_uses_the_canonical_text() -> None:
    """原文为空时用规范化文本：至少还有一条可读的内容。"""
    assert local_index_memory_text("", "User has a cat named Tom") == "User has a cat named Tom"
