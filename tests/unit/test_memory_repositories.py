import threading
from datetime import datetime

import pytest
from sqlalchemy.exc import IntegrityError

from tests.unit.test_memory_service import make_append
from thinkback.infra.database import engine as engine_module
from thinkback.infra.database.models import MemoryRecord
from thinkback.memory.repositories import (
    InMemoryMemoryRepository,
    JournalEntry,
    SqlAlchemyMemoryRepository,
)
from thinkback.memory.repositories import sqlalchemy as repositories_module
from thinkback.memory.repositories._l1_cache import L1_CACHE_MAX_KEYS, L1_CACHE_TTL_SECONDS
from thinkback.memory.schemas import DataClassification, MemoryStatus, SourceType


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

    await repository._active_memories("user-1", "thinkback")
    await repository._list_memories("user-1", "thinkback")

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
    from thinkback.memory.repositories import InMemoryMemoryRepository

    repository = InMemoryMemoryRepository()

    first = repository.add_memory_index(
        backend_memory_id="local-p0:communication:abc",
        user_id="user-1",
        memory_scope_id="thinkback",
        source_refs=[{"session_id": "s1", "round_id": "r1"}],
        memory_text="old",
    )
    second = repository.add_memory_index(
        backend_memory_id="local-p0:communication:abc",
        user_id="user-1",
        memory_scope_id="thinkback",
        source_refs=[{"session_id": "s1", "round_id": "r1"}],
        memory_text="new",
    )

    active = repository.active_memories("user-1", "thinkback")
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
        memory_scope_id="thinkback",
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
        memory_scope_id="thinkback",
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


def _make_l1_entry(*, round_id: str, user_id: str, scope_id: str, session_id: str) -> JournalEntry:
    """构造 (user, scope, session) 三维可独立变化的 L1 条目。"""
    return JournalEntry(
        journal_id=f"journal-{round_id}",
        user_id=user_id,
        memory_scope_id=scope_id,
        session_id=session_id,
        round_id=round_id,
        messages=[{"role": "user", "content": f"content-{round_id}"}],
        source_timestamp=datetime(2026, 5, 4, 10, 0, 0),
        round_fingerprint=f"fp-{round_id}",
    )


def test_l1_cache_clear_l1_targets_only_the_named_session() -> None:
    """clear_l1 语义钉死：按 session 清、不越界到同前缀的其它 session。"""
    repository = InMemoryMemoryRepository()
    for session_id in ("s-1", "s-10", "s-2"):
        repository.update_l1(
            _make_l1_entry(
                round_id=f"r-{session_id}",
                user_id="user-1",
                scope_id="scope-1",
                session_id=session_id,
            ),
            limit=10,
        )

    repository.clear_l1("user-1", "scope-1", "s-1")

    assert repository.get_l1("user-1", "scope-1", "s-1") == []
    assert [entry.round_id for entry in repository.get_l1("user-1", "scope-1", "s-10")] == [
        "r-s-10"
    ]
    assert [entry.round_id for entry in repository.get_l1("user-1", "scope-1", "s-2")] == ["r-s-2"]


def test_l1_cache_clear_l1_without_session_clears_scope_and_isolates_users() -> None:
    repository = InMemoryMemoryRepository()
    repository.update_l1(
        _make_l1_entry(round_id="r-a", user_id="user-1", scope_id="scope-1", session_id="s-a"),
        limit=10,
    )
    repository.update_l1(
        _make_l1_entry(round_id="r-b", user_id="user-1", scope_id="scope-1", session_id="s-b"),
        limit=10,
    )
    repository.update_l1(
        _make_l1_entry(round_id="r-x", user_id="user-other", scope_id="scope-1", session_id="s-a"),
        limit=10,
    )

    repository.clear_l1("user-1", "scope-1")
    assert repository.get_l1("user-1", "scope-1", "s-a") == []
    assert repository.get_l1("user-1", "scope-1", "s-b") == []
    assert [entry.round_id for entry in repository.get_l1("user-other", "scope-1", "s-a")] == [
        "r-x"
    ]


def test_l1_cache_key_separates_users_and_scopes() -> None:
    """缓存键必须同时含 user_id 与 scope：任一维度漏掉都会跨维度串味。"""
    repository = InMemoryMemoryRepository()
    repository.update_l1(
        _make_l1_entry(round_id="r-1", user_id="user-1", scope_id="scope-1", session_id="s-1"),
        limit=10,
    )

    # 同 session / 同 namespace，但 user 不同 → 不能命中
    assert repository.get_l1("user-other", "scope-1", "s-1") == []
    # 同 user / 同 session，但 scope 不同 → 不能命中
    assert repository.get_l1("user-1", "scope-2", "s-1") == []
    # 本人本 scope → 必须命中
    assert [entry.round_id for entry in repository.get_l1("user-1", "scope-1", "s-1")] == ["r-1"]


def test_l1_cache_clear_l1_removes_timestamp_bookkeeping() -> None:
    """清缓存必须同时清掉 TTL 时间戳：残留时间戳会让缓存条目「有主无名」。"""
    repository = InMemoryMemoryRepository()
    repository.update_l1(
        _make_l1_entry(round_id="r-1", user_id="user-1", scope_id="scope-1", session_id="s-1"),
        limit=10,
    )
    repository.update_l1(
        _make_l1_entry(round_id="r-2", user_id="user-1", scope_id="scope-1", session_id="s-2"),
        limit=10,
    )

    repository.clear_l1("user-1", "scope-1", "s-1")

    stamps = repository._l1_timestamps()
    assert not any(key.endswith(":s-1") for key in stamps), f"时间戳残留：{sorted(stamps)}"

    # 整 scope 清空：时间戳也要一起清
    repository.clear_l1("user-1", "scope-1")
    stamps = repository._l1_timestamps()
    assert not any("scope-1" in key for key in stamps), f"时间戳残留：{sorted(stamps)}"


def test_l1_cache_expired_entry_is_dropped_with_timestamp(monkeypatch) -> None:
    """TTL 过期：条目与时间戳一起清掉，不留孤儿时间戳。"""
    repository = InMemoryMemoryRepository()
    repository.update_l1(
        _make_l1_entry(round_id="r-1", user_id="user-1", scope_id="scope-1", session_id="s-1"),
        limit=10,
    )
    key = next(iter(repository.l1_cache))
    assert key in repository._l1_timestamps()

    monkeypatch.setattr(
        "thinkback.memory.repositories._l1_cache.time.monotonic",
        lambda: 0.0,
    )
    repository._l1_timestamps()[key] = -10_000.0  # 远超 TTL

    assert repository.get_l1("user-1", "scope-1", "s-1") == []
    assert key not in repository.l1_cache
    assert key not in repository._l1_timestamps()


def test_l1_cache_update_l1_deduplicates_and_caps_by_limit() -> None:
    repository = InMemoryMemoryRepository()
    for index in range(4):
        repository.update_l1(
            _make_journal_entry(round_id=f"r-{index}", session_id="session-1"),
            limit=2,
        )
    assert [entry.round_id for entry in repository.get_l1("user-1", "session-1", "session-1")] == [
        "r-2",
        "r-3",
    ]

    repository.update_l1(
        _make_journal_entry(round_id="r-3", session_id="session-1"),
        limit=2,
    )
    assert [entry.round_id for entry in repository.get_l1("user-1", "session-1", "session-1")] == [
        "r-2",
        "r-3",
    ]

    repository.update_l1(
        _make_journal_entry(round_id="r-1", session_id="session-1"),
        limit=2,
    )
    assert [entry.round_id for entry in repository.get_l1("user-1", "session-1", "session-1")] == [
        "r-3",
        "r-1",
    ]


def test_l1_cache_update_l1_default_limit_is_ten() -> None:
    repository = InMemoryMemoryRepository()
    for index in range(12):
        repository.update_l1(_make_journal_entry(round_id=f"r-{index:02d}", session_id="session-1"))

    cached = repository.get_l1("user-1", "session-1", "session-1")
    assert [entry.round_id for entry in cached] == [f"r-{index:02d}" for index in range(2, 12)]


def test_l1_cache_key_space_is_bounded() -> None:
    """会话只增不减时，缓存的**键**数必须有上界。

    值有上界（每键 10 条轮次），键没有 → 长跑服务里键空间无界增长，每个键
    还带着最多 10 条轮次原文。TTL 是惰性的：不再访问的键永远不过期，
    所以必须显式淘汰。
    """
    repository = InMemoryMemoryRepository()
    for index in range(L1_CACHE_MAX_KEYS + 50):
        repository.update_l1(
            _make_l1_entry(
                round_id=f"r-{index}",
                user_id="user-1",
                scope_id="scope-1",
                session_id=f"s-{index}",
            )
        )

    assert len(repository.l1_cache) == L1_CACHE_MAX_KEYS
    # 淘汰的是最早的键，最新的键仍在（淘汰不能把刚写的也扔掉）
    assert repository.get_l1("user-1", "scope-1", f"s-{L1_CACHE_MAX_KEYS + 49}")


def test_l1_cache_eviction_drops_timestamp_bookkeeping_too() -> None:
    """淘汰缓存键时必须同步删掉时间戳簿记，否则簿记本身也无界增长。"""
    repository = InMemoryMemoryRepository()
    for index in range(L1_CACHE_MAX_KEYS + 10):
        repository.update_l1(
            _make_l1_entry(
                round_id=f"r-{index}",
                user_id="user-1",
                scope_id="scope-1",
                session_id=f"s-{index}",
            )
        )

    assert len(repository._l1_timestamps()) == len(repository.l1_cache)


def test_l1_cache_eviction_tolerates_a_key_without_timestamp() -> None:
    """淘汰时遇到「有缓存条目但没有时间戳」的脏状态不能抛异常。

    脏状态是真实存在的（整 scope 清理会留下这种条目）。淘汰跑在每次
    append 上，这里抛异常等于把一次缓存清理升级成写入失败。
    """
    repository = InMemoryMemoryRepository()
    repository.update_l1(
        _make_l1_entry(round_id="r-0", user_id="user-1", scope_id="scope-1", session_id="s-0")
    )
    oldest = next(iter(repository.l1_cache))
    repository._l1_timestamps().pop(oldest, None)  # 脏状态：簿记没了，缓存还在

    for index in range(1, L1_CACHE_MAX_KEYS + 1):
        repository.update_l1(
            _make_l1_entry(
                round_id=f"r-{index}",
                user_id="user-1",
                scope_id="scope-1",
                session_id=f"s-{index}",
            )
        )

    assert oldest not in repository.l1_cache
    assert len(repository.l1_cache) == L1_CACHE_MAX_KEYS


# ---------------------------------------------------------------------------
# L1 缓存：直接断言缓存内部状态（get_l1 有 journal 回源兜底，光看返回值
# 看不见「缓存条目还在不在」）
# ---------------------------------------------------------------------------


def test_l1_cache_clear_l1_removes_the_entry_from_cache() -> None:
    """按 session 清缓存必须真的把条目从缓存里摘掉。

    get_l1 拿不到缓存时会回源 journal，所以「返回值是空」证明不了缓存被清；
    这里直接看缓存字典。
    """
    repository = InMemoryMemoryRepository()
    repository.update_l1(
        _make_l1_entry(round_id="r-1", user_id="user-1", scope_id="scope-1", session_id="s-1"),
        limit=10,
    )
    key = next(iter(repository.l1_cache))

    repository.clear_l1("user-1", "scope-1", "s-1")

    assert key not in repository.l1_cache


def test_l1_cache_clear_l1_on_uncached_session_is_a_noop() -> None:
    """清一个从没缓存过的 session：不能抛异常，也不能误伤别的 session。"""
    repository = InMemoryMemoryRepository()
    repository.update_l1(
        _make_l1_entry(round_id="r-1", user_id="user-1", scope_id="scope-1", session_id="s-1"),
        limit=10,
    )

    repository.clear_l1("user-1", "scope-1", "s-never-cached")

    assert [entry.round_id for entry in repository.get_l1("user-1", "scope-1", "s-1")] == ["r-1"]


def test_l1_cache_clear_scope_removes_entries_that_have_no_timestamp() -> None:
    """整 scope 清理要能处理「有条目但没有时间戳」的脏状态：不能抛异常。

    时间戳缺失说明这条缓存是别处塞进来的（或者时间戳已被单独清掉）；
    清理路径必须容错，而不是 KeyError。
    """
    repository = InMemoryMemoryRepository()
    entry = _make_l1_entry(round_id="r-1", user_id="user-1", scope_id="scope-1", session_id="s-1")
    key = repository._l1_cache_key("user-1", "scope-1", "s-1", "thinkback")
    repository.l1_cache[key] = [entry]

    repository.clear_l1("user-1", "scope-1")

    assert key not in repository.l1_cache


def test_l1_cache_entry_exactly_at_ttl_is_still_valid(monkeypatch) -> None:
    """TTL 边界：刚好等于 TTL 的条目仍然有效（过期判定是严格大于）。

    边界写成 >= 会让「正好到点」的条目被丢掉 —— 差一毫秒的行为分叉。
    """
    repository = InMemoryMemoryRepository()
    repository.update_l1(
        _make_l1_entry(round_id="r-1", user_id="user-1", scope_id="scope-1", session_id="s-1"),
        limit=10,
    )
    key = next(iter(repository.l1_cache))
    now = 1_000_000.0
    repository._l1_timestamps()[key] = now - L1_CACHE_TTL_SECONDS
    monkeypatch.setattr("thinkback.memory.repositories._l1_cache.time.monotonic", lambda: now)

    assert [entry.round_id for entry in repository.get_l1("user-1", "scope-1", "s-1")] == ["r-1"]


def test_l1_cache_entry_without_timestamp_is_dropped() -> None:
    """缓存条目没有时间戳 = 无法判断新鲜度，按 miss 处理并清掉。

    清时间戳时必须容错（这条时间戳本来就不存在）。
    """
    repository = InMemoryMemoryRepository()
    entry = _make_l1_entry(round_id="r-1", user_id="user-1", scope_id="scope-1", session_id="s-1")
    key = repository._l1_cache_key("user-1", "scope-1", "s-1", "thinkback")
    repository.l1_cache[key] = [entry]

    assert repository.get_l1("user-1", "scope-1", "s-1") == []
    assert key not in repository.l1_cache


def test_l1_cache_close_on_in_memory_repository_is_a_noop() -> None:
    """内存仓储的 close 是 no-op：调用不应报错（协议要求有 close）。"""
    repository = InMemoryMemoryRepository()

    repository.close()
