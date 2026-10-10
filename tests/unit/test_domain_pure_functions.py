"""领域纯函数契约：输出格式/摘要算法/排序键的精确行为。

变异测试审计（mutmut）发现这些纯函数此前只被间接覆盖，等价变异大量存活。
指纹会随行持久化（幂等判断），摘要格式是 L2/UI 契约——都必须钉死。
"""

from __future__ import annotations

from datetime import UTC, datetime

from tests.unit.test_memory_service import make_append
from thinkback.domain.entities import JournalEntry
from thinkback.domain.keys import fingerprint, request_fingerprint, round_sort_key
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


class TestFingerprint:
    def test_digest_is_stable_and_pinned(self) -> None:
        """指纹算法/分隔符是持久化契约：改动会让已存幂等判断全部失配。"""
        assert (
            fingerprint([{"role": "user", "content": "a"}])
            == "767321b3673f590fcc4b5b441afbaade7a8be6db95273f71c67df0366924f43b"
        )
        assert (
            fingerprint([{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}])
            == "ec192370a912900b610b41e8b9462cbed52702d75c6709578c165b7d1e7f2984"
        )

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
