"""``domain/slots/support.py`` 的判定分支契约。

这个模块是**来源仲裁**的口径：哪个来源更新（同槽位冲突时谁胜出）、
这条既有记忆是不是同源、抽取前的廉价闸门放不放行。它的每个判定都
对应一个「线上表现完全不同、但功能测试照样绿」的分支：

- 排序游标的优先级顺序写反 → 用户改了偏好，系统还在用旧的那条；
- 同源判定放宽 → 新事实被当成「已有的那条」而跳过写入；
- 廉价闸门的字节阈值写成 ``>`` → 恰好到阈值的一轮被丢掉。

行覆盖率证明不了这些。每条用例钉一个具体结论。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from thinkback.domain.entities import JournalEntry
from thinkback.domain.slots.support import (
    _parse_source_timestamp,
    is_backend_managed_memory_id,
    is_local_index_memory_id,
    is_same_source_local_p0_memory,
    round_order,
    should_extract_to_l3,
    source_cursor,
    source_cursor_from_refs,
    source_has_any,
)
from thinkback.memory.repositories import InMemoryMemoryRepository

NOW = datetime(2026, 1, 1, tzinfo=UTC)


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


# ---------------------------------------------------------------------------
# 本地索引 ID 与后端 ID 的区分
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "memory_id",
    ["local-p0:abc", "deleted-source:abc", "delete-all:abc", "delete-session:abc"],
)
def test_a_local_index_id_is_not_backend_managed(memory_id: str) -> None:
    assert is_local_index_memory_id(memory_id) is True
    assert is_backend_managed_memory_id(memory_id) is False


def test_a_backend_id_is_backend_managed() -> None:
    assert is_backend_managed_memory_id("mem0-123") is True


def test_an_empty_id_is_neither_local_nor_backend_managed() -> None:
    """空 ID 不是后端记忆 —— 否则空串会被当成「要删的后端 ID」。"""

    assert is_backend_managed_memory_id("") is False


# ---------------------------------------------------------------------------
# 同源判定：同槽位 + source_refs 相交
# ---------------------------------------------------------------------------


class _Memory:
    def __init__(
        self,
        *,
        backend_memory_id: str = "local-p0:1",
        memory_text: str = "User lives in 杭州",
        source_refs: list[dict[str, str]] | None = None,
    ) -> None:
        self.backend_memory_id = backend_memory_id
        self.memory_text = memory_text
        self.source_refs = source_refs if source_refs is not None else []


def test_a_local_p0_memory_from_the_same_round_is_the_same_source() -> None:
    memory = _Memory(source_refs=[{"session_id": "s1", "round_id": "r1"}])

    assert is_same_source_local_p0_memory(
        memory,
        memory_text="User lives in 杭州",
        l3_metadata={"source_refs": [{"session_id": "s1", "round_id": "r1"}]},
    )


def test_a_local_p0_memory_from_another_round_is_not_the_same_source() -> None:
    memory = _Memory(source_refs=[{"session_id": "s1", "round_id": "r1"}])

    assert not is_same_source_local_p0_memory(
        memory,
        memory_text="User lives in 杭州",
        l3_metadata={"source_refs": [{"session_id": "s1", "round_id": "r2"}]},
    )


def test_a_backend_managed_memory_is_never_a_local_p0_memory() -> None:
    """只有 local-p0: 前缀的记忆才走同源判定 —— 后端记忆的 ID 不带这个前缀。"""

    memory = _Memory(
        backend_memory_id="mem0-123", source_refs=[{"session_id": "s1", "round_id": "r1"}]
    )

    assert not is_same_source_local_p0_memory(
        memory,
        memory_text="User lives in 杭州",
        l3_metadata={"source_refs": [{"session_id": "s1", "round_id": "r1"}]},
    )


def test_a_memory_in_another_slot_is_not_the_same_source() -> None:
    """同槽位是前提：住址与昵称不是同一条事实。"""

    memory = _Memory(source_refs=[{"session_id": "s1", "round_id": "r1"}])

    assert not is_same_source_local_p0_memory(
        memory,
        memory_text="User prefers to be called 小明",
        l3_metadata={"source_refs": [{"session_id": "s1", "round_id": "r1"}]},
    )


def test_a_memory_without_source_refs_in_the_metadata_is_not_the_same_source() -> None:
    """metadata 里没有 source_refs → 无法证明同源 → 不当同源。"""

    memory = _Memory(source_refs=[{"session_id": "s1", "round_id": "r1"}])

    assert not is_same_source_local_p0_memory(
        memory,
        memory_text="User lives in 杭州",
        l3_metadata={},
    )


# ---------------------------------------------------------------------------
# 回合排序：来源时间 > 库内时间戳 > round_index > round_id 数字后缀
# ---------------------------------------------------------------------------


def _save(repository: InMemoryMemoryRepository, entry: JournalEntry) -> None:
    repository.journal_by_round[entry.round_id] = entry


def test_a_source_timestamp_outranks_everything_else() -> None:
    """带来源时间时，它说了算 —— 库里的 round_index 再大也不算数。"""

    repository = InMemoryMemoryRepository()
    _save(repository, _entry(round_index=999))

    order = round_order(
        repository,
        "user-1",
        "s1",
        "r1",
        source_timestamp=NOW,
    )

    assert order[0] == 4


def test_a_round_without_a_source_timestamp_falls_back_to_the_library_entry() -> None:
    repository = InMemoryMemoryRepository()
    _save(repository, _entry(source_timestamp=NOW))

    order = round_order(repository, "user-1", "s1", "r1")

    assert order[0] == 3


def test_a_round_of_another_user_is_not_used() -> None:
    """库里那条属于别的用户 → 不能拿来定这次的新旧。

    判据是排序档位：库内条目是第 3 档，round_id 数字后缀是第 1 档。
    别人的条目不能被用，退回第 1 档就说明没被用上。
    """

    repository = InMemoryMemoryRepository()
    _save(repository, _entry(user_id="other-user"))

    order = round_order(repository, "user-1", "s1", "r1")

    assert order[0] == 1


def test_a_round_of_another_session_is_not_used() -> None:
    repository = InMemoryMemoryRepository()
    _save(repository, _entry(session_id="other-session"))

    order = round_order(repository, "user-1", "s1", "r1")

    assert order[0] == 1


def test_a_round_without_a_timestamp_falls_back_to_the_round_index() -> None:
    """有 round_index 时用它定序（第 2 档）。"""

    repository = InMemoryMemoryRepository()
    entry = _entry()
    entry.source_timestamp = None  # type: ignore[assignment]
    entry.round_index = 7
    _save(repository, entry)

    order = round_order(repository, "user-1", "s1", "r1")

    assert order[0] == 2
    assert order[1] == 7.0


def test_a_round_id_with_a_number_falls_back_to_that_number() -> None:
    """round_id 里的数字后缀是最后的权宜之计。"""

    repository = InMemoryMemoryRepository()

    order = round_order(repository, "user-1", "s1", "round-42")

    assert order[0] == 1
    assert order[1] == 42.0


def test_a_round_id_without_a_number_sorts_last() -> None:
    repository = InMemoryMemoryRepository()

    order = round_order(repository, "user-1", "s1", "round-alpha")

    assert order[0] == 0


def test_the_round_id_is_normalized_to_lower_case() -> None:
    """大小写不同的 round_id 排序要一致 —— 否则同一条轮次有两个序。"""

    repository = InMemoryMemoryRepository()

    lower = round_order(repository, "user-1", "s1", "round-alpha")
    upper = round_order(repository, "user-1", "s1", "ROUND-ALPHA")

    assert lower == upper


def test_a_numeric_round_id_sorts_by_its_number_not_its_text() -> None:
    """ "round-9" 比 "round-10" 新吗？按数字：9 < 10，所以 round-10 更新。"""

    repository = InMemoryMemoryRepository()

    nine = round_order(repository, "user-1", "s1", "round-9")
    ten = round_order(repository, "user-1", "s1", "round-10")

    assert nine < ten


# ---------------------------------------------------------------------------
# 来源游标：一组引用里取最新的那条
# ---------------------------------------------------------------------------


def test_the_cursor_picks_the_newest_ref() -> None:
    repository = InMemoryMemoryRepository()

    cursor = source_cursor_from_refs(
        repository,
        "user-1",
        [
            {"session_id": "s1", "round_id": "round-1"},
            {"session_id": "s1", "round_id": "round-9"},
            {"session_id": "s1", "round_id": "round-5"},
        ],
    )

    assert cursor is not None
    assert cursor[2] == "round-9"


def test_the_cursor_ignores_malformed_refs() -> None:
    """缺 session_id / round_id 的引用直接跳过，不能让整批游标失效。"""

    repository = InMemoryMemoryRepository()

    cursor = source_cursor_from_refs(
        repository,
        "user-1",
        [
            {"round_id": "round-1"},
            {"session_id": "s1"},
            "not-a-dict",
            {"session_id": "s1", "round_id": "round-3"},
        ],
    )

    assert cursor is not None
    assert cursor[2] == "round-3"


def test_no_usable_ref_means_no_cursor() -> None:
    repository = InMemoryMemoryRepository()

    assert source_cursor_from_refs(repository, "user-1", []) is None
    assert source_cursor_from_refs(repository, "user-1", [{"session_id": "s1"}]) is None


def test_the_cursor_uses_the_source_timestamp_when_it_is_given() -> None:
    """来源时间在场时不再按 round_id 猜新旧。"""

    repository = InMemoryMemoryRepository()

    cursor = source_cursor_from_refs(
        repository,
        "user-1",
        [{"session_id": "s1", "round_id": "round-1"}],
        source_timestamp=NOW,
    )

    assert cursor is not None
    assert cursor[0][0] == 4


def test_the_metadata_cursor_is_none_without_source_refs() -> None:
    repository = InMemoryMemoryRepository()

    assert source_cursor(repository, "user-1", {}) is None
    assert source_cursor(repository, "user-1", {"source_refs": "not-a-list"}) is None


# ---------------------------------------------------------------------------
# 来源时间解析
# ---------------------------------------------------------------------------


def test_a_z_suffixed_timestamp_is_parsed_as_utc() -> None:
    assert _parse_source_timestamp("2026-01-01T00:00:00Z") == NOW


def test_a_naive_timestamp_is_read_as_utc() -> None:
    assert _parse_source_timestamp("2026-01-01T00:00:00") == NOW


def test_an_aware_timestamp_keeps_its_offset() -> None:
    parsed = _parse_source_timestamp("2026-01-01T08:00:00+08:00")

    assert parsed is not None
    assert parsed.utcoffset() == datetime.fromisoformat("2026-01-01T08:00:00+08:00").utcoffset()


def test_an_unparsable_timestamp_is_ignored() -> None:
    assert _parse_source_timestamp("not-a-timestamp") is None


def test_an_empty_or_non_string_timestamp_is_ignored() -> None:
    assert _parse_source_timestamp("") is None
    assert _parse_source_timestamp(None) is None
    assert _parse_source_timestamp(123) is None


def test_a_datetime_passes_through() -> None:
    assert _parse_source_timestamp(NOW) is NOW


# ---------------------------------------------------------------------------
# 关键字匹配：大小写不敏感
# ---------------------------------------------------------------------------


def test_the_marker_match_is_case_insensitive() -> None:
    """源文本大写、marker 小写（或反过来）都要命中。"""

    assert source_has_any("I LIVE in Hangzhou", ("live",)) is True
    assert source_has_any("I live in Hangzhou", ("LIVE",)) is True


def test_no_marker_present_means_no_match() -> None:
    assert source_has_any("today is fine", ("live", "住")) is False


# ---------------------------------------------------------------------------
# 抽取前的廉价闸门
# ---------------------------------------------------------------------------


def _messages(*contents: str) -> list[dict[str, Any]]:
    return [{"role": "user", "content": content} for content in contents]


def test_the_gate_is_disabled_when_no_slots_are_allowed() -> None:
    """白名单留空 = 闸门关闭 —— 所有轮都送 mem0（向后兼容）。"""

    assert should_extract_to_l3(messages=_messages("hi"), metadata=None) is True


def test_force_extract_always_wins() -> None:
    assert (
        should_extract_to_l3(
            messages=_messages("hi"),
            metadata={"force_extract": True},
            allowed_slots=["favorite:food"],
        )
        is True
    )


def test_a_slot_hit_passes_the_gate() -> None:
    """消息里明确有槽位价值 → 抽取。"""

    assert (
        should_extract_to_l3(
            messages=_messages("我养了一只猫，名字叫阿福"),
            metadata={},
            allowed_slots=["favorite:food"],
        )
        is True
    )


def test_a_short_message_without_a_slot_is_filtered_out() -> None:
    assert (
        should_extract_to_l3(
            messages=_messages("今天天气不错"),
            metadata={},
            allowed_slots=["favorite:food"],
        )
        is False
    )


def test_a_long_message_passes_the_gate_even_without_a_slot() -> None:
    """足够长的消息也值得抽取 —— 信息量在那儿。"""

    long_text = "我今天跟同事讨论了下季度的排期，主要是把上线时间往后挪两周"
    assert len(long_text.encode("utf-8")) >= 24
    assert (
        should_extract_to_l3(
            messages=_messages(long_text),
            metadata={},
            allowed_slots=["favorite:food"],
        )
        is True
    )


def test_only_user_messages_count_toward_the_length_gate() -> None:
    """助手的回复不算用户事实 —— 长回复不该放行抽取。"""

    messages = [
        {"role": "user", "content": "嗯"},
        {"role": "assistant", "content": "好" * 100},
    ]
    assert (
        should_extract_to_l3(messages=messages, metadata={}, allowed_slots=["favorite:food"])
        is False
    )


def test_a_message_without_content_is_tolerated() -> None:
    """缺 content 的消息不能让整轮判定炸掉。"""

    messages: list[dict[str, Any]] = [{"role": "user"}]
    assert (
        should_extract_to_l3(messages=messages, metadata={}, allowed_slots=["favorite:food"])
        is False
    )
