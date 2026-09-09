import threading
from datetime import datetime

import pytest
from sqlalchemy.exc import IntegrityError

from innies_memory.infra.database import engine as engine_module
from innies_memory.infra.database.models import MemoryRecord
from innies_memory.memory.repositories import (
    InMemoryMemoryRepository,
    JournalEntry,
    SqlAlchemyMemoryRepository,
)
from innies_memory.memory.repositories import sqlalchemy as repositories_module
from innies_memory.memory.schemas import DataClassification, MemoryStatus, SourceType
from test.unit.test_memory_service import make_append


def test_in_memory_repository_orders_equal_timestamp_rounds_by_round_index() -> None:
    repository = InMemoryMemoryRepository()
    later = repository.save_round(
        make_append(
            round_id="round-10",
            source_timestamp="2026-05-04T10:00:03Z",
            round_index=10,
            content="第十轮",
        )
    )
    earlier = repository.save_round(
        make_append(
            round_id="round-2",
            source_timestamp="2026-05-04T10:00:03Z",
            round_index=2,
            content="第二轮",
        )
    )

    rounds = repository.list_rounds("user-1", "session-1")
    summary = repository.upsert_summary_from_rounds("user-1", "session-1", rounds)

    assert [entry.round_id for entry in rounds] == [earlier.round_id, later.round_id]
    assert summary.summary_cursor_round == later.round_id


def test_in_memory_repository_excludes_pending_append_rounds_from_active_round_reads() -> None:
    repository = InMemoryMemoryRepository()
    repository.save_round(
        make_append(
            round_id="round-pending",
            round_index=1,
            content="失败写入不应进入召回",
        )
    )
    repository.mark_round_pending_append("round-pending")

    assert repository.list_rounds("user-1", "session-1") == []
    assert repository.list_user_rounds("user-1") == []


@pytest.mark.asyncio
async def test_sql_repository_lists_rounds_with_round_index_tie_breaker() -> None:
    repository = SqlAlchemyMemoryRepository()

    class FakeScalars:
        def all(self):  # type: ignore[no-untyped-def]
            return []

    class FakeSession:
        statement = None

        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return self

        async def __aexit__(self, *args):  # type: ignore[no-untyped-def]
            return None

        async def scalars(self, statement):  # type: ignore[no-untyped-def]
            self.statement = statement
            return FakeScalars()

    session = FakeSession()
    repository.session_factory = lambda: session  # type: ignore[assignment]

    await repository._list_rounds("user-1", "session-1", None)

    compiled = str(session.statement.compile(compile_kwargs={"literal_binds": True}))
    assert "ORDER BY" in compiled
    assert "source_timestamp" in compiled
    assert "round_index" in compiled
    assert "round_id" in compiled


@pytest.mark.asyncio
async def test_sql_repository_orders_null_round_index_like_in_memory_repository() -> None:
    repository = SqlAlchemyMemoryRepository()

    class FakeScalars:
        def all(self):  # type: ignore[no-untyped-def]
            return []

    class FakeSession:
        statements = []

        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return self

        async def __aexit__(self, *args):  # type: ignore[no-untyped-def]
            return None

        async def scalars(self, statement):  # type: ignore[no-untyped-def]
            self.statements.append(statement)
            return FakeScalars()

    session = FakeSession()
    repository.session_factory = lambda: session  # type: ignore[assignment]

    await repository._list_rounds("user-1", "session-1", None)
    await repository._list_user_rounds("user-1")

    compiled_statements = [
        str(statement.compile(compile_kwargs={"literal_binds": True}))
        for statement in session.statements
    ]
    assert all("round_index NULLS FIRST" in statement for statement in compiled_statements)


@pytest.mark.asyncio
async def test_sql_repository_lists_memories_with_deterministic_order() -> None:
    repository = SqlAlchemyMemoryRepository()

    class FakeScalars:
        def all(self):  # type: ignore[no-untyped-def]
            return []

    class FakeSession:
        statements = []

        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return self

        async def __aexit__(self, *args):  # type: ignore[no-untyped-def]
            return None

        async def scalars(self, statement):  # type: ignore[no-untyped-def]
            self.statements.append(statement)
            return FakeScalars()

    session = FakeSession()
    repository.session_factory = lambda: session  # type: ignore[assignment]

    await repository._active_memories("user-1", "innies")
    await repository._list_memories("user-1", "innies")

    compiled_statements = [
        str(statement.compile(compile_kwargs={"literal_binds": True}))
        for statement in session.statements
    ]
    assert all("ORDER BY" in statement for statement in compiled_statements)
    assert all("created_at" in statement for statement in compiled_statements)
    assert all("memory_id" in statement for statement in compiled_statements)


@pytest.mark.asyncio
async def test_sql_repository_save_round_returns_existing_round_after_concurrent_insert() -> None:
    request = make_append(round_id="round-race", round_index=10)
    repository = SqlAlchemyMemoryRepository()
    entry = repository._journal_entry_from_append_request(request)

    class FakeSession:
        selects = 0
        rolled_back = False

        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return self

        async def __aexit__(self, *args):  # type: ignore[no-untyped-def]
            return None

        async def scalar(self, _statement):  # type: ignore[no-untyped-def]
            self.selects += 1
            if self.selects == 1:
                return None
            return repository._journal_record_from_entry(entry)

        def add(self, _record):  # type: ignore[no-untyped-def]
            pass

        async def commit(self):  # type: ignore[no-untyped-def]
            raise IntegrityError("insert", {}, Exception("duplicate key value"))

        async def rollback(self):  # type: ignore[no-untyped-def]
            self.rolled_back = True

    session = FakeSession()
    repository.session_factory = lambda: session  # type: ignore[assignment]

    saved = await repository._save_round(request)

    assert saved.round_id == request.round_id
    assert saved.round_fingerprint == entry.round_fingerprint
    assert session.rolled_back is True


def test_in_memory_repository_add_memory_index_updates_existing_active_backend_id() -> None:
    from innies_memory.memory.repositories import InMemoryMemoryRepository

    repository = InMemoryMemoryRepository()

    first = repository.add_memory_index(
        backend_memory_id="local-p0:communication:abc",
        user_id="user-1",
        memory_scope_id="innies",
        source_refs=[{"session_id": "s1", "round_id": "r1"}],
        memory_text="old",
    )
    second = repository.add_memory_index(
        backend_memory_id="local-p0:communication:abc",
        user_id="user-1",
        memory_scope_id="innies",
        source_refs=[{"session_id": "s1", "round_id": "r1"}],
        memory_text="new",
    )

    active = repository.active_memories("user-1", "innies")
    assert len(active) == 1
    assert second.memory_id == first.memory_id
    assert active[0].memory_text == "new"
    assert active[0].memory_status is MemoryStatus.ACTIVE


def test_sql_repository_run_uses_dedicated_loop_instead_of_asyncio_run(monkeypatch) -> None:
    repository = SqlAlchemyMemoryRepository()

    def fail_asyncio_run(awaitable):  # type: ignore[no-untyped-def]
        awaitable.close()
        raise AssertionError("SqlAlchemy repository must not create a new loop per call")

    async def immediate_value() -> str:
        return "ok"

    monkeypatch.setattr(repositories_module.asyncio, "run", fail_asyncio_run)

    assert repository._run(immediate_value()) == "ok"


def test_database_engine_uses_connection_pool_instead_of_null_pool() -> None:
    assert type(engine_module.engine.sync_engine.pool).__name__ != "NullPool"


@pytest.mark.asyncio
async def test_sql_repository_add_memory_index_updates_existing_after_unique_conflict() -> None:
    repository = SqlAlchemyMemoryRepository()
    existing = MemoryRecord(
        memory_id="memory-existing",
        backend_memory_id="backend-duplicate",
        user_id="user-1",
        memory_scope_id="innies",
        source_refs=[{"session_id": "old-session", "round_id": "old-round"}],
        source_type=SourceType.CHAT_ROUND.value,
        memory_status=MemoryStatus.ACTIVE.value,
        data_classification=DataClassification.NORMAL.value,
        memory_text="old",
        memory_type=None,
        backend_categories=[],
        memory_metadata={},
    )

    class FakeSession:
        commits = 0
        rolled_back = False
        refreshed: MemoryRecord | None = None

        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return self

        async def __aexit__(self, *args):  # type: ignore[no-untyped-def]
            return None

        async def scalar(self, _statement):  # type: ignore[no-untyped-def]
            if self.commits == 0:
                return None
            return existing

        def add(self, _record):  # type: ignore[no-untyped-def]
            pass

        async def commit(self):  # type: ignore[no-untyped-def]
            self.commits += 1
            if self.commits == 1:
                raise IntegrityError("insert", {}, Exception("duplicate active backend id"))

        async def rollback(self):  # type: ignore[no-untyped-def]
            self.rolled_back = True

        async def refresh(self, record):  # type: ignore[no-untyped-def]
            self.refreshed = record

    session = FakeSession()
    repository.session_factory = lambda: session  # type: ignore[assignment]

    saved = await repository._add_memory_index(
        backend_memory_id="backend-duplicate",
        user_id="user-1",
        memory_scope_id="innies",
        source_refs=[{"session_id": "new-session", "round_id": "new-round"}],
        memory_text="new",
        source_type=SourceType.CHAT_ROUND.value,
        data_classification=DataClassification.NORMAL.value,
        memory_type="event",
        backend_categories=["preference"],
        metadata={"source": "race"},
    )

    assert saved.memory_id == "memory-existing"
    assert saved.memory_text == "new"
    assert saved.source_refs == [{"session_id": "new-session", "round_id": "new-round"}]
    assert saved.backend_categories == ["preference"]
    assert saved.metadata == {"source": "race"}
    assert session.rolled_back is True
    assert session.refreshed is existing


def test_sql_repository_run_times_out_instead_of_blocking_worker_forever() -> None:
    """回归：数据库操作卡住时 _run 必须超时报错，不能永久占住调用线程。"""
    import asyncio

    repository = SqlAlchemyMemoryRepository(operation_timeout_seconds=0.05)

    async def stalled() -> str:
        await asyncio.sleep(10)
        return "never"

    with pytest.raises(RuntimeError, match="timed out"):
        repository._run(stalled())


def test_sql_repository_rejects_non_positive_operation_timeout() -> None:
    with pytest.raises(ValueError, match="operation_timeout_seconds"):
        SqlAlchemyMemoryRepository(operation_timeout_seconds=0)


def test_sql_repository_run_propagates_coroutine_raised_timeout_error() -> None:
    """协程自身抛出的 TimeoutError（如 asyncpg 命令超时）应原样传播，不得误报等待超时。"""
    repository = SqlAlchemyMemoryRepository(operation_timeout_seconds=5.0)

    async def db_command_timeout() -> str:
        raise TimeoutError("asyncpg command timeout")

    with pytest.raises(TimeoutError, match="asyncpg command timeout"):
        repository._run(db_command_timeout())


# ---------------------------------------------------------------------------
# L-5 回归测试：SqlAlchemyMemoryRepository.close() 防御性关闭事件循环
# ---------------------------------------------------------------------------


def test_sql_alchemy_repository_close_stops_event_loop() -> None:
    """回归 L-5：close() 后 _loop_thread 必须已退出（is_alive() == False）。"""
    repository = SqlAlchemyMemoryRepository()
    # close 之前线程必须 alive
    assert repository._loop_thread.is_alive() is True
    assert repository._loop.is_running() is True

    repository.close(timeout=5.0)

    # close 之后线程必须退出
    assert repository._loop_thread.is_alive() is False


def test_sql_alchemy_repository_close_is_idempotent() -> None:
    """回归 L-5：close() 重复调用不应抛异常（shutdown 重入安全）。"""
    repository = SqlAlchemyMemoryRepository()
    repository.close(timeout=5.0)
    # 第二次调用不应抛异常
    repository.close(timeout=1.0)
    assert repository._loop_thread.is_alive() is False


# ---------------------------------------------------------------------------
# N-5 回归测试：L1CacheMixin.update_l1/clear_l1/get_l1 必须线程安全
# ---------------------------------------------------------------------------


def _make_journal_entry(*, round_id: str, session_id: str = "session-1") -> JournalEntry:
    """构造一个最小可用的 JournalEntry 用于 L1 缓存填充。"""
    return JournalEntry(
        journal_id=f"journal-{round_id}",
        user_id="user-1",
        memory_scope_id=session_id,
        session_id=session_id,
        round_id=round_id,
        messages=[{"role": "user", "content": f"content-{round_id}"}],
        source_timestamp=datetime(2026, 5, 4, 10, 0, 0),
        round_fingerprint=f"fp-{round_id}",
    )


def test_l1_cache_update_l1_is_thread_safe() -> None:
    """N-5: 多线程并发调用 update_l1 不应丢数据。

    修复前：l1_cache 是裸 dict，并发读-改-写会丢回合（race condition）。
    修复后：单 Lock 串行化 update_l1，所有写入都应保留。
    """
    repository = InMemoryMemoryRepository()
    user_id = "user-1"
    session_id = "session-concurrent"
    thread_count = 8
    rounds_per_thread = 50
    # 使用一个大于 thread_count * rounds_per_thread 的 limit，确保
    # LRU 裁剪不会触发，结果只反映锁保护是否完整。
    total_writes = thread_count * rounds_per_thread
    barrier = threading.Barrier(thread_count)

    def worker(thread_index: int) -> None:
        barrier.wait()
        for round_index in range(rounds_per_thread):
            repository.update_l1(
                _make_journal_entry(
                    round_id=f"thread-{thread_index}-round-{round_index}",
                    session_id=session_id,
                ),
                limit=total_writes + 10,
            )

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    cached = repository.get_l1(user_id, session_id, session_id)
    expected_total = total_writes
    # 每轮都有唯一 round_id，并发后应全部保留。
    assert len(cached) == expected_total, (
        f"L1 cache 丢数据: 期望 {expected_total} 条，实得 {len(cached)}"
    )
    round_ids = {entry.round_id for entry in cached}
    assert len(round_ids) == expected_total


def test_l1_cache_clear_l1_under_concurrent_update_l1_no_corruption() -> None:
    """N-5: update_l1 和 clear_l1 并发调用不应破坏缓存状态。

    修复前：裸 dict 在 clear 与 update 交错时会触发 KeyError 或保留幽灵数据。
    修复后：单 Lock 串行化后操作要么先 update 再 clear（最终空），要么先 clear
    再 update（最终仅含新写入）。两种合法终态，不允许抛异常。
    """
    repository = InMemoryMemoryRepository()
    user_id = "user-1"
    session_id = "session-race"
    iterations = 100

    def updater() -> None:
        for round_index in range(iterations):
            repository.update_l1(
                _make_journal_entry(
                    round_id=f"r-{round_index}",
                    session_id=session_id,
                )
            )

    def clearer() -> None:
        for _ in range(iterations // 5):
            repository.clear_l1(user_id, session_id, session_id)

    threads = [
        threading.Thread(target=updater),
        threading.Thread(target=updater),
        threading.Thread(target=clearer),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # 最终要么是空（clear 赢），要么全是合法 round_id（update 赢）。
    cached = repository.get_l1(user_id, session_id, session_id)
    for entry in cached:
        assert entry.round_id.startswith("r-"), f"脏数据残留: {entry.round_id}"


def test_l1_cache_mixin_exposes_lock_field() -> None:
    """N-5: L1CacheMixin 子类必须暴露可获取的 _l1_lock 字段供并发保护。"""
    repository = InMemoryMemoryRepository()
    assert hasattr(repository, "_l1_lock"), "InMemoryMemoryRepository 缺少 _l1_lock"
    # 锁必须可获取、可释放（acquire/release 验证接口可用）
    acquired = repository._l1_lock.acquire(blocking=False)
    try:
        assert acquired is True
    finally:
        repository._l1_lock.release()
