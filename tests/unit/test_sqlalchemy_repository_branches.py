"""SQL 仓储实现（生产真值）的分支钉子测试：只钉可观察行为。

覆盖目标（行号见各用例）是 ``sqlalchemy.py`` 里契约测试没走到、但真实
可观察的分支：close 竞态、操作超时取消、L1 缓存命中、时间字段更新、
erase 过滤分支，以及两条「并发首写撞唯一约束」的生产竞态。

并发分支复现方式：单线程测试无法自然制造「读未见行、插入却撞约束」的
多副本交错——用 commit 钩子在旁路连接先落冲突行。探针实证：主 session
前置 DML 持读锁时，旁路提交会被 SQLite 单写者锁挡成 ``database is
locked``，因此这两个用例的引擎用 ``AUTOCOMMIT`` 隔离级（语句即提交、
不持锁），在数据库层把生产并发状态确定性复现。
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from tests.unit.test_memory_service import make_append
from thinkback.domain.entities import TaskEntry
from thinkback.domain.enums import OperationType, TaskStatus
from thinkback.infra.database.base import Base
from thinkback.infra.database.models import CurrentSummaryRecord, MemoryTaskRecord
from thinkback.memory.repositories import InMemoryMemoryRepository, SqlAlchemyMemoryRepository


def _naive(value: datetime | None) -> datetime | None:
    """SQLite 往返丢 tz，时间断言统一剥掉 tzinfo 再比。"""
    return value.replace(tzinfo=None) if value is not None else None


def _make_repository(
    db_path: Path,
    *,
    connect_args: dict[str, Any] | None = None,
    **repo_kwargs: Any,
) -> tuple[SqlAlchemyMemoryRepository, AsyncEngine]:
    """契约测试同款：SQLite 文件库 + NullPool，无需真实 PG。"""

    async def _init_schema() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{db_path}", poolclass=NullPool, **(connect_args or {})
        )
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(_init_schema())
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}", poolclass=NullPool, **(connect_args or {})
    )
    repository = SqlAlchemyMemoryRepository(
        session_factory=async_sessionmaker(engine, expire_on_commit=False), **repo_kwargs
    )
    return repository, engine


def _dispose(engine: AsyncEngine) -> None:
    asyncio.run(engine.dispose())


@pytest.fixture(params=["in_memory", "sqlalchemy"])
def repository(request: pytest.FixtureRequest, tmp_path: Path):  # type: ignore[no-untyped-def]
    """双后端同行为：镜像分支的断言对两条实现同时成立。"""
    if request.param == "in_memory":
        yield InMemoryMemoryRepository()
        return

    repo, engine = _make_repository(tmp_path / "contract.db")
    try:
        yield repo
    finally:
        repo.close()
        _dispose(engine)


@pytest.fixture()
def sql_repository(tmp_path: Path):  # type: ignore[no-untyped-def]
    repo, engine = _make_repository(tmp_path / "branches.db")
    try:
        yield repo
    finally:
        repo.close()
        _dispose(engine)


@pytest.fixture()
def fast_timeout_repository(tmp_path: Path):  # type: ignore[no-untyped-def]
    repo, engine = _make_repository(tmp_path / "timeout.db", operation_timeout_seconds=0.05)
    try:
        yield repo
    finally:
        repo.close()
        _dispose(engine)


@pytest.fixture()
def autocommit_repository(tmp_path: Path):  # type: ignore[no-untyped-def]
    repo, engine = _make_repository(
        tmp_path / "race.db", connect_args={"isolation_level": "AUTOCOMMIT"}
    )
    try:
        yield repo
    finally:
        repo.close()
        _dispose(engine)


def _occupy_loop(repository: SqlAlchemyMemoryRepository, *, seconds: float) -> None:
    """用协程内的同步 sleep 卡住仓储事件循环。

    循环被占用期间 ``call_soon_threadsafe`` 排入的回调（loop.stop、新提交
    的协程）都无法被处理——这是确定性制造「提交了但没启动」的手段。
    """

    async def _blocker() -> None:
        time.sleep(seconds)  # noqa: ASYNC251 — 故意阻塞事件循环，用来制造「提交了但没启动」

    asyncio.run_coroutine_threadsafe(_blocker(), repository._loop)  # noqa: SLF001


class _CommitSabotage:
    """session_factory 替身：目标 session 首次 commit 前先在旁路连接落冲突行。

    生产里「读未见行、INSERT 却撞唯一约束」来自多副本并发首写（源码
    注释记录过输家拿到裸 IntegrityError 变 500 的事故）。测试在 commit
    边界注入旁路写入，把同一数据库状态确定性复现。一次性：触发后自动
    解除，后续 session 走原样 commit。
    """

    def __init__(self, factory: async_sessionmaker[Any], conflict_writer: Any) -> None:
        self._factory = factory
        self._armed_writer = conflict_writer

    def __call__(self) -> _SabotagedSession:
        return _SabotagedSession(self._factory(), self)

    async def fire_once(self) -> None:
        if self._armed_writer is not None:
            writer, self._armed_writer = self._armed_writer, None
            await writer()


class _SabotagedSession:
    def __init__(self, session: AsyncSession, sabotage: _CommitSabotage) -> None:
        self._session = session
        self._sabotage = sabotage

    async def __aenter__(self) -> _SabotagedSession:
        await self._session.__aenter__()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self._session.__aexit__(exc_type, exc, tb)

    async def commit(self) -> None:
        await self._sabotage.fire_once()
        await self._session.commit()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)


# ---------------------------------------------------------------------------
# close / _run 生命周期（124-137、197）
# ---------------------------------------------------------------------------


def test_close_treats_closed_loop_with_live_thread_as_noop(sql_repository) -> None:  # type: ignore[no-untyped-def]
    """双重 close 竞态：loop 已关、线程对象仍报 alive → 记 debug 返回，不抛。"""
    repository = sql_repository
    repository.close()
    # 模拟竞态窗口：线程尚未 join 完就再次 close（loop 已关、alive 仍真）。
    survivor = threading.Thread(target=lambda: time.sleep(0.2), daemon=True)
    survivor.start()
    repository._loop_thread = survivor  # noqa: SLF001

    repository.close()  # 不得抛 RuntimeError

    assert repository._loop.is_closed()  # noqa: SLF001
    survivor.join(timeout=5)


def test_close_warns_and_returns_when_worker_join_times_out(sql_repository) -> None:  # type: ignore[no-untyped-def]
    """worker 被长任务卡住时 close 不得永久阻塞：join 超时记 warning 后返回。"""
    repository = sql_repository
    _occupy_loop(repository, seconds=0.3)

    repository.close(timeout=0.05)

    # 阻塞任务结束后 loop.stop 生效、线程退出，不泄漏。
    repository._loop_thread.join(timeout=10)  # noqa: SLF001
    assert not repository._loop_thread.is_alive()  # noqa: SLF001


def test_run_timeout_on_never_started_coroutine_raises_runtime_error(
    fast_timeout_repository: Any,
) -> None:
    """等待超时且协程尚未启动（inner_task 为空）→ 取消 future 并抛 RuntimeError。"""
    repository = fast_timeout_repository
    _occupy_loop(repository, seconds=0.3)

    with pytest.raises(RuntimeError, match="timed out after 0.05s"):
        repository.get_round("missing")


# ---------------------------------------------------------------------------
# L1 缓存命中（222）
# ---------------------------------------------------------------------------


def test_get_l1_serves_within_ttl_from_cache_without_db_read(sql_repository) -> None:  # type: ignore[no-untyped-def]
    """缓存命中：TTL 内二次读不再回源——底层行物理删除后仍能读到。"""
    repository = sql_repository
    repository.save_round(make_append(round_id="r1"))
    assert [entry.round_id for entry in repository.get_l1("user-1", "session-1", "session-1")] == [
        "r1"
    ]

    assert repository.delete_rounds("user-1", "session-1") == 1

    cached = repository.get_l1("user-1", "session-1", "session-1")
    assert [entry.round_id for entry in cached] == ["r1"]


# ---------------------------------------------------------------------------
# 并发首写竞态（472-489、1416-1419）
# ---------------------------------------------------------------------------


def test_upsert_summary_insert_race_returns_concurrent_winner(autocommit_repository) -> None:  # type: ignore[no-untyped-def]
    """摘要并发首写：输家 INSERT 撞唯一约束后回读胜者返回，不冒裸 IntegrityError。"""
    repository = autocommit_repository
    real_factory = repository.session_factory

    async def _write_winner() -> None:
        async with real_factory() as session:
            session.add(
                CurrentSummaryRecord(
                    summary_id="summary-winner",
                    user_id="user-1",
                    memory_scope_id="session-1",
                    namespace="thinkback",
                    summary_text="胜者摘要",
                )
            )
            await session.commit()

    repository.session_factory = _CommitSabotage(real_factory, _write_winner)

    summary = repository.upsert_summary_from_rounds(
        "user-1", "session-1", [], summary_text="输家摘要"
    )

    assert summary.summary_text == "胜者摘要"
    fetched = repository.get_summary("user-1", "session-1")
    assert fetched is not None
    assert fetched.summary_id == "summary-winner"


def test_save_task_insert_race_falls_back_to_cas_update(autocommit_repository) -> None:  # type: ignore[no-untyped-def]
    """任务并发首写：插入撞 PK 后回读并发行、走 CAS 更新落本方状态，不变 500。"""
    repository = autocommit_repository
    real_factory = repository.session_factory

    async def _write_winner() -> None:
        async with real_factory() as session:
            session.add(
                MemoryTaskRecord(
                    task_id="race-task",
                    request_id="winner-req",
                    op_type=OperationType.WRITE_ROUND.value,
                    scope={},
                    status=TaskStatus.PENDING.value,
                    row_version=0,
                )
            )
            await session.commit()

    repository.session_factory = _CommitSabotage(real_factory, _write_winner)

    loser = TaskEntry(
        task_id="race-task",
        request_id="loser-req",
        op_type=OperationType.WRITE_ROUND,
        scope={"user_id": "user-1"},
        status=TaskStatus.RUNNING,
        retry_count=3,
    )
    saved = repository.save_task(loser)

    assert saved.row_version == 1
    stored = repository.get_task("race-task")
    assert stored is not None
    assert stored.status is TaskStatus.RUNNING
    assert stored.request_id == "loser-req"
    assert stored.retry_count == 3


# ---------------------------------------------------------------------------
# 时间字段与过滤分支（810-814、1021-1034、1087、1096、1132、1162）
# ---------------------------------------------------------------------------


def test_add_memory_index_existing_row_update_keeps_temporal_fields(sql_repository) -> None:  # type: ignore[no-untyped-def]
    """upsert 更新已存在 ACTIVE 行时，显式给定的时间字段必须落库。"""
    repository = sql_repository
    repository.add_memory_index(
        backend_memory_id="b-1", user_id="user-1", source_refs=[], memory_text="v1"
    )
    valid_at = datetime(2026, 3, 1, tzinfo=UTC)
    expires_at = datetime(2026, 4, 1, tzinfo=UTC)
    recorded_at = datetime(2026, 2, 1, tzinfo=UTC)

    updated = repository.add_memory_index(
        backend_memory_id="b-1",
        user_id="user-1",
        source_refs=[],
        memory_text="v2",
        valid_at=valid_at,
        expires_at=expires_at,
        recorded_at=recorded_at,
    )

    assert updated.memory_text == "v2"
    assert _naive(updated.valid_at) == _naive(valid_at)
    assert _naive(updated.expires_at) == _naive(expires_at)
    assert _naive(updated.recorded_at) == _naive(recorded_at)


def test_update_memory_index_rebinds_backend_id_and_temporal_fields(repository) -> None:  # type: ignore[no-untyped-def]
    memory = repository.add_memory_index(
        backend_memory_id="b-old", user_id="user-1", source_refs=[], memory_text="v1"
    )
    valid_at = datetime(2026, 3, 1, tzinfo=UTC)
    expires_at = datetime(2026, 4, 1, tzinfo=UTC)
    recorded_at = datetime(2026, 2, 1, tzinfo=UTC)

    updated = repository.update_memory_index(
        memory.memory_id,
        backend_memory_id="b-new",
        source_refs=[],
        memory_text="v2",
        valid_at=valid_at,
        expires_at=expires_at,
        recorded_at=recorded_at,
    )

    assert updated is not None
    assert updated.backend_memory_id == "b-new"
    assert _naive(updated.valid_at) == _naive(valid_at)
    assert _naive(updated.expires_at) == _naive(expires_at)
    assert _naive(updated.recorded_at) == _naive(recorded_at)


def test_list_expired_memories_filters_by_memory_scope(repository) -> None:  # type: ignore[no-untyped-def]
    before = datetime(2026, 1, 1, tzinfo=UTC)
    for scope_id, backend_id in [("scope-a", "b-a"), ("scope-b", "b-b")]:
        repository.add_memory_index(
            backend_memory_id=backend_id,
            user_id="user-1",
            memory_scope_id=scope_id,
            source_refs=[],
            memory_text=f"记忆 {backend_id}",
            expires_at=before - timedelta(days=1),
        )

    listed = repository.list_expired_memories(before=before, limit=10, memory_scope_id="scope-a")

    assert [entry.backend_memory_id for entry in listed] == ["b-a"]


def test_delete_memory_rows_empty_list_short_circuits(repository) -> None:  # type: ignore[no-untyped-def]
    assert repository.delete_memory_rows([]) == 0


def test_delete_rounds_scopes_to_single_session(repository) -> None:  # type: ignore[no-untyped-def]
    repository.save_round(make_append(round_id="r-a", session_id="session-a"))
    repository.save_round(make_append(round_id="r-b", session_id="session-b"))

    assert repository.delete_rounds("user-1", "session-a", session_id="session-a") == 1
    assert repository.get_round("r-a") is None
    assert repository.get_round("r-b") is not None


def test_clear_summary_without_existing_row_is_noop(repository) -> None:  # type: ignore[no-untyped-def]
    repository.clear_summary("user-1", "session-1")

    assert repository.get_summary("user-1", "session-1") is None
