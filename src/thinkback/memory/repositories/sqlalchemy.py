"""PostgreSQL 仓储实现（生产）。

异步 SQLAlchemy (asyncpg) 持久化 L1 流水 / L2 摘要 / L3 业务索引 / 任务；
对外保持同步接口：内部用独立事件循环线程桥接 ``run_coroutine_threadsafe``。
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from threading import Event, Thread
from typing import Any, TypeVar
from uuid import uuid4

from loguru import logger
from sqlalchemy import and_, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from thinkback.domain.entities import (
    JournalEntry,
    MemoryIndexEntry,
    SummaryEntry,
    TaskEntry,
)
from thinkback.domain.enums import (
    DataClassification,
    MemoryStatus,
    OperationType,
    SourceType,
    SummaryState,
    TaskStatus,
)
from thinkback.domain.errors import TaskStaleWriteError
from thinkback.domain.keys import fingerprint, source_ref_key
from thinkback.domain.summarization import summarize_rounds
from thinkback.infra.database.engine import SessionLocal
from thinkback.infra.database.models import (
    CurrentSummaryRecord,
    MemoryRecord,
    MemoryTaskRecord,
    SummaryRoundJournalRecord,
)
from thinkback.memory.repositories._l1_cache import L1_CACHE_LIMIT, L1CacheMixin
from thinkback.memory.schemas import AppendMemoryRequest

T = TypeVar("T")


class SqlAlchemyMemoryRepository(L1CacheMixin):
    # 单次数据库操作的最大等待秒数。没有超时的话，数据库卡顿会把调用方的
    # API worker 线程永久阻塞在 future.result() 上，逐步耗尽读写线程池，
    # 即使数据库恢复也只能持续返回 503。
    OPERATION_TIMEOUT_SECONDS = 30.0

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] = SessionLocal,
        *,
        operation_timeout_seconds: float | None = None,
    ) -> None:
        if operation_timeout_seconds is not None and operation_timeout_seconds <= 0:
            raise ValueError("operation_timeout_seconds must be > 0")
        self.session_factory = session_factory
        self.operation_timeout_seconds = (
            operation_timeout_seconds
            if operation_timeout_seconds is not None
            else self.OPERATION_TIMEOUT_SECONDS
        )
        self.l1_cache: dict[str, list[JournalEntry]] = {}
        # N-5: 给 L1CacheMixin 注入锁，串行化 update_l1/clear_l1/get_l1。
        self.init_l1_lock()
        self._loop = asyncio.new_event_loop()
        self._loop_started = Event()
        self._loop_thread = Thread(
            target=self._run_loop,
            name="innies-memory-sqlalchemy-repository",
            daemon=True,
        )
        self._loop_thread.start()
        self._loop_started.wait(timeout=5)
        logger.bind(
            timeout_seconds=self.operation_timeout_seconds,
            thread_name=self._loop_thread.name,
        ).info("sqlalchemy repository initialized")

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop_started.set()
        self._loop.run_forever()

    def close(self, timeout: float = 5.0) -> None:
        """Defensively shut down the worker thread and its event loop.

        ``daemon=True`` already keeps the process from hanging on interpreter
        shutdown, but in-flight ``run_coroutine_threadsafe`` tasks would be
        discarded and their database sessions left unclosed. SIGTERM-driven
        shutdown paths (``FastAPI`` lifespan, scripts) should call this so the
        event loop stops cleanly and the worker thread can join before exit.

        Idempotent: re-invoking after a previous close is a no-op (loop already
        stopped, thread already exited).
        """
        if not self._loop_thread.is_alive():
            logger.debug("sqlalchemy repository close skipped: worker thread already exited")
            return
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except RuntimeError:
            # Loop already closed (e.g. close() called twice in quick
            # succession). Treat as a no-op rather than a hard failure.
            logger.debug("sqlalchemy repository close skipped: loop already closed")
            return
        self._loop_thread.join(timeout=timeout)
        if self._loop_thread.is_alive():
            # Last-resort: still alive, leave daemon flag to clean up on exit.
            # We do NOT call _loop.close() here to avoid races with in-flight
            # operations that the caller is responsible for draining.
            logger.bind(timeout_seconds=timeout).warning(
                "sqlalchemy repository worker thread join timed out"
            )
            return
        # Worker has joined. Close the loop so any lingering resources (e.g.
        # selectors) are released. Wrap in suppress because a loop that has
        # already been closed raises RuntimeError.
        with suppress(RuntimeError):
            self._loop.close()
        logger.bind(timeout_seconds=timeout).info("sqlalchemy repository closed")

    def _run(self, awaitable: Coroutine[Any, Any, T]) -> T:
        # Wrap the coroutine to capture the inner asyncio.Task so we can
        # reliably cancel it on timeout. concurrent.futures.Future.cancel()
        # does not cancel RUNNING futures, which would leave the database
        # session held until the slow query finishes.
        inner_task: list[asyncio.Task[Any] | None] = [None]

        async def _wrapped() -> T:
            inner_task[0] = asyncio.current_task()
            return await awaitable

        # 快速失败：仓储已关闭（关机顺序中 L3 executor 的残留任务可能晚于
        # repository.close 到达）时，run_coroutine_threadsafe 永不解析，
        # 调用线程会白等满 operation_timeout 秒才报一个误导性的超时。
        if self._loop.is_closed() or not self._loop_thread.is_alive():
            raise RuntimeError("memory repository event loop is closed")
        future = asyncio.run_coroutine_threadsafe(_wrapped(), self._loop)
        try:
            return future.result(timeout=self.operation_timeout_seconds)
        except TimeoutError:
            if future.done():
                # 协程自身抛出了 TimeoutError（如 asyncpg 命令超时），
                # 不是等待超时，按原始异常向上传播。
                raise
            # 取消事件循环里的协程，释放其占用的数据库连接，避免连接泄漏。
            task = inner_task[0]
            logger.bind(
                timeout_seconds=self.operation_timeout_seconds,
                has_inner_task=task is not None,
            ).warning("sqlalchemy repository operation timed out")
            if task is not None:
                # 事件循环已停止时 call_soon_threadsafe 会抛 RuntimeError，
                # 此时无法取消，安全忽略。
                with suppress(RuntimeError):
                    self._loop.call_soon_threadsafe(task.cancel)
            else:
                future.cancel()
            raise RuntimeError(
                f"memory repository operation timed out after {self.operation_timeout_seconds:g}s"
            ) from None

    def get_round(self, round_id: str) -> JournalEntry | None:
        return self._run(self._get_round(round_id))

    def get_l1(self, user_id: str, memory_scope_id: str, session_id: str) -> list[JournalEntry]:
        """L1 读取：进程缓存优先，缓存缺失时回源 PG journal 派生。

        短期记忆的持久层是 PostgreSQL；进程内 L1 缓存只是单 Pod 读加速。
        多副本（或进程重启）下，append 落在其他 Pod 会让本 Pod 缓存为空 ——
        此时必须从 journal（round_state=active 的最近 N 轮）派生，否则
        recall 的 L1 会随机丢失最近轮次。
        """
        cached = super().get_l1(user_id, memory_scope_id, session_id)
        if cached:
            return cached
        rounds = self.list_rounds(user_id, memory_scope_id, session_id)[-L1_CACHE_LIMIT:]
        for entry in rounds:
            super().update_l1(entry, limit=L1_CACHE_LIMIT)
        return super().get_l1(user_id, memory_scope_id, session_id)

    async def _get_round(self, round_id: str) -> JournalEntry | None:
        async with self.session_factory() as session:
            record = await session.scalar(
                select(SummaryRoundJournalRecord).where(
                    SummaryRoundJournalRecord.round_id == round_id
                )
            )
            return self._journal_from_record(record) if record else None

    def save_round(self, request: AppendMemoryRequest) -> JournalEntry:
        return self._run(self._save_round(request))

    @staticmethod
    def _journal_entry_from_append_request(request: AppendMemoryRequest) -> JournalEntry:
        messages = [
            {
                "message_id": message.message_id,
                "role": message.role.value,
                "content": message.content,
                "timestamp": message.timestamp.isoformat(),
            }
            for message in request.messages
        ]
        return JournalEntry(
            journal_id=f"journal-{uuid4()}",
            user_id=request.user_id,
            memory_scope_id=request.session_id,
            session_id=request.session_id,
            round_id=request.round_id,
            round_index=request.round_index,
            messages=messages,
            source_timestamp=request.source_timestamp,
            round_fingerprint=fingerprint(messages),
        )

    @staticmethod
    def _journal_record_from_entry(entry: JournalEntry) -> SummaryRoundJournalRecord:
        return SummaryRoundJournalRecord(
            journal_id=entry.journal_id,
            user_id=entry.user_id,
            memory_scope_id=entry.memory_scope_id,
            session_id=entry.session_id,
            round_id=entry.round_id,
            round_index=entry.round_index,
            round_fingerprint=entry.round_fingerprint,
            messages=entry.messages,
            source_timestamp=entry.source_timestamp,
            round_state=entry.round_state,
        )

    async def _save_round(self, request: AppendMemoryRequest) -> JournalEntry:
        entry = self._journal_entry_from_append_request(request)
        async with self.session_factory() as session:
            existing = await session.scalar(
                select(SummaryRoundJournalRecord).where(
                    SummaryRoundJournalRecord.round_id == request.round_id
                )
            )
            if existing:
                return self._journal_from_record(existing)
            session.add(self._journal_record_from_entry(entry))
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                concurrent = await session.scalar(
                    select(SummaryRoundJournalRecord).where(
                        SummaryRoundJournalRecord.round_id == request.round_id
                    )
                )
                if concurrent and concurrent.round_fingerprint == entry.round_fingerprint:
                    return self._journal_from_record(concurrent)
                raise
        return entry

    def upsert_summary_from_journal(self, user_id: str, memory_scope_id: str) -> SummaryEntry:
        excluded_refs = self.excluded_source_refs(user_id, memory_scope_id)
        rounds = [
            entry
            for entry in self.list_rounds(user_id, memory_scope_id)
            if (entry.session_id, entry.round_id) not in excluded_refs
        ]
        return self.upsert_summary_from_rounds(user_id, memory_scope_id, rounds)

    def upsert_summary_from_rounds(
        self,
        user_id: str,
        memory_scope_id: str,
        rounds: list[JournalEntry],
        *,
        summary_text: str | None = None,
        summary_kind: str = "concat",
        preserve_llm: bool = False,
    ) -> SummaryEntry:
        composed_text, latest = summarize_rounds(rounds)
        return self._run(
            self._upsert_summary(
                user_id=user_id,
                memory_scope_id=memory_scope_id,
                summary_text=summary_text if summary_text is not None else composed_text,
                latest=latest,
                summary_kind=summary_kind,
                preserve_llm=preserve_llm,
            )
        )

    async def _upsert_summary(
        self,
        *,
        user_id: str,
        memory_scope_id: str,
        summary_text: str,
        latest: JournalEntry | None,
        summary_kind: str = "concat",
        preserve_llm: bool = False,
    ) -> SummaryEntry:
        async with self.session_factory() as session:
            record = await session.scalar(
                select(CurrentSummaryRecord).where(
                    and_(
                        CurrentSummaryRecord.user_id == user_id,
                        CurrentSummaryRecord.memory_scope_id == memory_scope_id,
                    )
                )
            )
            if (
                preserve_llm
                and summary_kind == "concat"
                and record is not None
                and record.summary_kind == "llm"
            ):
                # append 的拼接占位不覆盖已有 LLM 综合摘要（P0 语义；
                # 该 select 本就存在，无额外查询）。
                return self._summary_from_record(record)
            if record is None:
                record = CurrentSummaryRecord(
                    summary_id=f"summary-{uuid4()}",
                    user_id=user_id,
                    memory_scope_id=memory_scope_id,
                    summary_text="",
                    summary_cursor_round=None,
                    latest_source_round_id=None,
                    latest_source_timestamp=None,
                    summary_state=SummaryState.ACTIVE.value,
                )
                session.add(record)
            record.summary_text = summary_text
            record.summary_cursor_round = latest.round_id if latest else None
            record.latest_source_round_id = latest.round_id if latest else None
            record.latest_source_timestamp = latest.source_timestamp if latest else None
            record.summary_state = SummaryState.ACTIVE.value
            record.summary_kind = summary_kind
            await session.commit()
            await session.refresh(record)
            return self._summary_from_record(record)

    def get_summary(self, user_id: str, memory_scope_id: str) -> SummaryEntry | None:
        return self._run(self._get_summary(user_id, memory_scope_id))

    async def _get_summary(self, user_id: str, memory_scope_id: str) -> SummaryEntry | None:
        async with self.session_factory() as session:
            record = await session.scalar(
                select(CurrentSummaryRecord).where(
                    and_(
                        CurrentSummaryRecord.user_id == user_id,
                        CurrentSummaryRecord.memory_scope_id == memory_scope_id,
                    )
                )
            )
            return self._summary_from_record(record) if record else None

    def mark_summary(self, user_id: str, memory_scope_id: str, state: SummaryState) -> None:
        self._run(self._mark_summary(user_id, memory_scope_id, state))

    async def _mark_summary(self, user_id: str, memory_scope_id: str, state: SummaryState) -> None:
        async with self.session_factory() as session:
            record = await session.scalar(
                select(CurrentSummaryRecord).where(
                    and_(
                        CurrentSummaryRecord.user_id == user_id,
                        CurrentSummaryRecord.memory_scope_id == memory_scope_id,
                    )
                )
            )
            if record:
                record.summary_state = state.value
                await session.commit()

    def list_rounds(
        self, user_id: str, memory_scope_id: str, session_id: str | None = None
    ) -> list[JournalEntry]:
        return self._run(self._list_rounds(user_id, memory_scope_id, session_id))

    async def _list_rounds(
        self, user_id: str, memory_scope_id: str, session_id: str | None
    ) -> list[JournalEntry]:
        filters = [
            SummaryRoundJournalRecord.user_id == user_id,
            SummaryRoundJournalRecord.memory_scope_id == memory_scope_id,
            SummaryRoundJournalRecord.round_state == "active",
        ]
        if session_id is not None:
            filters.append(SummaryRoundJournalRecord.session_id == session_id)
        async with self.session_factory() as session:
            records = (
                await session.scalars(
                    select(SummaryRoundJournalRecord)
                    .where(and_(*filters))
                    .order_by(
                        SummaryRoundJournalRecord.source_timestamp,
                        SummaryRoundJournalRecord.round_index.nulls_first(),
                        SummaryRoundJournalRecord.round_id,
                    )
                )
            ).all()
            return [self._journal_from_record(record) for record in records]

    def list_user_rounds(self, user_id: str) -> list[JournalEntry]:
        return self._run(self._list_user_rounds(user_id))

    async def _list_user_rounds(self, user_id: str) -> list[JournalEntry]:
        async with self.session_factory() as session:
            records = (
                await session.scalars(
                    select(SummaryRoundJournalRecord)
                    .where(
                        and_(
                            SummaryRoundJournalRecord.user_id == user_id,
                            SummaryRoundJournalRecord.round_state == "active",
                        )
                    )
                    .order_by(
                        SummaryRoundJournalRecord.source_timestamp,
                        SummaryRoundJournalRecord.round_index.nulls_first(),
                        SummaryRoundJournalRecord.round_id,
                    )
                )
            ).all()
            return [self._journal_from_record(record) for record in records]

    def mark_rounds_deleted(
        self, user_id: str, memory_scope_id: str, session_id: str | None = None
    ) -> int:
        return self._run(self._mark_rounds_deleted(user_id, memory_scope_id, session_id))

    def mark_round_deleted(self, round_id: str) -> bool:
        return self._run(self._mark_round_deleted(round_id))

    def mark_round_active(self, round_id: str) -> bool:
        return self._run(self._mark_round_active(round_id))

    def mark_round_pending_append(self, round_id: str) -> bool:
        return self._run(self._mark_round_pending_append(round_id))

    async def _mark_rounds_deleted(
        self, user_id: str, memory_scope_id: str, session_id: str | None
    ) -> int:
        filters = [
            SummaryRoundJournalRecord.user_id == user_id,
            SummaryRoundJournalRecord.memory_scope_id == memory_scope_id,
            SummaryRoundJournalRecord.round_state != "deleted_tombstone",
        ]
        if session_id is not None:
            filters.append(SummaryRoundJournalRecord.session_id == session_id)
        async with self.session_factory() as session:
            records = (
                await session.scalars(select(SummaryRoundJournalRecord).where(and_(*filters)))
            ).all()
            for record in records:
                record.round_state = "deleted_tombstone"
            await session.commit()
            return len(records)

    async def _mark_round_deleted(self, round_id: str) -> bool:
        async with self.session_factory() as session:
            record = await session.scalar(
                select(SummaryRoundJournalRecord).where(
                    SummaryRoundJournalRecord.round_id == round_id
                )
            )
            if record is None:
                return False
            record.round_state = "deleted_tombstone"
            await session.commit()
            return True

    async def _mark_round_active(self, round_id: str) -> bool:
        async with self.session_factory() as session:
            record = await session.scalar(
                select(SummaryRoundJournalRecord).where(
                    SummaryRoundJournalRecord.round_id == round_id
                )
            )
            if record is None:
                return False
            record.round_state = "active"
            await session.commit()
            return True

    async def _mark_round_pending_append(self, round_id: str) -> bool:
        async with self.session_factory() as session:
            record = await session.scalar(
                select(SummaryRoundJournalRecord).where(
                    SummaryRoundJournalRecord.round_id == round_id
                )
            )
            if record is None:
                return False
            record.round_state = "pending_append"
            await session.commit()
            return True

    def add_memory_index(
        self,
        *,
        backend_memory_id: str,
        user_id: str,
        memory_scope_id: str = "innies",
        source_refs: list[dict[str, str]],
        memory_text: str,
        source_type: str = SourceType.CHAT_ROUND.value,
        data_classification: str = DataClassification.NORMAL.value,
        memory_type: str | None = None,
        backend_categories: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        valid_at: datetime | None = None,
    ) -> MemoryIndexEntry:
        return self._run(
            self._add_memory_index(
                backend_memory_id=backend_memory_id,
                user_id=user_id,
                memory_scope_id=memory_scope_id,
                source_refs=source_refs,
                memory_text=memory_text,
                source_type=source_type,
                data_classification=data_classification,
                memory_type=memory_type,
                backend_categories=backend_categories,
                metadata=metadata,
                valid_at=valid_at,
            )
        )

    async def _add_memory_index(
        self,
        *,
        backend_memory_id: str,
        user_id: str,
        memory_scope_id: str = "innies",
        source_refs: list[dict[str, str]],
        memory_text: str,
        source_type: str = SourceType.CHAT_ROUND.value,
        data_classification: str = DataClassification.NORMAL.value,
        memory_type: str | None = None,
        backend_categories: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        valid_at: datetime | None = None,
    ) -> MemoryIndexEntry:
        async with self.session_factory() as session:
            existing = await session.scalar(
                select(MemoryRecord).where(
                    and_(
                        MemoryRecord.user_id == user_id,
                        MemoryRecord.memory_scope_id == memory_scope_id,
                        MemoryRecord.backend_memory_id == backend_memory_id,
                        MemoryRecord.memory_status == MemoryStatus.ACTIVE.value,
                    )
                )
            )
            if existing is not None:
                existing.source_refs = source_refs
                existing.memory_text = memory_text
                existing.source_type = source_type
                existing.data_classification = data_classification
                existing.memory_type = memory_type
                existing.backend_categories = list(backend_categories or [])
                existing.memory_metadata = dict(metadata or {})
                await session.commit()
                await session.refresh(existing)
                logger.bind(
                    user_id=user_id,
                    memory_scope_id=memory_scope_id,
                    backend_memory_id=backend_memory_id,
                    memory_id=existing.memory_id,
                    action="updated",
                ).debug("memory index updated")
                return self._memory_from_record(existing)
            # P2#6：未显式给定时以写入时刻为事实生效时刻
            effective_valid_at = valid_at or datetime.now(UTC)
            entry = MemoryIndexEntry(
                memory_id=f"memory-{uuid4()}",
                backend_memory_id=backend_memory_id,
                user_id=user_id,
                memory_scope_id=memory_scope_id,
                source_refs=source_refs,
                memory_text=memory_text,
                source_type=source_type,
                data_classification=data_classification,
                memory_type=memory_type,
                backend_categories=list(backend_categories or []),
                metadata=dict(metadata or {}),
                valid_at=effective_valid_at,
            )
            session.add(
                MemoryRecord(
                    memory_id=entry.memory_id,
                    backend_memory_id=entry.backend_memory_id,
                    user_id=entry.user_id,
                    memory_scope_id=entry.memory_scope_id,
                    source_refs=entry.source_refs,
                    source_type=entry.source_type,
                    memory_status=entry.memory_status.value,
                    data_classification=entry.data_classification,
                    memory_text=entry.memory_text,
                    memory_type=entry.memory_type,
                    backend_categories=entry.backend_categories,
                    memory_metadata=entry.metadata,
                    valid_at=effective_valid_at,
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                logger.bind(
                    user_id=user_id,
                    memory_scope_id=memory_scope_id,
                    backend_memory_id=backend_memory_id,
                ).debug("memory index insert raced: falling back to concurrent row")
                concurrent = await session.scalar(
                    select(MemoryRecord).where(
                        and_(
                            MemoryRecord.user_id == user_id,
                            MemoryRecord.memory_scope_id == memory_scope_id,
                            MemoryRecord.backend_memory_id == backend_memory_id,
                            MemoryRecord.memory_status == MemoryStatus.ACTIVE.value,
                        )
                    )
                )
                if concurrent is None:
                    raise
                concurrent.source_refs = source_refs
                concurrent.memory_text = memory_text
                concurrent.source_type = source_type
                concurrent.data_classification = data_classification
                concurrent.memory_type = memory_type
                concurrent.backend_categories = list(backend_categories or [])
                concurrent.memory_metadata = dict(metadata or {})
                await session.commit()
                await session.refresh(concurrent)
                return self._memory_from_record(concurrent)
        logger.bind(
            user_id=user_id,
            memory_scope_id=memory_scope_id,
            backend_memory_id=backend_memory_id,
            memory_id=entry.memory_id,
            action="created",
        ).debug("memory index created")
        return entry

    def active_memories(self, user_id: str, memory_scope_id: str) -> list[MemoryIndexEntry]:
        return self._run(self._active_memories(user_id, memory_scope_id))

    async def _active_memories(self, user_id: str, memory_scope_id: str) -> list[MemoryIndexEntry]:
        async with self.session_factory() as session:
            records = (
                await session.scalars(
                    select(MemoryRecord)
                    .where(
                        and_(
                            MemoryRecord.user_id == user_id,
                            MemoryRecord.memory_scope_id == memory_scope_id,
                            MemoryRecord.memory_status == MemoryStatus.ACTIVE.value,
                        )
                    )
                    .order_by(MemoryRecord.created_at, MemoryRecord.memory_id)
                )
            ).all()
            return [self._memory_from_record(record) for record in records]

    def list_memories(self, user_id: str, memory_scope_id: str) -> list[MemoryIndexEntry]:
        return self._run(self._list_memories(user_id, memory_scope_id))

    async def _list_memories(self, user_id: str, memory_scope_id: str) -> list[MemoryIndexEntry]:
        async with self.session_factory() as session:
            records = (
                await session.scalars(
                    select(MemoryRecord)
                    .where(
                        and_(
                            MemoryRecord.user_id == user_id,
                            MemoryRecord.memory_scope_id == memory_scope_id,
                        )
                    )
                    .order_by(MemoryRecord.created_at, MemoryRecord.memory_id)
                )
            ).all()
            return [self._memory_from_record(record) for record in records]

    def get_active_memory_by_backend_id(
        self, user_id: str, memory_scope_id: str, backend_memory_id: str
    ) -> MemoryIndexEntry | None:
        return self._run(
            self._get_active_memory_by_backend_id(user_id, memory_scope_id, backend_memory_id)
        )

    async def _get_active_memory_by_backend_id(
        self, user_id: str, memory_scope_id: str, backend_memory_id: str
    ) -> MemoryIndexEntry | None:
        async with self.session_factory() as session:
            record = await session.scalar(
                select(MemoryRecord).where(
                    and_(
                        MemoryRecord.user_id == user_id,
                        MemoryRecord.memory_scope_id == memory_scope_id,
                        MemoryRecord.backend_memory_id == backend_memory_id,
                        MemoryRecord.memory_status == MemoryStatus.ACTIVE.value,
                    )
                )
            )
            return self._memory_from_record(record) if record else None

    def update_memory_index(
        self,
        memory_id: str,
        *,
        backend_memory_id: str | None = None,
        source_refs: list[dict[str, str]],
        memory_text: str,
        source_type: str = SourceType.CHAT_ROUND.value,
        data_classification: str = DataClassification.NORMAL.value,
        memory_type: str | None = None,
        backend_categories: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        valid_at: datetime | None = None,
    ) -> MemoryIndexEntry | None:
        return self._run(
            self._update_memory_index(
                memory_id,
                backend_memory_id=backend_memory_id,
                source_refs=source_refs,
                memory_text=memory_text,
                source_type=source_type,
                data_classification=data_classification,
                memory_type=memory_type,
                backend_categories=backend_categories,
                metadata=metadata,
                valid_at=valid_at,
            )
        )

    async def _update_memory_index(
        self,
        memory_id: str,
        *,
        backend_memory_id: str | None = None,
        source_refs: list[dict[str, str]],
        memory_text: str,
        source_type: str = SourceType.CHAT_ROUND.value,
        data_classification: str = DataClassification.NORMAL.value,
        memory_type: str | None = None,
        backend_categories: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        valid_at: datetime | None = None,
    ) -> MemoryIndexEntry | None:
        async with self.session_factory() as session:
            record = await session.get(MemoryRecord, memory_id)
            if record is None:
                return None
            if backend_memory_id is not None:
                record.backend_memory_id = backend_memory_id
            record.source_refs = source_refs
            record.memory_text = memory_text
            record.source_type = source_type
            record.data_classification = data_classification
            record.memory_type = memory_type
            record.backend_categories = list(backend_categories or [])
            record.memory_metadata = dict(metadata or {})
            if valid_at is not None:
                record.valid_at = valid_at
            await session.commit()
            await session.refresh(record)
            return self._memory_from_record(record)

    def mark_memory_deleted(self, memory_id: str) -> MemoryIndexEntry | None:
        return self._run(self._mark_memory_deleted(memory_id))

    async def _mark_memory_deleted(self, memory_id: str) -> MemoryIndexEntry | None:
        return await self._set_memory_status(memory_id, MemoryStatus.DELETED)

    def mark_memory_superseded(self, memory_id: str) -> MemoryIndexEntry | None:
        return self._run(self._mark_memory_superseded(memory_id))

    async def _mark_memory_superseded(self, memory_id: str) -> MemoryIndexEntry | None:
        return await self._set_memory_status(memory_id, MemoryStatus.SUPERSEDED)

    def mark_memory_active(self, memory_id: str) -> MemoryIndexEntry | None:
        return self._run(self._mark_memory_active(memory_id))

    def mark_memory_suppressed(self, memory_id: str) -> MemoryIndexEntry | None:
        """P2#7 decay：SUPPRESSED（不可达而非删除）+ 幂等失效时刻。"""
        return self._run(self._set_memory_status(memory_id, MemoryStatus.SUPPRESSED))

    async def _mark_memory_active(self, memory_id: str) -> MemoryIndexEntry | None:
        return await self._set_memory_status(memory_id, MemoryStatus.ACTIVE)

    def update_memory_source_refs(
        self, memory_id: str, source_refs: list[dict[str, str]]
    ) -> MemoryIndexEntry | None:
        return self._run(self._update_memory_source_refs(memory_id, source_refs))

    async def _update_memory_source_refs(
        self, memory_id: str, source_refs: list[dict[str, str]]
    ) -> MemoryIndexEntry | None:
        async with self.session_factory() as session:
            record = await session.get(MemoryRecord, memory_id)
            if record is None:
                return None
            record.source_refs = source_refs
            await session.commit()
            await session.refresh(record)
            return self._memory_from_record(record)

    async def _set_memory_status(
        self, memory_id: str, status: MemoryStatus
    ) -> MemoryIndexEntry | None:
        async with self.session_factory() as session:
            record = await session.get(MemoryRecord, memory_id)
            if record is None:
                return None
            record.memory_status = status.value
            # P2#6 双时态：失效态记录事实失效时刻（幂等，不覆盖更早时刻）；
            # 重新激活 = 事实重新为真，清除历史失效时间。
            # P2#7：SUPPRESSED（decay）与 DELETED/SUPERSEDED 同为失效态。
            if status in {MemoryStatus.DELETED, MemoryStatus.SUPERSEDED, MemoryStatus.SUPPRESSED}:
                if record.invalid_at is None:
                    record.invalid_at = datetime.now(UTC)
            elif status is MemoryStatus.ACTIVE:
                record.invalid_at = None
            await session.commit()
            await session.refresh(record)
            return self._memory_from_record(record)

    def deleted_source_refs(
        self, user_id: str, memory_scope_id: str
    ) -> set[tuple[str | None, str | None]]:
        return self._run(self._deleted_source_refs(user_id, memory_scope_id))

    async def _deleted_source_refs(
        self, user_id: str, memory_scope_id: str
    ) -> set[tuple[str | None, str | None]]:
        async with self.session_factory() as session:
            records = (
                await session.scalars(
                    select(MemoryRecord).where(
                        and_(
                            MemoryRecord.user_id == user_id,
                            MemoryRecord.memory_scope_id == memory_scope_id,
                            MemoryRecord.memory_status == MemoryStatus.DELETED.value,
                        )
                    )
                )
            ).all()
            refs: set[tuple[str | None, str | None]] = set()
            for record in records:
                refs.update(source_ref_key(source_ref) for source_ref in record.source_refs)
            return refs

    def excluded_source_refs(
        self, user_id: str, memory_scope_id: str
    ) -> set[tuple[str | None, str | None]]:
        return self._run(self._excluded_source_refs(user_id, memory_scope_id))

    async def _excluded_source_refs(
        self, user_id: str, memory_scope_id: str
    ) -> set[tuple[str | None, str | None]]:
        async with self.session_factory() as session:
            records = (
                await session.scalars(
                    select(MemoryRecord).where(
                        and_(
                            MemoryRecord.user_id == user_id,
                            MemoryRecord.memory_scope_id == memory_scope_id,
                            MemoryRecord.memory_status.in_(
                                [
                                    MemoryStatus.DELETED.value,
                                    MemoryStatus.SUPERSEDED.value,
                                    # P2#7：decay 记忆的来源不参与重建/摘要（防复活）
                                    MemoryStatus.SUPPRESSED.value,
                                ]
                            ),
                        )
                    )
                )
            ).all()
            refs: set[tuple[str | None, str | None]] = set()
            for record in records:
                refs.update(source_ref_key(source_ref) for source_ref in record.source_refs)
            return refs

    def touch_memory_recalled(self, memory_ids: list[str]) -> int:
        """P2#7：批量记录召回触达（单条 UPDATE，last_recalled_at=now, count+1）。"""
        if not memory_ids:
            return 0
        return self._run(self._touch_memory_recalled(memory_ids))

    async def _touch_memory_recalled(self, memory_ids: list[str]) -> int:
        async with self.session_factory() as session:
            result = await session.execute(
                update(MemoryRecord)
                .where(MemoryRecord.memory_id.in_(memory_ids))
                .values(
                    last_recalled_at=datetime.now(UTC),
                    recall_count=MemoryRecord.recall_count + 1,
                )
            )
            await session.commit()
            return int(getattr(result, "rowcount", 0) or 0)

    def save_task(self, task: TaskEntry) -> TaskEntry:
        return self._run(self._save_task(task))

    def claim_task(self, task: TaskEntry) -> tuple[TaskEntry, bool]:
        return self._run(self._claim_task(task))

    async def _claim_task(self, task: TaskEntry) -> tuple[TaskEntry, bool]:
        async with self.session_factory() as session:
            record = MemoryTaskRecord(
                task_id=task.task_id,
                request_id=task.request_id,
                op_type=task.op_type.value,
                scope=task.scope,
                operation_id=task.operation_id,
                history_version=task.history_version,
                status=task.status.value,
                retry_count=task.retry_count,
                last_error=task.last_error,
                result=task.result,
            )
            session.add(record)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await session.get(MemoryTaskRecord, task.task_id)
                if existing is None:
                    raise
                logger.bind(
                    task_id=task.task_id,
                    existing_status=existing.status,
                ).debug("task claim lost race: returning existing task")
                return self._task_from_record(existing), False
        logger.bind(task_id=task.task_id).debug("task claim succeeded")
        return task, True

    async def _save_task(self, task: TaskEntry) -> TaskEntry:
        """P1a：乐观锁写入。

        更新走原子 ``UPDATE ... WHERE task_id=? AND row_version=?``，
        rowcount=0（另一副本已基于更新快照提交）抛 ``TaskStaleWriteError``，
        由调用方重读重放或放弃——杜绝多副本盲写整行导致的丢失更新
        （retry_count 少计 / pending 计数失真 / 状态回跳）。
        成功后行版本 +1，并把 entry 的版本同步抬升，支持同锁内链式变更。
        """
        async with self.session_factory() as session:
            record = await session.get(MemoryTaskRecord, task.task_id)
            if record is None:
                record = MemoryTaskRecord(
                    task_id=task.task_id,
                    request_id=task.request_id,
                    op_type=task.op_type.value,
                    scope=task.scope,
                    operation_id=task.operation_id,
                    history_version=task.history_version,
                    status=task.status.value,
                    retry_count=task.retry_count,
                    last_error=task.last_error,
                    result=task.result,
                    row_version=0,
                )
                session.add(record)
                task.row_version = 0
            else:
                result = await session.execute(
                    update(MemoryTaskRecord)
                    .where(
                        MemoryTaskRecord.task_id == task.task_id,
                        MemoryTaskRecord.row_version == task.row_version,
                    )
                    .values(
                        request_id=task.request_id,
                        op_type=task.op_type.value,
                        scope=task.scope,
                        operation_id=task.operation_id,
                        history_version=task.history_version,
                        status=task.status.value,
                        retry_count=task.retry_count,
                        last_error=task.last_error,
                        result=task.result,
                        row_version=task.row_version + 1,
                    )
                )
                if int(getattr(result, "rowcount", 0) or 0) == 0:
                    await session.rollback()
                    raise TaskStaleWriteError(task.task_id, task.row_version)
                task.row_version += 1
            await session.commit()
        logger.bind(
            task_id=task.task_id,
            status=task.status.value,
            op_type=task.op_type.value,
            row_version=task.row_version,
        ).debug("task saved")
        return task

    def get_task(self, task_id: str) -> TaskEntry | None:
        return self._run(self._get_task(task_id))

    async def _get_task(self, task_id: str) -> TaskEntry | None:
        async with self.session_factory() as session:
            record = await session.get(MemoryTaskRecord, task_id)
            return self._task_from_record(record) if record else None

    def reclaim_stale_running_tasks(self, *, max_age_seconds: float) -> list[str]:
        return self._run(self._reclaim_stale_running_tasks(max_age_seconds=max_age_seconds))

    async def _reclaim_stale_running_tasks(self, *, max_age_seconds: float) -> list[str]:
        """把超过 ``max_age_seconds`` 未更新的 running 任务回收为 failed。

        服务进程被硬杀（SIGKILL / OOM / 节点驱逐）时，in-flight 任务
        停在 running 且无人推进，重启后 ``GetTask`` 对客户端永远返回
        running。启动时调用本方法让这些孤儿如实呈现失败。

        原子 ``UPDATE ... WHERE status='running' AND updated_at < cutoff``
        集合级条件更新：多副本并发执行时后到者的 WHERE 不再命中，
        天然幂等；正在健康执行的任务会随每次 ``save_task`` 刷新
        ``updated_at``（onupdate），只要阈值显著大于最长任务时长
        （L3 抽取分钟级）就不会被误回收。row_version 随行 +1，
        不与 ``TaskStaleWriteError`` 路径冲突（本更新不读旧快照）。
        """
        cutoff = datetime.now(UTC) - timedelta(seconds=max_age_seconds)
        async with self.session_factory() as session:
            result = await session.execute(
                update(MemoryTaskRecord)
                .where(
                    MemoryTaskRecord.status == TaskStatus.RUNNING.value,
                    MemoryTaskRecord.updated_at < cutoff,
                )
                .values(
                    status=TaskStatus.FAILED.value,
                    last_error=(
                        f"reclaimed: orphaned running task with no update for "
                        f"{max_age_seconds:g}s (startup recovery)"
                    ),
                    row_version=MemoryTaskRecord.row_version + 1,
                    updated_at=func.now(),
                )
                .returning(MemoryTaskRecord.task_id)
            )
            reclaimed = [str(row[0]) for row in result.fetchall()]
            await session.commit()
        if reclaimed:
            logger.bind(
                reclaimed_count=len(reclaimed),
                max_age_seconds=max_age_seconds,
                task_ids=reclaimed[:20],
            ).warning("memory stale running tasks reclaimed")
        return reclaimed

    @staticmethod
    def _journal_from_record(record: SummaryRoundJournalRecord) -> JournalEntry:
        return JournalEntry(
            journal_id=record.journal_id,
            user_id=record.user_id,
            memory_scope_id=record.memory_scope_id,
            session_id=record.session_id,
            round_id=record.round_id,
            round_index=record.round_index,
            messages=[dict(message) for message in record.messages],
            source_timestamp=record.source_timestamp,
            round_fingerprint=record.round_fingerprint,
            round_state=record.round_state,
        )

    @staticmethod
    def _summary_from_record(record: CurrentSummaryRecord) -> SummaryEntry:
        return SummaryEntry(
            summary_id=record.summary_id,
            user_id=record.user_id,
            memory_scope_id=record.memory_scope_id,
            summary_text=record.summary_text,
            summary_cursor_round=record.summary_cursor_round,
            latest_source_round_id=record.latest_source_round_id,
            latest_source_timestamp=record.latest_source_timestamp,
            summary_state=SummaryState(record.summary_state),
            summary_kind=getattr(record, "summary_kind", "concat") or "concat",
        )

    @staticmethod
    def _memory_from_record(record: MemoryRecord) -> MemoryIndexEntry:
        return MemoryIndexEntry(
            memory_id=record.memory_id,
            backend_memory_id=record.backend_memory_id,
            user_id=record.user_id,
            memory_scope_id=record.memory_scope_id,
            source_refs=[dict(source_ref) for source_ref in record.source_refs],
            memory_text=record.memory_text,
            memory_status=MemoryStatus(record.memory_status),
            source_type=record.source_type,
            data_classification=record.data_classification,
            memory_type=record.memory_type,
            backend_categories=list(record.backend_categories or []),
            metadata=dict(record.memory_metadata or {}),
            valid_at=getattr(record, "valid_at", None),
            invalid_at=getattr(record, "invalid_at", None),
            last_recalled_at=getattr(record, "last_recalled_at", None),
            recall_count=int(getattr(record, "recall_count", 0) or 0),
        )

    @staticmethod
    def _task_from_record(record: MemoryTaskRecord) -> TaskEntry:
        return TaskEntry(
            task_id=record.task_id,
            request_id=record.request_id,
            op_type=OperationType(record.op_type),
            scope=dict(record.scope),
            status=TaskStatus(record.status),
            operation_id=record.operation_id,
            history_version=record.history_version,
            retry_count=record.retry_count,
            last_error=record.last_error,
            result=dict(record.result or {}),
            row_version=int(getattr(record, "row_version", 0) or 0),
        )
