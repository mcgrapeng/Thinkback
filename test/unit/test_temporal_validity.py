"""P2#6：时序两列（valid_at/invalid_at + 新事实优先）的单元测试。

覆盖：
- 仓储：add 默认/显式 valid_at、mark deleted/superseded 置 invalid_at（幂等）、
  mark active 清除 invalid_at、读写往返（in-memory；SQL 经 mapper 断言字段接线）。
- 服务召回：同槽位多条 ACTIVE 只回填 valid_at 最新一条；invalid_at 非空
  的记忆不进入召回（backend 命中与槽位回填双路径）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from thinkback.memory.backends import FakeMemoryBackend
from thinkback.memory.repositories import InMemoryMemoryRepository
from thinkback.memory.schemas import RecallMemoryRequest
from thinkback.memory.service import MemoryService


def _add_memory(
    repository: InMemoryMemoryRepository,
    *,
    backend_id: str,
    text: str,
    valid_at: datetime | None = None,
    round_id: str = "round-x",
) -> Any:
    return repository.add_memory_index(
        backend_memory_id=backend_id,
        user_id="user-1",
        memory_scope_id="innies",
        source_refs=[{"session_id": "session-1", "round_id": round_id}],
        memory_text=text,
        valid_at=valid_at,
    )


# ── 仓储语义 ─────────────────────────────────────────────────────────


def test_add_memory_index_defaults_valid_at_to_now() -> None:
    repository = InMemoryMemoryRepository()
    before = datetime.now(UTC)
    entry = _add_memory(repository, backend_id="b1", text="User has a cat named 麻薯")
    after = datetime.now(UTC)
    assert entry.valid_at is not None
    assert before <= entry.valid_at <= after
    assert entry.invalid_at is None


def test_add_memory_index_accepts_explicit_valid_at() -> None:
    repository = InMemoryMemoryRepository()
    explicit = datetime(2026, 1, 1, tzinfo=UTC)
    entry = _add_memory(repository, backend_id="b2", text="User lives in 北京", valid_at=explicit)
    assert entry.valid_at == explicit


def test_mark_deleted_and_superseded_stamp_invalid_at_idempotently() -> None:
    repository = InMemoryMemoryRepository()
    entry = _add_memory(repository, backend_id="b3", text="User has a dog named 旺财")
    first_stamp = datetime.now(UTC) - timedelta(hours=1)
    entry.invalid_at = first_stamp  # 模拟已存在的更早失效时刻

    repository.mark_memory_superseded(entry.memory_id)
    got = repository.get_active_memory_by_backend_id("user-1", "innies", "b3")
    assert got is None  # 已非 ACTIVE
    listed = repository.list_memories("user-1", "innies")
    assert listed[0].invalid_at == first_stamp  # 幂等：不覆盖更早时刻

    entry2 = _add_memory(repository, backend_id="b4", text="User favorite drink: 咖啡")
    assert entry2.invalid_at is None
    repository.mark_memory_deleted(entry2.memory_id)
    listed2 = repository.list_memories("user-1", "innies")
    deleted = next(m for m in listed2 if m.backend_memory_id == "b4")
    assert deleted.invalid_at is not None


def test_mark_active_clears_invalid_at() -> None:
    repository = InMemoryMemoryRepository()
    entry = _add_memory(repository, backend_id="b5", text="User lives in 上海")
    repository.mark_memory_superseded(entry.memory_id)
    assert repository.list_memories("user-1", "innies")[0].invalid_at is not None

    repository.mark_memory_active(entry.memory_id)
    reactivated = repository.get_active_memory_by_backend_id("user-1", "innies", "b5")
    assert reactivated is not None
    assert reactivated.invalid_at is None
    assert reactivated.valid_at is not None


def test_update_memory_index_accepts_valid_at() -> None:
    repository = InMemoryMemoryRepository()
    entry = _add_memory(repository, backend_id="b6", text="old text")
    new_valid = datetime(2026, 6, 1, tzinfo=UTC)
    repository.update_memory_index(
        entry.memory_id,
        source_refs=[{"session_id": "session-1", "round_id": "round-x"}],
        memory_text="new text",
        valid_at=new_valid,
    )
    got = repository.get_active_memory_by_backend_id("user-1", "innies", "b6")
    assert got is not None and got.valid_at == new_valid


# ── 服务召回仲裁 ─────────────────────────────────────────────────────


def _recall(service: MemoryService, query: str) -> Any:
    return service.recall(
        RecallMemoryRequest(user_id="user-1", session_id="session-1", query=query)
    )


def test_slot_backfill_returns_only_newest_valid_fact() -> None:
    """同槽位两条 ACTIVE（模拟异步竞争/旧副本遗留）：只回填 valid_at 最新。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    old_time = datetime(2026, 1, 1, tzinfo=UTC)
    new_time = datetime(2026, 9, 1, tzinfo=UTC)
    _add_memory(repository, backend_id="loc-old", text="User lives in 北京", valid_at=old_time)
    _add_memory(repository, backend_id="loc-new", text="User lives in 杭州", valid_at=new_time)

    recall = _recall(service, "你现在住在哪里？")
    slot_items = [
        item
        for item in recall.items
        if item.layer == "L3" and item.metadata.get("slot_backfill") == "current_location"
    ]
    assert len(slot_items) == 1
    assert "杭州" in slot_items[0].content


def test_slot_backfill_legacy_null_valid_at_treated_as_oldest() -> None:
    """遗留行（valid_at=None）与带时间的新行并存：新行胜出。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    legacy = _add_memory(repository, backend_id="nick-old", text="叫我老张")
    legacy.valid_at = None  # 模拟迁移前的遗留行
    _add_memory(
        repository,
        backend_id="nick-new",
        text="叫我小朋",
        valid_at=datetime(2026, 9, 1, tzinfo=UTC),
    )

    recall = _recall(service, "你应该怎么称呼我？")
    slot_items = [
        item
        for item in recall.items
        if item.layer == "L3" and item.metadata.get("slot_backfill") == "preferred_nickname"
    ]
    assert len(slot_items) == 1
    assert "小朋" in slot_items[0].content


def test_invalidated_memory_excluded_from_slot_backfill() -> None:
    """invalid_at 已置但状态仍 ACTIVE 的异常行（多副本竞争）：不进回填。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    stale = _add_memory(
        repository,
        backend_id="drink-stale",
        text="User favorite drink: 拿铁",
        valid_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    stale.invalid_at = datetime(2026, 8, 1, tzinfo=UTC)  # 直接构造异常态
    _add_memory(
        repository,
        backend_id="drink-fresh",
        text="User favorite drink: 美式咖啡",
        valid_at=datetime(2026, 9, 1, tzinfo=UTC),
    )

    recall = _recall(service, "我最喜欢喝什么？")
    l3_contents = [item.content for item in recall.items if item.layer == "L3"]
    assert any("美式咖啡" in content for content in l3_contents)
    assert not any("拿铁" in content for content in l3_contents)


def test_supersede_on_write_stamps_invalid_at() -> None:
    """append 写入同槽位新事实后，旧记忆被 supersede 且带失效时间（端到端）。"""
    from test.unit.test_l2_llm_summary import make_append

    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    service.append(make_append("r-pet-1", "我养了一只猫，名字叫麻薯"))
    service.append(make_append("r-pet-2", "我的猫不叫麻薯了，现在叫糯米", index=2))
    service.drain_l3_background_tasks(timeout=5)

    memories = repository.list_memories("user-1", "innies")
    # 本地 P0 槽位回填/supersede 路径：同槽位旧条目应带 invalid_at
    invalidated = [m for m in memories if m.invalid_at is not None]
    assert invalidated, "同槽位被取代的记忆应带 invalid_at"
    for memory in invalidated:
        assert memory.memory_status is not None
