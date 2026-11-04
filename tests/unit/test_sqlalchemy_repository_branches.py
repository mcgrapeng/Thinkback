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
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError
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
from thinkback.infra.database.models import (
    CurrentSummaryRecord,
    MemoryTaskRecord,
    SummaryRoundJournalRecord,
)
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
    """关引擎。

    夹具的收尾顺序必须是**先 dispose 引擎、再关仓储的事件循环**：aiosqlite
    的 worker 线程要把结果投递回它创建时所在的那个循环（仓储的 worker
    loop）。先关 loop 再 dispose 引擎，投递就撞上已关的循环 → "Event loop
    is closed" 的 unraisable 异常，偶尔会被 pytest 记成下一条用例的 ERROR
    （一次偶发红看起来像 flaky，其实是自己的收尾顺序错了）。
    """

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
        _dispose(engine)
        _settle(repo)
        repo.close()


@pytest.fixture()
def sql_repository(tmp_path: Path):  # type: ignore[no-untyped-def]
    repo, engine = _make_repository(tmp_path / "branches.db")
    try:
        yield repo
    finally:
        _dispose(engine)
        _settle(repo)
        repo.close()


@pytest.fixture()
def fast_timeout_repository(tmp_path: Path):  # type: ignore[no-untyped-def]
    repo, engine = _make_repository(tmp_path / "timeout.db", operation_timeout_seconds=0.05)
    try:
        yield repo
    finally:
        _dispose(engine)
        _settle(repo)
        repo.close()


@pytest.fixture()
def autocommit_repository(tmp_path: Path):  # type: ignore[no-untyped-def]
    repo, engine = _make_repository(
        tmp_path / "race.db", connect_args={"isolation_level": "AUTOCOMMIT"}
    )
    try:
        yield repo
    finally:
        _dispose(engine)
        _settle(repo)
        repo.close()


def _settle(repository: SqlAlchemyMemoryRepository) -> None:
    """让仓储的事件循环醒一次，把待处理的取消做完。

    被取消但没启动的协程还遗弃在 loop 里（它从没启动，握着的 session 也
    悬着）。loop 一关，它的 GC 清理就撞上已关的循环 → "Event loop is
    closed" 的 unraisable 异常，pytest 把它记成**下一条**用例的 ERROR
    （看着像 flaky，其实是自己收尾太快）。先醒一次，这些取消就在 loop 还
    活着时被处理掉。
    """

    with suppress(Exception):  # loop 已停/已关时没什么可等的
        repository._run(asyncio.sleep(0))  # noqa: SLF001


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
# 故障注入：DB 异常路径（commit 冲突、回读不到行、行凭空消失）
# ---------------------------------------------------------------------------


class _FaultInjection:
    """故障注入开关。

    ``commit_error``：commit 抛这个异常（模拟唯一约束冲突）。
    ``get_none`` / ``scalar_none``：回读恒返回 None（模拟「对方那行不存在」/
    「刚写进去的行读不回来」）。
    """

    def __init__(
        self,
        *,
        commit_error: Exception | None = None,
        get_none: bool = False,
        scalar_none: bool = False,
    ) -> None:
        self.commit_error = commit_error
        self.get_none = get_none
        self.scalar_none = scalar_none


class _FaultySession:
    """session 替身：按 ``_FaultInjection`` 开关改变 commit / get / scalar 的结果。"""

    def __init__(self, session: AsyncSession, fault: _FaultInjection) -> None:
        self._session = session
        self._fault = fault

    async def __aenter__(self) -> _FaultySession:
        await self._session.__aenter__()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self._session.__aexit__(exc_type, exc, tb)

    async def commit(self) -> None:
        if self._fault.commit_error is not None:
            raise self._fault.commit_error
        await self._session.commit()

    async def get(self, *args: Any, **kwargs: Any) -> Any:
        if self._fault.get_none:
            return None
        return await self._session.get(*args, **kwargs)

    async def scalar(self, *args: Any, **kwargs: Any) -> Any:
        if self._fault.scalar_none:
            return None
        return await self._session.scalar(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)


class _FaultyFactory:
    def __init__(self, factory: async_sessionmaker[Any], fault: _FaultInjection) -> None:
        self._factory = factory
        self._fault = fault

    def __call__(self) -> _FaultySession:
        return _FaultySession(self._factory(), self._fault)


def _integrity_error(message: str = "forced unique violation") -> IntegrityError:
    return IntegrityError("INSERT", {}, Exception(message))


def _block_loop_until_started(repository: SqlAlchemyMemoryRepository, *, seconds: float) -> None:
    """真正占住仓储事件循环：等阻塞协程确认已启动再返回。

    ``_occupy_loop`` 只提交不等待：``loop.stop`` 会在阻塞协程启动前就被处理，
    循环根本没被占住（协程甚至没跑就被丢弃，见 ``coroutine was never
    awaited`` 警告）。等 started 事件再返回，才是真的占住。
    """

    started = threading.Event()

    async def _blocker() -> None:
        started.set()
        time.sleep(seconds)  # noqa: ASYNC251 — 故意阻塞事件循环，用来制造 join 超时

    asyncio.run_coroutine_threadsafe(_blocker(), repository._loop)  # noqa: SLF001
    assert started.wait(timeout=5), "阻塞协程没有启动，事件循环没被占住"


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
    """worker 被长任务卡住时 close 不得永久阻塞：join 超时记 warning 后返回。

    顺带钉住「超时那条路不关 loop」：关掉 loop 会与在飞的操作抢资源
    （源码注释写明：宁可把 loop 留给调用方去 drain）。
    """
    repository = sql_repository
    _block_loop_until_started(repository, seconds=0.3)

    repository.close(timeout=0.05)

    # join 超时 → 走 last-resort 分支：不关 loop，也不阻塞调用方。
    assert not repository._loop.is_closed()  # noqa: SLF001
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


# ---------------------------------------------------------------------------
# DB 异常路径（134-137、175-177、331、488、503、885、1374、1421）
# ---------------------------------------------------------------------------


def test_a_submit_that_loses_the_race_to_a_closed_loop_is_rejected(
    sql_repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """守卫通过后 loop 被关：提交必须立刻拒绝，不能白等满操作超时。

    关机顺序里 L3 executor 的残留任务可能晚到：这时再提交给已关的 loop
    只会白等到超时，报出来的却是误导性的「超时」。
    """
    repository = sql_repository

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("event loop is closed")

    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", _boom)

    async def _noop() -> str:
        return "never"

    with pytest.raises(RuntimeError, match="memory repository event loop is closed"):
        repository._run(_noop())  # noqa: SLF001


def test_save_round_race_returns_the_concurrent_winner_journal(
    autocommit_repository,
) -> None:  # type: ignore[no-untyped-def]
    """轮次并发首写：输家 INSERT 撞 round_id 唯一约束后回读胜者返回，不冒裸 IntegrityError。"""
    repository = autocommit_repository
    real_factory = repository.session_factory

    async def _write_winner() -> None:
        async with real_factory() as session:
            session.add(
                SummaryRoundJournalRecord(
                    journal_id="journal-winner",
                    user_id="user-1",
                    memory_scope_id="thinkback::session-1",
                    session_id="session-1",
                    round_id="race-round",
                    round_index=0,
                    round_fingerprint="fingerprint-winner",
                    messages=[{"role": "user", "content": "胜者原文"}],
                    source_timestamp=datetime(2026, 9, 7, 10, 0, 1),
                    round_state="active",
                    namespace="thinkback",
                )
            )
            await session.commit()

    repository.session_factory = _CommitSabotage(real_factory, _write_winner)

    entry = repository.save_round(make_append(round_id="race-round"))

    # 返回的是**胜者那一轮**：输家的写入放弃，但调用方拿到的是已落库的那轮。
    assert entry.round_id == "race-round"
    assert entry.messages[0]["content"] == "胜者原文"
    assert entry.round_fingerprint == "fingerprint-winner"


def test_a_save_round_conflict_without_a_winner_row_reraises(sql_repository) -> None:  # type: ignore[no-untyped-def]
    """轮次写入撞约束且回读不到对方那行：原样上抛，不返回半截轮次。

    「回读不到」意味着不是 round_id 撞车，而是别的完整性约束被违反 ——
    吞掉它等于把数据损坏说成「已写好」。
    """
    repository = sql_repository
    repository.session_factory = _FaultyFactory(
        repository.session_factory,
        _FaultInjection(commit_error=_integrity_error(), scalar_none=True),
    )

    with pytest.raises(IntegrityError):
        repository.save_round(make_append(round_id="r-conflict"))


def test_a_summary_upsert_conflict_without_a_winner_row_reraises(sql_repository) -> None:  # type: ignore[no-untyped-def]
    """摘要写入撞约束且回读不到胜者：原样上抛，不返回半成品摘要。"""
    repository = sql_repository
    repository.session_factory = _FaultyFactory(
        repository.session_factory,
        _FaultInjection(commit_error=_integrity_error(), scalar_none=True),
    )

    with pytest.raises(IntegrityError):
        repository.upsert_summary_from_rounds("user-1", "session-1", [], summary_text="摘要")


def test_a_summary_that_vanishes_after_upsert_raises_instead_of_returning_a_ghost(
    sql_repository,
) -> None:  # type: ignore[no-untyped-def]
    """写完却读不回摘要行：必须报错，不能把半截摘要交给调用方。"""
    repository = sql_repository
    # 先落一行摘要：走 UPDATE 成功路径（行存在 → UPDATE 命中）。
    repository.upsert_summary_from_rounds("user-1", "session-1", [], summary_text="旧摘要")
    repository.session_factory = _FaultyFactory(
        repository.session_factory,
        _FaultInjection(scalar_none=True),
    )

    with pytest.raises(RuntimeError, match="summary vanished after upsert"):
        repository.upsert_summary_from_rounds("user-1", "session-1", [], summary_text="新摘要")


def test_a_memory_index_insert_race_without_a_winner_row_reraises(sql_repository) -> None:  # type: ignore[no-untyped-def]
    """记忆索引写入撞约束且回读不到对方那行：原样上抛，不静默丢索引。"""
    repository = sql_repository
    repository.session_factory = _FaultyFactory(
        repository.session_factory,
        _FaultInjection(commit_error=_integrity_error(), scalar_none=True),
    )

    with pytest.raises(IntegrityError):
        repository.add_memory_index(
            backend_memory_id="m-1",
            user_id="user-1",
            memory_scope_id="thinkback",
            source_refs=[{"session_id": "session-1", "round_id": "r-1"}],
            memory_text="居住城市：上海",
        )


def test_a_task_claim_race_without_a_winner_row_reraises(sql_repository) -> None:  # type: ignore[no-untyped-def]
    """任务认领撞主键且回读不到对方那行：原样上抛，不假装认领成功。"""
    repository = sql_repository
    repository.session_factory = _FaultyFactory(
        repository.session_factory,
        _FaultInjection(commit_error=_integrity_error(), get_none=True),
    )
    task = TaskEntry(
        task_id="race-task",
        request_id="req-1",
        op_type=OperationType.WRITE_ROUND,
        scope={"user_id": "user-1"},
        status=TaskStatus.RUNNING,
        retry_count=1,
    )

    with pytest.raises(IntegrityError):
        repository.claim_task(task)


def test_a_task_that_vanishes_after_an_insert_conflict_raises(sql_repository) -> None:  # type: ignore[no-untyped-def]
    """任务插入撞主键后回读不到行：必须报错，不能继续把任务当已落库。"""
    repository = sql_repository
    repository.session_factory = _FaultyFactory(
        repository.session_factory,
        _FaultInjection(commit_error=_integrity_error(), get_none=True),
    )
    task = TaskEntry(
        task_id="vanishing-task",
        request_id="req-1",
        op_type=OperationType.WRITE_ROUND,
        scope={"user_id": "user-1"},
        status=TaskStatus.RUNNING,
        retry_count=0,
    )

    with pytest.raises(RuntimeError, match="task vanished after insert conflict"):
        repository.save_task(task)


def test_the_concurrent_l1_read_serves_from_the_cache_without_a_db_read(
    sql_repository,
) -> None:  # type: ignore[no-untyped-def]
    """并发取回的 L1 读命中缓存时不得再查库：四个并发取回里最热的一条。"""
    repository = sql_repository
    repository.save_round(make_append(round_id="r-cache"))

    # 第一次读把行放进 L1 缓存；随后物理删掉，命中缓存就还能读到。
    assert [entry.round_id for entry in repository.get_l1("user-1", "session-1", "session-1")]
    assert repository.delete_rounds("user-1", "session-1") == 1

    cached = repository._run(  # noqa: SLF001
        repository._read_l1(  # noqa: SLF001
            "user-1",
            "session-1",
            "session-1",
            namespace="thinkback",
        )
    )

    assert [entry.round_id for entry in cached] == ["r-cache"]
