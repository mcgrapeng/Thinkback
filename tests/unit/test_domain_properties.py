"""领域纯函数的属性测试（hypothesis）。

变异测试审计的教训：示例测试对边界组合覆盖不足，等价变异大量存活。
属性测试用随机输入钉住不变量：排序键的全序、指纹的确定性与边界敏感性、
摘要窗口的切片语义。全部离线确定性（hypothesis 固定数据库 + 无网络）。
"""

from __future__ import annotations

from datetime import UTC, datetime

from hypothesis import given
from hypothesis import strategies as st

from thinkback.domain.entities import JournalEntry
from thinkback.domain.keys import fingerprint, round_sort_key
from thinkback.domain.summarization import summarize_rounds


def _entry(
    round_id: str,
    timestamp: datetime,
    *,
    round_index: int | None = None,
    content: str = "x",
) -> JournalEntry:
    return JournalEntry(
        journal_id=f"journal-{round_id}",
        user_id="user-1",
        memory_scope_id="session-1",
        session_id="session-1",
        round_id=round_id,
        round_index=round_index,
        messages=[{"role": "user", "content": content}],
        source_timestamp=timestamp,
        round_fingerprint="fp",
    )


timestamps = st.datetimes(
    min_value=datetime(2020, 1, 1, tzinfo=UTC),
    max_value=datetime(2030, 1, 1, tzinfo=UTC),
)


class TestRoundSortKey:
    @given(ts1=timestamps, ts2=timestamps, id1=st.integers(0, 99), id2=st.integers(0, 99))
    def test_timestamp_dominates_index_and_id(
        self, ts1: datetime, ts2: datetime, id1: int, id2: int
    ) -> None:
        earlier, later = sorted([ts1, ts2])
        if earlier == later:
            return  # 同刻由 index/round_id 决序，见 test_equal_timestamp_orders_by_index_then_round_id
        key_early = round_sort_key(_entry("a", earlier, round_index=id2))
        key_late = round_sort_key(_entry("b", later, round_index=id1))
        assert key_early < key_late

    @given(ts=timestamps, index=st.integers(0, 99))
    def test_null_round_index_sorts_before_numeric_at_same_timestamp(
        self, ts: datetime, index: int
    ) -> None:
        assert round_sort_key(_entry("a", ts, round_index=None)) < round_sort_key(
            _entry("b", ts, round_index=index)
        )

    @given(ts=timestamps, index=st.integers(0, 99))
    def test_equal_timestamp_orders_by_index_then_round_id(self, ts: datetime, index: int) -> None:
        assert round_sort_key(_entry("a", ts, round_index=index)) < round_sort_key(
            _entry("b", ts, round_index=index)
        )


class TestFingerprint:
    @given(
        messages=st.lists(
            st.fixed_dictionaries(
                {"role": st.sampled_from(["user", "assistant"]), "content": st.text()}
            ),
            min_size=0,
            max_size=5,
        )
    )
    def test_deterministic(self, messages: list[dict[str, str]]) -> None:
        assert fingerprint(messages) == fingerprint([dict(m) for m in messages])

    @given(content=st.text(min_size=1), other=st.text(min_size=1))
    def test_content_sensitivity(self, content: str, other: str) -> None:
        assume_distinct = content != other
        if not assume_distinct:
            return
        single = [{"role": "user", "content": content}]
        changed = [{"role": "user", "content": other}]
        assert fingerprint(single) != fingerprint(changed)

    @given(a=st.text(min_size=1), b=st.text(min_size=1))
    def test_message_boundary_sensitivity(self, a: str, b: str) -> None:
        split = [{"role": "user", "content": a}, {"role": "assistant", "content": b}]
        merged = [{"role": "user", "content": a + b}]
        assert fingerprint(split) != fingerprint(merged)


class TestSummarizeRounds:
    @given(
        contents=st.lists(st.text(), min_size=0, max_size=15),
        ts=timestamps,
    )
    def test_empty_and_window_semantics(self, contents: list[str], ts: datetime) -> None:
        rounds = [_entry(f"r-{index:02d}", ts, content=c) for index, c in enumerate(contents)]
        text, latest = summarize_rounds(rounds)

        if not rounds:
            assert (text, latest) == ("", None)
            return

        expected_sources = rounds[-10:]
        assert text.split("；") == [entry.messages[0]["content"] for entry in expected_sources]
        assert latest is expected_sources[-1]
