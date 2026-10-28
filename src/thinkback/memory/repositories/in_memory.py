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

from thinkback.domain.entities import (
    AdminAuditEntry,
    JournalEntry,
    MemoryIndexEntry,
    SummaryEntry,
    TaskEntry,
)
from thinkback.domain.enums import (
    DataClassification,
    MemoryStatus,
    SourceType,
    SummaryState,
    TaskStatus,
)
from thinkback.domain.errors import TaskStaleWriteError
from thinkback.domain.keys import (
    DEFAULT_NAMESPACE,
    namespace_scope_key,
    request_fingerprint,
    round_sort_key,
    source_ref_key,
)
from thinkback.domain.summarization import summarize_rounds
from thinkback.memory.repositories._l1_cache import L1_CACHE_LIMIT, L1CacheMixin
from thinkback.memory.schemas import AppendMemoryRequest


def _stamp_invalidated(memory: MemoryIndexEntry) -> None:
    """置失效时间（幂等：已设置时不覆盖更早的失效时刻）。"""

    if memory.invalid_at is None:
        memory.invalid_at = datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    """naive 时间按 UTC 处理（asyncpg/SQLite 可能丢掉 tzinfo）。"""

    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@dataclass
class InMemoryMemoryRepository(L1CacheMixin):
    journal_by_round: dict[str, JournalEntry] = field(default_factory=dict)
    summaries: dict[str, SummaryEntry] = field(default_factory=dict)
    memories: dict[str, MemoryIndexEntry] = field(default_factory=dict)
    tasks: dict[str, TaskEntry] = field(default_factory=dict)
    admin_audit: list[AdminAuditEntry] = field(default_factory=list)
    # 与 SQL 版 updated_at 对应的墙钟时间戳，reclaim_stale_running_tasks 用
    task_updated_at: dict[str, float] = field(default_factory=dict)
    l1_cache: dict[str, list[JournalEntry]] = field(default_factory=dict)
    task_lock: Lock = field(default_factory=Lock, repr=False)
    # N-5: 与 task_lock 同风格，作为 dataclass 字段提供，避免 dataclass __init__
    # 不调用 mixin __init__ 导致 _l1_lock 未初始化。
    _l1_lock: Lock = field(default_factory=Lock, repr=False)

    def get_round(self, round_id: str) -> JournalEntry | None:
        return self.journal_by_round.get(round_id)

    def get_l1(
        self,
        user_id: str,
        memory_scope_id: str,
        session_id: str,
        *,
        namespace: str = DEFAULT_NAMESPACE,
    ) -> list[JournalEntry]:
        """L1 读取：缓存优先，缺失时从 journal 派生（与 SQL 实现同语义）。

        冷缓存（进程重启 / 其他写者）必须回源，否则召回会丢最近轮次。
        """
        cached = super().get_l1(user_id, memory_scope_id, session_id, namespace=namespace)
        if cached:
            return cached
        rounds = self.list_rounds(user_id, memory_scope_id, session_id, namespace=namespace)
        for entry in rounds[-L1_CACHE_LIMIT:]:
            super().update_l1(entry, limit=L1_CACHE_LIMIT)
        return super().get_l1(user_id, memory_scope_id, session_id, namespace=namespace)

    def save_round(
        self, request: AppendMemoryRequest, *, request_metadata: dict[str, Any] | None = None
    ) -> JournalEntry:
        # 与 SQL 实现同语义：round_id 幂等、首写胜出（含 round_state），
        # 重复 save 不覆盖已存回合（append 重试不得复活/改写墓碑与 pending 态）。
        existing = self.journal_by_round.get(request.round_id)
        if existing is not None:
            return existing
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
            # L1 的隔离维度是 (user, namespace, session)：namespace 存在
            # journal.namespace 列，作用域键仍是会话维度。
            memory_scope_id=request.session_id,
            session_id=request.session_id,
            round_id=request.round_id,
            round_index=request.round_index,
            messages=messages,
            source_timestamp=request.source_timestamp,
            round_fingerprint=request_fingerprint(request),
            namespace=request.namespace,
            request_metadata=dict(request_metadata or {}),
        )
        self.journal_by_round[request.round_id] = entry
        return entry

    def upsert_summary_from_journal(
        self, user_id: str, memory_scope_id: str, *, namespace: str = DEFAULT_NAMESPACE
    ) -> SummaryEntry:
        # 排除集按**用户**聚合（不按 scope）：L3 记忆在长期 scope，
        # 轮次在会话 scope；按 scope 过滤会漏掉已删除事实 → 摘要把原文复活。
        deleted_refs = self.excluded_source_refs_for_user(user_id)
        rounds = [
            entry
            for entry in self.list_rounds(user_id, memory_scope_id, namespace=namespace)
            if (entry.session_id, entry.round_id) not in deleted_refs
        ]
        return self.upsert_summary_from_rounds(
            user_id, memory_scope_id, rounds, namespace=namespace
        )

    def upsert_summary_from_rounds(
        self,
        user_id: str,
        memory_scope_id: str,
        rounds: list[JournalEntry],
        *,
        summary_text: str | None = None,
        summary_kind: str = "concat",
        structured_sections: dict[str, str] | None = None,
        preserve_llm: bool = False,
        expected_row_version: int | None = None,
        namespace: str = DEFAULT_NAMESPACE,
    ) -> SummaryEntry:
        composed_text, latest = summarize_rounds(rounds)
        key = namespace_scope_key(user_id, namespace, memory_scope_id)
        existing = self.summaries.get(key)
        # 乐观锁：版本已经变了（删除、状态变更、别人写过）就不写。
        if (
            existing is not None
            and expected_row_version is not None
            and existing.row_version != expected_row_version
        ):
            return existing
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
            namespace=namespace,
            summary_text=summary_text if summary_text is not None else composed_text,
            summary_cursor_round=latest.round_id if latest else None,
            latest_source_round_id=latest.round_id if latest else None,
            latest_source_timestamp=latest.source_timestamp if latest else None,
            summary_state=SummaryState.ACTIVE,
            summary_kind=summary_kind,
            structured_sections=dict(structured_sections) if structured_sections else {},
            row_version=(existing.row_version + 1) if existing else 0,
        )
        self.summaries[key] = summary
        return summary

    def get_summary(
        self, user_id: str, memory_scope_id: str, *, namespace: str = DEFAULT_NAMESPACE
    ) -> SummaryEntry | None:
        return self.summaries.get(namespace_scope_key(user_id, namespace, memory_scope_id))

    def mark_summary(
        self,
        user_id: str,
        memory_scope_id: str,
        state: SummaryState,
        *,
        namespace: str = DEFAULT_NAMESPACE,
    ) -> None:
        summary = self.get_summary(user_id, memory_scope_id, namespace=namespace)
        if summary:
            summary.summary_state = state
            # 状态变更也要 +1：让基于旧快照的在飞刷新写不进来。
            summary.row_version += 1

    def list_rounds(
        self,
        user_id: str,
        memory_scope_id: str,
        session_id: str | None = None,
        *,
        limit: int | None = None,
        descending: bool = False,
        include_pending: bool = False,
        namespace: str = DEFAULT_NAMESPACE,
    ) -> list[JournalEntry]:
        states = ("active", "pending_append") if include_pending else ("active",)
        rounds = [
            entry
            for entry in self.journal_by_round.values()
            if entry.user_id == user_id
            and entry.memory_scope_id == memory_scope_id
            and entry.namespace == namespace
            and entry.round_state in states
            and (session_id is None or entry.session_id == session_id)
        ]
        ordered = sorted(rounds, key=round_sort_key, reverse=descending)
        if limit is not None:
            ordered = ordered[:limit]
        return ordered

    def list_user_rounds(
        self,
        user_id: str,
        *,
        include_pending: bool = False,
        namespace: str | None = None,
    ) -> list[JournalEntry]:
        states = ("active", "pending_append") if include_pending else ("active",)
        rounds = [
            entry
            for entry in self.journal_by_round.values()
            if entry.user_id == user_id
            and entry.round_state in states
            and (namespace is None or entry.namespace == namespace)
        ]
        return sorted(rounds, key=round_sort_key)

    def mark_rounds_deleted(
        self,
        user_id: str,
        memory_scope_id: str,
        session_id: str | None = None,
        *,
        namespace: str = DEFAULT_NAMESPACE,
    ) -> int:
        affected = 0
        for entry in self.journal_by_round.values():
            if (
                entry.user_id == user_id
                and entry.memory_scope_id == memory_scope_id
                and entry.namespace == namespace
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
        memory_scope_id: str = "thinkback",
        source_refs: list[dict[str, str]],
        memory_text: str,
        source_type: str = SourceType.CHAT_ROUND.value,
        data_classification: str = DataClassification.NORMAL.value,
        memory_type: str | None = None,
        backend_categories: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        valid_at: datetime | None = None,
        expires_at: datetime | None = None,
        recorded_at: datetime | None = None,
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
                expires_at=expires_at,
                recorded_at=recorded_at,
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
            # TTL 到期时刻：后端会隐藏过期记忆，本地索引必须记同一份，
            # 否则过期事实会从本地召回/列表回流。
            expires_at=expires_at,
            # 记录时刻（本地写下这条事实的时刻）。
            recorded_at=recorded_at or datetime.now(UTC),
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
        expires_at: datetime | None = None,
        recorded_at: datetime | None = None,
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
        if expires_at is not None:
            memory.expires_at = expires_at
        if recorded_at is not None:
            memory.recorded_at = recorded_at
        return memory

    def mark_memory_deleted(self, memory_id: str) -> MemoryIndexEntry | None:
        memory = self.memories.get(memory_id)
        if memory:
            memory.memory_status = MemoryStatus.DELETED
            _stamp_invalidated(memory)
        return memory

    def list_expired_memories(
        self,
        *,
        before: datetime,
        limit: int = 500,
        memory_scope_id: str | None = None,
        user_id: str | None = None,
    ) -> list[MemoryIndexEntry]:
        """列出到期时刻早于 ``before`` 的 ACTIVE 记忆索引行。"""
        expired = [
            memory
            for memory in self.memories.values()
            if memory.memory_status is MemoryStatus.ACTIVE
            and memory.expires_at is not None
            and _as_utc(memory.expires_at) <= _as_utc(before)
            and (memory_scope_id is None or memory.memory_scope_id == memory_scope_id)
            and (user_id is None or memory.user_id == user_id)
        ]
        expired.sort(key=lambda memory: memory.expires_at or before)
        return expired[:limit]

    def delete_memory_rows(self, memory_ids: list[str]) -> int:
        """物理删除 L3 业务索引行（erase 语义）。"""
        deleted = 0
        for memory_id in memory_ids:
            if self.memories.pop(memory_id, None) is not None:
                deleted += 1
        return deleted

    def delete_rounds(
        self,
        user_id: str,
        memory_scope_id: str,
        session_id: str | None = None,
        round_ids: list[str] | None = None,
        *,
        namespace: str = DEFAULT_NAMESPACE,
    ) -> int:
        """物理删除 L1 轮次原文行（erase 语义）。"""
        victims = [
            round_id
            for round_id, entry in self.journal_by_round.items()
            if entry.user_id == user_id
            and entry.memory_scope_id == memory_scope_id
            and entry.namespace == namespace
            and (session_id is None or entry.session_id == session_id)
            and (round_ids is None or round_id in round_ids)
        ]
        for round_id in victims:
            del self.journal_by_round[round_id]
        return len(victims)

    def clear_summary(
        self, user_id: str, memory_scope_id: str, *, namespace: str = DEFAULT_NAMESPACE
    ) -> None:
        """清空 L2 会话摘要正文（erase 语义）。

        同时置 DIRTY 并递增版本：在飞的 LLM 刷新带着旧版本号写不回来，
        已擦除的内容不会回到 ACTIVE 摘要。
        """
        summary = self.summaries.get(namespace_scope_key(user_id, namespace, memory_scope_id))
        if summary is not None:
            summary.summary_text = ""
            summary.structured_sections = {}
            summary.summary_state = SummaryState.DIRTY
            summary.row_version += 1

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

    def excluded_source_refs_for_user(self, user_id: str) -> set[tuple[str | None, str | None]]:
        """该用户全部 scope 下已删除/被取代/被遗忘记忆的来源轮次。

        与 SQL 版同语义：L3 记忆在长期 scope，L1/L2 在会话 scope，
        跨表聚合排除集时不能按单一 scope 过滤。
        """
        refs: set[tuple[str | None, str | None]] = set()
        for memory in self.memories.values():
            if memory.user_id != user_id:
                continue
            if memory.memory_status not in {
                MemoryStatus.DELETED,
                MemoryStatus.SUPERSEDED,
                MemoryStatus.SUPPRESSED,
            }:
                continue
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

    def reclaim_stale_running_task(self, task_id: str, *, max_age_seconds: float) -> bool:
        """与 SQL 版同语义：单任务惰性回收（get_task 读路径自愈用）。"""

        cutoff = time.time() - max_age_seconds
        with self.task_lock:
            task = self.tasks.get(task_id)
            if task is None or task.status != TaskStatus.RUNNING:
                return False
            if self.task_updated_at.get(task_id, 0.0) >= cutoff:
                return False
            task.status = TaskStatus.FAILED
            task.last_error = (
                f"reclaimed: orphaned running task with no update for "
                f"{max_age_seconds:g}s (stale read recovery)"
            )
            task.row_version += 1
            self.task_updated_at[task_id] = time.time()
            return True

    def list_tasks(
        self,
        *,
        statuses: list[str] | None = None,
        older_than_seconds: float | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[TaskEntry]:
        """与 SQL 版同语义：状态/年龄过滤，按更新时间倒序分页。"""

        now = time.time()
        with self.task_lock:
            matched: list[tuple[float, TaskEntry]] = []
            for task_id, task in self.tasks.items():
                if statuses and task.status.value not in statuses:
                    continue
                if (
                    older_than_seconds is not None
                    and now - self.task_updated_at.get(task_id, 0.0) < older_than_seconds
                ):
                    continue
                matched.append((self.task_updated_at.get(task_id, 0.0), deepcopy(task)))
        matched.sort(key=lambda item: item[0], reverse=True)
        return [task for _, task in matched[offset : offset + limit]]

    def count_tasks_by_status(self) -> dict[str, int]:
        with self.task_lock:
            counts: dict[str, int] = {}
            for task in self.tasks.values():
                counts[task.status.value] = counts.get(task.status.value, 0) + 1
            return counts

    def count_memories_by_status(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for memory in self.memories.values():
            counts[memory.memory_status.value] = counts.get(memory.memory_status.value, 0) + 1
        return counts

    def count_memories_by_classification(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for memory in self.memories.values():
            key = str(getattr(memory, "data_classification", "") or "normal")
            counts[key] = counts.get(key, 0) + 1
        return counts

    def count_memories_by_source_type(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for memory in self.memories.values():
            key = str(getattr(memory, "source_type", "") or "chat_round")
            counts[key] = counts.get(key, 0) + 1
        return counts

    def admin_list_memories(
        self,
        *,
        user_id: str | None = None,
        memory_scope_id: str | None = None,
        statuses: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[MemoryIndexEntry], int]:
        """与 SQL 版同语义：过滤 + 分页，memory_id 稳定排序。"""

        matched = [
            deepcopy(memory)
            for memory in self.memories.values()
            if (user_id is None or memory.user_id == user_id)
            and (memory_scope_id is None or memory.memory_scope_id == memory_scope_id)
            and (statuses is None or memory.memory_status.value in statuses)
        ]
        matched.sort(key=lambda memory: memory.memory_id)
        return matched[offset : offset + limit], len(matched)

    def get_memory_index(self, memory_id: str) -> MemoryIndexEntry | None:
        memory = self.memories.get(memory_id)
        return deepcopy(memory) if memory is not None else None

    def get_rounds_by_refs(self, refs: list[dict[str, str]]) -> list[JournalEntry]:
        rounds: list[JournalEntry] = []
        for ref in refs:
            entry = self.journal_by_round.get(ref.get("round_id", ""))
            if entry is not None:
                rounds.append(deepcopy(entry))
        return rounds

    def save_admin_audit(self, entry: AdminAuditEntry) -> None:
        self.admin_audit.append(entry)

    def list_admin_audit(
        self,
        *,
        action: str | None = None,
        operator: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AdminAuditEntry]:
        matched = [
            deepcopy(entry)
            for entry in self.admin_audit
            if (action is None or entry.action == action)
            and (operator is None or entry.operator == operator)
        ]
        matched.sort(key=lambda entry: entry.created_at, reverse=True)
        return matched[offset : offset + limit]
