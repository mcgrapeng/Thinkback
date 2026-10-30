"""service.py 里「决定结果」却从未被断言的分支。

覆盖率把 service.py 的未覆盖行摊到方法上之后，剩下的不是胶水：这些行的
共同点是**它们决定结果** —— 重建时哪些轮次算已覆盖、冲突策略怎么回落、
L3 写因为什么被跳过、索引写失败怎么回滚、缺参数时该拒绝什么。

行覆盖率只证明「这行跑过」，证明不了「结论对不对」。这里每个用例钉一个
具体结论：改坏源码（比较方向翻转、阈值差一、默认值写反）时必须红。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from thinkback.domain.entities import JournalEntry
from thinkback.domain.memory_types import MemoryTypeRegistry, MemoryTypeSpec
from thinkback.infra import config as config_module
from thinkback.memory.backends import FakeMemoryBackend
from thinkback.memory.repositories import InMemoryMemoryRepository
from thinkback.memory.schemas import (
    AppendMemoryRequest,
    DeleteMemoryRequest,
    DeleteScope,
    MemoryMessage,
    MessageRole,
    RecallMemoryRequest,
)
from thinkback.memory.service import MemoryService

LOCATION_SLOT = "current_location"
FOOD_SLOT = "favorite:food"
LOCATION_TEXT = "User lives in Shanghai"


def _service(**kwargs: Any) -> MemoryService:
    return MemoryService(
        repository=InMemoryMemoryRepository(), backend=FakeMemoryBackend(), **kwargs
    )


def _entry(
    *,
    user_id: str = "user-1",
    session_id: str = "s1",
    round_id: str = "r1",
    source_timestamp: datetime = datetime(2026, 1, 1, tzinfo=UTC),
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
    )


def _round(
    *,
    round_id: str,
    session_id: str,
    source_timestamp: str,
    content: str = "我养了一只猫，名字叫阿福。",
) -> AppendMemoryRequest:
    return AppendMemoryRequest(
        request_id=f"req-{round_id}",
        user_id="user-1",
        session_id=session_id,
        round_id=round_id,
        messages=[
            MemoryMessage(
                message_id=f"{round_id}-u",
                role=MessageRole.USER,
                content=content,
                timestamp=datetime.fromisoformat(source_timestamp),
            )
        ],
        source_timestamp=datetime.fromisoformat(source_timestamp),
    )


# --- 冲突策略回落 -------------------------------------------------------


def test_an_unregistered_memory_type_falls_back_to_the_overwrite_conflict_policy() -> None:
    """类型没注册时不能悄悄换成别的策略，也不能拒绝写入 —— 回落 overwrite。"""
    service = _service()

    assert service._conflict_policy({"memory_type": "not-registered"}, "thinkback") == "overwrite"


def test_the_conflict_policy_comes_from_the_registered_type() -> None:
    """配置说 append 就 append：事件类记忆被覆盖就是丢历史。"""
    service = _service()

    assert service._conflict_policy({"memory_type": "event"}, "thinkback") == "append"
    assert service._conflict_policy({"memory_type": "profile"}, "thinkback") == "overwrite"


def test_a_memory_without_a_declared_type_uses_the_overwrite_policy() -> None:
    service = _service()

    assert service._conflict_policy({}, "thinkback") == "overwrite"


# --- 索引生命周期 -------------------------------------------------------


def test_an_unknown_memory_type_gets_no_expiration() -> None:
    """类型查不到时不能凭空造一个到期日 —— 否则事实会被误判过期。"""
    service = _service()

    _, expires_at, _ = service._index_lifecycle({"memory_type": "not-registered"}, "thinkback")

    assert expires_at is None


def test_a_ttl_type_expires_at_the_end_of_its_last_day() -> None:
    """Mem0 的 expiration_date 是「这天之后失效」：按当天结束计。"""
    service = _service()

    _, expires_at, _ = service._index_lifecycle({"memory_type": "state"}, "thinkback")

    assert expires_at is not None
    assert (expires_at.hour, expires_at.minute, expires_at.second) == (23, 59, 59)
    assert expires_at.microsecond == 999999


def test_a_replayed_memory_keeps_the_expiration_it_was_written_with() -> None:
    """重放沿用写入时的到期日：否则恢复机制会给本该过期的事实续命。"""
    service = _service()

    _, expires_at, _ = service._index_lifecycle(
        {"memory_type": "state", "expires_at": "2020-01-01"}, "thinkback"
    )

    assert expires_at is not None
    assert expires_at.year == 2020
    assert expires_at.month == 1
    assert expires_at.day == 1


def test_a_forever_type_never_expires() -> None:
    service = _service()

    _, expires_at, _ = service._index_lifecycle({"memory_type": "profile"}, "thinkback")

    assert expires_at is None


def test_the_index_lifecycle_records_the_source_time_as_valid_at() -> None:
    """valid_at 是「事实何时为真」，不是「何时写下」。"""
    service = _service()

    valid_at, _, recorded_at = service._index_lifecycle(
        {"source_timestamp": "2020-06-01T00:00:00Z"}, "thinkback"
    )

    assert valid_at == datetime(2020, 6, 1, tzinfo=UTC)
    assert recorded_at is not None
    assert recorded_at.tzinfo is not None


# --- 时间解析 -----------------------------------------------------------


def test_a_naive_source_timestamp_is_read_as_utc() -> None:
    service = _service()

    parsed = service._parse_source_timestamp(datetime(2026, 5, 4, 10, 0, 0))

    assert parsed == datetime(2026, 5, 4, 10, 0, 0, tzinfo=UTC)


def test_an_unparsable_source_timestamp_is_ignored() -> None:
    """解析不了就当没有来源时间，不能让整条写入崩掉。"""
    service = _service()

    assert service._parse_source_timestamp("not-a-timestamp") is None
    assert service._parse_source_timestamp("") is None
    assert service._parse_source_timestamp(None) is None
    assert service._parse_iso_datetime("not-a-timestamp") is None
    assert service._parse_iso_datetime("") is None
    assert service._parse_iso_datetime(None) is None


def test_a_naive_round_timestamp_is_compared_against_an_aware_cutoff_as_utc() -> None:
    """时区混用不能变成 TypeError，也不能按本地时区算。"""
    naive = datetime(2026, 5, 4, 10, 0, 0)
    cutoff = datetime(2026, 5, 4, 9, 0, 0, tzinfo=UTC)

    assert MemoryService._round_is_after_timestamp(naive, cutoff) is True
    assert MemoryService._round_is_after_timestamp(cutoff, naive) is False


# --- 重建时的槽位覆盖判定 ------------------------------------------------


def test_a_round_whose_slot_is_covered_by_the_same_source_ref_is_not_rebuilt() -> None:
    """同一来源轮的槽位已被现有记忆覆盖 —— 这轮不必再抽一次。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    memory = repository.add_memory_index(
        backend_memory_id="backend-1",
        user_id="user-1",
        memory_scope_id="thinkback",
        source_refs=[{"session_id": "s1", "round_id": "r1"}],
        memory_text=LOCATION_TEXT,
    )

    assert service._memory_conflict_slot(memory.memory_text) == LOCATION_SLOT
    covered = service._rebuild_slot_is_covered_by_active_memory(_entry(), LOCATION_SLOT, [memory])

    assert covered is True


def test_a_round_whose_slot_is_not_held_by_any_active_memory_is_rebuilt() -> None:
    """槽位没人占 —— 这轮必须重抽。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    memory = repository.add_memory_index(
        backend_memory_id="backend-1",
        user_id="user-1",
        memory_scope_id="thinkback",
        source_refs=[{"session_id": "s1", "round_id": "r1"}],
        memory_text=LOCATION_TEXT,
    )

    covered = service._rebuild_slot_is_covered_by_active_memory(_entry(), FOOD_SLOT, [memory])

    assert covered is False


def test_a_newer_active_memory_covers_an_older_round_in_the_same_slot() -> None:
    """事实按来源时间定新旧：新记忆覆盖旧轮次。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    repository.save_round(
        _round(round_id="r-old", session_id="s1", source_timestamp="2026-01-01T00:00:00Z")
    )
    repository.save_round(
        _round(round_id="r-new", session_id="s2", source_timestamp="2026-06-01T00:00:00Z")
    )
    newer_memory = repository.add_memory_index(
        backend_memory_id="backend-new",
        user_id="user-1",
        memory_scope_id="thinkback",
        source_refs=[{"session_id": "s2", "round_id": "r-new"}],
        memory_text=LOCATION_TEXT,
    )
    older_entry = _entry(
        session_id="s1",
        round_id="r-old",
        source_timestamp=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert (
        service._rebuild_slot_is_covered_by_active_memory(
            older_entry, LOCATION_SLOT, [newer_memory]
        )
        is True
    )


def test_an_older_active_memory_does_not_cover_a_newer_round_in_the_same_slot() -> None:
    """方向不能写反：旧记忆不覆盖新轮次（新事实更该被抽出来）。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    repository.save_round(
        _round(round_id="r-old", session_id="s1", source_timestamp="2026-01-01T00:00:00Z")
    )
    repository.save_round(
        _round(round_id="r-new", session_id="s2", source_timestamp="2026-06-01T00:00:00Z")
    )
    older_memory = repository.add_memory_index(
        backend_memory_id="backend-old",
        user_id="user-1",
        memory_scope_id="thinkback",
        source_refs=[{"session_id": "s1", "round_id": "r-old"}],
        memory_text=LOCATION_TEXT,
    )
    newer_entry = _entry(
        session_id="s2",
        round_id="r-new",
        source_timestamp=datetime(2026, 6, 1, tzinfo=UTC),
    )

    assert (
        service._rebuild_slot_is_covered_by_active_memory(
            newer_entry, LOCATION_SLOT, [older_memory]
        )
        is False
    )


# --- 类型准入与 L3 写入跳过 ---------------------------------------------


def _registry_with(**overrides: Any) -> MemoryTypeRegistry:
    fields: dict[str, Any] = {
        "name": "custom",
        "extractor": "mem0",
        "source_roles": ("user",),
        "conflict": "overwrite",
        "retention": "forever",
        "classification": "personal",
    }
    fields.update(overrides)
    return MemoryTypeRegistry(
        namespace="thinkback", version="1", types={"custom": MemoryTypeSpec(**fields)}
    )


def test_a_type_without_an_extractor_skips_the_l3_write() -> None:
    """extractor=none 的类型（程序性规则）不抽取，但也不该报错。"""
    registry = _registry_with(extractor="none")
    repository = InMemoryMemoryRepository()
    service = MemoryService(
        repository=repository,
        backend=FakeMemoryBackend(),
        registry_provider=lambda _namespace: registry,
    )

    response = service.append(
        _round(
            round_id="r-none", session_id="s1", source_timestamp="2026-05-04T10:00:00Z"
        ).model_copy(update={"metadata": {"memory_type": "custom"}})
    )

    task = repository.get_task(response.task_id)
    assert task is not None
    assert task.result["l3_events"] == [{"event": "SKIP", "reason": "type_not_extractable"}]


def test_a_type_whose_roles_exclude_the_source_skips_the_l3_write() -> None:
    """助手的话不是用户事实来源：类型不接受该角色就不抽取。"""
    registry = _registry_with(source_roles=("tool",))
    repository = InMemoryMemoryRepository()
    service = MemoryService(
        repository=repository,
        backend=FakeMemoryBackend(),
        registry_provider=lambda _namespace: registry,
    )

    response = service.append(
        _round(
            round_id="r-role", session_id="s1", source_timestamp="2026-05-04T10:00:00Z"
        ).model_copy(update={"metadata": {"memory_type": "custom"}})
    )

    task = repository.get_task(response.task_id)
    assert task is not None
    assert task.result["l3_events"] == [{"event": "SKIP", "reason": "role_not_allowed_for_type"}]


def test_the_pre_extract_gate_skips_the_l3_write_for_a_round_with_no_p0_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """廉价过滤层判不通过时只落 L1/L2，不进 mem0 —— 不留「写进去但永不可召回」的暗数据。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    monkeypatch.setattr(config_module.settings, "memory_p0_slots", "favorite:food")

    response = service.append(
        _round(
            round_id="r-gate",
            session_id="s1",
            source_timestamp="2026-05-04T10:00:00Z",
            content="今天天气不错。",
        ).model_copy(update={"metadata": {}})
    )

    task = repository.get_task(response.task_id)
    assert task is not None
    assert task.result["l3_events"] == [{"event": "SKIP", "reason": "pre_extract_gate_failed"}]


# --- 召回的降级路径 -----------------------------------------------------


def test_a_broken_type_registry_does_not_break_recall() -> None:
    """注册表坏了不该让召回整个挂掉 —— 降级成「没有类型级上限」。"""

    def _broken(_namespace: str) -> MemoryTypeRegistry:
        raise RuntimeError("registry unavailable")

    service = MemoryService(
        repository=InMemoryMemoryRepository(),
        backend=FakeMemoryBackend(),
        registry_provider=_broken,
    )

    assert service._type_recall_caps("thinkback") == {}


def test_the_type_recall_caps_come_from_the_registry() -> None:
    registry = MemoryTypeRegistry(
        namespace="thinkback",
        version="1",
        types={
            "capped": MemoryTypeSpec(name="capped", recall_budget=128),
            "uncapped": MemoryTypeSpec(name="uncapped", recall_budget=None),
        },
    )
    service = MemoryService(
        repository=InMemoryMemoryRepository(),
        backend=FakeMemoryBackend(),
        registry_provider=lambda _namespace: registry,
    )

    assert service._type_recall_caps("thinkback") == {"capped": 128}


# --- 删除请求的参数校验 -------------------------------------------------


def test_a_memory_delete_without_a_memory_id_is_rejected() -> None:
    """服务层的兜底校验：绕开 schema 校验的调用方也不能删错东西。"""
    service = _service()
    request = DeleteMemoryRequest(
        request_id="req-1",
        user_id="user-1",
        scope=DeleteScope.MEMORY,
        operation_id="op-1",
        memory_id="mem-1",
    ).model_copy(update={"memory_id": None})

    with pytest.raises(ValueError, match="memory_id is required"):
        service._validate_delete_required_identifiers(request)


def test_a_session_delete_without_a_session_id_is_rejected() -> None:
    service = _service()
    request = DeleteMemoryRequest(
        request_id="req-1",
        user_id="user-1",
        scope=DeleteScope.SESSION,
        operation_id="op-1",
        session_id="s1",
    ).model_copy(update={"session_id": None})

    with pytest.raises(ValueError, match="session_id is required"):
        service._validate_delete_required_identifiers(request)


def test_a_complete_delete_request_passes_the_identifier_check() -> None:
    service = _service()
    request = DeleteMemoryRequest(
        request_id="req-1",
        user_id="user-1",
        scope=DeleteScope.MEMORY,
        operation_id="op-1",
        memory_id="mem-1",
    )

    assert service._validate_delete_required_identifiers(request) is None


# --- 可管理记忆的判定 ---------------------------------------------------


def test_a_tombstone_is_not_a_manageable_memory() -> None:
    """墓碑不参与管理台操作：它是屏障，不是记忆。"""
    service = _service()

    class _Memory:
        backend_memory_id = "backend-1"
        metadata = {"delete_all_tombstone": True}

    assert service._is_manageable_memory(_Memory()) is False


def test_a_deleted_source_tombstone_id_is_not_a_manageable_memory() -> None:
    service = _service()

    class _Memory:
        backend_memory_id = "deleted-source:abc"
        metadata: dict[str, Any] = {}

    assert service._is_manageable_memory(_Memory()) is False


def test_an_ordinary_memory_is_manageable() -> None:
    service = _service()

    class _Memory:
        backend_memory_id = "backend-1"
        metadata = {"source_text": "我养了一只猫"}

    assert service._is_manageable_memory(_Memory()) is True


# --- L3 结果的相关性判定（ADR-0004 的放行口径）--------------------------


def test_a_backend_item_without_any_score_is_accepted() -> None:
    """后端没给分数（fake 后端就是）：它自己已按阈值筛过，服务侧不再拦。"""

    assert MemoryService._l3_item_relevant({"id": "x"}, threshold=0.9) is True


def test_a_semantic_score_at_the_threshold_is_accepted() -> None:
    item = {"id": "x", "score_details": {"semantic_score": 0.5}}
    assert MemoryService._l3_item_relevant(item, threshold=0.5) is True


def test_a_semantic_score_below_the_threshold_is_rejected() -> None:
    item = {"id": "x", "score_details": {"semantic_score": 0.49}}
    assert MemoryService._l3_item_relevant(item, threshold=0.5) is False


def test_the_combined_score_carries_a_low_semantic_score() -> None:
    """ADR-0004：语义分不高但词命中的事实要放行 —— 中文里词命中是有分量的。"""

    item = {"id": "x", "score_details": {"semantic_score": 0.39}, "score": 0.69}
    assert MemoryService._l3_item_relevant(item, threshold=0.5) is True


def test_both_scores_below_the_threshold_are_rejected() -> None:
    item = {"id": "x", "score_details": {"semantic_score": 0.1}, "score": 0.2}
    assert MemoryService._l3_item_relevant(item, threshold=0.5) is False


def test_a_non_numeric_score_is_not_treated_as_a_pass() -> None:
    """分数不是数字时不能当成「达标」 —— 那是凭空放行。"""

    item = {"id": "x", "score_details": {"semantic_score": "0.9"}, "score": "0.9"}
    assert MemoryService._l3_item_relevant(item, threshold=0.5) is False


# --- 召回的 L3 过滤：条数上限与失效闸门 --------------------------------


def _seed_backend_memory(
    repository: InMemoryMemoryRepository,
    backend: FakeMemoryBackend,
    *,
    backend_id: str,
    text: str,
    round_id: str,
    expires_at: datetime | None = None,
    invalid_at: datetime | None = None,
) -> Any:
    backend.memories[backend_id] = {
        "id": backend_id,
        "memory": text,
        "event": "ADD",
        "user_id": "user-1",
        "agent_id": "thinkback",
        "metadata": {},
        "score": 1.0,
    }
    entry = repository.add_memory_index(
        backend_memory_id=backend_id,
        user_id="user-1",
        memory_scope_id="thinkback",
        source_refs=[{"session_id": "s1", "round_id": round_id}],
        memory_text=text,
        expires_at=expires_at,
    )
    if invalid_at is not None:
        entry.invalid_at = invalid_at
    return entry


def test_the_l3_limit_caps_the_number_of_recalled_backend_hits() -> None:
    """l3_limit 是「最多返回几条」的上限，不是建议值。"""

    repository = InMemoryMemoryRepository()
    backend = FakeMemoryBackend()
    service = MemoryService(repository=repository, backend=backend)
    for index in range(3):
        _seed_backend_memory(
            repository,
            backend,
            backend_id=f"b{index}",
            text=f"RCA 槽排在第 {index + 1} 道",
            round_id=f"r{index}",
        )

    response = service.recall(
        RecallMemoryRequest(user_id="user-1", session_id="s1", query="RCA 槽", l3_limit=2)
    )

    assert len([item for item in response.items if item.layer == "L3"]) == 2


def test_an_expired_memory_is_not_recalled() -> None:
    """过期事实不进上下文：后端会隐藏它，本地索引也必须挡住它。"""

    repository = InMemoryMemoryRepository()
    backend = FakeMemoryBackend()
    service = MemoryService(repository=repository, backend=backend)
    _seed_backend_memory(
        repository,
        backend,
        backend_id="expired",
        text="RCA 槽排在第 3 道",
        round_id="r-expired",
        expires_at=datetime(2020, 1, 1, tzinfo=UTC),
    )

    response = service.recall(
        RecallMemoryRequest(user_id="user-1", session_id="s1", query="RCA 槽")
    )

    assert not [item for item in response.items if item.layer == "L3"]


def test_a_memory_with_an_invalid_at_is_not_recalled() -> None:
    """双时态兜底：失效时刻已定的记忆不再进入召回（状态过滤之外的第二道闸）。"""

    repository = InMemoryMemoryRepository()
    backend = FakeMemoryBackend()
    service = MemoryService(repository=repository, backend=backend)
    _seed_backend_memory(
        repository,
        backend,
        backend_id="invalid",
        text="RCA 槽排在第 3 道",
        round_id="r-invalid",
        invalid_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    response = service.recall(
        RecallMemoryRequest(user_id="user-1", session_id="s1", query="RCA 槽")
    )

    assert not [item for item in response.items if item.layer == "L3"]


def test_an_active_memory_is_recalled() -> None:
    """对照组：上面三条闸门关掉时，正常记忆必须能召回（否则全是假红）。"""

    repository = InMemoryMemoryRepository()
    backend = FakeMemoryBackend()
    service = MemoryService(repository=repository, backend=backend)
    _seed_backend_memory(
        repository, backend, backend_id="live", text="RCA 槽排在第 3 道", round_id="r-live"
    )

    response = service.recall(
        RecallMemoryRequest(user_id="user-1", session_id="s1", query="RCA 槽")
    )

    assert [item for item in response.items if item.layer == "L3"]


# --- 擦除取回轮次引用：会话级擦除不许顺带清别人的原文 -------------------


def _tombstone(
    repository: InMemoryMemoryRepository,
    *,
    backend_id: str,
    metadata: dict[str, Any],
    source_refs: list[dict[str, str]],
) -> Any:
    return repository.add_memory_index(
        backend_memory_id=backend_id,
        user_id="user-1",
        memory_scope_id="thinkback",
        source_refs=source_refs,
        memory_text="tombstone",
        metadata=metadata,
    )


def test_a_memory_scope_delete_takes_no_round_refs() -> None:
    """单条删除没有「这一轮原文要不要清」的问题 —— 不取引用。"""

    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    _tombstone(
        repository,
        backend_id="t1",
        metadata={"delete_all_tombstone": True},
        source_refs=[{"session_id": "s1", "round_id": "r1"}],
    )

    assert service._deleted_round_refs("user-1", "thinkback", DeleteScope.MEMORY) == []


def test_a_session_delete_ignores_the_delete_all_tombstone() -> None:
    """delete-all 墓碑覆盖所有会话：会话级擦除不能据此清别人的原文。"""

    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    _tombstone(
        repository,
        backend_id="t-all",
        metadata={"delete_all_tombstone": True},
        source_refs=[{"session_id": "other-session", "round_id": "r-other"}],
    )

    assert (
        service._deleted_round_refs("user-1", "thinkback", DeleteScope.SESSION, session_id="s1")
        == []
    )


def test_a_session_delete_ignores_another_sessions_tombstone() -> None:
    """别人的会话墓碑不能被本次会话擦除顺带走。"""

    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    _tombstone(
        repository,
        backend_id="t-other",
        metadata={"session_delete_tombstone": True, "deleted_session_id": "other-session"},
        source_refs=[{"session_id": "other-session", "round_id": "r-other"}],
    )

    assert (
        service._deleted_round_refs("user-1", "thinkback", DeleteScope.SESSION, session_id="s1")
        == []
    )


def test_a_session_delete_takes_its_own_tombstone_refs() -> None:
    """本会话的墓碑要取回轮次引用 —— 擦除阶段按引用回原文。"""

    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    _tombstone(
        repository,
        backend_id="t-own",
        metadata={"session_delete_tombstone": True, "deleted_session_id": "s1"},
        source_refs=[{"session_id": "s1", "round_id": "r1"}],
    )

    assert service._deleted_round_refs(
        "user-1", "thinkback", DeleteScope.SESSION, session_id="s1"
    ) == [{"session_id": "s1", "round_id": "r1"}]


def test_a_user_wide_delete_takes_every_tombstone_ref() -> None:
    """ALL 范围：delete-all 与会话墓碑都要取回引用。"""

    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    _tombstone(
        repository,
        backend_id="t-all",
        metadata={"delete_all_tombstone": True},
        source_refs=[{"session_id": "s9", "round_id": "r9"}],
    )
    _tombstone(
        repository,
        backend_id="t-session",
        metadata={"session_delete_tombstone": True, "deleted_session_id": "s1"},
        source_refs=[{"session_id": "s1", "round_id": "r1"}],
    )

    refs = service._deleted_round_refs("user-1", "thinkback", DeleteScope.ALL)
    assert {ref["round_id"] for ref in refs} == {"r1", "r9"}


def test_an_ordinary_memory_is_not_a_tombstone() -> None:
    """普通记忆行不是墓碑 —— 擦除阶段不能拿它去回原文。"""

    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    _tombstone(
        repository,
        backend_id="ordinary",
        metadata={"source_text": "我养了一只猫"},
        source_refs=[{"session_id": "s1", "round_id": "r1"}],
    )

    assert service._deleted_round_refs("user-1", "thinkback", DeleteScope.ALL) == []


def test_a_tombstone_without_a_round_id_contributes_no_ref() -> None:
    """引用里没有 round_id 就没法回原文 —— 不能塞一个空引用进去。"""

    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    _tombstone(
        repository,
        backend_id="t-empty",
        metadata={"delete_all_tombstone": True},
        source_refs=[{"session_id": "s1", "round_id": ""}],
    )

    assert service._deleted_round_refs("user-1", "thinkback", DeleteScope.ALL) == []


# --- 类型名解析与准入 ----------------------------------------------------


def test_a_memory_type_enum_resolves_to_its_value() -> None:
    from thinkback.domain.enums import MemoryType

    request = AppendMemoryRequest(
        request_id="req-1",
        user_id="user-1",
        session_id="s1",
        round_id="r1",
        messages=[
            MemoryMessage(
                message_id="m1",
                role=MessageRole.USER,
                content="hi",
                timestamp=datetime(2026, 1, 1, tzinfo=UTC),
            )
        ],
        source_timestamp=datetime(2026, 1, 1, tzinfo=UTC),
    ).model_copy(update={"memory_type": MemoryType.PROFILE})

    assert MemoryService._resolve_type_name(request) == MemoryType.PROFILE.value


def test_a_declared_type_name_is_trimmed() -> None:
    request = AppendMemoryRequest(
        request_id="req-1",
        user_id="user-1",
        session_id="s1",
        round_id="r1",
        messages=[
            MemoryMessage(
                message_id="m1",
                role=MessageRole.USER,
                content="hi",
                timestamp=datetime(2026, 1, 1, tzinfo=UTC),
            )
        ],
        source_timestamp=datetime(2026, 1, 1, tzinfo=UTC),
    ).model_copy(update={"memory_type": "  state  "})

    assert MemoryService._resolve_type_name(request) == "state"


def test_an_unregistered_type_does_not_use_attribute_keys() -> None:
    """类型没注册 → 没有抽取指令 → 不走属性键路径（而不是随便挑一个类型）。"""

    service = _service()

    assert service._type_uses_attribute_keys("no-such-type", "thinkback") is False


def test_an_unresolvable_conflict_key_is_not_reported_for_an_unknown_type() -> None:
    """类型都没注册，谈不上「声明了冲突策略却算不出键」。"""

    service = _service()

    service._report_unresolvable_conflict_key("no-such-type", "thinkback")


def test_an_unresolvable_conflict_key_is_not_reported_for_a_non_overwrite_policy() -> None:
    """append / none 策略本来就不靠冲突键生效，报了是噪音。"""

    registry = MemoryTypeRegistry(
        namespace="thinkback",
        version="1",
        types={"appender": MemoryTypeSpec(name="appender", conflict="append")},
    )
    service = MemoryService(
        repository=InMemoryMemoryRepository(),
        backend=FakeMemoryBackend(),
        registry_provider=lambda _namespace: registry,
    )

    service._report_unresolvable_conflict_key("appender", "thinkback")


# --- 启动时的待写重放：只重放「该重放的那一批」-------------------------


def _pending_task(
    repository: InMemoryMemoryRepository,
    *,
    task_id: str,
    op_type: Any,
    scope: dict[str, Any],
) -> Any:
    from thinkback.domain.entities import TaskEntry
    from thinkback.domain.enums import TaskStatus

    return repository.save_task(
        TaskEntry(
            task_id=task_id,
            request_id=f"req-{task_id}",
            op_type=op_type,
            status=TaskStatus.RUNNING,
            scope=scope,
        )
    )


def test_the_recovery_skips_tasks_that_are_not_round_writes() -> None:
    """删除 / 重建任务不该被当成 L3 写入重放 —— 那是另一条恢复路径。"""

    from thinkback.domain.enums import OperationType

    repository = InMemoryMemoryRepository()
    service = MemoryService(
        repository=repository, backend=FakeMemoryBackend(), l3_write_mode="async"
    )
    _pending_task(
        repository,
        task_id="t-delete",
        op_type=OperationType.DELETE_MEMORY,
        scope={"round_id": "r1", "user_id": "user-1"},
    )

    assert service.recover_pending_writes() == 0


def test_the_recovery_skips_a_task_without_a_complete_scope() -> None:
    """缺 round_id 或 user_id 的任务没法定位原文 —— 跳过而不是乱猜。"""

    from thinkback.domain.enums import OperationType

    repository = InMemoryMemoryRepository()
    service = MemoryService(
        repository=repository, backend=FakeMemoryBackend(), l3_write_mode="async"
    )
    _pending_task(
        repository,
        task_id="t-no-user",
        op_type=OperationType.WRITE_ROUND,
        scope={"round_id": "r1"},
    )
    _pending_task(
        repository,
        task_id="t-no-round",
        op_type=OperationType.WRITE_ROUND,
        scope={"user_id": "user-1"},
    )

    assert service.recover_pending_writes() == 0


def test_the_recovery_skips_a_round_that_was_already_erased() -> None:
    """轮次已被擦除：不重放 —— 否则把擦掉的原文又写回 L3。"""

    from thinkback.domain.enums import OperationType

    repository = InMemoryMemoryRepository()
    service = MemoryService(
        repository=repository, backend=FakeMemoryBackend(), l3_write_mode="async"
    )
    _pending_task(
        repository,
        task_id="t-erased",
        op_type=OperationType.WRITE_ROUND,
        scope={"round_id": "gone", "user_id": "user-1"},
    )

    assert service.recover_pending_writes() == 0


def test_the_recovery_is_a_no_op_in_sync_mode() -> None:
    """同步模式下没有「未完成的异步写入」可重放。"""

    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())

    assert service.recover_pending_writes() == 0


# --- 编辑：找不到记忆时必须报错，而不是静默成功 ------------------------


def test_updating_a_memory_that_does_not_exist_is_rejected() -> None:
    """找不到就是找不到 —— 不能返回「成功」让调用方以为改掉了。"""

    from thinkback.memory.schemas import UpdateMemoryRequest

    service = _service()
    request = UpdateMemoryRequest(
        request_id="req-1",
        user_id="user-1",
        operation_id="op-1",
        memory_id="no-such-memory",
        content="new text",
    )

    class _Memory:
        memory_id = "no-such-memory"

    with pytest.raises(ValueError, match="memory not found"):
        service._apply_memory_update(
            _Memory(),
            request=request,
            l3_scope_id="thinkback",
        )


def test_updating_to_an_unregistered_memory_type_is_rejected() -> None:
    """未注册的类型是调用方的错误 —— 报出来，不是内部故障。"""

    from thinkback.memory.schemas import UpdateMemoryRequest

    service = _service()
    request = UpdateMemoryRequest(
        request_id="req-1",
        user_id="user-1",
        operation_id="op-1",
        memory_id="mem-1",
        content="new text",
        memory_type_name="no-such-type",
    )

    with pytest.raises(ValueError):
        service._resolve_updated_memory_type(request)


def test_updating_to_a_type_that_requires_review_is_rejected() -> None:
    """程序性规则要审批，而这里没有审批通道 —— 不能让普通编辑绕过去。"""

    from thinkback.memory.schemas import UpdateMemoryRequest

    registry = MemoryTypeRegistry(
        namespace="thinkback",
        version="1",
        types={"procedure": MemoryTypeSpec(name="procedure", review_required=True)},
    )
    service = MemoryService(
        repository=InMemoryMemoryRepository(),
        backend=FakeMemoryBackend(),
        registry_provider=lambda _namespace: registry,
    )
    request = UpdateMemoryRequest(
        request_id="req-1",
        user_id="user-1",
        operation_id="op-1",
        memory_id="mem-1",
        content="new text",
        memory_type_name="procedure",
    )

    with pytest.raises(ValueError, match="requires review"):
        service._resolve_updated_memory_type(request)
