"""Memory repository implementations and persistence DTOs."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Coroutine
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, TypeVar
from uuid import uuid4

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from infra.database.engine import SessionLocal
from infra.database.models import (
    CurrentSummaryRecord,
    MemoryRecord,
    MemoryTaskRecord,
    SummaryRoundJournalRecord,
)
from memory.schemas import (
    AppendMemoryRequest,
    ContextType,
    DataClassification,
    FactSubject,
    MemoryStatus,
    OperationType,
    RoleplayMode,
    SourceType,
    SummaryState,
    TaskStatus,
)

T = TypeVar("T")


def scope_key(user_id: str, character_id: str) -> str:
    return f"{user_id}:{character_id}"


def fingerprint(messages: list[dict[str, Any]]) -> str:
    raw = "|".join(f"{message['role']}:{message['content']}" for message in messages)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def request_fingerprint(request: AppendMemoryRequest) -> str:
    return fingerprint(
        [{"role": message.role.value, "content": message.content} for message in request.messages]
    )


def source_ref_key(source_ref: dict[str, str]) -> tuple[str | None, str | None]:
    return source_ref.get("session_id"), source_ref.get("round_id")


def summarize_rounds(rounds: list[JournalEntry]) -> tuple[str, JournalEntry | None]:
    text_parts: list[str] = []
    latest: JournalEntry | None = None
    for entry in rounds[-10:]:
        latest = entry
        text_parts.extend(message["content"] for message in entry.messages if message["role"] == "user")
    return "；".join(text_parts), latest


@dataclass
class JournalEntry:
    journal_id: str
    user_id: str
    character_id: str
    session_id: str
    round_id: str
    messages: list[dict[str, Any]]
    source_timestamp: datetime
    round_fingerprint: str
    round_index: int | None = None
    round_state: str = "active"


@dataclass
class SummaryEntry:
    summary_id: str
    user_id: str
    character_id: str
    summary_text: str
    summary_cursor_round: str | None
    latest_source_round_id: str | None
    latest_source_timestamp: datetime | None
    summary_state: SummaryState = SummaryState.ACTIVE


@dataclass
class MemoryIndexEntry:
    memory_id: str
    backend_memory_id: str
    user_id: str
    character_id: str
    source_refs: list[dict[str, str]]
    memory_text: str
    memory_status: MemoryStatus = MemoryStatus.ACTIVE
    source_type: str = SourceType.CHAT_ROUND.value
    fact_subject: str = FactSubject.USER.value
    context_type: str = ContextType.REAL_USER.value
    roleplay_mode: str = RoleplayMode.OFF.value
    data_classification: str = DataClassification.NORMAL.value
    memory_type: str | None = None
    backend_categories: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskEntry:
    task_id: str
    request_id: str
    op_type: OperationType
    scope: dict[str, Any]
    status: TaskStatus
    operation_id: str | None = None
    history_version: str | None = None
    retry_count: int = 0
    last_error: str | None = None
    result: dict[str, Any] = field(default_factory=dict)


class MemoryRepository(Protocol):
    def get_round(self, round_id: str) -> JournalEntry | None:
        ...

    def save_round(self, request: AppendMemoryRequest) -> JournalEntry:
        ...

    def update_l1(self, entry: JournalEntry, limit: int = 10) -> None:
        ...

    def clear_l1(self, user_id: str, character_id: str, session_id: str | None = None) -> None:
        ...

    def get_l1(self, user_id: str, character_id: str, session_id: str) -> list[JournalEntry]:
        ...

    def upsert_summary_from_journal(self, user_id: str, character_id: str) -> SummaryEntry:
        ...

    def upsert_summary_from_rounds(
        self, user_id: str, character_id: str, rounds: list[JournalEntry]
    ) -> SummaryEntry:
        ...

    def get_summary(self, user_id: str, character_id: str) -> SummaryEntry | None:
        ...

    def mark_summary(self, user_id: str, character_id: str, state: SummaryState) -> None:
        ...

    def list_rounds(
        self, user_id: str, character_id: str, session_id: str | None = None
    ) -> list[JournalEntry]:
        ...

    def mark_rounds_deleted(
        self, user_id: str, character_id: str, session_id: str | None = None
    ) -> int:
        ...

    def add_memory_index(
        self,
        *,
        backend_memory_id: str,
        user_id: str,
        character_id: str,
        source_refs: list[dict[str, str]],
        memory_text: str,
        source_type: str = SourceType.CHAT_ROUND.value,
        fact_subject: str = FactSubject.USER.value,
        context_type: str = ContextType.REAL_USER.value,
        roleplay_mode: str = RoleplayMode.OFF.value,
        data_classification: str = DataClassification.NORMAL.value,
        memory_type: str | None = None,
        backend_categories: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryIndexEntry:
        ...

    def active_memories(self, user_id: str, character_id: str) -> list[MemoryIndexEntry]:
        ...

    def list_memories(self, user_id: str, character_id: str) -> list[MemoryIndexEntry]:
        ...

    def get_active_memory_by_backend_id(
        self, user_id: str, character_id: str, backend_memory_id: str
    ) -> MemoryIndexEntry | None:
        ...

    def update_memory_index(
        self,
        memory_id: str,
        *,
        source_refs: list[dict[str, str]],
        memory_text: str,
        source_type: str = SourceType.CHAT_ROUND.value,
        fact_subject: str = FactSubject.USER.value,
        context_type: str = ContextType.REAL_USER.value,
        roleplay_mode: str = RoleplayMode.OFF.value,
        data_classification: str = DataClassification.NORMAL.value,
        memory_type: str | None = None,
        backend_categories: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryIndexEntry | None:
        ...

    def mark_memory_deleted(self, memory_id: str) -> MemoryIndexEntry | None:
        ...

    def mark_memory_superseded(self, memory_id: str) -> MemoryIndexEntry | None:
        ...

    def update_memory_source_refs(
        self, memory_id: str, source_refs: list[dict[str, str]]
    ) -> MemoryIndexEntry | None:
        ...

    def deleted_source_refs(self, user_id: str, character_id: str) -> set[tuple[str | None, str | None]]:
        ...

    def excluded_source_refs(self, user_id: str, character_id: str) -> set[tuple[str | None, str | None]]:
        ...

    def save_task(self, task: TaskEntry) -> TaskEntry:
        ...

    def get_task(self, task_id: str) -> TaskEntry | None:
        ...


class L1CacheMixin:
    l1_cache: dict[str, list[JournalEntry]]

    def update_l1(self, entry: JournalEntry, limit: int = 10) -> None:
        key = f"{scope_key(entry.user_id, entry.character_id)}:{entry.session_id}"
        existing = [
            cached_entry
            for cached_entry in self.l1_cache.get(key, [])
            if cached_entry.round_id != entry.round_id
        ]
        existing.append(entry)
        self.l1_cache[key] = existing[-limit:]

    def clear_l1(self, user_id: str, character_id: str, session_id: str | None = None) -> None:
        prefix = scope_key(user_id, character_id)
        if session_id:
            self.l1_cache.pop(f"{prefix}:{session_id}", None)
            return
        for key in list(self.l1_cache):
            if key.startswith(f"{prefix}:"):
                self.l1_cache.pop(key, None)

    def get_l1(self, user_id: str, character_id: str, session_id: str) -> list[JournalEntry]:
        return list(self.l1_cache.get(f"{scope_key(user_id, character_id)}:{session_id}", []))


@dataclass
class InMemoryMemoryRepository(L1CacheMixin):
    journal_by_round: dict[str, JournalEntry] = field(default_factory=dict)
    summaries: dict[str, SummaryEntry] = field(default_factory=dict)
    memories: dict[str, MemoryIndexEntry] = field(default_factory=dict)
    tasks: dict[str, TaskEntry] = field(default_factory=dict)
    l1_cache: dict[str, list[JournalEntry]] = field(default_factory=dict)

    def get_round(self, round_id: str) -> JournalEntry | None:
        return self.journal_by_round.get(round_id)

    def save_round(self, request: AppendMemoryRequest) -> JournalEntry:
        messages = [
            {
                "message_id": message.message_id,
                "role": message.role.value,
                "content": message.content,
                "timestamp": message.timestamp.isoformat(),
            }
            for message in request.messages
        ]
        entry = JournalEntry(
            journal_id=f"journal-{uuid4()}",
            user_id=request.user_id,
            character_id=request.character_id,
            session_id=request.session_id,
            round_id=request.round_id,
            round_index=request.round_index,
            messages=messages,
            source_timestamp=request.source_timestamp,
            round_fingerprint=fingerprint(messages),
        )
        self.journal_by_round[request.round_id] = entry
        return entry

    def upsert_summary_from_journal(self, user_id: str, character_id: str) -> SummaryEntry:
        deleted_refs = self.deleted_source_refs(user_id, character_id)
        rounds = [
            entry
            for entry in self.list_rounds(user_id, character_id)
            if (entry.session_id, entry.round_id) not in deleted_refs
        ]
        return self.upsert_summary_from_rounds(user_id, character_id, rounds)

    def upsert_summary_from_rounds(
        self, user_id: str, character_id: str, rounds: list[JournalEntry]
    ) -> SummaryEntry:
        summary_text, latest = summarize_rounds(rounds)
        existing = self.summaries.get(scope_key(user_id, character_id))
        summary = SummaryEntry(
            summary_id=existing.summary_id if existing else f"summary-{uuid4()}",
            user_id=user_id,
            character_id=character_id,
            summary_text=summary_text,
            summary_cursor_round=latest.round_id if latest else None,
            latest_source_round_id=latest.round_id if latest else None,
            latest_source_timestamp=latest.source_timestamp if latest else None,
            summary_state=SummaryState.ACTIVE,
        )
        self.summaries[scope_key(user_id, character_id)] = summary
        return summary

    def get_summary(self, user_id: str, character_id: str) -> SummaryEntry | None:
        return self.summaries.get(scope_key(user_id, character_id))

    def mark_summary(self, user_id: str, character_id: str, state: SummaryState) -> None:
        summary = self.get_summary(user_id, character_id)
        if summary:
            summary.summary_state = state

    def list_rounds(
        self, user_id: str, character_id: str, session_id: str | None = None
    ) -> list[JournalEntry]:
        rounds = [
            entry
            for entry in self.journal_by_round.values()
            if entry.user_id == user_id
            and entry.character_id == character_id
            and entry.round_state == "active"
            and (session_id is None or entry.session_id == session_id)
        ]
        return sorted(rounds, key=lambda entry: entry.source_timestamp)

    def mark_rounds_deleted(
        self, user_id: str, character_id: str, session_id: str | None = None
    ) -> int:
        affected = 0
        for entry in self.journal_by_round.values():
            if (
                entry.user_id == user_id
                and entry.character_id == character_id
                and (session_id is None or entry.session_id == session_id)
                and entry.round_state != "deleted_tombstone"
            ):
                entry.round_state = "deleted_tombstone"
                affected += 1
        return affected

    def add_memory_index(
        self,
        *,
        backend_memory_id: str,
        user_id: str,
        character_id: str,
        source_refs: list[dict[str, str]],
        memory_text: str,
        source_type: str = SourceType.CHAT_ROUND.value,
        fact_subject: str = FactSubject.USER.value,
        context_type: str = ContextType.REAL_USER.value,
        roleplay_mode: str = RoleplayMode.OFF.value,
        data_classification: str = DataClassification.NORMAL.value,
        memory_type: str | None = None,
        backend_categories: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryIndexEntry:
        entry = MemoryIndexEntry(
            memory_id=f"memory-{uuid4()}",
            backend_memory_id=backend_memory_id,
            user_id=user_id,
            character_id=character_id,
            source_refs=source_refs,
            memory_text=memory_text,
            source_type=source_type,
            fact_subject=fact_subject,
            context_type=context_type,
            roleplay_mode=roleplay_mode,
            data_classification=data_classification,
            memory_type=memory_type,
            backend_categories=list(backend_categories or []),
            metadata=dict(metadata or {}),
        )
        self.memories[entry.memory_id] = entry
        return entry

    def active_memories(self, user_id: str, character_id: str) -> list[MemoryIndexEntry]:
        return [
            memory
            for memory in self.memories.values()
            if memory.user_id == user_id
            and memory.character_id == character_id
            and memory.memory_status is MemoryStatus.ACTIVE
        ]

    def list_memories(self, user_id: str, character_id: str) -> list[MemoryIndexEntry]:
        return [
            memory
            for memory in self.memories.values()
            if memory.user_id == user_id and memory.character_id == character_id
        ]

    def get_active_memory_by_backend_id(
        self, user_id: str, character_id: str, backend_memory_id: str
    ) -> MemoryIndexEntry | None:
        return next(
            (
                memory
                for memory in self.active_memories(user_id, character_id)
                if memory.backend_memory_id == backend_memory_id
            ),
            None,
        )

    def update_memory_index(
        self,
        memory_id: str,
        *,
        source_refs: list[dict[str, str]],
        memory_text: str,
        source_type: str = SourceType.CHAT_ROUND.value,
        fact_subject: str = FactSubject.USER.value,
        context_type: str = ContextType.REAL_USER.value,
        roleplay_mode: str = RoleplayMode.OFF.value,
        data_classification: str = DataClassification.NORMAL.value,
        memory_type: str | None = None,
        backend_categories: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryIndexEntry | None:
        memory = self.memories.get(memory_id)
        if memory is None:
            return None
        memory.source_refs = source_refs
        memory.memory_text = memory_text
        memory.source_type = source_type
        memory.fact_subject = fact_subject
        memory.context_type = context_type
        memory.roleplay_mode = roleplay_mode
        memory.data_classification = data_classification
        memory.memory_type = memory_type
        memory.backend_categories = list(backend_categories or [])
        memory.metadata = dict(metadata or {})
        return memory

    def mark_memory_deleted(self, memory_id: str) -> MemoryIndexEntry | None:
        memory = self.memories.get(memory_id)
        if memory:
            memory.memory_status = MemoryStatus.DELETED
        return memory

    def mark_memory_superseded(self, memory_id: str) -> MemoryIndexEntry | None:
        memory = self.memories.get(memory_id)
        if memory:
            memory.memory_status = MemoryStatus.SUPERSEDED
        return memory

    def update_memory_source_refs(
        self, memory_id: str, source_refs: list[dict[str, str]]
    ) -> MemoryIndexEntry | None:
        memory = self.memories.get(memory_id)
        if memory:
            memory.source_refs = source_refs
        return memory

    def deleted_source_refs(self, user_id: str, character_id: str) -> set[tuple[str | None, str | None]]:
        refs: set[tuple[str | None, str | None]] = set()
        for memory in self.memories.values():
            if (
                memory.user_id == user_id
                and memory.character_id == character_id
                and memory.memory_status is MemoryStatus.DELETED
            ):
                refs.update(source_ref_key(source_ref) for source_ref in memory.source_refs)
        return refs

    def excluded_source_refs(self, user_id: str, character_id: str) -> set[tuple[str | None, str | None]]:
        refs: set[tuple[str | None, str | None]] = set()
        for memory in self.memories.values():
            if (
                memory.user_id == user_id
                and memory.character_id == character_id
                and memory.memory_status in {MemoryStatus.DELETED, MemoryStatus.SUPERSEDED}
            ):
                refs.update(source_ref_key(source_ref) for source_ref in memory.source_refs)
        return refs

    def save_task(self, task: TaskEntry) -> TaskEntry:
        self.tasks[task.task_id] = task
        return task

    def get_task(self, task_id: str) -> TaskEntry | None:
        return self.tasks.get(task_id)


class SqlAlchemyMemoryRepository(L1CacheMixin):
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] = SessionLocal,
    ) -> None:
        self.session_factory = session_factory
        self.l1_cache: dict[str, list[JournalEntry]] = {}

    def _run(self, awaitable: Coroutine[Any, Any, T]) -> T:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(awaitable)
        raise RuntimeError(
            "SqlAlchemyMemoryRepository is sync at the service boundary; call it from a worker thread."
        )

    def get_round(self, round_id: str) -> JournalEntry | None:
        return self._run(self._get_round(round_id))

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

    async def _save_round(self, request: AppendMemoryRequest) -> JournalEntry:
        messages = [
            {
                "message_id": message.message_id,
                "role": message.role.value,
                "content": message.content,
                "timestamp": message.timestamp.isoformat(),
            }
            for message in request.messages
        ]
        entry = JournalEntry(
            journal_id=f"journal-{uuid4()}",
            user_id=request.user_id,
            character_id=request.character_id,
            session_id=request.session_id,
            round_id=request.round_id,
            round_index=request.round_index,
            messages=messages,
            source_timestamp=request.source_timestamp,
            round_fingerprint=fingerprint(messages),
        )
        async with self.session_factory() as session:
            existing = await session.scalar(
                select(SummaryRoundJournalRecord).where(
                    SummaryRoundJournalRecord.round_id == request.round_id
                )
            )
            if existing:
                return self._journal_from_record(existing)
            session.add(
                SummaryRoundJournalRecord(
                    journal_id=entry.journal_id,
                    user_id=entry.user_id,
                    character_id=entry.character_id,
                    session_id=entry.session_id,
                    round_id=entry.round_id,
                    round_index=entry.round_index,
                    round_fingerprint=entry.round_fingerprint,
                    messages=entry.messages,
                    source_timestamp=entry.source_timestamp,
                    round_state=entry.round_state,
                )
            )
            await session.commit()
        return entry

    def upsert_summary_from_journal(self, user_id: str, character_id: str) -> SummaryEntry:
        rounds = [
            entry
            for entry in self.list_rounds(user_id, character_id)
            if (entry.session_id, entry.round_id) not in self.deleted_source_refs(user_id, character_id)
        ]
        return self.upsert_summary_from_rounds(user_id, character_id, rounds)

    def upsert_summary_from_rounds(
        self, user_id: str, character_id: str, rounds: list[JournalEntry]
    ) -> SummaryEntry:
        summary_text, latest = summarize_rounds(rounds)
        return self._run(
            self._upsert_summary(
                user_id=user_id,
                character_id=character_id,
                summary_text=summary_text,
                latest=latest,
            )
        )

    async def _upsert_summary(
        self,
        *,
        user_id: str,
        character_id: str,
        summary_text: str,
        latest: JournalEntry | None,
    ) -> SummaryEntry:
        async with self.session_factory() as session:
            record = await session.scalar(
                select(CurrentSummaryRecord).where(
                    and_(
                        CurrentSummaryRecord.user_id == user_id,
                        CurrentSummaryRecord.character_id == character_id,
                    )
                )
            )
            if record is None:
                record = CurrentSummaryRecord(
                    summary_id=f"summary-{uuid4()}",
                    user_id=user_id,
                    character_id=character_id,
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
            await session.commit()
            await session.refresh(record)
            return self._summary_from_record(record)

    def get_summary(self, user_id: str, character_id: str) -> SummaryEntry | None:
        return self._run(self._get_summary(user_id, character_id))

    async def _get_summary(self, user_id: str, character_id: str) -> SummaryEntry | None:
        async with self.session_factory() as session:
            record = await session.scalar(
                select(CurrentSummaryRecord).where(
                    and_(
                        CurrentSummaryRecord.user_id == user_id,
                        CurrentSummaryRecord.character_id == character_id,
                    )
                )
            )
            return self._summary_from_record(record) if record else None

    def mark_summary(self, user_id: str, character_id: str, state: SummaryState) -> None:
        self._run(self._mark_summary(user_id, character_id, state))

    async def _mark_summary(self, user_id: str, character_id: str, state: SummaryState) -> None:
        async with self.session_factory() as session:
            record = await session.scalar(
                select(CurrentSummaryRecord).where(
                    and_(
                        CurrentSummaryRecord.user_id == user_id,
                        CurrentSummaryRecord.character_id == character_id,
                    )
                )
            )
            if record:
                record.summary_state = state.value
                await session.commit()

    def list_rounds(
        self, user_id: str, character_id: str, session_id: str | None = None
    ) -> list[JournalEntry]:
        return self._run(self._list_rounds(user_id, character_id, session_id))

    async def _list_rounds(
        self, user_id: str, character_id: str, session_id: str | None
    ) -> list[JournalEntry]:
        filters = [
            SummaryRoundJournalRecord.user_id == user_id,
            SummaryRoundJournalRecord.character_id == character_id,
            SummaryRoundJournalRecord.round_state == "active",
        ]
        if session_id is not None:
            filters.append(SummaryRoundJournalRecord.session_id == session_id)
        async with self.session_factory() as session:
            records = (
                await session.scalars(
                    select(SummaryRoundJournalRecord)
                    .where(and_(*filters))
                    .order_by(SummaryRoundJournalRecord.source_timestamp)
                )
            ).all()
            return [self._journal_from_record(record) for record in records]

    def mark_rounds_deleted(
        self, user_id: str, character_id: str, session_id: str | None = None
    ) -> int:
        return self._run(self._mark_rounds_deleted(user_id, character_id, session_id))

    async def _mark_rounds_deleted(
        self, user_id: str, character_id: str, session_id: str | None
    ) -> int:
        filters = [
            SummaryRoundJournalRecord.user_id == user_id,
            SummaryRoundJournalRecord.character_id == character_id,
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

    def add_memory_index(
        self,
        *,
        backend_memory_id: str,
        user_id: str,
        character_id: str,
        source_refs: list[dict[str, str]],
        memory_text: str,
        source_type: str = SourceType.CHAT_ROUND.value,
        fact_subject: str = FactSubject.USER.value,
        context_type: str = ContextType.REAL_USER.value,
        roleplay_mode: str = RoleplayMode.OFF.value,
        data_classification: str = DataClassification.NORMAL.value,
        memory_type: str | None = None,
        backend_categories: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryIndexEntry:
        return self._run(
            self._add_memory_index(
                backend_memory_id=backend_memory_id,
                user_id=user_id,
                character_id=character_id,
                source_refs=source_refs,
                memory_text=memory_text,
                source_type=source_type,
                fact_subject=fact_subject,
                context_type=context_type,
                roleplay_mode=roleplay_mode,
                data_classification=data_classification,
                memory_type=memory_type,
                backend_categories=backend_categories,
                metadata=metadata,
            )
        )

    async def _add_memory_index(
        self,
        *,
        backend_memory_id: str,
        user_id: str,
        character_id: str,
        source_refs: list[dict[str, str]],
        memory_text: str,
        source_type: str = SourceType.CHAT_ROUND.value,
        fact_subject: str = FactSubject.USER.value,
        context_type: str = ContextType.REAL_USER.value,
        roleplay_mode: str = RoleplayMode.OFF.value,
        data_classification: str = DataClassification.NORMAL.value,
        memory_type: str | None = None,
        backend_categories: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryIndexEntry:
        entry = MemoryIndexEntry(
            memory_id=f"memory-{uuid4()}",
            backend_memory_id=backend_memory_id,
            user_id=user_id,
            character_id=character_id,
            source_refs=source_refs,
            memory_text=memory_text,
            source_type=source_type,
            fact_subject=fact_subject,
            context_type=context_type,
            roleplay_mode=roleplay_mode,
            data_classification=data_classification,
            memory_type=memory_type,
            backend_categories=list(backend_categories or []),
            metadata=dict(metadata or {}),
        )
        async with self.session_factory() as session:
            session.add(
                MemoryRecord(
                    memory_id=entry.memory_id,
                    backend_memory_id=entry.backend_memory_id,
                    user_id=entry.user_id,
                    character_id=entry.character_id,
                    source_refs=entry.source_refs,
                    source_type=entry.source_type,
                    fact_subject=entry.fact_subject,
                    context_type=entry.context_type,
                    roleplay_mode=entry.roleplay_mode,
                    memory_status=entry.memory_status.value,
                    data_classification=entry.data_classification,
                    memory_text=entry.memory_text,
                    memory_type=entry.memory_type,
                    backend_categories=entry.backend_categories,
                    memory_metadata=entry.metadata,
                )
            )
            await session.commit()
        return entry

    def active_memories(self, user_id: str, character_id: str) -> list[MemoryIndexEntry]:
        return self._run(self._active_memories(user_id, character_id))

    async def _active_memories(self, user_id: str, character_id: str) -> list[MemoryIndexEntry]:
        async with self.session_factory() as session:
            records = (
                await session.scalars(
                    select(MemoryRecord).where(
                        and_(
                            MemoryRecord.user_id == user_id,
                            MemoryRecord.character_id == character_id,
                            MemoryRecord.memory_status == MemoryStatus.ACTIVE.value,
                        )
                    )
                )
            ).all()
            return [self._memory_from_record(record) for record in records]

    def list_memories(self, user_id: str, character_id: str) -> list[MemoryIndexEntry]:
        return self._run(self._list_memories(user_id, character_id))

    async def _list_memories(self, user_id: str, character_id: str) -> list[MemoryIndexEntry]:
        async with self.session_factory() as session:
            records = (
                await session.scalars(
                    select(MemoryRecord).where(
                        and_(
                            MemoryRecord.user_id == user_id,
                            MemoryRecord.character_id == character_id,
                        )
                    )
                )
            ).all()
            return [self._memory_from_record(record) for record in records]

    def get_active_memory_by_backend_id(
        self, user_id: str, character_id: str, backend_memory_id: str
    ) -> MemoryIndexEntry | None:
        return self._run(
            self._get_active_memory_by_backend_id(user_id, character_id, backend_memory_id)
        )

    async def _get_active_memory_by_backend_id(
        self, user_id: str, character_id: str, backend_memory_id: str
    ) -> MemoryIndexEntry | None:
        async with self.session_factory() as session:
            record = await session.scalar(
                select(MemoryRecord).where(
                    and_(
                        MemoryRecord.user_id == user_id,
                        MemoryRecord.character_id == character_id,
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
        source_refs: list[dict[str, str]],
        memory_text: str,
        source_type: str = SourceType.CHAT_ROUND.value,
        fact_subject: str = FactSubject.USER.value,
        context_type: str = ContextType.REAL_USER.value,
        roleplay_mode: str = RoleplayMode.OFF.value,
        data_classification: str = DataClassification.NORMAL.value,
        memory_type: str | None = None,
        backend_categories: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryIndexEntry | None:
        return self._run(
            self._update_memory_index(
                memory_id,
                source_refs=source_refs,
                memory_text=memory_text,
                source_type=source_type,
                fact_subject=fact_subject,
                context_type=context_type,
                roleplay_mode=roleplay_mode,
                data_classification=data_classification,
                memory_type=memory_type,
                backend_categories=backend_categories,
                metadata=metadata,
            )
        )

    async def _update_memory_index(
        self,
        memory_id: str,
        *,
        source_refs: list[dict[str, str]],
        memory_text: str,
        source_type: str = SourceType.CHAT_ROUND.value,
        fact_subject: str = FactSubject.USER.value,
        context_type: str = ContextType.REAL_USER.value,
        roleplay_mode: str = RoleplayMode.OFF.value,
        data_classification: str = DataClassification.NORMAL.value,
        memory_type: str | None = None,
        backend_categories: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryIndexEntry | None:
        async with self.session_factory() as session:
            record = await session.get(MemoryRecord, memory_id)
            if record is None:
                return None
            record.source_refs = source_refs
            record.memory_text = memory_text
            record.source_type = source_type
            record.fact_subject = fact_subject
            record.context_type = context_type
            record.roleplay_mode = roleplay_mode
            record.data_classification = data_classification
            record.memory_type = memory_type
            record.backend_categories = list(backend_categories or [])
            record.memory_metadata = dict(metadata or {})
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
            await session.commit()
            await session.refresh(record)
            return self._memory_from_record(record)

    def deleted_source_refs(self, user_id: str, character_id: str) -> set[tuple[str | None, str | None]]:
        return self._run(self._deleted_source_refs(user_id, character_id))

    async def _deleted_source_refs(
        self, user_id: str, character_id: str
    ) -> set[tuple[str | None, str | None]]:
        async with self.session_factory() as session:
            records = (
                await session.scalars(
                    select(MemoryRecord).where(
                        and_(
                            MemoryRecord.user_id == user_id,
                            MemoryRecord.character_id == character_id,
                            MemoryRecord.memory_status == MemoryStatus.DELETED.value,
                        )
                    )
                )
            ).all()
            refs: set[tuple[str | None, str | None]] = set()
            for record in records:
                refs.update(source_ref_key(source_ref) for source_ref in record.source_refs)
            return refs

    def excluded_source_refs(self, user_id: str, character_id: str) -> set[tuple[str | None, str | None]]:
        return self._run(self._excluded_source_refs(user_id, character_id))

    async def _excluded_source_refs(
        self, user_id: str, character_id: str
    ) -> set[tuple[str | None, str | None]]:
        async with self.session_factory() as session:
            records = (
                await session.scalars(
                    select(MemoryRecord).where(
                        and_(
                            MemoryRecord.user_id == user_id,
                            MemoryRecord.character_id == character_id,
                            MemoryRecord.memory_status.in_(
                                [MemoryStatus.DELETED.value, MemoryStatus.SUPERSEDED.value]
                            ),
                        )
                    )
                )
            ).all()
            refs: set[tuple[str | None, str | None]] = set()
            for record in records:
                refs.update(source_ref_key(source_ref) for source_ref in record.source_refs)
            return refs

    def save_task(self, task: TaskEntry) -> TaskEntry:
        return self._run(self._save_task(task))

    async def _save_task(self, task: TaskEntry) -> TaskEntry:
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
                )
                session.add(record)
            else:
                record.request_id = task.request_id
                record.op_type = task.op_type.value
                record.scope = task.scope
                record.operation_id = task.operation_id
                record.history_version = task.history_version
                record.status = task.status.value
                record.retry_count = task.retry_count
                record.last_error = task.last_error
                record.result = task.result
            await session.commit()
        return task

    def get_task(self, task_id: str) -> TaskEntry | None:
        return self._run(self._get_task(task_id))

    async def _get_task(self, task_id: str) -> TaskEntry | None:
        async with self.session_factory() as session:
            record = await session.get(MemoryTaskRecord, task_id)
            return self._task_from_record(record) if record else None

    @staticmethod
    def _journal_from_record(record: SummaryRoundJournalRecord) -> JournalEntry:
        return JournalEntry(
            journal_id=record.journal_id,
            user_id=record.user_id,
            character_id=record.character_id,
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
            character_id=record.character_id,
            summary_text=record.summary_text,
            summary_cursor_round=record.summary_cursor_round,
            latest_source_round_id=record.latest_source_round_id,
            latest_source_timestamp=record.latest_source_timestamp,
            summary_state=SummaryState(record.summary_state),
        )

    @staticmethod
    def _memory_from_record(record: MemoryRecord) -> MemoryIndexEntry:
        return MemoryIndexEntry(
            memory_id=record.memory_id,
            backend_memory_id=record.backend_memory_id,
            user_id=record.user_id,
            character_id=record.character_id,
            source_refs=[dict(source_ref) for source_ref in record.source_refs],
            memory_text=record.memory_text,
            memory_status=MemoryStatus(record.memory_status),
            source_type=record.source_type,
            fact_subject=record.fact_subject,
            context_type=record.context_type,
            roleplay_mode=record.roleplay_mode,
            data_classification=record.data_classification,
            memory_type=record.memory_type,
            backend_categories=list(record.backend_categories or []),
            metadata=dict(record.memory_metadata or {}),
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
        )
