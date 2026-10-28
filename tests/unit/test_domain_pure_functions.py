"""领域纯函数契约：输出格式/摘要算法/排序键的精确行为。

变异测试审计（mutmut）发现这些纯函数此前只被间接覆盖，等价变异大量存活。
指纹会随行持久化（幂等判断），摘要格式是 L2/UI 契约——都必须钉死。
"""

from __future__ import annotations

from datetime import UTC, datetime

from tests.unit.test_memory_service import make_append
from thinkback.domain.entities import JournalEntry
from thinkback.domain.keys import fingerprint, request_fingerprint, round_sort_key
from thinkback.domain.slots.canonical import (
    canonical_memory_text,
    count_p0_facts,
    is_correction_text,
    local_index_memory_text,
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
