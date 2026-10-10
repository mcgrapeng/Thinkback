"""仓储契约测试：``InMemoryMemoryRepository`` 与 ``SqlAlchemyMemoryRepository`` 行为必须一致。

同一组用例参数化跑两条后端（SQL 后端用 SQLite 文件库 + NullPool，
无需真实 PG 即可覆盖 async 查询/事务/CAS 分支）。任何一侧行为漂移，
对应测试即失败——这正是"内存假实现绿、生产实现红"的防线。

已知无害差异（断言中已归一，不视为漂移）：
- 时区：SQLite 读回 naive datetime，in-memory 保留 aware，断言统一去 tz；
- 任务行版本绝对值：创建后 in-memory 为 1、SQL 为 0，自洽即可，
  断言只验证"相对 +1"与 CAS 冲突。
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tests.unit.test_memory_service import make_append
from thinkback.domain.entities import AdminAuditEntry, TaskEntry
from thinkback.domain.enums import (
    DataClassification,
    MemoryStatus,
    OperationType,
    SourceType,
    SummaryState,
    TaskStatus,
)
from thinkback.domain.errors import TaskStaleWriteError
from thinkback.domain.keys import source_ref_key
from thinkback.infra.database.base import Base
from thinkback.infra.database.models import MemoryTaskRecord
from thinkback.memory.repositories import InMemoryMemoryRepository, SqlAlchemyMemoryRepository


def _naive(value: datetime | None) -> datetime | None:
    """SQLite 往返丢 tz；两条后端时间断言统一剥掉 tzinfo 再比。"""
    return value.replace(tzinfo=None) if value is not None else None


def _make_task(
    task_id: str = "task-1",
    *,
    status: TaskStatus = TaskStatus.RUNNING,
    row_version: int = 0,
) -> TaskEntry:
    return TaskEntry(
        task_id=task_id,
        request_id=f"req-{task_id}",
        op_type=OperationType.WRITE_ROUND,
        scope={"user_id": "user-1", "session_id": "session-1"},
        status=status,
        row_version=row_version,
    )


@pytest.fixture(params=["in_memory", "sqlalchemy"])
def repository(request: pytest.FixtureRequest, tmp_path: Path):  # type: ignore[no-untyped-def]
    if request.param == "in_memory":
        yield InMemoryMemoryRepository()
        return

    db_path = tmp_path / "contract.db"

    def _init_schema() -> None:
        async def _create() -> None:
            engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", poolclass=NullPool)
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            await engine.dispose()

        asyncio.run(_create())

    _init_schema()
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", poolclass=NullPool)
    repo = SqlAlchemyMemoryRepository(
        session_factory=async_sessionmaker(engine, expire_on_commit=False)
    )
    try:
        yield repo
    finally:
        repo.close()

        async def _dispose() -> None:
            await engine.dispose()

        asyncio.run(_dispose())


@pytest.fixture()
def age_task(repository):  # type: ignore[no-untyped-def]
    """把任务最后更新时间拨旧，触发 stale 回收路径（两条后端各自的时钟）。"""

    def _age(task_id: str, seconds: float) -> None:
        if isinstance(repository, InMemoryMemoryRepository):
            repository.task_updated_at[task_id] = time.time() - seconds
            return
        stamp = datetime.now(UTC) - timedelta(seconds=seconds)

        async def _backdate() -> None:
            async with repository.session_factory() as session:
                await session.execute(
                    update(MemoryTaskRecord)
                    .where(MemoryTaskRecord.task_id == task_id)
                    .values(updated_at=stamp)
                )
                await session.commit()

        repository._run(_backdate())

    return _age


# ---------------------------------------------------------------------------
# journal（回合流水）
# ---------------------------------------------------------------------------


def test_save_round_is_idempotent_first_write_wins(repository) -> None:  # type: ignore[no-untyped-def]
    first = repository.save_round(make_append(round_id="r1", content="第一版"))
    again = repository.save_round(make_append(round_id="r1", content="第二版不应覆盖"))

    assert again.journal_id == first.journal_id
    assert again.round_fingerprint == first.round_fingerprint
    stored = repository.get_round("r1")
    assert stored is not None
    assert stored.messages[0]["content"] == "第一版"


def test_save_round_keeps_pending_state_on_resave(repository) -> None:  # type: ignore[no-untyped-def]
    repository.save_round(make_append(round_id="r1"))
    assert repository.mark_round_pending_append("r1") is True

    repository.save_round(make_append(round_id="r1"))
    assert repository.list_rounds("user-1", "session-1") == []


def test_get_round_returns_missing_as_none(repository) -> None:  # type: ignore[no-untyped-def]
    assert repository.get_round("missing") is None


def test_list_rounds_orders_by_timestamp_then_round_index(repository) -> None:  # type: ignore[no-untyped-def]
    repository.save_round(
        make_append(round_id="round-10", source_timestamp="2026-05-04T10:00:03Z", round_index=10)
    )
    repository.save_round(
        make_append(round_id="round-2", source_timestamp="2026-05-04T10:00:03Z", round_index=2)
    )
    repository.save_round(
        make_append(round_id="round-early", source_timestamp="2026-05-04T09:00:00Z", round_index=99)
    )

    rounds = repository.list_rounds("user-1", "session-1")
    assert [entry.round_id for entry in rounds] == ["round-early", "round-2", "round-10"]

    scoped = repository.list_rounds("user-1", "session-1", "session-1")
    assert [entry.round_id for entry in scoped] == ["round-early", "round-2", "round-10"]
    assert repository.list_rounds("user-1", "session-1", "other-session") == []
    assert repository.list_rounds("user-1", "other-scope") == []


def test_list_rounds_null_round_index_sorts_first(repository) -> None:  # type: ignore[no-untyped-def]
    repository.save_round(
        make_append(round_id="round-3", source_timestamp="2026-05-04T10:00:03Z", round_index=3)
    )
    repository.save_round(
        make_append(round_id="round-null", source_timestamp="2026-05-04T10:00:03Z", round_index=None)
    )

    rounds = repository.list_rounds("user-1", "session-1")
    assert [entry.round_id for entry in rounds] == ["round-null", "round-3"]


def test_list_user_rounds_spans_sessions(repository) -> None:  # type: ignore[no-untyped-def]
    repository.save_round(make_append(round_id="r1", session_id="session-a"))
    repository.save_round(make_append(round_id="r2", session_id="session-b"))

    rounds = repository.list_user_rounds("user-1")
    assert sorted(entry.round_id for entry in rounds) == ["r1", "r2"]
    assert repository.list_user_rounds("user-missing") == []


def test_mark_round_states_round_trip(repository) -> None:  # type: ignore[no-untyped-def]
    repository.save_round(make_append(round_id="r1"))
    assert repository.mark_round_deleted("r1") is True
    assert repository.list_rounds("user-1", "session-1") == []
    assert repository.mark_round_active("r1") is True
    assert [entry.round_id for entry in repository.list_rounds("user-1", "session-1")] == ["r1"]

    assert repository.mark_round_deleted("missing") is False
    assert repository.mark_round_active("missing") is False
    assert repository.mark_round_pending_append("missing") is False


def test_mark_rounds_deleted_bulk_counts_and_scopes(repository) -> None:  # type: ignore[no-untyped-def]
    # journal 条目的 memory_scope_id 即 session_id，按 scope 整体墓碑。
    repository.save_round(make_append(round_id="r1", session_id="session-a"))
    repository.save_round(make_append(round_id="r2", session_id="session-a"))
    repository.save_round(make_append(round_id="r3", session_id="session-b"))
    repository.mark_round_deleted("r1")

    assert repository.mark_rounds_deleted("user-1", "session-a", "session-a") == 1
    assert repository.list_rounds("user-1", "session-a", "session-a") == []
    assert [entry.round_id for entry in repository.list_rounds("user-1", "session-b", "session-b")] == [
        "r3"
    ]

    assert repository.mark_rounds_deleted("user-1", "session-b") == 1
    assert repository.list_rounds("user-1", "session-b") == []


# ---------------------------------------------------------------------------
# L1 缓存
# ---------------------------------------------------------------------------


def test_get_l1_falls_back_to_journal_and_clears(repository) -> None:  # type: ignore[no-untyped-def]
    repository.save_round(make_append(round_id="r1", content="缓存回源"))
    entries = repository.get_l1("user-1", "session-1", "session-1")
    assert [entry.round_id for entry in entries] == ["r1"]

    repository.update_l1(entries[0], limit=5)
    repository.clear_l1("user-1", "session-1", "session-1")
    repopulated = repository.get_l1("user-1", "session-1", "session-1")
    assert [entry.round_id for entry in repopulated] == ["r1"]


# ---------------------------------------------------------------------------
# L2 摘要
# ---------------------------------------------------------------------------


def test_upsert_summary_from_rounds_tracks_cursor(repository) -> None:  # type: ignore[no-untyped-def]
    repository.save_round(make_append(round_id="r1", content="第一句", source_timestamp="2026-05-04T10:00:00Z"))
    repository.save_round(make_append(round_id="r2", content="第二句", source_timestamp="2026-05-04T10:00:03Z"))

    summary = repository.upsert_summary_from_rounds(
        "user-1", "session-1", repository.list_rounds("user-1", "session-1")
    )
    assert summary.summary_cursor_round == "r2"
    assert summary.latest_source_round_id == "r2"
    assert summary.summary_state is SummaryState.ACTIVE
    assert "第一句" in summary.summary_text

    fetched = repository.get_summary("user-1", "session-1")
    assert fetched is not None
    assert fetched.summary_id == summary.summary_id


def test_mark_summary_overrides_state(repository) -> None:  # type: ignore[no-untyped-def]
    repository.upsert_summary_from_rounds("user-1", "session-1", [])
    repository.mark_summary("user-1", "session-1", SummaryState.STALE)

    summary = repository.get_summary("user-1", "session-1")
    assert summary is not None
    assert summary.summary_state is SummaryState.STALE

    repository.mark_summary("user-missing", "session-1", SummaryState.DIRTY)
    assert repository.get_summary("user-missing", "session-1") is None


def test_upsert_summary_preserve_llm_blocks_concat_overwrite(repository) -> None:  # type: ignore[no-untyped-def]
    repository.upsert_summary_from_rounds(
        "user-1", "session-1", [], summary_text="LLM 综合画像", summary_kind="llm"
    )
    kept = repository.upsert_summary_from_rounds(
        "user-1",
        "session-1",
        [],
        summary_text="concat 占位",
        summary_kind="concat",
        preserve_llm=True,
    )
    assert kept.summary_kind == "llm"
    assert kept.summary_text == "LLM 综合画像"

    overwritten = repository.upsert_summary_from_rounds(
        "user-1", "session-1", [], summary_text="concat 占位", summary_kind="concat"
    )
    assert overwritten.summary_text == "concat 占位"
    assert overwritten.summary_kind == "concat"


def test_upsert_summary_from_journal_excludes_deleted_memory_sources(
    repository,
) -> None:  # type: ignore[no-untyped-def]
    repository.save_round(make_append(round_id="r1", content="请记住我叫阿鹏"))
    repository.save_round(make_append(round_id="r2", content="我住在沈阳"))
    memory = repository.add_memory_index(
        backend_memory_id="backend-1",
        user_id="user-1",
        memory_scope_id="session-1",
        source_refs=[{"session_id": "session-1", "round_id": "r1"}],
        memory_text="用户叫阿鹏",
    )
    repository.mark_memory_deleted(memory.memory_id)

    summary = repository.upsert_summary_from_journal("user-1", "session-1")
    assert "我住在沈阳" in summary.summary_text
    assert "请记住我叫阿鹏" not in summary.summary_text


# ---------------------------------------------------------------------------
# L3 记忆索引
# ---------------------------------------------------------------------------


def test_add_memory_index_upserts_by_backend_id(repository) -> None:  # type: ignore[no-untyped-def]
    first = repository.add_memory_index(
        backend_memory_id="backend-1",
        user_id="user-1",
        source_refs=[{"session_id": "s1", "round_id": "r1"}],
        memory_text="旧文本",
    )
    second = repository.add_memory_index(
        backend_memory_id="backend-1",
        user_id="user-1",
        source_refs=[{"session_id": "s1", "round_id": "r1"}],
        memory_text="新文本",
    )

    assert second.memory_id == first.memory_id
    active = repository.active_memories("user-1", "thinkback")
    assert [entry.memory_text for entry in active] == ["新文本"]
    assert repository.get_active_memory_by_backend_id("user-1", "thinkback", "backend-1") is not None


def test_add_memory_index_reuses_backend_id_after_delete(repository) -> None:  # type: ignore[no-untyped-def]
    first = repository.add_memory_index(
        backend_memory_id="backend-1",
        user_id="user-1",
        source_refs=[{"session_id": "s1", "round_id": "r1"}],
        memory_text="旧事实",
    )
    repository.mark_memory_deleted(first.memory_id)

    second = repository.add_memory_index(
        backend_memory_id="backend-1",
        user_id="user-1",
        source_refs=[{"session_id": "s1", "round_id": "r2"}],
        memory_text="新事实",
    )
    assert second.memory_id != first.memory_id
    assert [entry.memory_text for entry in repository.active_memories("user-1", "thinkback")] == [
        "新事实"
    ]
    assert len(repository.list_memories("user-1", "thinkback")) == 2


def test_memory_status_transitions_stamp_invalid_at(repository) -> None:  # type: ignore[no-untyped-def]
    memory = repository.add_memory_index(
        backend_memory_id="backend-1",
        user_id="user-1",
        source_refs=[{"session_id": "s1", "round_id": "r1"}],
        memory_text="事实",
    )

    deleted = repository.mark_memory_deleted(memory.memory_id)
    assert deleted is not None
    assert deleted.memory_status is MemoryStatus.DELETED
    assert deleted.invalid_at is not None
    first_invalid_at = deleted.invalid_at

    suppressed = repository.mark_memory_suppressed(memory.memory_id)
    assert suppressed is not None
    assert suppressed.memory_status is MemoryStatus.SUPPRESSED
    assert _naive(suppressed.invalid_at) == _naive(first_invalid_at)

    revived = repository.mark_memory_active(memory.memory_id)
    assert revived is not None
    assert revived.memory_status is MemoryStatus.ACTIVE
    assert revived.invalid_at is None

    superseded = repository.mark_memory_superseded(memory.memory_id)
    assert superseded is not None
    assert superseded.memory_status is MemoryStatus.SUPERSEDED

    assert repository.mark_memory_deleted("missing") is None
    assert repository.mark_memory_active("missing") is None
    assert repository.mark_memory_suppressed("missing") is None
    assert repository.mark_memory_superseded("missing") is None


def test_update_memory_index_and_source_refs(repository) -> None:  # type: ignore[no-untyped-def]
    memory = repository.add_memory_index(
        backend_memory_id="backend-1",
        user_id="user-1",
        source_refs=[{"session_id": "s1", "round_id": "r1"}],
        memory_text="原文",
    )

    updated = repository.update_memory_index(
        memory.memory_id,
        source_refs=[{"session_id": "s1", "round_id": "r2"}],
        memory_text="改后",
        source_type=SourceType.MANUAL_FIX.value,
        data_classification=DataClassification.PERSONAL.value,
        memory_type="preference",
        backend_categories=["偏好"],
        metadata={"source": "admin"},
    )
    assert updated is not None
    assert updated.memory_text == "改后"
    assert updated.source_type == SourceType.MANUAL_FIX.value
    assert updated.data_classification == DataClassification.PERSONAL.value
    assert updated.backend_categories == ["偏好"]
    assert updated.metadata == {"source": "admin"}

    assert (
        repository.update_memory_index(
            "missing",
            source_refs=[],
            memory_text="x",
        )
        is None
    )

    rebound = repository.update_memory_source_refs(
        memory.memory_id, [{"session_id": "s9", "round_id": "r9"}]
    )
    assert rebound is not None
    assert rebound.source_refs == [{"session_id": "s9", "round_id": "r9"}]
    assert repository.update_memory_source_refs("missing", []) is None


def test_touch_memory_recalled_counts_reaches(repository) -> None:  # type: ignore[no-untyped-def]
    memory = repository.add_memory_index(
        backend_memory_id="backend-1",
        user_id="user-1",
        source_refs=[{"session_id": "s1", "round_id": "r1"}],
        memory_text="事实",
    )

    assert repository.touch_memory_recalled([memory.memory_id, "missing"]) == 1
    entry = repository.get_memory_index(memory.memory_id)
    assert entry is not None
    assert entry.recall_count == 1
    assert entry.last_recalled_at is not None

    assert repository.touch_memory_recalled([memory.memory_id]) == 1
    assert repository.get_memory_index(memory.memory_id).recall_count == 2  # type: ignore[union-attr]
    assert repository.touch_memory_recalled([]) == 0
    assert repository.touch_memory_recalled(["missing"]) == 0


def test_source_ref_sets_split_deleted_and_excluded(repository) -> None:  # type: ignore[no-untyped-def]
    deleted = repository.add_memory_index(
        backend_memory_id="b-deleted",
        user_id="user-1",
        memory_scope_id="session-1",
        source_refs=[{"session_id": "session-1", "round_id": "r1"}],
        memory_text="已删",
    )
    superseded = repository.add_memory_index(
        backend_memory_id="b-superseded",
        user_id="user-1",
        memory_scope_id="session-1",
        source_refs=[{"session_id": "session-1", "round_id": "r2"}],
        memory_text="被取代",
    )
    suppressed = repository.add_memory_index(
        backend_memory_id="b-suppressed",
        user_id="user-1",
        memory_scope_id="session-1",
        source_refs=[{"session_id": "session-1", "round_id": "r3"}],
        memory_text="被遗忘",
    )
    repository.add_memory_index(
        backend_memory_id="b-active",
        user_id="user-1",
        memory_scope_id="session-1",
        source_refs=[{"session_id": "session-1", "round_id": "r4"}],
        memory_text="仍有效",
    )
    repository.mark_memory_deleted(deleted.memory_id)
    repository.mark_memory_superseded(superseded.memory_id)
    repository.mark_memory_suppressed(suppressed.memory_id)

    assert repository.deleted_source_refs("user-1", "session-1") == {
        source_ref_key({"session_id": "session-1", "round_id": "r1"})
    }
    assert repository.excluded_source_refs("user-1", "session-1") == {
        source_ref_key({"session_id": "session-1", "round_id": "r1"}),
        source_ref_key({"session_id": "session-1", "round_id": "r2"}),
        source_ref_key({"session_id": "session-1", "round_id": "r3"}),
    }


# ---------------------------------------------------------------------------
# 后台任务（claim / CAS / stale 回收）
# ---------------------------------------------------------------------------


def test_claim_task_is_exclusive(repository) -> None:  # type: ignore[no-untyped-def]
    claimed, created = repository.claim_task(_make_task("task-1"))
    assert created is True
    assert claimed.task_id == "task-1"

    again, created_again = repository.claim_task(_make_task("task-1"))
    assert created_again is False
    assert again.task_id == "task-1"

    assert repository.get_task("task-1") is not None
    assert repository.get_task("missing") is None


def test_save_task_cas_rejects_stale_versions(repository) -> None:  # type: ignore[no-untyped-def]
    task = _make_task("task-1", status=TaskStatus.PENDING)
    created = repository.save_task(task)
    stored = repository.get_task("task-1")
    assert stored is not None
    assert stored.row_version == created.row_version

    stored.status = TaskStatus.RUNNING
    stored.retry_count = 1
    advanced = repository.save_task(stored)
    assert advanced.row_version == created.row_version + 1

    stale = repository.get_task("task-1")
    assert stale is not None
    stale.row_version = created.row_version
    stale.retry_count = 99
    with pytest.raises(TaskStaleWriteError):
        repository.save_task(stale)

    untouched = repository.get_task("task-1")
    assert untouched is not None
    assert untouched.retry_count == 1


def test_reclaim_stale_running_tasks_recycles_only_aged_running(
    repository, age_task
) -> None:  # type: ignore[no-untyped-def]
    repository.claim_task(_make_task("orphan"))
    repository.claim_task(_make_task("fresh", status=TaskStatus.RUNNING))
    repository.save_task(_make_task("done", status=TaskStatus.COMPLETED))
    age_task("orphan", 7200)

    assert repository.reclaim_stale_running_tasks(max_age_seconds=1800.0) == ["orphan"]

    orphan = repository.get_task("orphan")
    assert orphan is not None
    assert orphan.status is TaskStatus.FAILED
    assert orphan.last_error is not None and "reclaimed" in orphan.last_error
    assert repository.get_task("fresh").status is TaskStatus.RUNNING  # type: ignore[union-attr]
    assert repository.get_task("done").status is TaskStatus.COMPLETED  # type: ignore[union-attr]

    assert repository.reclaim_stale_running_tasks(max_age_seconds=1800.0) == []


def test_reclaim_stale_running_task_single_target(repository, age_task) -> None:  # type: ignore[no-untyped-def]
    repository.claim_task(_make_task("orphan"))
    repository.claim_task(_make_task("fresh"))
    age_task("orphan", 7200)

    assert repository.reclaim_stale_running_task("orphan", max_age_seconds=1800.0) is True
    assert repository.reclaim_stale_running_task("orphan", max_age_seconds=1800.0) is False
    assert repository.reclaim_stale_running_task("fresh", max_age_seconds=1800.0) is False
    assert repository.reclaim_stale_running_task("missing", max_age_seconds=1800.0) is False

    orphan = repository.get_task("orphan")
    assert orphan is not None
    assert orphan.status is TaskStatus.FAILED
    assert orphan.last_error is not None and "reclaimed" in orphan.last_error


def test_list_tasks_filters_and_paginates(repository, age_task) -> None:  # type: ignore[no-untyped-def]
    repository.claim_task(_make_task("task-a", status=TaskStatus.RUNNING))
    repository.claim_task(_make_task("task-b", status=TaskStatus.PENDING))
    repository.claim_task(_make_task("task-c", status=TaskStatus.COMPLETED))
    age_task("task-a", 7200)

    running = repository.list_tasks(statuses=[TaskStatus.RUNNING.value])
    assert [task.task_id for task in running] == ["task-a"]

    aged = repository.list_tasks(older_than_seconds=3600.0)
    assert [task.task_id for task in aged] == ["task-a"]

    all_first_page = repository.list_tasks(limit=2)
    all_second_page = repository.list_tasks(limit=2, offset=2)
    assert len(all_first_page) == 2
    assert {task.task_id for task in all_first_page + all_second_page} == {
        "task-a",
        "task-b",
        "task-c",
    }
    assert repository.list_tasks(statuses=[TaskStatus.FAILED.value]) == []


def test_count_aggregates(repository) -> None:  # type: ignore[no-untyped-def]
    repository.claim_task(_make_task("task-a", status=TaskStatus.RUNNING))
    repository.claim_task(_make_task("task-b", status=TaskStatus.RUNNING))
    repository.claim_task(_make_task("task-c", status=TaskStatus.FAILED))

    repository.add_memory_index(
        backend_memory_id="b-1",
        user_id="user-1",
        source_refs=[],
        memory_text="正常",
    )
    second = repository.add_memory_index(
        backend_memory_id="b-2",
        user_id="user-1",
        source_refs=[],
        memory_text="机密",
        data_classification=DataClassification.SENSITIVE.value,
    )
    repository.mark_memory_deleted(second.memory_id)

    assert repository.count_tasks_by_status() == {
        TaskStatus.RUNNING.value: 2,
        TaskStatus.FAILED.value: 1,
    }
    assert repository.count_memories_by_status() == {
        MemoryStatus.ACTIVE.value: 1,
        MemoryStatus.DELETED.value: 1,
    }
    assert repository.count_memories_by_classification() == {
        DataClassification.NORMAL.value: 1,
        DataClassification.SENSITIVE.value: 1,
    }
    assert repository.count_memories_by_source_type() == {SourceType.CHAT_ROUND.value: 2}


def test_admin_list_memories_filters_and_paginates(repository) -> None:  # type: ignore[no-untyped-def]
    first = repository.add_memory_index(
        backend_memory_id="b-1",
        user_id="user-1",
        memory_scope_id="scope-a",
        source_refs=[],
        memory_text="第一条",
    )
    second = repository.add_memory_index(
        backend_memory_id="b-2",
        user_id="user-1",
        memory_scope_id="scope-a",
        source_refs=[],
        memory_text="第二条",
    )
    repository.add_memory_index(
        backend_memory_id="b-3",
        user_id="user-2",
        memory_scope_id="scope-b",
        source_refs=[],
        memory_text="别人家的",
    )
    repository.mark_memory_deleted(second.memory_id)

    entries, total = repository.admin_list_memories(user_id="user-1")
    assert total == 2
    assert {entry.memory_text for entry in entries} == {"第一条", "第二条"}

    entries, total = repository.admin_list_memories(user_id="user-1", statuses=["DELETED"])
    assert total == 1
    assert entries[0].memory_id == second.memory_id

    entries, total = repository.admin_list_memories(memory_scope_id="scope-b")
    assert total == 1
    assert entries[0].memory_text == "别人家的"

    page, total = repository.admin_list_memories(user_id="user-1", limit=1)
    assert total == 2
    assert len(page) == 1

    detail = repository.get_memory_index(first.memory_id)
    assert detail is not None
    assert detail.memory_text == "第一条"
    assert repository.get_memory_index("missing") is None


def test_get_rounds_by_refs_preserves_order_and_skips_missing(repository) -> None:  # type: ignore[no-untyped-def]
    repository.save_round(make_append(round_id="r1", content="一"))
    repository.save_round(make_append(round_id="r2", content="二"))

    rounds = repository.get_rounds_by_refs(
        [
            {"session_id": "session-1", "round_id": "r2"},
            {"session_id": "session-1", "round_id": "missing"},
            {"session_id": "session-1", "round_id": "r1"},
        ]
    )
    assert [entry.round_id for entry in rounds] == ["r2", "r1"]
    assert repository.get_rounds_by_refs([]) == []


# ---------------------------------------------------------------------------
# 管理审计
# ---------------------------------------------------------------------------


def test_admin_audit_round_trip_orders_desc(repository) -> None:  # type: ignore[no-untyped-def]
    earlier = datetime(2026, 5, 4, 10, 0, 0, tzinfo=UTC)
    later = datetime(2026, 5, 4, 11, 0, 0, tzinfo=UTC)
    repository.save_admin_audit(
        AdminAuditEntry(
            audit_id="audit-1",
            created_at=earlier,
            operator="admin",
            action="delete_memory",
            target="memory-1",
            detail={"reason": "test"},
        )
    )
    repository.save_admin_audit(
        AdminAuditEntry(
            audit_id="audit-2",
            created_at=later,
            operator="ops",
            action="rebuild_l2",
            target="session-1",
        )
    )

    entries = repository.list_admin_audit()
    assert [entry.audit_id for entry in entries] == ["audit-2", "audit-1"]
    assert [entry.audit_id for entry in repository.list_admin_audit(action="delete_memory")] == [
        "audit-1"
    ]
    assert [entry.audit_id for entry in repository.list_admin_audit(operator="ops")] == ["audit-2"]
    assert [entry.audit_id for entry in repository.list_admin_audit(limit=1, offset=1)] == [
        "audit-1"
    ]
    assert repository.list_admin_audit(action="nope") == []
