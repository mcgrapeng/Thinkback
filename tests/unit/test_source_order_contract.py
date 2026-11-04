"""来源排序游标与本地 P0 判定口径的行为用例。

``domain/slots/source_order.py`` 是**唯一**一个没有任何测试文件引用的源模块
（其余 73 个模块都有）—— 它是从 ``support.py`` 拆出来的判定逻辑模块，
而拆出来之后没有配套的行为用例。

判定逻辑改坏都是静默的线上故障：

- ``round_order`` 的档位顺序写反 → 「用户改了偏好，系统还在用旧的那条」
- ``is_same_source_local_p0_memory`` 的同源判定放宽 → 新事件被当成同源，
  覆盖掉不相关的旧事实
- ``should_extract_to_l3`` 的闸门放行条件写坏 → 该抽的不抽（事实静默消失）
  或不该抽的也抽（Milvus 沉淀永远召不回来的暗数据）

这些都在变异审计范围内（``pyproject.toml`` 的 ``only_mutate``），但变异
审计钉的是「改坏会不会被现有用例杀掉」；本文件提供的是**行为语义**的
直接断言：口径是什么、为什么是这样。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from thinkback.domain.slots.source_order import (
    is_backend_managed_memory_id,
    is_local_index_memory_id,
    is_same_source_local_p0_memory,
    round_order,
    should_extract_to_l3,
    source_cursor,
    source_cursor_from_refs,
    source_has_any,
)

_T0 = datetime(2026, 10, 5, 12, 0, 0, tzinfo=UTC)
_T1 = datetime(2026, 10, 5, 13, 0, 0, tzinfo=UTC)


class _Entry:
    def __init__(
        self,
        *,
        user_id: str = "u1",
        session_id: str = "s1",
        source_timestamp: datetime | None = None,
        round_index: int | None = None,
    ) -> None:
        self.user_id = user_id
        self.session_id = session_id
        self.source_timestamp = source_timestamp
        self.round_index = round_index


class _Repo:
    """只提供 ``get_round`` 的最小仓储替身。"""

    def __init__(self, entries: dict[str, Any] | None = None) -> None:
        self._entries = entries or {}

    def get_round(self, round_id: str) -> Any:
        return self._entries.get(round_id)


# ── 本地索引 ID 与后端 ID 的判定 ────────────────────────────────────────────


@pytest.mark.parametrize(
    "memory_id",
    ["local-p0:abc", "deleted-source:abc", "delete-all:abc", "delete-session:abc"],
)
def test_a_local_index_id_is_recognized(memory_id: str) -> None:
    assert is_local_index_memory_id(memory_id) is True
    assert is_backend_managed_memory_id(memory_id) is False


@pytest.mark.parametrize("memory_id", ["mem-123", "abc-def", "fake-uuid"])
def test_a_backend_managed_id_is_recognized(memory_id: str) -> None:
    assert is_local_index_memory_id(memory_id) is False
    assert is_backend_managed_memory_id(memory_id) is True


def test_an_empty_id_is_neither_local_nor_backend_managed() -> None:
    """空 ID 不是后端管理的 —— 它没有主人。

    ``is_backend_managed_memory_id`` 显式要求非空：空串会被
    ``is_local_index_memory_id("")`` 判成 False，若不额外判非空就会被
    误认为「后端管理的记忆」。
    """
    assert is_local_index_memory_id("") is False
    assert is_backend_managed_memory_id("") is False


# ── 回合全序排序键 ──────────────────────────────────────────────────────────


def test_a_source_timestamp_outranks_everything_else() -> None:
    """调用方手里有来源时间时，它就是最高档（档位 4）。

    外部历史回放时库里没有这轮，round_id 的字典序与事实新旧无关 ——
    所以来源时间必须压过 round_id 兜底。
    """
    repo = _Repo({"r1": _Entry(source_timestamp=_T0, round_index=1)})
    key = round_order(repo, "u1", "s1", "r1", source_timestamp=_T1)
    assert key[0] == 4
    assert key[1] == _T1.timestamp()


def test_a_stored_source_timestamp_is_the_second_tier() -> None:
    """库里有来源时间（档位 3）时，round_index 只做同刻的次序。"""
    repo = _Repo({"r1": _Entry(source_timestamp=_T0, round_index=3)})
    key = round_order(repo, "u1", "s1", "r1")
    assert key[0] == 3
    assert key[1] == _T0.timestamp()


def test_a_stored_round_index_is_the_third_tier() -> None:
    """库里有轮次序号但没有来源时间 → 档位 2。"""
    repo = _Repo({"r1": _Entry(round_index=7)})
    key = round_order(repo, "u1", "s1", "r1")
    assert key[0] == 2
    assert key[1] == 7.0


def test_the_round_id_number_is_the_last_resort() -> None:
    """库里查不到、也没有来源时间 → 退到 round_id 里的数字（档位 1）。"""
    repo = _Repo()
    key = round_order(repo, "u1", "s1", "round-42")
    assert key[0] == 1
    assert key[1] == 42.0


def test_a_round_id_without_any_number_sorts_last() -> None:
    """round_id 里没有数字 → 档位 0，排在所有有数字的后面。"""
    repo = _Repo()
    key = round_order(repo, "u1", "s1", "round-alpha")
    assert key[0] == 0
    assert key[1] == 0.0


def test_a_round_from_another_user_does_not_contribute_its_timestamp() -> None:
    """库里那一轮属于别的用户 → 不得用它的时间。

    越权取别人的来源时间 = 把别人的事实排到自己前面。
    """
    repo = _Repo({"r1": _Entry(user_id="other-user", source_timestamp=_T1, round_index=9)})
    key = round_order(repo, "u1", "s1", "r1")
    assert key[0] == 1  # 退到 round_id 数字，而不是用别人的时间


def test_a_round_from_another_session_does_not_contribute_its_timestamp() -> None:
    """库里那一轮属于别的会话 → 同样不得用它的时间。"""
    repo = _Repo({"r1": _Entry(session_id="other-session", source_timestamp=_T1)})
    key = round_order(repo, "u1", "s1", "r1")
    assert key[0] == 1


def test_a_later_source_timestamp_sorts_after_an_earlier_one() -> None:
    """两个回合都带来源时间 → 新的在后（全序的基本性质）。"""
    repo = _Repo()
    early = round_order(repo, "u1", "s1", "r-early", source_timestamp=_T0)
    late = round_order(repo, "u1", "s1", "r-late", source_timestamp=_T1)
    assert early < late


def test_a_source_timestamp_outranks_a_larger_round_id_number() -> None:
    """带来源时间的轮次排在只有 round_id 数字的轮次之后。

    round_id 的数字大不代表事实新 —— 这是档位分层存在的全部理由。
    """
    repo = _Repo()
    with_ts = round_order(repo, "u1", "s1", "r-1", source_timestamp=_T0)
    without_ts = round_order(repo, "u1", "s1", "r-999")
    assert without_ts < with_ts


# ── 来源游标 ────────────────────────────────────────────────────────────────


def test_the_cursor_is_none_when_there_are_no_usable_refs() -> None:
    """没有可用引用 → 没有游标（调用方据此跳过覆盖判定）。"""
    assert source_cursor_from_refs(_Repo(), "u1", []) is None
    assert source_cursor_from_refs(_Repo(), "u1", [{"session_id": "s1"}]) is None
    assert source_cursor_from_refs(_Repo(), "u1", [{"round_id": "r1"}]) is None


def test_the_cursor_picks_the_newest_ref() -> None:
    """一组引用里取最新的那个游标。"""
    repo = _Repo()
    cursor = source_cursor_from_refs(
        repo,
        "u1",
        [
            {"session_id": "s1", "round_id": "r-1"},
            {"session_id": "s1", "round_id": "r-9"},
            {"session_id": "s1", "round_id": "r-5"},
        ],
    )
    assert cursor is not None
    assert cursor[1] == "s1"
    assert cursor[2] == "r-9"


def test_the_cursor_ignores_non_dict_entries() -> None:
    """引用列表里混进非字典项 → 跳过它，不崩。"""
    repo = _Repo()
    cursor = source_cursor_from_refs(
        repo,
        "u1",
        ["not-a-dict", {"session_id": "s1", "round_id": "r-3"}],  # type: ignore[list-item]
    )
    assert cursor is not None
    assert cursor[2] == "r-3"


def test_source_cursor_is_none_when_metadata_has_no_refs() -> None:
    """metadata 里没有 source_refs → 没有游标。"""
    assert source_cursor(_Repo(), "u1", {}) is None
    assert source_cursor(_Repo(), "u1", {"source_refs": "not-a-list"}) is None


def test_source_cursor_uses_the_metadata_source_timestamp() -> None:
    """metadata 带来源时间 → 按真实时间定新旧，不按 round_id 猜。

    外部历史回放时库里查不到源轮次，round_id 的字典序与新旧无关。
    """
    repo = _Repo()
    cursor = source_cursor(
        repo,
        "u1",
        {
            "source_refs": [{"session_id": "s1", "round_id": "r-1"}],
            "source_timestamp": _T1.isoformat(),
        },
    )
    assert cursor is not None
    assert cursor[0][0] == 4


def test_source_cursor_treats_a_naive_source_timestamp_as_utc() -> None:
    """naive 时间按 UTC 读 —— 与别处的口径一致。"""
    repo = _Repo()
    cursor = source_cursor(
        repo,
        "u1",
        {
            "source_refs": [{"session_id": "s1", "round_id": "r-1"}],
            "source_timestamp": "2026-10-05T12:00:00",
        },
    )
    assert cursor is not None
    assert cursor[0][0] == 4


def test_source_cursor_ignores_an_unparsable_source_timestamp() -> None:
    """时间解析不了 → 当作没有时间，退回 round_id 兜底。"""
    repo = _Repo()
    cursor = source_cursor(
        repo,
        "u1",
        {
            "source_refs": [{"session_id": "s1", "round_id": "r-7"}],
            "source_timestamp": "not-a-timestamp",
        },
    )
    assert cursor is not None
    assert cursor[0][0] == 1


# ── 同源判定 ────────────────────────────────────────────────────────────────


class _Memory:
    def __init__(
        self,
        *,
        backend_memory_id: str,
        memory_text: str,
        source_refs: list[dict[str, str]] | None = None,
    ) -> None:
        self.backend_memory_id = backend_memory_id
        self.memory_text = memory_text
        self.source_refs = source_refs or []


def test_a_local_p0_memory_from_the_same_slot_and_refs_is_the_same_source() -> None:
    """同槽位 + source_refs 相交 → 同源。"""
    memory = _Memory(
        backend_memory_id="local-p0:1",
        memory_text="居住城市：上海",
        source_refs=[{"session_id": "s1", "round_id": "r1"}],
    )
    assert (
        is_same_source_local_p0_memory(
            memory,
            memory_text="居住城市：杭州",
            l3_metadata={"source_refs": [{"session_id": "s1", "round_id": "r1"}]},
        )
        is True
    )


def test_a_different_slot_is_not_the_same_source() -> None:
    """槽位不同 → 不是同源（槽位是冲突判定的第一道口径）。"""
    memory = _Memory(
        backend_memory_id="local-p0:1",
        memory_text="居住城市：上海",
        source_refs=[{"session_id": "s1", "round_id": "r1"}],
    )
    assert (
        is_same_source_local_p0_memory(
            memory,
            memory_text="User has a cat named 麻薯",
            l3_metadata={"source_refs": [{"session_id": "s1", "round_id": "r1"}]},
        )
        is False
    )


def test_disjoint_source_refs_are_not_the_same_source() -> None:
    """槽位相同但来源引用不相交 → 不是同源。"""
    memory = _Memory(
        backend_memory_id="local-p0:1",
        memory_text="居住城市：上海",
        source_refs=[{"session_id": "s1", "round_id": "r1"}],
    )
    assert (
        is_same_source_local_p0_memory(
            memory,
            memory_text="居住城市：杭州",
            l3_metadata={"source_refs": [{"session_id": "s1", "round_id": "r9"}]},
        )
        is False
    )


def test_a_backend_managed_memory_is_never_the_same_source() -> None:
    """只有本地 P0 索引行才谈同源；后端记忆不是。"""
    memory = _Memory(
        backend_memory_id="mem-abc",
        memory_text="居住城市：上海",
        source_refs=[{"session_id": "s1", "round_id": "r1"}],
    )
    assert (
        is_same_source_local_p0_memory(
            memory,
            memory_text="居住城市：杭州",
            l3_metadata={"source_refs": [{"session_id": "s1", "round_id": "r1"}]},
        )
        is False
    )


def test_metadata_without_source_refs_is_not_the_same_source() -> None:
    """metadata 里没有 source_refs → 判不了同源 → 不是。"""
    memory = _Memory(
        backend_memory_id="local-p0:1",
        memory_text="居住城市：上海",
        source_refs=[{"session_id": "s1", "round_id": "r1"}],
    )
    assert (
        is_same_source_local_p0_memory(
            memory,
            memory_text="居住城市：杭州",
            l3_metadata={},
        )
        is False
    )


# ── 抽取闸门 ────────────────────────────────────────────────────────────────


def _user_message(content: str) -> dict[str, Any]:
    return {"role": "user", "content": content}


def test_the_gate_is_disabled_when_no_slots_are_configured() -> None:
    """allowed_slots 留空 = 闸门关闭，所有轮都送抽取（向后兼容）。"""
    assert should_extract_to_l3(messages=[_user_message("短")], metadata=None) is True
    assert (
        should_extract_to_l3(messages=[_user_message("短")], metadata=None, allowed_slots=[])
        is True
    )


def test_force_extract_always_passes_the_gate() -> None:
    """force_extract=True → 始终放行，不管闸门配了什么。"""
    assert (
        should_extract_to_l3(
            messages=[_user_message("短")],
            metadata={"force_extract": True},
            allowed_slots=["current_location"],
        )
        is True
    )


def test_a_truthy_but_not_true_force_extract_does_not_bypass_the_gate() -> None:
    """只有布尔 True 才强制放行 —— 字符串 "true" 不是。"""
    assert (
        should_extract_to_l3(
            messages=[_user_message("短")],
            metadata={"force_extract": "true"},
            allowed_slots=["current_location"],
        )
        is False
    )


def test_a_slot_hit_passes_the_gate() -> None:
    """命中 P0 槽位的轮次 → 放行。"""
    assert (
        should_extract_to_l3(
            messages=[_user_message("我住在杭州")],
            metadata=None,
            allowed_slots=["current_location"],
        )
        is True
    )


def test_a_long_message_passes_the_gate_even_without_a_slot_hit() -> None:
    """长消息（≥ 24 字节）→ 放行（内容多，可能藏着事实）。"""
    long_text = "这是一段足够长的消息内容，用来通过字节阈值的抽取闸门判定逻辑。"
    assert (
        should_extract_to_l3(
            messages=[_user_message(long_text)],
            metadata=None,
            allowed_slots=["current_location"],
        )
        is True
    )


def test_a_short_message_without_a_slot_hit_is_gated_out() -> None:
    """短消息 + 没命中槽位 → 不送抽取（只落 L1/L2）。"""
    assert (
        should_extract_to_l3(
            messages=[_user_message("好的")],
            metadata=None,
            allowed_slots=["current_location"],
        )
        is False
    )


def test_only_user_messages_count_toward_the_gate() -> None:
    """assistant 的话不参与槽位判定与字节计数。

    助手的话不是用户事实来源；把它的长度算进去会让闸门对纯闲聊轮次
    误放行。
    """
    assert (
        should_extract_to_l3(
            messages=[{"role": "assistant", "content": "x" * 100}],
            metadata=None,
            allowed_slots=["current_location"],
        )
        is False
    )


# ── 依据词匹配 ──────────────────────────────────────────────────────────────


def test_source_has_any_is_case_insensitive() -> None:
    assert source_has_any("我们用 ASML 的 EUV 机台", ("asml",)) is True
    assert source_has_any("我们用 ASML 的 EUV 机台", ("TEL",)) is False


def test_source_has_any_with_no_markers_is_false() -> None:
    assert source_has_any("任何文本", ()) is False
