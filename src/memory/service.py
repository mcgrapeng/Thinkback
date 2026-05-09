"""P0 memory service workflows aligned with the three-layer architecture."""

from __future__ import annotations

import re
from concurrent.futures import Future, ThreadPoolExecutor, wait
from enum import StrEnum
from hashlib import sha256
from threading import BoundedSemaphore, Lock
from time import monotonic
from typing import Any, Literal, Protocol

from memory.backends import MemoryBackend
from memory.repositories import (
    InMemoryMemoryRepository,
    JournalEntry,
    MemoryRepository,
    SqlAlchemyMemoryRepository,
    TaskEntry,
    request_fingerprint,
    source_ref_key,
)
from memory.schemas import (
    AppendMemoryRequest,
    AppendMemoryResponse,
    ContextType,
    DataClassification,
    DeleteMemoryRequest,
    DeleteMemoryResponse,
    DeleteScope,
    FactSubject,
    MemoryItem,
    MemoryStatus,
    MemoryType,
    MessageRole,
    OperationType,
    RebuildMemoryRequest,
    RebuildMemoryResponse,
    RecallIntent,
    RecallMemoryRequest,
    RecallMemoryResponse,
    RoleplayMode,
    SourceType,
    SummaryState,
    TaskResponse,
    TaskStatus,
)


class HistorySource(Protocol):
    def current_version(self, user_id: str, character_id: str) -> str:
        ...

    def list_rounds(
        self, user_id: str, character_id: str, session_id: str | None = None
    ) -> list[JournalEntry]:
        ...


class MemoryService:
    name = "thinkback-memory"

    def __init__(
        self,
        *,
        repository: MemoryRepository,
        backend: MemoryBackend,
        history_source: HistorySource | None = None,
        active_memory_cache_ttl_seconds: float = 2.0,
        l3_write_mode: Literal["sync", "async"] = "sync",
        l3_executor: ThreadPoolExecutor | None = None,
        l3_executor_workers: int = 2,
        l3_max_pending_tasks: int = 64,
        l3_queue_wait_seconds: float = 0.25,
    ) -> None:
        if l3_executor_workers < 1:
            raise ValueError("l3_executor_workers must be >= 1")
        if l3_max_pending_tasks < 1:
            raise ValueError("l3_max_pending_tasks must be >= 1")
        if l3_queue_wait_seconds < 0:
            raise ValueError("l3_queue_wait_seconds must be >= 0")
        self.repository = repository
        self.backend = backend
        self.history_source = history_source
        self.active_memory_cache_ttl_seconds = active_memory_cache_ttl_seconds
        self._active_memory_cache: dict[tuple[str, str], tuple[float, list[Any]]] = {}
        self._summary_cache: dict[tuple[str, str], tuple[float, Any | None]] = {}
        self.l3_write_mode = l3_write_mode
        self.l3_executor_workers = l3_executor_workers
        self.l3_max_pending_tasks = l3_max_pending_tasks
        self.l3_queue_wait_seconds = l3_queue_wait_seconds
        self._l3_executor = l3_executor or ThreadPoolExecutor(
            max_workers=l3_executor_workers,
            thread_name_prefix="thinkback-l3",
        )
        self._l3_write_capacity = BoundedSemaphore(l3_max_pending_tasks)
        self._l3_futures_lock = Lock()
        self._l3_pending_write_slots = 0
        self._l3_background_futures: set[Future[None]] = set()
        self._l3_cleanup_futures: set[Future[None]] = set()

    def append(self, request: AppendMemoryRequest) -> AppendMemoryResponse:
        existing = self.repository.get_round(request.round_id)
        task_id = f"memory-extract:{request.round_id}"
        if existing:
            self._assert_round_matches_request(existing, request)
            existing_task = self.repository.get_task(task_id)
            if existing_task is None or existing_task.status in {
                TaskStatus.PENDING,
                TaskStatus.RUNNING,
                TaskStatus.COMPLETED,
            }:
                return AppendMemoryResponse(status="already_done", task_id=task_id, round_id=request.round_id)

        restricted_or_unsafe = self._is_restricted_or_unsafe(request.messages)
        l3_slot_reserved = False
        if self.l3_write_mode == "async" and not restricted_or_unsafe:
            self._reserve_l3_background_write_slot()
            l3_slot_reserved = True

        previous_task = self.repository.get_task(task_id)
        task = previous_task or TaskEntry(
            task_id=task_id,
            request_id=request.request_id,
            op_type=OperationType.WRITE_ROUND,
            scope={"user_id": request.user_id, "character_id": request.character_id},
            status=TaskStatus.RUNNING,
        )
        task.request_id = request.request_id
        task.status = TaskStatus.RUNNING
        task.last_error = None
        if previous_task and previous_task.status is TaskStatus.FAILED:
            task.retry_count += 1
        self.repository.save_task(task)
        try:
            entry = existing or self.repository.save_round(request)
            self.repository.update_l1(entry)
            if restricted_or_unsafe:
                l3_events = [{"event": "SKIP", "reason": "restricted_or_unsafe_memory_content"}]
                self.repository.mark_rounds_deleted(
                    request.user_id,
                    request.character_id,
                    request.session_id,
                )
                self.repository.clear_l1(
                    request.user_id,
                    request.character_id,
                    request.session_id,
                )
            else:
                self._upsert_default_summary(request.user_id, request.character_id)
                self._backfill_p0_slots_from_round_source(
                    source_text=" ".join(
                        message.content.strip()
                        for message in request.messages
                        if message.role is MessageRole.USER
                        if message.content.strip()
                    ),
                    user_id=request.user_id,
                    character_id=request.character_id,
                    source_refs=[{"session_id": request.session_id, "round_id": request.round_id}],
                    request_metadata=request.metadata,
                )
                self._invalidate_scope_caches(request.user_id, request.character_id)
                if self.l3_write_mode == "async":
                    l3_events = [{"event": "DEFERRED", "reason": "l3_background_write"}]
                    self._submit_l3_write(
                        task,
                        messages=[
                            {"role": message.role.value, "content": message.content}
                            for message in request.messages
                        ],
                        user_id=request.user_id,
                        character_id=request.character_id,
                        source_refs=[{"session_id": request.session_id, "round_id": request.round_id}],
                        request_metadata=request.metadata,
                        slot_reserved=l3_slot_reserved,
                    )
                    l3_slot_reserved = False
                    return AppendMemoryResponse(
                        status="completed",
                        task_id=task.task_id,
                        round_id=request.round_id,
                        l3_events=l3_events,
                    )
                l3_events = self._run_l3_write(
                    messages=[
                        {"role": message.role.value, "content": message.content}
                        for message in request.messages
                    ],
                    user_id=request.user_id,
                    character_id=request.character_id,
                    source_refs=[{"session_id": request.session_id, "round_id": request.round_id}],
                    request_metadata=request.metadata,
                )
        except Exception as exc:
            if l3_slot_reserved:
                self._release_l3_background_write_slot()
                l3_slot_reserved = False
            task.status = TaskStatus.FAILED
            task.last_error = str(exc)
            self.repository.save_task(task)
            raise

        task.status = TaskStatus.COMPLETED
        task.result = {
            "round_id": request.round_id,
            "l3_events": [self._redact_event(event) for event in l3_events],
        }
        self.repository.save_task(task)
        return AppendMemoryResponse(
            status="completed",
            task_id=task.task_id,
            round_id=request.round_id,
            l3_events=l3_events,
        )

    def drain_l3_background_tasks(self, timeout: float | None = None) -> None:
        with self._l3_futures_lock:
            futures = list(self._l3_background_futures | self._l3_cleanup_futures)
        if not futures:
            return
        wait(futures, timeout=timeout)
        for future in futures:
            if future.done():
                future.result()

    def l3_background_status(self) -> dict[str, int | str]:
        with self._l3_futures_lock:
            pending_write_tasks = self._l3_pending_write_slots
            cleanup_tasks = len(self._l3_cleanup_futures)
        return {
            "write_mode": self.l3_write_mode,
            "executor_workers": self.l3_executor_workers,
            "max_pending_tasks": self.l3_max_pending_tasks,
            "pending_write_tasks": pending_write_tasks,
            "cleanup_tasks": cleanup_tasks,
            "available_capacity": max(0, self.l3_max_pending_tasks - pending_write_tasks),
        }

    @staticmethod
    def _bounded_internal_request_id(*parts: str) -> str:
        raw = ":".join(part for part in parts if part)
        if len(raw) <= 128:
            return raw
        digest = sha256(raw.encode("utf-8")).hexdigest()[:32]
        return f"memory-extract:{digest}"

    def _submit_l3_write(
        self,
        task: TaskEntry,
        *,
        messages: list[dict[str, str]],
        user_id: str,
        character_id: str,
        source_refs: list[dict[str, str]],
        request_metadata: dict,
        slot_reserved: bool = False,
    ) -> None:
        reserved_here = False
        if not slot_reserved:
            self._reserve_l3_background_write_slot()
            reserved_here = True
        try:
            future = self._l3_executor.submit(
                self._complete_async_l3_write,
                task.task_id,
                messages,
                user_id,
                character_id,
                source_refs,
                request_metadata,
            )
        except Exception:
            if reserved_here:
                self._release_l3_background_write_slot()
            raise
        with self._l3_futures_lock:
            self._l3_background_futures.add(future)
        future.add_done_callback(self._finish_l3_background_write)

    def _reserve_l3_background_write_slot(self) -> None:
        acquired = self._l3_write_capacity.acquire(timeout=self.l3_queue_wait_seconds)
        if not acquired:
            pending = self._l3_pending_write_count()
            raise RuntimeError(
                "l3 background queue full: "
                f"pending={pending} max={self.l3_max_pending_tasks}"
            )
        with self._l3_futures_lock:
            self._l3_pending_write_slots += 1

    def _release_l3_background_write_slot(self) -> None:
        with self._l3_futures_lock:
            if self._l3_pending_write_slots > 0:
                self._l3_pending_write_slots -= 1
        self._l3_write_capacity.release()

    def _l3_pending_write_count(self) -> int:
        with self._l3_futures_lock:
            return self._l3_pending_write_slots

    def _finish_l3_background_write(self, future: Future[None]) -> None:
        with self._l3_futures_lock:
            self._l3_background_futures.discard(future)
        self._release_l3_background_write_slot()

    def _finish_l3_cleanup(self, future: Future[None]) -> None:
        with self._l3_futures_lock:
            self._l3_cleanup_futures.discard(future)

    def _delete_backend_memory(self, memory_id: str) -> None:
        if self.l3_write_mode == "async":
            future = self._l3_executor.submit(self.backend.delete, memory_id)
            with self._l3_futures_lock:
                self._l3_cleanup_futures.add(future)
            future.add_done_callback(self._finish_l3_cleanup)
            return
        self.backend.delete(memory_id)

    def _complete_async_l3_write(
        self,
        task_id: str,
        messages: list[dict[str, str]],
        user_id: str,
        character_id: str,
        source_refs: list[dict[str, str]],
        request_metadata: dict,
    ) -> None:
        task = self.repository.get_task(task_id)
        if task is None:
            return
        try:
            l3_events = self._run_l3_write(
                messages=messages,
                user_id=user_id,
                character_id=character_id,
                source_refs=source_refs,
                request_metadata=request_metadata,
            )
        except Exception as exc:
            task.status = TaskStatus.FAILED
            task.last_error = str(exc)
            self.repository.save_task(task)
            return
        task.status = TaskStatus.COMPLETED
        task.last_error = None
        task.result = {
            "round_id": source_refs[0].get("round_id") if source_refs else None,
            "l3_events": [self._redact_event(event) for event in l3_events],
        }
        self.repository.save_task(task)

    def _run_l3_write(
        self,
        *,
        messages: list[dict[str, str]],
        user_id: str,
        character_id: str,
        source_refs: list[dict[str, str]],
        request_metadata: dict,
    ) -> list[dict]:
        if self._source_refs_excluded(user_id, character_id, source_refs):
            return [{"event": "SKIP", "reason": "source_ref_excluded"}]
        l3_events = self._add_l3_from_messages(
            messages,
            user_id=user_id,
            character_id=character_id,
            source_refs=source_refs,
            request_metadata=request_metadata,
        )
        self._invalidate_scope_caches(user_id, character_id)
        return l3_events

    def recall(self, request: RecallMemoryRequest) -> RecallMemoryResponse:
        if request.intent in {
            RecallIntent.DELETE_CONFIRMATION,
            RecallIntent.PRIVACY,
            RecallIntent.SENSITIVE,
        }:
            raise ValueError(f"fail-closed recall intent: {request.intent.value}")

        items: list[MemoryItem] = []
        degradation_reasons: list[str] = []
        for entry in self.repository.get_l1(request.user_id, request.character_id, request.session_id):
            content = " ".join(
                message["content"]
                for message in entry.messages
                if not self._text_is_restricted_or_unsafe(message["content"])
            )
            items.append(MemoryItem(layer="L1", content=content, source=entry.round_id))

        summary = self._summary(request.user_id, request.character_id)
        if summary and summary.summary_state is SummaryState.ACTIVE and summary.summary_text:
            items.append(MemoryItem(layer="L2", content=summary.summary_text, source=summary.summary_id))
        elif summary and summary.summary_state is SummaryState.STALE and summary.summary_text:
            degradation_reasons.append("l2_stale")
            items.append(
                MemoryItem(
                    layer="L2",
                    content=summary.summary_text,
                    source=summary.summary_id,
                    metadata={"summary_state": SummaryState.STALE.value},
                )
            )
        elif summary and summary.summary_state is SummaryState.DIRTY:
            degradation_reasons.append("l2_dirty_skipped")

        must_query_l3 = request.intent in {
            RecallIntent.MEMORY_QUERY,
            RecallIntent.PERSONAL_INFO,
            RecallIntent.PREFERENCE,
            RecallIntent.RELATIONSHIP_CONTINUITY,
        }
        if must_query_l3:
            active_memories = self._active_memories(request.user_id, request.character_id)
            active_memory_by_backend_id = {
                memory.backend_memory_id: memory for memory in active_memories
            }
            query_slot = self._query_conflict_slot(request.query)
            if query_slot:
                self._backfill_matching_slot_memories(
                    items,
                    query=request.query,
                    active_memories=active_memories,
                )
            if query_slot and not any(item.layer == "L3" for item in items):
                pass
            elif not query_slot or not any(item.layer == "L3" for item in items):
                backend_items = self.backend.search(
                    request.query,
                    user_id=request.user_id,
                    character_id=request.character_id,
                    limit=request.l3_limit,
                    threshold=request.l3_score_threshold,
                )
                for item in backend_items:
                    memory_id = str(item.get("id", ""))
                    indexed_memory = active_memory_by_backend_id.get(memory_id)
                    if indexed_memory is None:
                        continue
                    if not self._memory_context_allowed(indexed_memory, request.query):
                        continue
                    if query_slot and self._memory_conflict_slot(indexed_memory.memory_text) != query_slot:
                        continue
                    items.append(
                        MemoryItem(
                            layer="L3",
                            content=indexed_memory.memory_text,
                            source="mem0",
                            memory_id=memory_id,
                            score=item.get("score"),
                            metadata={"memory_as_data": True, **(item.get("metadata") or {})},
                        )
                    )

        return RecallMemoryResponse(
            status="ok",
            degraded=bool(degradation_reasons),
            degradation_reasons=degradation_reasons,
            items=self._dedupe_and_clip(items, request.token_budget),
        )

    def delete(self, request: DeleteMemoryRequest) -> DeleteMemoryResponse:
        existing_task = self.repository.get_task(f"memory-delete:{request.operation_id}")
        if existing_task and existing_task.status is TaskStatus.COMPLETED:
            return DeleteMemoryResponse(
                status="already_done",
                task_id=existing_task.task_id,
                affected_memories=int(existing_task.result.get("affected_count", 0)),
                summary_state=SummaryState.DIRTY,
            )
        task = TaskEntry(
            task_id=f"memory-delete:{request.operation_id}",
            request_id=request.request_id,
            op_type={
                DeleteScope.MEMORY: OperationType.DELETE_MEMORY,
                DeleteScope.SESSION: OperationType.DELETE_SESSION,
                DeleteScope.ALL: OperationType.DELETE_ALL,
            }[request.scope],
            scope={"user_id": request.user_id, "character_id": request.character_id},
            status=TaskStatus.RUNNING,
            operation_id=request.operation_id,
        )
        self.repository.save_task(task)
        affected_memory_ids: list[str] = []

        try:
            if request.scope is DeleteScope.MEMORY:
                affected_memory_ids = self._delete_one_memory(request)
            elif request.scope is DeleteScope.SESSION:
                affected_memory_ids = self._delete_session_memories(request)
            else:
                affected_memory_ids = self._delete_all_memories(request)
        except Exception as exc:
            task.status = TaskStatus.FAILED
            task.last_error = str(exc)
            self.repository.save_task(task)
            raise

        task.status = TaskStatus.COMPLETED
        task.result = {
            "affected_count": len(affected_memory_ids),
            "affected_memory_ids": affected_memory_ids,
            "summary_state": SummaryState.DIRTY.value,
            "scope": request.scope.value,
            "session_id": request.session_id,
        }
        self.repository.save_task(task)
        self._invalidate_scope_caches(request.user_id, request.character_id)
        return DeleteMemoryResponse(
            status="completed",
            task_id=task.task_id,
            affected_memories=len(affected_memory_ids),
            summary_state=SummaryState.DIRTY,
        )

    def rebuild(self, request: RebuildMemoryRequest) -> RebuildMemoryResponse:
        existing_task = self.repository.get_task(f"memory-rebuild:{request.operation_id}")
        if existing_task and existing_task.status is TaskStatus.COMPLETED:
            return RebuildMemoryResponse(
                status="already_done",
                task_id=existing_task.task_id,
                rebuilt_l2=request.rebuild_l2,
                rebuilt_l3=request.rebuild_l3,
            )
        task = TaskEntry(
            task_id=f"memory-rebuild:{request.operation_id}",
            request_id=request.request_id,
            op_type=OperationType.REBUILD_L2,
            scope={"user_id": request.user_id, "character_id": request.character_id},
            status=TaskStatus.RUNNING,
            operation_id=request.operation_id,
            history_version=request.history_version,
        )
        self.repository.save_task(task)
        try:
            if request.history_version and self.history_source:
                current_version = self.history_source.current_version(
                    request.user_id,
                    request.character_id,
                )
                if current_version != request.history_version:
                    raise ValueError(
                        f"history_version mismatch: expected {request.history_version}, got {current_version}"
            )
            rounds = self._rebuild_rounds(request)
            if request.rebuild_l2:
                deleted_refs = self.repository.deleted_source_refs(
                    request.user_id,
                    request.character_id,
                )
                l2_rounds = [
                    entry
                    for entry in self._effective_history_rounds(
                        request.user_id,
                        request.character_id,
                        None,
                    )
                    if (entry.session_id, entry.round_id) not in deleted_refs
                    and self._round_allowed_in_default_summary(entry)
                ]
                self.repository.upsert_summary_from_rounds(
                    request.user_id,
                    request.character_id,
                    l2_rounds,
                )
                self._invalidate_summary_cache(request.user_id, request.character_id)
            if request.rebuild_l3:
                deleted_refs = self.repository.deleted_source_refs(
                    request.user_id,
                    request.character_id,
                )
                stale_memories = [
                    memory
                    for memory in self._l3_memories_in_rebuild_scope(request)
                    if all(
                        (ref.get("session_id"), ref.get("round_id")) in deleted_refs
                        for ref in memory.source_refs
                    )
                ]
                self.backend.delete_many(
                    [
                        memory.backend_memory_id
                        for memory in stale_memories
                        if self._is_backend_managed_memory_id(memory.backend_memory_id)
                    ]
                )
                for memory in stale_memories:
                    self.repository.mark_memory_superseded(memory.memory_id)
                for entry in self._uncovered_rebuild_rounds(request, rounds, deleted_refs):
                    messages = [
                        {"role": message["role"], "content": message["content"]}
                        for message in entry.messages
                    ]
                    if self._messages_are_restricted_or_unsafe(messages):
                        continue
                    source_refs = [{"session_id": entry.session_id, "round_id": entry.round_id}]
                    if self.l3_write_mode == "async":
                        extract_task, should_submit = self._ensure_l3_extract_task(
                            request_id=self._bounded_internal_request_id(
                                request.request_id,
                                entry.round_id,
                            ),
                            user_id=request.user_id,
                            character_id=request.character_id,
                            round_id=entry.round_id,
                        )
                        if not should_submit:
                            continue
                        self._submit_l3_write(
                            extract_task,
                            messages=messages,
                            user_id=request.user_id,
                            character_id=request.character_id,
                            source_refs=source_refs,
                            request_metadata={},
                        )
                        continue
                    self._run_l3_write(
                        messages=messages,
                        user_id=request.user_id,
                        character_id=request.character_id,
                        source_refs=source_refs,
                        request_metadata={},
                    )
        except Exception as exc:
            task.status = TaskStatus.FAILED
            task.last_error = str(exc)
            self.repository.save_task(task)
            raise
        task.status = TaskStatus.COMPLETED
        task.result = {
            "rebuilt_l2": request.rebuild_l2,
            "rebuilt_l3": request.rebuild_l3,
            "session_id": request.session_id,
            "history_version": request.history_version,
        }
        self.repository.save_task(task)
        self._invalidate_scope_caches(request.user_id, request.character_id)
        return RebuildMemoryResponse(
            status="completed",
            task_id=task.task_id,
            rebuilt_l2=request.rebuild_l2,
            rebuilt_l3=request.rebuild_l3,
        )

    def get_task(self, task_id: str) -> TaskResponse | None:
        task = self.repository.get_task(task_id)
        if not task:
            return None
        return TaskResponse(
            task_id=task.task_id,
            request_id=task.request_id,
            op_type=task.op_type,
            status=task.status,
            scope=task.scope,
            last_error=task.last_error,
            result=task.result,
        )

    def _ensure_l3_extract_task(
        self,
        *,
        request_id: str,
        user_id: str,
        character_id: str,
        round_id: str,
    ) -> tuple[TaskEntry, bool]:
        task_id = f"memory-extract:{round_id}"
        existing = self.repository.get_task(task_id)
        if existing is not None and existing.status in {
            TaskStatus.PENDING,
            TaskStatus.RUNNING,
            TaskStatus.COMPLETED,
        }:
            return existing, False
        task = existing or TaskEntry(
            task_id=task_id,
            request_id=self._bounded_internal_request_id(request_id),
            op_type=OperationType.WRITE_ROUND,
            scope={"user_id": user_id, "character_id": character_id},
            status=TaskStatus.RUNNING,
        )
        task.request_id = self._bounded_internal_request_id(request_id)
        task.status = TaskStatus.RUNNING
        task.last_error = None
        if existing and existing.status is TaskStatus.FAILED:
            task.retry_count += 1
        self.repository.save_task(task)
        return task, True

    def _delete_one_memory(self, request: DeleteMemoryRequest) -> list[str]:
        if not request.memory_id:
            raise ValueError("memory_id is required for memory deletion")
        memory = next(
            (
                memory
                for memory in self.repository.active_memories(request.user_id, request.character_id)
                if memory.memory_id == request.memory_id
            ),
            None,
        )
        if not memory:
            return []

        if self._is_backend_managed_memory_id(memory.backend_memory_id):
            self.backend.delete(memory.backend_memory_id)
        self.repository.mark_memory_deleted(memory.memory_id)
        self._mark_superseded_slot_sources_deleted(
            user_id=request.user_id,
            character_id=request.character_id,
            slot=self._memory_conflict_slot(memory.memory_text),
            deleted_memory_id=memory.memory_id,
        )
        for source_ref in memory.source_refs:
            self.repository.clear_l1(
                request.user_id,
                request.character_id,
                source_ref.get("session_id"),
            )
        self.repository.mark_summary(request.user_id, request.character_id, SummaryState.DIRTY)
        self._invalidate_summary_cache(request.user_id, request.character_id)
        return [memory.memory_id]

    def _delete_session_memories(self, request: DeleteMemoryRequest) -> list[str]:
        if not request.session_id:
            raise ValueError("session_id is required for session deletion")
        affected: list[str] = []
        for memory in self.repository.active_memories(request.user_id, request.character_id):
            if any(ref.get("session_id") == request.session_id for ref in memory.source_refs):
                remaining_refs = [
                    ref for ref in memory.source_refs if ref.get("session_id") != request.session_id
                ]
                if remaining_refs:
                    self.repository.update_memory_source_refs(memory.memory_id, remaining_refs)
                else:
                    if self._is_backend_managed_memory_id(memory.backend_memory_id):
                        self.backend.delete(memory.backend_memory_id)
                    self.repository.mark_memory_deleted(memory.memory_id)
                affected.append(memory.memory_id)
        self.repository.clear_l1(request.user_id, request.character_id, request.session_id)
        self.repository.mark_rounds_deleted(
            request.user_id,
            request.character_id,
            request.session_id,
        )
        self.repository.mark_summary(request.user_id, request.character_id, SummaryState.DIRTY)
        self._invalidate_summary_cache(request.user_id, request.character_id)
        return affected

    def _delete_all_memories(self, request: DeleteMemoryRequest) -> list[str]:
        active_memories = self.repository.active_memories(request.user_id, request.character_id)
        self.backend.delete_many(
            [
                memory.backend_memory_id
                for memory in active_memories
                if self._is_backend_managed_memory_id(memory.backend_memory_id)
            ]
        )
        for memory in active_memories:
            self.repository.mark_memory_deleted(memory.memory_id)
        self.repository.clear_l1(request.user_id, request.character_id)
        self.repository.mark_rounds_deleted(request.user_id, request.character_id)
        self.repository.mark_summary(request.user_id, request.character_id, SummaryState.DIRTY)
        self._invalidate_summary_cache(request.user_id, request.character_id)
        return [memory.memory_id for memory in active_memories]

    def _active_memories(self, user_id: str, character_id: str) -> list[Any]:
        cache_key = (user_id, character_id)
        now = monotonic()
        cached = self._active_memory_cache.get(cache_key)
        if cached is not None:
            cached_at, cached_memories = cached
            if now - cached_at <= self.active_memory_cache_ttl_seconds:
                return list(cached_memories)
        memories = self.repository.active_memories(user_id, character_id)
        self._active_memory_cache[cache_key] = (now, list(memories))
        return memories

    def _invalidate_active_memory_cache(self, user_id: str, character_id: str) -> None:
        self._active_memory_cache.pop((user_id, character_id), None)

    def _summary(self, user_id: str, character_id: str) -> Any | None:
        cache_key = (user_id, character_id)
        now = monotonic()
        cached = self._summary_cache.get(cache_key)
        if cached is not None:
            cached_at, cached_summary = cached
            if now - cached_at <= self.active_memory_cache_ttl_seconds:
                return cached_summary
        summary = self.repository.get_summary(user_id, character_id)
        self._summary_cache[cache_key] = (now, summary)
        return summary

    def _invalidate_summary_cache(self, user_id: str, character_id: str) -> None:
        self._summary_cache.pop((user_id, character_id), None)

    def _invalidate_scope_caches(self, user_id: str, character_id: str) -> None:
        self._invalidate_active_memory_cache(user_id, character_id)
        self._invalidate_summary_cache(user_id, character_id)

    def _dedupe_and_clip(self, items: list[MemoryItem], token_budget: int) -> list[MemoryItem]:
        priority = {"L3": 3, "L2": 2, "L1": 1}
        deduped: dict[str, MemoryItem] = {}
        for item in items:
            normalized = item.content.strip()
            if not normalized:
                continue
            existing = deduped.get(normalized)
            if existing is None or priority.get(item.layer, 0) > priority.get(existing.layer, 0):
                deduped[normalized] = item

        clipped: list[MemoryItem] = []
        used = 0
        for item in deduped.values():
            normalized = item.content.strip()
            cost = max(1, len(normalized) // 2)
            if used + cost > token_budget:
                break
            used += cost
            clipped.append(item)
        return clipped

    def _backfill_matching_slot_memories(
        self,
        items: list[MemoryItem],
        *,
        query: str,
        active_memories: list[Any],
    ) -> None:
        query_slot = self._query_conflict_slot(query)
        if query_slot is None:
            return
        existing_l3_ids = {item.memory_id for item in items if item.layer == "L3"}
        for memory in active_memories:
            if memory.backend_memory_id in existing_l3_ids:
                continue
            if not self._memory_context_allowed(memory, query):
                continue
            if self._memory_conflict_slot(memory.memory_text) != query_slot:
                continue
            items.append(
                MemoryItem(
                    layer="L3",
                    content=memory.memory_text,
                    source="business_index",
                    memory_id=memory.backend_memory_id,
                    metadata={"memory_as_data": True, "slot_backfill": query_slot},
                )
            )

    @staticmethod
    def _query_conflict_slot(query: str) -> str | None:
        normalized = query.lower()
        if any(marker in normalized for marker in ("猫", "cat")):
            return "pet_name:cat"
        if any(marker in normalized for marker in ("狗", "dog")):
            return "pet_name:dog"
        if any(marker in normalized for marker in ("鸟", "鹦鹉", "bird", "parrot")):
            return "pet_name:bird"
        if any(marker in normalized for marker in ("兔", "兔子", "rabbit", "bunny")):
            return "pet_name:rabbit"
        if any(
            marker in normalized
            for marker in ("称呼", "nickname", "called", "preferred name", "叫用户", "叫我")
        ):
            return "preferred_nickname"
        if any(marker in normalized for marker in ("住哪", "住在", "城市", "location", "live")):
            return "current_location"
        if any(marker in normalized for marker in ("工作状态", "offer", "job", "work status", "换工作")):
            return "current_work_status"
        if any(marker in normalized for marker in ("怎么给建议", "沟通", "建议", "advice", "communication")):
            return "communication_preference"
        if any(marker in normalized for marker in ("生日", "birthday")):
            return "birthday"
        if any(
            marker in normalized
            for marker in (
                "喜欢喝",
                "饮品",
                "饮料",
                "favorite drink",
                "preferred drink",
                "drink preference",
                "beverage preference",
            )
        ):
            return "favorite:drink"
        if any(
            marker in normalized
            for marker in ("喜欢吃", "食物", "favorite food", "food preference")
        ):
            return "favorite:food"
        if any(
            marker in normalized
            for marker in ("提醒睡觉", "催睡觉", "睡眠提醒", "睡觉偏好", "sleep reminder")
        ):
            return "sleep_reminder_preference"
        return None

    @staticmethod
    def _memory_context_allowed(memory: Any, query: str) -> bool:
        normalized_query = query.lower()
        asks_roleplay = any(
            marker in normalized_query
            for marker in ("剧情", "角色扮演", "设定", "story", "roleplay", "fiction")
        )
        context_type = str(getattr(memory, "context_type", "") or "").lower()
        fact_subject = str(getattr(memory, "fact_subject", "") or "").lower()
        roleplay_mode = str(getattr(memory, "roleplay_mode", "") or "").lower()
        is_roleplay_memory = (
            context_type in {"roleplay", "fictional_setting"}
            or fact_subject == "story_world"
            or roleplay_mode == "on"
        )
        if asks_roleplay:
            return is_roleplay_memory
        return not is_roleplay_memory

    def _add_l3_from_messages(
        self,
        messages: list[dict[str, str]],
        *,
        user_id: str,
        character_id: str,
        source_refs: list[dict[str, str]],
        request_metadata: dict,
    ) -> list[dict]:
        l3_metadata = self._l3_metadata(source_refs, request_metadata)
        source_text = " ".join(
            message.get("content", "").strip() for message in messages if message.get("content", "").strip()
        )
        if source_text:
            l3_metadata["source_text"] = source_text
        events = self.backend.add(
            messages,
            user_id=user_id,
            character_id=character_id,
            metadata=l3_metadata,
        )
        if self._source_refs_excluded(user_id, character_id, source_refs):
            self.backend.delete_many(
                [
                    str(event.get("id", ""))
                    for event in events
                    if self._is_backend_managed_memory_id(str(event.get("id", "")))
                ]
            )
            return [{"event": "SKIP", "reason": "source_ref_excluded"}]
        for event in events:
            self._index_l3_event(
                event,
                user_id=user_id,
                character_id=character_id,
                source_refs=source_refs,
                l3_metadata=l3_metadata,
            )
        return [dict(event) for event in events]

    def _backfill_p0_slots_from_round_source(
        self,
        *,
        source_text: str,
        user_id: str,
        character_id: str,
        source_refs: list[dict[str, str]],
        request_metadata: dict[str, Any],
    ) -> None:
        l3_metadata = self._l3_metadata(
            source_refs,
            {**request_metadata, "source_text": source_text},
        )
        for memory_text in self._p0_canonical_memories_from_source(source_text):
            if not self._memory_supported_by_source(memory_text, l3_metadata):
                continue
            existing_same_source = [
                memory
                for memory in self.repository.active_memories(user_id, character_id)
                if self._memory_conflict_slot(memory.memory_text) == self._memory_conflict_slot(memory_text)
                and self._conflict_partition(memory) == self._conflict_partition_from_metadata(l3_metadata)
                and any(source_ref_key(ref) in {source_ref_key(raw_ref) for raw_ref in source_refs} for ref in memory.source_refs)
            ]
            if any(memory.memory_text == memory_text for memory in existing_same_source):
                continue
            backend_id = self._local_p0_backend_memory_id(
                user_id=user_id,
                character_id=character_id,
                slot=self._memory_conflict_slot(memory_text),
                source_refs=source_refs,
            )
            if self._is_older_than_active_conflicting_memory(
                user_id=user_id,
                character_id=character_id,
                backend_memory_id=backend_id,
                memory_text=memory_text,
                l3_metadata=l3_metadata,
            ):
                continue
            self._supersede_conflicting_memories(
                user_id=user_id,
                character_id=character_id,
                backend_memory_id=backend_id,
                memory_text=memory_text,
                l3_metadata=l3_metadata,
            )
            self.repository.add_memory_index(
                backend_memory_id=backend_id,
                user_id=user_id,
                character_id=character_id,
                source_refs=source_refs,
                memory_text=memory_text,
                source_type=str(l3_metadata["source_type"]),
                fact_subject=str(l3_metadata["fact_subject"]),
                context_type=str(l3_metadata["context_type"]),
                roleplay_mode=str(l3_metadata["roleplay_mode"]),
                data_classification=str(l3_metadata["data_classification"]),
                memory_type=str(l3_metadata["memory_type"]),
                backend_categories=self._list_metadata_value(l3_metadata.get("backend_categories")),
                metadata={**l3_metadata, "p0_source_backfill": True},
            )

    @staticmethod
    def _local_p0_backend_memory_id(
        *,
        user_id: str,
        character_id: str,
        slot: str | None,
        source_refs: list[dict[str, str]],
    ) -> str:
        raw = "|".join(
            (
                user_id,
                character_id,
                slot or "unknown",
                *(
                    f"{source_ref.get('session_id', '')}:{source_ref.get('round_id', '')}"
                    for source_ref in source_refs
                ),
            )
        )
        digest = sha256(raw.encode("utf-8")).hexdigest()[:32]
        return f"local-p0:{slot or 'unknown'}:{digest}"

    def _index_l3_event(
        self,
        event: dict[str, Any],
        *,
        user_id: str,
        character_id: str,
        source_refs: list[dict[str, str]],
        l3_metadata: dict[str, Any],
    ) -> None:
        backend_id = str(event.get("id", ""))
        original_memory_text = str(event.get("memory", ""))
        memory_text = self._canonical_memory_text_from_source(original_memory_text, l3_metadata)
        if not backend_id or event.get("event") not in {"ADD", "UPDATE"}:
            return
        if self._source_refs_excluded(user_id, character_id, source_refs):
            self.backend.delete(backend_id)
            return

        raw_event_metadata = event.get("metadata")
        event_metadata: dict[str, Any] = (
            dict(raw_event_metadata) if isinstance(raw_event_metadata, dict) else {}
        )
        backend_categories = self._list_metadata_value(
            event_metadata.get("categories")
            or event_metadata.get("backend_categories")
            or l3_metadata.get("backend_categories")
        )
        index_metadata = {**dict(l3_metadata), **event_metadata}

        existing = self.repository.get_active_memory_by_backend_id(
            user_id,
            character_id,
            backend_id,
        )
        if not self._memory_supported_by_source(memory_text, l3_metadata):
            if existing is None:
                self.backend.delete(backend_id)
            return
        if existing:
            self.repository.update_memory_index(
                existing.memory_id,
                source_refs=source_refs,
                memory_text=memory_text,
                source_type=str(l3_metadata["source_type"]),
                fact_subject=str(l3_metadata["fact_subject"]),
                context_type=str(l3_metadata["context_type"]),
                roleplay_mode=str(l3_metadata["roleplay_mode"]),
                data_classification=str(l3_metadata["data_classification"]),
                memory_type=str(l3_metadata["memory_type"]),
                backend_categories=backend_categories,
                metadata=index_metadata,
            )
            if memory_text != original_memory_text:
                self.backend.update(backend_id, memory_text)
            return

        same_source_local = self._same_source_local_p0_memory(
            user_id=user_id,
            character_id=character_id,
            memory_text=memory_text,
            l3_metadata=l3_metadata,
        )
        if same_source_local is not None:
            self.repository.update_memory_index(
                same_source_local.memory_id,
                backend_memory_id=backend_id,
                source_refs=source_refs,
                memory_text=memory_text,
                source_type=str(l3_metadata["source_type"]),
                fact_subject=str(l3_metadata["fact_subject"]),
                context_type=str(l3_metadata["context_type"]),
                roleplay_mode=str(l3_metadata["roleplay_mode"]),
                data_classification=str(l3_metadata["data_classification"]),
                memory_type=str(l3_metadata["memory_type"]),
                backend_categories=backend_categories,
                metadata={**index_metadata, "p0_source_backfill": True, "backend_materialized": True},
            )
            if memory_text != original_memory_text:
                self.backend.update(backend_id, memory_text)
            return

        if self._is_older_than_active_conflicting_memory(
            user_id=user_id,
            character_id=character_id,
            backend_memory_id=backend_id,
            memory_text=memory_text,
            l3_metadata=l3_metadata,
        ):
            self.backend.delete(backend_id)
            return
        self._supersede_conflicting_memories(
            user_id=user_id,
            character_id=character_id,
            backend_memory_id=backend_id,
            memory_text=memory_text,
            l3_metadata=l3_metadata,
        )
        self.repository.add_memory_index(
            backend_memory_id=backend_id,
            user_id=user_id,
            character_id=character_id,
            source_refs=source_refs,
            memory_text=memory_text,
            source_type=str(l3_metadata["source_type"]),
            fact_subject=str(l3_metadata["fact_subject"]),
            context_type=str(l3_metadata["context_type"]),
            roleplay_mode=str(l3_metadata["roleplay_mode"]),
            data_classification=str(l3_metadata["data_classification"]),
            memory_type=str(l3_metadata["memory_type"]),
            backend_categories=backend_categories,
            metadata=index_metadata,
        )
        if memory_text != original_memory_text:
            self.backend.update(backend_id, memory_text)

    def _same_source_local_p0_memory(
        self,
        *,
        user_id: str,
        character_id: str,
        memory_text: str,
        l3_metadata: dict[str, Any],
    ) -> Any | None:
        return next(
            (
                memory
                for memory in self.repository.active_memories(user_id, character_id)
                if self._is_same_source_local_p0_memory(
                    memory,
                    memory_text=memory_text,
                    l3_metadata=l3_metadata,
                )
            ),
            None,
        )

    def _supersede_conflicting_memories(
        self,
        *,
        user_id: str,
        character_id: str,
        backend_memory_id: str,
        memory_text: str,
        l3_metadata: dict[str, Any] | None = None,
    ) -> None:
        new_slot = self._memory_conflict_slot(memory_text)
        if new_slot is None:
            return
        new_partition = self._conflict_partition_from_metadata(l3_metadata or {})
        for memory in self.repository.active_memories(user_id, character_id):
            if memory.backend_memory_id == backend_memory_id:
                continue
            if self._memory_conflict_slot(memory.memory_text) != new_slot:
                continue
            if self._conflict_partition(memory) != new_partition:
                continue
            if self._is_backend_managed_memory_id(memory.backend_memory_id):
                self._delete_backend_memory(memory.backend_memory_id)
            self.repository.mark_memory_superseded(memory.memory_id)

    @staticmethod
    def _is_backend_managed_memory_id(memory_id: str) -> bool:
        return not memory_id.startswith("local-p0:")

    def _is_older_than_active_conflicting_memory(
        self,
        *,
        user_id: str,
        character_id: str,
        backend_memory_id: str,
        memory_text: str,
        l3_metadata: dict[str, Any] | None = None,
    ) -> bool:
        new_slot = self._memory_conflict_slot(memory_text)
        if new_slot is None:
            return False
        new_partition = self._conflict_partition_from_metadata(l3_metadata or {})
        new_cursor = self._source_cursor(l3_metadata or {})
        if new_cursor is None:
            return False
        for memory in self.repository.active_memories(user_id, character_id):
            if memory.backend_memory_id == backend_memory_id:
                continue
            if self._memory_conflict_slot(memory.memory_text) != new_slot:
                continue
            if self._conflict_partition(memory) != new_partition:
                continue
            existing_cursor = self._source_cursor(memory.metadata)
            if existing_cursor is None:
                continue
            if new_cursor < existing_cursor:
                return True
            if new_cursor == existing_cursor and not self._is_same_source_local_p0_memory(
                memory,
                memory_text=memory_text,
                l3_metadata=l3_metadata or {},
            ):
                return True
        return False

    @staticmethod
    def _is_same_source_local_p0_memory(
        memory: Any,
        *,
        memory_text: str,
        l3_metadata: dict[str, Any],
    ) -> bool:
        backend_memory_id = str(getattr(memory, "backend_memory_id", ""))
        if not backend_memory_id.startswith("local-p0:"):
            return False
        if MemoryService._memory_conflict_slot(getattr(memory, "memory_text", "")) != MemoryService._memory_conflict_slot(memory_text):
            return False
        existing_refs = {source_ref_key(ref) for ref in getattr(memory, "source_refs", [])}
        raw_refs = l3_metadata.get("source_refs")
        if not isinstance(raw_refs, list):
            return False
        new_refs = {
            source_ref_key(ref)
            for ref in raw_refs
            if isinstance(ref, dict)
        }
        return bool(existing_refs & new_refs)

    @staticmethod
    def _source_cursor(metadata: dict[str, Any]) -> tuple[str, str] | None:
        raw_refs = metadata.get("source_refs")
        if not isinstance(raw_refs, list):
            return None
        refs: list[tuple[str, str]] = []
        for raw_ref in raw_refs:
            if not isinstance(raw_ref, dict):
                continue
            session_id = raw_ref.get("session_id")
            round_id = raw_ref.get("round_id")
            if session_id is None or round_id is None:
                continue
            refs.append((str(session_id), str(round_id)))
        if not refs:
            return None
        return max(refs)

    def _mark_superseded_slot_sources_deleted(
        self,
        *,
        user_id: str,
        character_id: str,
        slot: str | None,
        deleted_memory_id: str,
    ) -> None:
        if slot is None:
            return
        for memory in self.repository.list_memories(user_id, character_id):
            if memory.memory_id == deleted_memory_id:
                continue
            if self._memory_conflict_slot(memory.memory_text) != slot:
                continue
            if memory.memory_status.name != "SUPERSEDED":
                continue
            self.repository.mark_memory_deleted(memory.memory_id)

    @staticmethod
    def _conflict_partition_from_metadata(metadata: dict[str, Any]) -> tuple[str, str, str]:
        return (
            str(metadata.get("context_type") or ContextType.REAL_USER.value),
            str(metadata.get("fact_subject") or FactSubject.USER.value),
            str(metadata.get("roleplay_mode") or RoleplayMode.OFF.value),
        )

    @staticmethod
    def _conflict_partition(memory: Any) -> tuple[str, str, str]:
        return (
            str(getattr(memory, "context_type", "") or ContextType.REAL_USER.value),
            str(getattr(memory, "fact_subject", "") or FactSubject.USER.value),
            str(getattr(memory, "roleplay_mode", "") or RoleplayMode.OFF.value),
        )

    @staticmethod
    def _memory_conflict_slot(memory_text: str) -> str | None:
        normalized = MemoryService._canonical_memory_text(memory_text).lower()
        nickname_markers = (
            "叫我",
            "称呼",
            "preferred name",
            "prefers to be called",
            "prefers to be addressed as",
            "addressed as",
            "changed preferred name",
            "called",
        )
        if any(marker in normalized for marker in nickname_markers):
            return "preferred_nickname"
        pet_kind = MemoryService._extract_pet_name(memory_text)
        if pet_kind:
            return f"pet_name:{pet_kind[0]}"
        if any(
            marker in normalized
            for marker in ("lives in", "moved from", "moved to", "relocated to", "住在", "搬到", "搬去")
        ):
            return "current_location"
        if any(
            marker in normalized
            for marker in (
                "current work status",
                "job opportunities",
                "accepted an offer",
                "evaluating offers",
                "work status",
                "工作状态",
                "换工作",
                "offer",
            )
        ):
            return "current_work_status"
        if any(
            marker in normalized
            for marker in (
                "communication preference",
                "reassurance",
                "direct advice",
                "before advice",
                "instead of reassurance",
                "沟通偏好",
                "直接建议",
                "先安慰",
                "说教",
            )
        ):
            return "communication_preference"
        if any(marker in normalized for marker in ("birthday", "生日")):
            return "birthday"
        favorite = MemoryService._extract_favorite_consumable(memory_text)
        if favorite:
            return f"favorite:{favorite[0]}"
        if any(
            marker in normalized
            for marker in (
                "sleep reminder preference",
                "gentle reminders about sleep",
                "sleep reminders",
                "reminded to sleep",
                "reminders to sleep",
                "reminders to go to sleep",
                "gentle sleep reminders",
                "nighttime routine preference",
                "催睡觉",
                "提醒睡觉",
                "睡眠提醒",
            )
        ):
            return "sleep_reminder_preference"
        return None

    @classmethod
    def _memory_supported_by_source(cls, memory_text: str, l3_metadata: dict[str, Any]) -> bool:
        source_text = str(l3_metadata.get("source_text") or "")
        if not source_text:
            return True
        pet_name = cls._extract_pet_name(memory_text)
        if pet_name:
            pet_kind, name = pet_name
            kind_markers = {
                "cat": ("猫", "cat"),
                "dog": ("狗", "dog"),
                "bird": ("鸟", "鹦鹉", "bird", "parrot"),
                "rabbit": ("兔", "兔子", "rabbit", "bunny"),
            }.get(pet_kind, (pet_kind,))
            lowered_source = source_text.lower()
            source_pet_name = cls._extract_pet_name(source_text)
            if source_pet_name:
                return source_pet_name[0] == pet_kind
            return any(marker in lowered_source for marker in kind_markers) and name in source_text
        nickname = cls._extract_current_nickname(memory_text)
        if nickname:
            lowered_source = source_text.lower()
            nickname_markers = (
                "叫我",
                "称呼我",
                "叫用户",
                "称呼",
                "preferred name",
                "name should be",
                "name is",
                "called",
                "addressed as",
            )
            return nickname in source_text and any(marker in lowered_source for marker in nickname_markers)
        slot = cls._memory_conflict_slot(memory_text)
        if slot is None:
            return False
        if slot == "birthday":
            return cls._source_has_any(source_text, ("生日", "birthday"))
        source_slots = {
            cls._memory_conflict_slot(source_memory_text)
            for source_memory_text in cls._p0_canonical_memories_from_source(source_text)
        }
        source_slots.discard(None)
        if source_slots and slot not in source_slots:
            return False
        if slot == "current_location":
            return cls._source_has_any(
                source_text,
                ("住", "搬", "城市", "location", "live", "lived", "moving", "moved", "relocated"),
            )
        if slot == "current_work_status":
            return cls._source_has_any(source_text, ("工作", "offer", "job", "moonshot", "换工作"))
        if slot == "communication_preference":
            lowered_memory = memory_text.lower()
            if cls._source_has_any(
                memory_text,
                ("anxiety", "anxious", "break down", "breaking down", "焦虑", "拆解"),
            ):
                return cls._source_has_any(
                    source_text,
                    ("焦虑", "拆解", "anxiety", "anxious", "break down", "breaking down"),
                )
            if cls._source_has_any(
                memory_text,
                ("concise", "direct", "简洁", "直接"),
            ):
                return cls._source_has_any(
                    source_text,
                    ("简洁", "直接", "concise", "direct"),
                )
            if "reassurance" in lowered_memory or "comfort" in lowered_memory or "安慰" in memory_text:
                return cls._source_has_any(
                    source_text,
                    ("安慰", "reassurance", "comfort"),
                )
            return cls._source_has_any(
                source_text,
                (
                    "沟通",
                    "建议",
                    "安慰",
                    "说教",
                    "焦虑",
                    "communication",
                    "advice",
                    "suggestions",
                    "reassurance",
                    "anxious",
                ),
            )
        if slot == "favorite:drink":
            return cls._source_has_any(
                source_text,
                ("饮品", "饮料", "喝", "咖啡", "茶", "drink", "beverage", "coffee", "tea"),
            )
        if slot == "favorite:food":
            return cls._source_has_any(source_text, ("食物", "吃", "food"))
        if slot == "sleep_reminder_preference":
            return cls._source_has_any(source_text, ("睡", "提醒", "sleep", "reminder"))
        return True

    @staticmethod
    def _source_has_any(source_text: str, markers: tuple[str, ...]) -> bool:
        lowered_source = source_text.lower()
        return any(marker.lower() in lowered_source for marker in markers)

    @classmethod
    def _p0_canonical_memories_from_source(cls, source_text: str) -> list[str]:
        canonical: list[str] = []
        nickname = cls._extract_current_nickname(source_text)
        if nickname:
            canonical.append(f"User prefers to be called {nickname}")
        pet_name = cls._extract_pet_name(source_text)
        if pet_name:
            pet_kind, name = pet_name
            canonical.append(f"User has a {pet_kind} named {name}")
        current_location = cls._extract_current_location(source_text)
        if current_location:
            canonical.append(f"User lives in {current_location}")
        work_status = cls._extract_current_work_status(source_text)
        if work_status:
            canonical.append(f"User current work status: {work_status}")
        communication_preference = cls._extract_communication_preference(source_text)
        if communication_preference:
            canonical.append(f"User communication preference: {communication_preference}")
        birthday = cls._extract_birthday(source_text)
        if birthday:
            canonical.append(f"User birthday: {birthday}")
        favorite = cls._extract_favorite_consumable(source_text)
        if favorite:
            kind, value = favorite
            canonical.append(f"User favorite {kind}: {value}")
        sleep_reminder_preference = cls._extract_sleep_reminder_preference(source_text)
        if sleep_reminder_preference:
            canonical.append(f"User sleep reminder preference: {sleep_reminder_preference}")
        return canonical

    @classmethod
    def _canonical_memory_text(cls, memory_text: str) -> str:
        canonical_nickname = cls._extract_current_nickname(memory_text)
        if canonical_nickname:
            return f"User prefers to be called {canonical_nickname}"
        pet_name = cls._extract_pet_name(memory_text)
        if pet_name:
            pet_kind, name = pet_name
            return f"User has a {pet_kind} named {name}"
        current_location = cls._extract_current_location(memory_text)
        if current_location:
            return f"User lives in {current_location}"
        work_status = cls._extract_current_work_status(memory_text)
        if work_status:
            return f"User current work status: {work_status}"
        communication_preference = cls._extract_communication_preference(memory_text)
        if communication_preference:
            return f"User communication preference: {communication_preference}"
        birthday = cls._extract_birthday(memory_text)
        if birthday:
            return f"User birthday: {birthday}"
        favorite = cls._extract_favorite_consumable(memory_text)
        if favorite:
            kind, value = favorite
            return f"User favorite {kind}: {value}"
        sleep_reminder_preference = cls._extract_sleep_reminder_preference(memory_text)
        if sleep_reminder_preference:
            return f"User sleep reminder preference: {sleep_reminder_preference}"
        return memory_text

    @classmethod
    def _canonical_memory_text_from_source(
        cls, memory_text: str, l3_metadata: dict[str, Any]
    ) -> str:
        source_text = str(l3_metadata.get("source_text") or "")
        source_canonical = cls._p0_canonical_memory_for_source_slot(memory_text, source_text)
        if source_canonical is not None:
            return source_canonical
        source_pet_name = cls._extract_pet_name(source_text)
        memory_pet_name = cls._extract_pet_name(memory_text)
        if source_pet_name and memory_pet_name and source_pet_name[0] == memory_pet_name[0]:
            pet_kind, name = source_pet_name
            return f"User has a {pet_kind} named {name}"
        return cls._canonical_memory_text(memory_text)

    @classmethod
    def _p0_canonical_memory_for_source_slot(cls, memory_text: str, source_text: str) -> str | None:
        if not source_text:
            return None
        memory_slot = cls._memory_conflict_slot(memory_text)
        if memory_slot is None:
            return None
        for source_memory_text in cls._p0_canonical_memories_from_source(source_text):
            if cls._memory_conflict_slot(source_memory_text) == memory_slot:
                return source_memory_text
        return None

    @staticmethod
    def _extract_current_nickname(memory_text: str) -> str | None:
        patterns = (
            r"\bpreferred\s+name\s+from\s+.+?\s+to\s+'?([\u4e00-\u9fffA-Za-z0-9_-]{2,24})",
            r"\bpreferred\s+name\s+from\s+'?[\u4e00-\u9fffA-Za-z0-9_-]{2,24}'?\s+to\s+'?([\u4e00-\u9fffA-Za-z0-9_-]{2,24})'?",
            r"\bfrom\s+[\u4e00-\u9fffA-Za-z0-9_-]{2,24}\s+to\s+([\u4e00-\u9fffA-Za-z0-9_-]{2,24})",
            r"\bname\s+to\s+([\u4e00-\u9fffA-Za-z0-9_-]{2,24})",
            r"\bname\s+should\s+be\s+([\u4e00-\u9fffA-Za-z0-9_-]{2,24})",
            r"\bname\s+is\s+([\u4e00-\u9fffA-Za-z0-9_-]{2,24})",
            r"\bshould\s+be\s+called\s+'?([\u4e00-\u9fffA-Za-z0-9_-]{2,24})'?",
            r"\baddressed\s+as\s+'?([\u4e00-\u9fffA-Za-z0-9_-]{2,24})'?",
            r"(?:改成|改为|更正为|纠正为)\s*([\u4e00-\u9fffA-Za-z0-9_-]{2,24})",
            r"(?:叫我|称呼我)\s*([\u4e00-\u9fffA-Za-z0-9_-]{2,24})",
            r"(?:called|be called)\s+([\u4e00-\u9fffA-Za-z0-9_-]{2,24})",
        )
        normalized = memory_text.strip()
        if re.search(r"\b(?:cat|dog)(?:'s)?\s+name\s+is\b", normalized, flags=re.IGNORECASE):
            return None
        slot_markers = (
            "preferred name",
            "prefers to be called",
            "prefers to be addressed as",
            "name should be",
            "name is",
            "called",
            "addressed as",
            "叫我",
            "称呼",
        )
        if not any(marker in normalized.lower() for marker in slot_markers):
            return None
        for pattern in patterns:
            matches = re.findall(pattern, normalized, flags=re.IGNORECASE)
            if matches:
                value = str(matches[-1]).strip("。,.， ")
                value = re.split(r"\s*\(corrected\s+from\b", value, maxsplit=1, flags=re.IGNORECASE)[
                    0
                ].strip("。,.， ")
                return value
        return None

    @staticmethod
    def _extract_pet_name(memory_text: str) -> tuple[str, str] | None:
        normalized = memory_text.strip()
        lowered = normalized.lower()
        kind: str | None = None
        if "cat" in lowered or "猫" in normalized:
            kind = "cat"
        elif "dog" in lowered or "狗" in normalized:
            kind = "dog"
        elif "bird" in lowered or "parrot" in lowered or "鸟" in normalized or "鹦鹉" in normalized:
            kind = "bird"
        elif "rabbit" in lowered or "bunny" in lowered or "兔" in normalized:
            kind = "rabbit"
        if kind is None:
            return None

        patterns = (
            r"\bfrom\s+[\u4e00-\u9fffA-Za-z0-9_-]{1,24}\s+to\s+([\u4e00-\u9fffA-Za-z0-9_-]{1,24})",
            r"\b(?:cat|dog)\s+was\s+initially\s+named\s+[\u4e00-\u9fffA-Za-z0-9_-]{1,24}\s+but\s+corrected\s+to\s+([\u4e00-\u9fffA-Za-z0-9_-]{1,24})",
            r"\b(?:cat|dog|bird|parrot|rabbit|bunny)\s+character\s+named\s+([\u4e00-\u9fffA-Za-z0-9_-]{1,24})",
            r"\b(?:cat|dog|bird|parrot|rabbit|bunny)(?:'s)?\s+name\s+is\s+([\u4e00-\u9fffA-Za-z0-9_-]{1,24})",
            r"\b(?:cat|dog|bird|parrot|rabbit|bunny)\s+(?:is\s+)?named\s+([\u4e00-\u9fffA-Za-z0-9_-]{1,24})",
            r"(?:猫|狗|鸟|鹦鹉|兔|兔子)不叫[\u4e00-\u9fffA-Za-z0-9_-]{1,24}[，,]?\s*(?:现在)?(?:叫|名叫|名字叫)\s*([\u4e00-\u9fffA-Za-z0-9_-]{1,24})",
            r"(?:猫|狗|鸟|鹦鹉|兔|兔子)(?:的)?(?:名字)?(?:叫|名叫)\s*([\u4e00-\u9fffA-Za-z0-9_-]{1,24})",
            r"(?:养了|有).*?(?:猫|狗|鸟|鹦鹉|兔|兔子).*?(?:叫|名叫|名字叫)\s*([\u4e00-\u9fffA-Za-z0-9_-]{1,24})",
        )
        for pattern in patterns:
            matches = re.findall(pattern, normalized, flags=re.IGNORECASE)
            if matches:
                return kind, str(matches[-1]).strip("。,.， ")
        return None

    @staticmethod
    def _extract_current_location(memory_text: str) -> str | None:
        normalized = memory_text.strip()
        lowered = normalized.lower()
        if not any(marker in lowered for marker in ("live", "resided", "moved", "relocated", "住", "搬")):
            return None
        patterns = (
            r"\bfrom\s+[\u4e00-\u9fffA-Za-z0-9_-]{2,40}\s+to\s+([\u4e00-\u9fffA-Za-z0-9_-]{2,40})",
            r"\bmoved\s+to\s+([\u4e00-\u9fffA-Za-z0-9_-]{2,40})",
            r"\bbefore\s+moving\s+to\s+([\u4e00-\u9fffA-Za-z0-9_-]{2,40})",
            r"\bbefore\s+relocating\s+to\s+([\u4e00-\u9fffA-Za-z0-9_-]{2,40})",
            r"\bpreviously\s+resided\s+in\s+[\u4e00-\u9fffA-Za-z0-9_-]{2,40}\s+before\s+relocating\s+to\s+([\u4e00-\u9fffA-Za-z0-9_-]{2,40})",
            r"\brelocated\s+to\s+([\u4e00-\u9fffA-Za-z0-9_-]{2,40})",
            r"\blives?\s+in\s+([\u4e00-\u9fffA-Za-z0-9_-]{2,40})",
            r"(?:搬到|搬去|住在|现在住在)\s*([\u4e00-\u9fffA-Za-z0-9_-]{2,40})",
        )
        for pattern in patterns:
            matches = re.findall(pattern, normalized, flags=re.IGNORECASE)
            if matches:
                value = str(matches[-1]).strip("。,.， ")
                value = re.split(r"\s*\(corrected\s+from\b", value, maxsplit=1, flags=re.IGNORECASE)[
                    0
                ].strip("。,.， ")
                return value
        return None

    @staticmethod
    def _extract_current_work_status(memory_text: str) -> str | None:
        normalized = memory_text.strip()
        lowered = normalized.lower()
        markers = (
            "work status",
            "job opportunities",
            "evaluating offers",
            "accepted an offer",
            "current work",
            "stopped evaluating",
            "换工作",
            "工作状态",
            "offer",
        )
        if not any(marker in lowered for marker in markers):
            return None
        patterns = (
            r"\b(user\s+)?accepted\s+(.+?offer.+?)(?:\s+and\s+is\s+no\s+longer\b|[.;。]|$)",
            r"(?:work status|job status|current work status).*?\bfrom\b.+?\bto\b\s+(.+?)(?:[.;。]|$)",
            r"\bcurrent\s+work\s+status:\s*(.+?)(?:[.;。]|$)",
            r"\bstopped\s+evaluating\s+.+?\s+after\s+accepting\s+(.+?offer)(?:,|[.;。]|$)",
            r"\baccepted\s+(.+?offer.+?)(?:[.;。]|$)",
            r"\bis\s+(.+?job opportunities)(?:[.;。]|$)",
            r"(?:工作状态|现在)\s*(?:是|为)?\s*([^。,.，]+)",
        )
        for pattern in patterns:
            matches = re.findall(pattern, normalized, flags=re.IGNORECASE)
            if matches:
                match = matches[-1]
                if isinstance(match, tuple):
                    match = next((part for part in reversed(match) if part), "")
                value = str(match).strip("。,.， ")
                value = re.split(
                    r"\s+(?:and\s+)?(?:is\s+)?no\s+longer\b|\s+and\s+stopped\s+evaluating\b|，?不再|，?不需要",
                    value,
                    maxsplit=1,
                    flags=re.IGNORECASE,
                )[0].strip("。,.， ")
                if (
                    value.startswith("a job offer")
                    or value.startswith("an offer")
                    or ("offer" in value.lower() and not value.lower().startswith("accepted"))
                ):
                    value = f"accepted {value}"
                return value
        return None

    @staticmethod
    def _extract_communication_preference(memory_text: str) -> str | None:
        normalized = memory_text.strip()
        lowered = normalized.lower()
        markers = (
            "communication preference",
            "reassurance",
            "direct advice",
            "advice",
            "suggestions",
            "anxious",
            "沟通偏好",
            "建议",
            "安慰",
            "说教",
        )
        if not any(marker in lowered for marker in markers):
            return None
        patterns = (
            r"\bupdated\s+communication\s+preference\s+to\s+want\s+(.+?)(?:\s+without\b|\s+instead\s+of\b|[.;。]|$)",
            r"\bupdated\s+their\s+communication\s+preference\s+to\s+want\s+(.+?)(?:\s+without\b|\s+instead\s+of\b|[.;。]|$)",
            r"\bupdated\s+communication\s+preference\s+.+?\bto\s+prefer\s+(.+?)(?:\s+without\b|\s+instead\s+of\b|[.;。]|$)",
            r"\bcommunication\s+preference\s+updated\s+to\s+want\s+(.+?)(?:\s+without\b|\s+instead\s+of\b|[.;。]|$)",
            r"\bcommunication\s+preference\s+updated\s+to\s+prefer\s+(.+?)(?:\s+without\b|\s+instead\s+of\b|[.;。]|$)",
            r"\bcommunication\s+preference\s+updated:\s*wants\s+(.+?)(?:\s+without\b|\s+instead\s+of\b|[.;。]|$)",
            r"\bcommunication\s+preference\s+is\s+for\s+(.+?)(?:\s+without\b|\s+instead\s+of\b|[.;。]|$)",
            r"\bcommunication\s+preference\s+is\s+to\s+receive\s+(.+?)(?:\s+without\b|\s+instead\s+of\b|[.;。]|$)",
            r"\bcommunication\s+preference\s+has\s+been\s+updated\s+to\s+want\s+(.+?)(?:\s+without\b|\s+instead\s+of\b|[.;。]|$)",
            r"\b(?:user's\s+)?communication\s+preference\s+has\s+been\s+updated\s+to\s+prefer\s+(.+?)(?:\s+without\b|\s+instead\s+of\b|[.;。]|$)",
            r"\bcommunication\s+preference\s+has\s+been\s+updated:\s+they\s+now\s+want\s+(.+?)(?:\s+without\b|\s+instead\s+of\b|[.;。]|$)",
            r"\bprefers\s+(that\s+when\s+.+?)(?:\s+before\s+giving\b|[.;。]|$)",
            r"\bprefers\s+(.+?advice)(?:\s+without\b|\s+instead\s+of\b|[.;。]|$)",
            r"\bprefers\s+(.+?suggestions)(?:\s+without\b|\s+instead\s+of\b|[.;。]|$)",
            r"\bnow\s+wants\s+(.+?)(?:\s+without\b|\s+instead\s+of\b|[.;。]|$)",
            r"\bnow\s+wants\s+(.+?)\s+instead\s+of\b",
            r"\bprefers\s+(.+?)\s+before\b",
            r"\bcommunication preference:\s*(.+?)(?:[.;。]|$)",
            r"(?:沟通偏好).*?(?:现在)?(?:更)?(?:想要|希望|要|偏好|喜欢)\s*([^。,.，]+?)(?:，?不需要|，?不要|，?不想|[。,.，]|$)",
            r"(?:沟通偏好|希望你|想要你)\s*([^。,.，]+)",
        )
        for pattern in patterns:
            matches = re.findall(pattern, normalized, flags=re.IGNORECASE)
            if matches:
                value = str(matches[-1]).strip("。,.， ")
                value = re.split(
                    r"\s+without\b|\s+instead\s+of\b|，?不需要|，?不要",
                    value,
                    maxsplit=1,
                    flags=re.IGNORECASE,
                )[0].strip("。,.， ")
                return value
        return None

    @staticmethod
    def _extract_birthday(memory_text: str) -> str | None:
        normalized = memory_text.strip()
        lowered = normalized.lower()
        if "birthday" not in lowered and "生日" not in normalized:
            return None
        patterns = (
            r"\bfrom\s+.+?\s+to\s+(.+?)(?:[.;。]|$)",
            r"\bcorrected\s+(?:their\s+)?birthday\s+to\s+(.+?)(?:\s*\(|,|\s+not\b|[.;。]|$)",
            r"\bbirthday\s+falls\s+on\s+(.+?)(?:,|\s+correcting\b|[.;。]|$)",
            r"\bbirthday\s+has\s+been\s+updated\s+to\s+(.+?)(?:,|\s+not\b|[.;。]|$)",
            r"\bbirthday\s*(?:is|:)\s*(.+?)(?:[.;。]|$)",
            r"(?:生日|出生日期).*?不是[^。,.，]+[，,]?\s*(?:是|改成|改为|更正为|纠正为)\s*([^。,.，]+)",
            r"(?:生日|出生日期).*?(?:改成|改为|更正为|纠正为|(?<!不)是)\s*([^。,.，]+)",
            r"(?:生日|出生日期)(?:是|为|:|：)?\s*([^。,.，]+)",
        )
        for pattern in patterns:
            matches = re.findall(pattern, normalized, flags=re.IGNORECASE)
            if matches:
                value = str(matches[-1]).strip("。,.， ")
                value = re.split(
                    r"\s*\(corrected\s+from\b|\s*\(not\b|\s*\(previously\s+thought\s+to\s+be\b|\s*\(previously\s+stated\s+as\b|,\s*previously\b|,\s*not\b|,\s*correcting\s+a\s+previous\b|,\s*corrected\s+from\b",
                    value,
                    maxsplit=1,
                    flags=re.IGNORECASE,
                )[0].strip("。,.， ")
                return value
        return None

    @staticmethod
    def _extract_favorite_consumable(memory_text: str) -> tuple[str, str] | None:
        normalized = memory_text.strip()
        lowered = normalized.lower()
        kind: str | None = None
        if any(
            marker in lowered
            for marker in (
                "favorite drink",
                "preferred drink",
                "drink preference",
                "beverage preference",
                "preferred beverage",
                "beverage preference changed",
                "switched from drinking",
                "changed drink preference",
                "changed their drink preference",
                "drink preference changed",
                "drink preference updated",
                "switched drink preference",
                "switched their drink preference",
                "preferring",
                "no longer drinks",
                "no longer drinking",
                "favorite drink",
            )
        ) or any(marker in normalized for marker in ("喜欢喝", "饮品", "饮料")):
            kind = "drink"
        elif any(
            marker in lowered
            for marker in (
                "favorite food",
                "preferred food",
                "food preference",
                "changed food preference",
                "food preference changed",
                "food preference changed",
                "switched food preference",
                "switched their food preference",
                "prefers",
                "no longer eats",
                "no longer eating",
                "favorite food",
            )
        ) or any(
            marker in normalized for marker in ("喜欢吃", "食物")
        ):
            kind = "food"
        if kind is None:
            return None

        patterns: tuple[str, ...]
        if kind == "drink":
            patterns = (
                r"\bnow\s+prefers\s+(.+?)\s+instead\s+of\b.+?\bfavorite\s+drink\b",
                r"\bbeverage\s+preference\s+changed\s+from\s+.+?\s+to\s+(.+?)(?:\s+and\s+no\s+longer\b|[.;。]|$)",
                r"\bbeverage\s+preference\s+from\s+.+?\s+to\s+(.+?)(?:\s+and\s+no\s+longer\b|[.;。]|$)",
                r"\bchanged\s+drink\s+preference\s+from\s+.+?\s+to\s+(.+?)(?:[.;。]|$)",
                r"\bchanged\s+their\s+drink\s+preference\s+from\s+.+?\s+to\s+(.+?)(?:[.;。]|$)",
                r"\bdrink\s+preference\s+changed\s+to\s+(.+?)(?:[.;。]|$)",
                r"\bdrink\s+preference\s+updated\s+to\s+(.+?)(?:\s+only\b|,|\s+and\s+no\s+longer\b|[.;。]|$)",
                r"\bswitched\s+drink\s+preference\s+from\s+.+?\s+to\s+(.+?)(?:,|\s+and\s+no\s+longer\b|[.;。]|$)",
                r"\bswitched\s+their\s+drink\s+preference\s+from\s+.+?\s+to\s+(.+?)(?:,|\s+and\s+no\s+longer\b|[.;。]|$)",
                r"\bprefers\s+(.+?)\s+as\s+their\s+favorite\s+drink\b",
                r"\bswitched\s+from\s+drinking\s+.+?\s+to\s+(.+?)\s+as\s+their\s+preferred\s+beverage\b",
                r"\bswitched\s+from\s+drinking\s+.+?\s+to\s+preferring\s+(.+?)(?:[.;。]|$)",
                r"\bswitched\s+from\s+regularly\s+drinking\s+.+?\s+to\s+preferring\s+(.+?)\s+as\s+their\s+daily\s+beverage\b",
                r"\bswitched\s+from\s+.+?\s+to\s+(.+?)\s+as\s+their\s+preferred\s+beverage\b",
                r"\bswitched\s+from\s+.+?\s+to\s+(.+?)\s+as\s+their\s+preferred\s+drink\b",
                r"\bprefers\s+(.+?)\s+over\s+.+?\s+and\s+no\s+longer\s+drinks?\b",
                r"\bno\s+longer\s+drinks?\s+.+?,\s+only\s+(.+?)(?:[.;。]|$)",
                r"\bno\s+longer\s+drinking\s+.+?,\s+only\s+(.+?)(?:[.;。]|$)",
                r"\bfavorite\s+drink\s*(?:is|:)\s*(.+?)(?:[.;。]|$)",
                r"\bdrink\s+preference\s*(?:is|:|to)\s*(.+?)(?:[.;。]|$)",
                r"\bbeverage\s+preference\s*(?:is|:|to)\s*(.+?)(?:[.;。]|$)",
                r"(?:饮品|饮料).*?(?:偏好)?(?:改成|改为|更正为|纠正为|是|为|:|：)\s*([^。,.，]+)",
                r"(?:最喜欢喝|喜欢喝|饮品|饮料)(?:是|为|:|：)?\s*([^。,.，]+)",
            )
        else:
            patterns = (
                r"\bnow\s+prefers\s+(.+?)\s+instead\s+of\b.+?\bfavorite\s+food\b",
                r"\bprefers\s+(.+?)\s+over\b.+?\bfood\b",
                r"\bprefers\s+(.+?)\s+instead\s+of\b.+?\bfood\b",
                r"\bfood\s+preference\s+changed\s+from\s+.+?\s+to\s+(.+?)(?:[.;。]|$)",
                r"\bswitched\s+from\s+eating\s+.+?\s+to\s+(.+?)\s+as\s+their\s+preferred\s+food\b",
                r"\bswitched\s+from\s+eating\s+.+?\s+to\s+(.+?)\s+as\s+their\s+favorite\s+food\b",
                r"\bchanged\s+food\s+preference\s+from\s+.+?\s+to\s+(.+?)(?:[.;。]|$)",
                r"\bswitched\s+food\s+preference\s+from\s+.+?\s+to\s+(.+?)(?:[.;。]|$)",
                r"\bswitched\s+their\s+food\s+preference\s+from\s+.+?\s+to\s+(.+?)(?:[.;。]|$)",
                r"\bfood\s+preference\s+changed\s+to\s+(.+?)(?:[.;。]|$)",
                r"\bprefers\s+(.+?)\s+over\s+.+?\s+and\s+no\s+longer\s+eats?\b",
                r"\bno\s+longer\s+eats?\s+.+?,\s+only\s+(.+?)(?:[.;。]|$)",
                r"\bno\s+longer\s+eating\s+.+?,\s+only\s+(.+?)(?:[.;。]|$)",
                r"\bfavorite\s+food\s*(?:is|:)\s*(.+?)(?:[.;。]|$)",
                r"\bfood\s+preference\s*(?:is|:|to)\s*(.+?)(?:[.;。]|$)",
                r"(?:食物).*?(?:偏好)?(?:改成|改为|更正为|纠正为|是|为|:|：)\s*([^。,.，]+)",
                r"(?:最喜欢吃|喜欢吃|食物)(?:是|为|:|：)?\s*([^。,.，]+)",
            )
        for pattern in patterns:
            matches = re.findall(pattern, normalized, flags=re.IGNORECASE)
            if matches:
                value = str(matches[-1]).strip("。,.， ")
                value = re.split(
                    r",?\s+no\s+longer\s+drinking\b|,?\s+no\s+longer\s+drinks\b|,?\s+no\s+longer\s+eating\b|,?\s+no\s+longer\s+eats\b|\s+and\s+no\s+longer\s+drinks\b|\s+and\s+no\s+longer\s+eats\b|\s+and\s+stopped\s+drinking\b|\s+and\s+stopped\s+eating\b|，?不再",
                    value,
                    maxsplit=1,
                    flags=re.IGNORECASE,
                )[0].strip("。,.， ")
                return kind, value
        return None

    @staticmethod
    def _extract_sleep_reminder_preference(memory_text: str) -> str | None:
        normalized = memory_text.strip()
        lowered = normalized.lower()
        markers = (
            "sleep reminder preference",
            "gentle reminders about sleep",
            "sleep reminders",
            "reminded to sleep",
            "reminders to sleep",
            "reminders to go to sleep",
            "gentle sleep reminders",
            "going to sleep",
            "nighttime routine preference",
            "催睡觉",
            "提醒睡觉",
            "睡眠提醒",
        )
        if not any(marker in lowered for marker in markers):
            return None
        patterns = (
            r"\bnow\s+(?:is\s+)?((?:okay|ok|fine)\s+with\s+.+?sleep reminders?)(?:\s+instead\b|[.;。]|$)",
            r"\bnow\s+(accepts\s+gentle\s+reminders\s+to\s+go\s+to\s+sleep)(?:,|\s+representing\b|[.;。]|$)",
            r"\bnow\s+(accepts\s+gentle\s+reminders\s+to\s+sleep)(?:,|\s+representing\b|[.;。]|$)",
            r"\bnow\s+(accepts\s+gentle\s+reminders\s+about\s+going\s+to\s+sleep)(?:,|\s+representing\b|[.;。]|$)",
            r"\b(accepts\s+gentle\s+reminders\s+about\s+sleep)(?:\s+and\b|,|[.;。]|$)",
            r"\bnighttime\s+routine\s+preference\s+to\s+(now\s+accept\s+gentle\s+sleep\s+reminders)(?:,|[.;。]|$)",
            r"\b(can\s+now\s+accept\s+gentle\s+reminders\s+to\s+go\s+to\s+sleep)(?:,|\s+updating\b|[.;。]|$)",
            r"\b(can\s+now\s+accept\s+gentle\s+reminders\s+about\s+going\s+to\s+sleep)(?:[.;。]|$)",
            r"\bsleep reminder preference:\s*(.+?)(?:[.;。]|$)",
            r"\bdislikes\s+(.+?)(?:[.;。]|$)",
            r"(?:可以|接受|愿意).*?(温和.*?(?:提醒睡觉|睡眠提醒))",
            r"(?:不喜欢|讨厌|不要|不需要).*?(催睡觉|提醒睡觉|睡眠提醒)",
        )
        for pattern in patterns:
            matches = re.findall(pattern, normalized, flags=re.IGNORECASE)
            if matches:
                value = str(matches[-1]).strip("。,.， ")
                if value in {"催睡觉", "提醒睡觉", "睡眠提醒"}:
                    return "dislikes sleep reminders"
                if "温和" in value:
                    return "okay with gentle sleep reminders"
                return value
        return None

    def _l3_metadata(
        self, source_refs: list[dict[str, str]], request_metadata: dict[str, Any]
    ) -> dict[str, Any]:
        metadata = {
            "source_refs": source_refs,
            "source_type": self._enum_value(
                SourceType,
                request_metadata.get("source_type"),
                SourceType.CHAT_ROUND.value,
            ),
            "fact_subject": self._enum_value(
                FactSubject,
                request_metadata.get("fact_subject"),
                FactSubject.USER.value,
            ),
            "context_type": self._enum_value(
                ContextType,
                request_metadata.get("context_type"),
                ContextType.REAL_USER.value,
            ),
            "roleplay_mode": self._enum_value(
                RoleplayMode,
                request_metadata.get("roleplay_mode"),
                RoleplayMode.OFF.value,
            ),
            "data_classification": self._enum_value(
                DataClassification,
                request_metadata.get("data_classification"),
                self._classify_data(request_metadata),
            ),
            "memory_type": self._enum_value(
                MemoryType,
                request_metadata.get("memory_type"),
                MemoryType.PROFILE.value,
            ),
        }
        backend_categories = self._list_metadata_value(request_metadata.get("backend_categories"))
        if backend_categories:
            metadata["backend_categories"] = backend_categories
        if request_metadata.get("source_text"):
            metadata["source_text"] = str(request_metadata["source_text"])
        return metadata

    @staticmethod
    def _enum_value(enum_type: type[StrEnum], value: object, default: str) -> str:
        if isinstance(value, str):
            valid_values = {str(member.value) for member in enum_type}
            if value in valid_values:
                return value
        return default

    @staticmethod
    def _list_metadata_value(value: object) -> list[str]:
        if isinstance(value, list):
            return [str(item) for item in value]
        if isinstance(value, str) and value:
            return [value]
        return []

    @staticmethod
    def _classify_data(request_metadata: dict[str, Any]) -> str:
        if request_metadata.get("data_classification"):
            return str(request_metadata["data_classification"])
        return str(DataClassification.PERSONAL.value)

    def _is_restricted_or_unsafe(self, messages: list[Any]) -> bool:
        return self._messages_are_restricted_or_unsafe(
            [
                {
                    "role": getattr(message.role, "value", str(message.role)),
                    "content": message.content,
                }
                for message in messages
            ]
        )

    def _messages_are_restricted_or_unsafe(self, messages: list[dict[str, str]]) -> bool:
        return any(
            message.get("role") in {"system", "tool"}
            or self._text_is_restricted_or_unsafe(message.get("content", ""))
            for message in messages
        )

    def _upsert_default_summary(self, user_id: str, character_id: str) -> None:
        deleted_refs = self.repository.deleted_source_refs(user_id, character_id)
        rounds = [
            entry
            for entry in self.repository.list_rounds(user_id, character_id)
            if (entry.session_id, entry.round_id) not in deleted_refs
            and self._round_allowed_in_default_summary(entry)
        ]
        self.repository.upsert_summary_from_rounds(user_id, character_id, rounds)
        self._invalidate_summary_cache(user_id, character_id)

    @staticmethod
    def _round_allowed_in_default_summary(entry: JournalEntry) -> bool:
        user_text = " ".join(
            str(message.get("content", ""))
            for message in entry.messages
            if message.get("role") == "user"
        )
        lowered = user_text.lower()
        roleplay_markers = ("剧情", "角色扮演", "设定", "story", "roleplay", "fiction")
        return not any(marker in lowered for marker in roleplay_markers)

    @staticmethod
    def _text_is_restricted_or_unsafe(content: str) -> bool:
        normalized = content.lower()
        restricted_patterns = [
            r"\bapi[_-]?key\b",
            r"\bsecret\b",
            r"\bpassword\b",
            r"\btoken\b",
            r"\bsk-[a-z0-9_-]{6,}",
            r"ignore previous instructions",
            r"忽略.*指令",
            r"系统提示词",
            r"system:",
            r"tool:",
        ]
        return any(re.search(pattern, normalized, re.IGNORECASE) for pattern in restricted_patterns)

    @staticmethod
    def _redact_event(event: dict[str, Any]) -> dict[str, Any]:
        redacted = {key: value for key, value in event.items() if key not in {"memory", "content"}}
        if "id" in redacted:
            redacted["backend_memory_id"] = redacted.pop("id")
        return redacted

    def _assert_round_matches_request(self, existing: JournalEntry, request: AppendMemoryRequest) -> None:
        if (
            existing.user_id != request.user_id
            or existing.character_id != request.character_id
            or existing.session_id != request.session_id
            or existing.round_fingerprint != request_fingerprint(request)
        ):
            raise ValueError("round_id conflict: existing round scope or fingerprint does not match")

    def _source_refs_excluded(
        self,
        user_id: str,
        character_id: str,
        source_refs: list[dict[str, str]],
    ) -> bool:
        excluded_refs = self.repository.excluded_source_refs(user_id, character_id)
        return any(source_ref_key(source_ref) in excluded_refs for source_ref in source_refs)

    def _rebuild_rounds(self, request: RebuildMemoryRequest) -> list[JournalEntry]:
        excluded_refs = self.repository.deleted_source_refs(request.user_id, request.character_id)
        return [
            entry
            for entry in self._effective_history_rounds(
                request.user_id,
                request.character_id,
                request.session_id,
            )
            if (entry.session_id, entry.round_id) not in excluded_refs
        ]

    def _uncovered_rebuild_rounds(
        self,
        request: RebuildMemoryRequest,
        rounds: list[JournalEntry],
        deleted_refs: set[tuple[str | None, str | None]],
    ) -> list[JournalEntry]:
        processed_refs = self._processed_l3_source_refs(request, deleted_refs)
        return [
            entry
            for entry in rounds
            if (entry.session_id, entry.round_id) not in processed_refs
        ]

    def _processed_l3_source_refs(
        self,
        request: RebuildMemoryRequest,
        deleted_refs: set[tuple[str | None, str | None]],
    ) -> set[tuple[str | None, str | None]]:
        processed_refs: set[tuple[str | None, str | None]] = set()
        for memory in self.repository.list_memories(request.user_id, request.character_id):
            if memory.memory_status is MemoryStatus.DELETED:
                continue
            for source_ref in memory.source_refs:
                ref_key = source_ref_key(source_ref)
                if ref_key in deleted_refs:
                    continue
                if request.session_id is not None and source_ref.get("session_id") != request.session_id:
                    continue
                processed_refs.add(ref_key)
        return processed_refs

    def _effective_history_rounds(
        self, user_id: str, character_id: str, session_id: str | None
    ) -> list[JournalEntry]:
        if self.history_source:
            return list(self.history_source.list_rounds(user_id, character_id, session_id))
        return list(self.repository.list_rounds(user_id, character_id, session_id))

    def _l3_memories_in_rebuild_scope(self, request: RebuildMemoryRequest) -> list[Any]:
        active_memories = self.repository.active_memories(request.user_id, request.character_id)
        if request.session_id is None:
            return list(active_memories)
        return [
            memory
            for memory in active_memories
            if any(ref.get("session_id") == request.session_id for ref in memory.source_refs)
        ]


__all__ = [
    "InMemoryMemoryRepository",
    "MemoryService",
    "SqlAlchemyMemoryRepository",
]
