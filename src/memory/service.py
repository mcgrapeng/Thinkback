"""P0 memory service workflows aligned with the three-layer architecture."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any, Protocol

from memory.backends import MemoryBackend
from memory.repositories import (
    InMemoryMemoryRepository,
    JournalEntry,
    MemoryRepository,
    SqlAlchemyMemoryRepository,
    TaskEntry,
    request_fingerprint,
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
    MemoryType,
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
    ) -> None:
        self.repository = repository
        self.backend = backend
        self.history_source = history_source

    def append(self, request: AppendMemoryRequest) -> AppendMemoryResponse:
        existing = self.repository.get_round(request.round_id)
        task_id = f"memory-extract:{request.round_id}"
        if existing:
            self._assert_round_matches_request(existing, request)
            existing_task = self.repository.get_task(task_id)
            if existing_task is None or existing_task.status is TaskStatus.COMPLETED:
                return AppendMemoryResponse(status="already_done", task_id=task_id, round_id=request.round_id)

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
            if self._is_restricted_or_unsafe(request.messages):
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
                self.repository.upsert_summary_from_journal(request.user_id, request.character_id)
                l3_events = self._add_l3_from_messages(
                    [{"role": message.role.value, "content": message.content} for message in request.messages],
                    user_id=request.user_id,
                    character_id=request.character_id,
                    source_refs=[{"session_id": request.session_id, "round_id": request.round_id}],
                    request_metadata=request.metadata,
                )
        except Exception as exc:
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

        summary = self.repository.get_summary(request.user_id, request.character_id)
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
            backend_items = self.backend.search(
                request.query,
                user_id=request.user_id,
                character_id=request.character_id,
                limit=request.l3_limit,
            )
            active_backend_ids = {
                memory.backend_memory_id
                for memory in self.repository.active_memories(request.user_id, request.character_id)
            }
            for item in backend_items:
                memory_id = str(item.get("id", ""))
                if memory_id not in active_backend_ids:
                    continue
                items.append(
                    MemoryItem(
                        layer="L3",
                        content=str(item.get("memory", "")),
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
                ]
                self.repository.upsert_summary_from_rounds(
                    request.user_id,
                    request.character_id,
                    l2_rounds,
                )
            if request.rebuild_l3:
                active_memories = self._l3_memories_in_rebuild_scope(request)
                self.backend.delete_many([memory.backend_memory_id for memory in active_memories])
                for memory in active_memories:
                    self.repository.mark_memory_superseded(memory.memory_id)
                for entry in rounds:
                    messages = [
                        {"role": message["role"], "content": message["content"]}
                        for message in entry.messages
                    ]
                    if self._messages_are_restricted_or_unsafe(messages):
                        continue
                    self._add_l3_from_messages(
                        messages,
                        user_id=request.user_id,
                        character_id=request.character_id,
                        source_refs=[{"session_id": entry.session_id, "round_id": entry.round_id}],
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

        self.backend.delete(memory.backend_memory_id)
        self.repository.mark_memory_deleted(memory.memory_id)
        for source_ref in memory.source_refs:
            self.repository.clear_l1(
                request.user_id,
                request.character_id,
                source_ref.get("session_id"),
            )
        self.repository.mark_summary(request.user_id, request.character_id, SummaryState.DIRTY)
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
        return affected

    def _delete_all_memories(self, request: DeleteMemoryRequest) -> list[str]:
        active_memories = self.repository.active_memories(request.user_id, request.character_id)
        self.backend.delete_many([memory.backend_memory_id for memory in active_memories])
        for memory in active_memories:
            self.repository.mark_memory_deleted(memory.memory_id)
        self.repository.clear_l1(request.user_id, request.character_id)
        self.repository.mark_rounds_deleted(request.user_id, request.character_id)
        self.repository.mark_summary(request.user_id, request.character_id, SummaryState.DIRTY)
        return [memory.memory_id for memory in active_memories]

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
        events = self.backend.add(
            messages,
            user_id=user_id,
            character_id=character_id,
            metadata=l3_metadata,
        )
        for event in events:
            backend_id = str(event.get("id", ""))
            memory_text = str(event.get("memory", ""))
            if backend_id and event.get("event") in {"ADD", "UPDATE"}:
                event_metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
                backend_categories = self._list_metadata_value(
                    event_metadata.get("categories")
                    or event_metadata.get("backend_categories")
                    or request_metadata.get("backend_categories")
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
                    metadata=dict(l3_metadata),
                )
        return [dict(event) for event in events]

    def _l3_metadata(
        self, source_refs: list[dict[str, str]], request_metadata: dict[str, Any]
    ) -> dict[str, Any]:
        return {
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

    def _rebuild_rounds(self, request: RebuildMemoryRequest) -> list[JournalEntry]:
        deleted_refs = self.repository.deleted_source_refs(request.user_id, request.character_id)
        return [
            entry
            for entry in self._effective_history_rounds(
                request.user_id,
                request.character_id,
                request.session_id,
            )
            if (entry.session_id, entry.round_id) not in deleted_refs
        ]

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
