"""进程内仓储实现（开发 / 测试）。

数据只存进程内存，不持久化；接口与 ``SqlAlchemyMemoryRepository`` 完全一致。
"""

from __future__ import annotations

import time
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Lock
from typing import Any
from uuid import uuid4

from innies_memory.domain.entities import (
    JournalEntry,
    MemoryIndexEntry,
    SummaryEntry,
    TaskEntry,
)
from innies_memory.domain.enums import (
    DataClassification,
    MemoryStatus,
    SourceType,
    SummaryState,
    TaskStatus,
)
from innies_memory.domain.errors import TaskStaleWriteError
from innies_memory.domain.keys import fingerprint, round_sort_key, scope_key, source_ref_key
from innies_memory.domain.summarization import summarize_rounds
from innies_memory.memory.repositories._l1_cache import L1CacheMixin
from innies_memory.memory.schemas import AppendMemoryRequest


def _stamp_invalidated(memory: MemoryIndexEntry) -> None:
    """置失效时间（幂等：已设置时不覆盖更早的失效时刻）。"""

    if memory.invalid_at is None:
        memory.invalid_at = datetime.now(UTC)


@dataclass
class InMemoryMemoryRepository(L1CacheMixin):
    journal_by_round: dict[str, JournalEntry] = field(default_factory=dict)
    summaries: dict[str, SummaryEntry] = field(default_factory=dict)
    memories: dict[str, MemoryIndexEntry] = field(default_factory=dict)
    tasks: dict[str, TaskEntry] = field(default_factory=dict)
    # 与 SQL 版 updated_at 对应的墙钟时间戳，reclaim_stale_running_tasks 用
    task_updated_at: dict[str, float] = field(default_factory=dict)
    l1_cache: dict[str, list[JournalEntry]] = field(default_factory=dict)
    task_lock: Lock = field(default_factory=Lock, repr=False)
    # N-5: 与 task_lock 同风格，作为 dataclass 字段提供，避免 dataclass __init__
    # 不调用 mixin __init__ 导致 _l1_lock 未初始化。
    _l1_lock: Lock = field(default_factory=Lock, repr=False)

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
            memory_scope_id=request.session_id,
            session_id=request.session_id,
            round_id=request.round_id,
            round_index=request.round_index,
            messages=messages,
            source_timestamp=request.source_timestamp,
            round_fingerprint=fingerprint(messages),
        )
        self.journal_by_round[request.round_id] = entry
        return entry

    def upsert_summary_from_journal(self, user_id: str, memory_scope_id: str) -> SummaryEntry:
        deleted_refs = self.excluded_source_refs(user_id, memory_scope_id)
        rounds = [
            entry
            for entry in self.list_rounds(user_id, memory_scope_id)
            if (entry.session_id, entry.round_id) not in deleted_refs
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
        existing = self.summaries.get(scope_key(user_id, memory_scope_id))
        if (
            preserve_llm
            and summary_kind == "concat"
            and existing is not None
            and existing.summary_kind == "llm"
        ):
            return existing
        summary = SummaryEntry(
            summary_id=existing.summary_id if existing else f"summary-{uuid4()}",
            user_id=user_id,
            memory_scope_id=memory_scope_id,
            summary_text=summary_text if summary_text is not None else composed_text,
            summary_cursor_round=latest.round_id if latest else None,
            latest_source_round_id=latest.round_id if latest else None,
            latest_source_timestamp=latest.source_timestamp if latest else None,
            summary_state=SummaryState.ACTIVE,
            summary_kind=summary_kind,
        )
        self.summaries[scope_key(user_id, memory_scope_id)] = summary
        return summary

    def get_summary(self, user_id: str, memory_scope_id: str) -> SummaryEntry | None:
        return self.summaries.get(scope_key(user_id, memory_scope_id))

    def mark_summary(self, user_id: str, memory_scope_id: str, state: SummaryState) -> None:
        summary = self.get_summary(user_id, memory_scope_id)
        if summary:
            summary.summary_state = state

    def list_rounds(
        self, user_id: str, memory_scope_id: str, session_id: str | None = None
    ) -> list[JournalEntry]:
        rounds = [
            entry
            for entry in self.journal_by_round.values()
            if entry.user_id == user_id
            and entry.memory_scope_id == memory_scope_id
            and entry.round_state == "active"
            and (session_id is None or entry.session_id == session_id)
        ]
        return sorted(rounds, key=round_sort_key)

    def list_user_rounds(self, user_id: str) -> list[JournalEntry]:
        rounds = [
            entry
            for entry in self.journal_by_round.values()
            if entry.user_id == user_id and entry.round_state == "active"
        ]
        return sorted(rounds, key=round_sort_key)

    def mark_rounds_deleted(
        self, user_id: str, memory_scope_id: str, session_id: str | None = None
    ) -> int:
        affected = 0
        for entry in self.journal_by_round.values():
            if (
                entry.user_id == user_id
                and entry.memory_scope_id == memory_scope_id
                and (session_id is None or entry.session_id == session_id)
                and entry.round_state != "deleted_tombstone"
            ):
                entry.round_state = "deleted_tombstone"
                affected += 1
        return affected

    def mark_round_deleted(self, round_id: str) -> bool:
        entry = self.journal_by_round.get(round_id)
        if entry is None:
            return False
        entry.round_state = "deleted_tombstone"
        return True

    def mark_round_active(self, round_id: str) -> bool:
        entry = self.journal_by_round.get(round_id)
        if entry is None:
            return False
        entry.round_state = "active"
        return True

    def mark_round_pending_append(self, round_id: str) -> bool:
        entry = self.journal_by_round.get(round_id)
        if entry is None:
            return False
        entry.round_state = "pending_append"
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
        existing = self.get_active_memory_by_backend_id(user_id, memory_scope_id, backend_memory_id)
        if existing is not None:
            updated = self.update_memory_index(
                existing.memory_id,
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
            if updated is not None:
                return updated
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
            # P2#6：未显式给定时以写入时刻为事实生效时刻
            valid_at=valid_at or datetime.now(UTC),
        )
        self.memories[entry.memory_id] = entry
        return entry

    def active_memories(self, user_id: str, memory_scope_id: str) -> list[MemoryIndexEntry]:
        return [
            memory
            for memory in self.memories.values()
            if memory.user_id == user_id
            and memory.memory_scope_id == memory_scope_id
            and memory.memory_status is MemoryStatus.ACTIVE
        ]

    def list_memories(self, user_id: str, memory_scope_id: str) -> list[MemoryIndexEntry]:
        return [
            memory
            for memory in self.memories.values()
            if memory.user_id == user_id and memory.memory_scope_id == memory_scope_id
        ]

    def get_active_memory_by_backend_id(
        self, user_id: str, memory_scope_id: str, backend_memory_id: str
    ) -> MemoryIndexEntry | None:
        return next(
            (
                memory
                for memory in self.active_memories(user_id, memory_scope_id)
                if memory.backend_memory_id == backend_memory_id
            ),
            None,
        )

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
        memory = self.memories.get(memory_id)
        if memory is None:
            return None
        if backend_memory_id is not None:
            memory.backend_memory_id = backend_memory_id
        memory.source_refs = source_refs
        memory.memory_text = memory_text
        memory.source_type = source_type
        memory.data_classification = data_classification
        memory.memory_type = memory_type
        memory.backend_categories = list(backend_categories or [])
        memory.metadata = dict(metadata or {})
        if valid_at is not None:
            memory.valid_at = valid_at
        return memory

    def mark_memory_deleted(self, memory_id: str) -> MemoryIndexEntry | None:
        memory = self.memories.get(memory_id)
        if memory:
            memory.memory_status = MemoryStatus.DELETED
            _stamp_invalidated(memory)
        return memory

    def mark_memory_superseded(self, memory_id: str) -> MemoryIndexEntry | None:
        memory = self.memories.get(memory_id)
        if memory:
            memory.memory_status = MemoryStatus.SUPERSEDED
            _stamp_invalidated(memory)
        return memory

    def mark_memory_active(self, memory_id: str) -> MemoryIndexEntry | None:
        memory = self.memories.get(memory_id)
        if memory:
            memory.memory_status = MemoryStatus.ACTIVE
            # 重新激活 = 事实重新为真，清掉历史失效时间
            memory.invalid_at = None
        return memory

    def mark_memory_suppressed(self, memory_id: str) -> MemoryIndexEntry | None:
        """P2#7 decay：SUPPRESSED（不可达而非删除）+ 幂等失效时刻。"""
        memory = self.memories.get(memory_id)
        if memory:
            memory.memory_status = MemoryStatus.SUPPRESSED
            _stamp_invalidated(memory)
        return memory

    def touch_memory_recalled(self, memory_ids: list[str]) -> int:
        """P2#7：批量记录召回触达（被召回即强化，艾宾浩斯式）。"""
        touched = 0
        for memory_id in memory_ids:
            memory = self.memories.get(memory_id)
            if memory is not None:
                memory.last_recalled_at = datetime.now(UTC)
                memory.recall_count += 1
                touched += 1
        return touched

    def update_memory_source_refs(
        self, memory_id: str, source_refs: list[dict[str, str]]
    ) -> MemoryIndexEntry | None:
        memory = self.memories.get(memory_id)
        if memory:
            memory.source_refs = source_refs
        return memory

    def deleted_source_refs(
        self, user_id: str, memory_scope_id: str
    ) -> set[tuple[str | None, str | None]]:
        refs: set[tuple[str | None, str | None]] = set()
        for memory in self.memories.values():
            if (
                memory.user_id == user_id
                and memory.memory_scope_id == memory_scope_id
                and memory.memory_status is MemoryStatus.DELETED
            ):
                refs.update(source_ref_key(source_ref) for source_ref in memory.source_refs)
        return refs

    def excluded_source_refs(
        self, user_id: str, memory_scope_id: str
    ) -> set[tuple[str | None, str | None]]:
        refs: set[tuple[str | None, str | None]] = set()
        for memory in self.memories.values():
            if (
                memory.user_id == user_id
                and memory.memory_scope_id == memory_scope_id
                # P2#7：SUPPRESSED（decay）与 DELETED/SUPERSEDED 同口径排除，
                # 避免重建/摘要把已遗忘事实复活（此前 SUPPRESSED 从未赋值故无影响）。
                and memory.memory_status
                in {MemoryStatus.DELETED, MemoryStatus.SUPERSEDED, MemoryStatus.SUPPRESSED}
            ):
                refs.update(source_ref_key(source_ref) for source_ref in memory.source_refs)
        return refs

    def save_task(self, task: TaskEntry) -> TaskEntry:
        """P1a：乐观锁写入（与 SQL 实现同语义）。

        entry 的 row_version 落后于库中版本（另一写者已提交）时抛
        ``TaskStaleWriteError``；成功后版本 +1。get_task 返回深拷贝快照
        （对齐 SQL 每次读取新对象的语义），双"副本"共享本仓库即可
        忠实模拟跨进程竞争。
        """
        with self.task_lock:
            existing = self.tasks.get(task.task_id)
            if existing is not None and existing.row_version != task.row_version:
                raise TaskStaleWriteError(task.task_id, task.row_version)
            task.row_version = (existing.row_version if existing is not None else 0) + 1
            stored = deepcopy(task)
            self.tasks[task.task_id] = stored
            self.task_updated_at[task.task_id] = time.time()
            return deepcopy(stored)

    def claim_task(self, task: TaskEntry) -> tuple[TaskEntry, bool]:
        with self.task_lock:
            existing = self.tasks.get(task.task_id)
            if existing is not None:
                return deepcopy(existing), False
            task.row_version = 1
            stored = deepcopy(task)
            self.tasks[task.task_id] = stored
            self.task_updated_at[task.task_id] = time.time()
            return deepcopy(stored), True

    def get_task(self, task_id: str) -> TaskEntry | None:
        with self.task_lock:
            task = self.tasks.get(task_id)
            return deepcopy(task) if task is not None else None

    def reclaim_stale_running_tasks(self, *, max_age_seconds: float) -> list[str]:
        """与 SQL 版同语义：超龄 running 任务回收为 failed（进程内墙钟版）。"""

        cutoff = time.time() - max_age_seconds
        reclaimed: list[str] = []
        with self.task_lock:
            for task_id, task in self.tasks.items():
                if task.status != TaskStatus.RUNNING:
                    continue
                updated_at = self.task_updated_at.get(task_id, 0.0)
                if updated_at >= cutoff:
                    continue
                task.status = TaskStatus.FAILED
                task.last_error = (
                    f"reclaimed: orphaned running task with no update for "
                    f"{max_age_seconds:g}s (startup recovery)"
                )
                task.row_version += 1
                self.task_updated_at[task_id] = time.time()
                reclaimed.append(task_id)
        return reclaimed
