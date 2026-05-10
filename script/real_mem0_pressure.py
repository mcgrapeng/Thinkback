"""Run a real 5-round validation scenario against Mem0 Library.

This script intentionally does not support fake mode. It validates configuration
up front, then exercises append, recall, delete, rebuild, and recall again with
the real Mem0 Library adapter and shared Qdrant.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import uuid4

from dotenv import load_dotenv

from api.dependencies import get_memory_backend
from infra.config import Settings
from infra.database.engine import check_database
from memory.repositories import MemoryIndexEntry, SqlAlchemyMemoryRepository
from memory.schemas import (
    AppendMemoryRequest,
    DeleteMemoryRequest,
    DeleteScope,
    MemoryStatus,
    MessageRole,
    RebuildMemoryRequest,
    RecallIntent,
    RecallMemoryRequest,
)
from memory.service import MemoryService


def _require_real_config(settings: Settings) -> None:
    missing = []
    if not settings.openai_api_key:
        missing.append("OPENAI_API_KEY")
    if not settings.qdrant_url:
        missing.append("QDRANT_URL")
    if missing:
        raise RuntimeError(f"real validation requires: {', '.join(missing)}")


def _check_database() -> None:
    status = asyncio.run(check_database())
    if status["status"] != "ready":
        raise RuntimeError(f"database is not ready: {status['detail']}")


@dataclass(frozen=True)
class RunScope:
    run_id: str
    user_id: str
    character_id: str
    session_id: str

    def request_id(self, index: int) -> str:
        return f"{self.run_id}-req-{index}"

    def round_id(self, index: int) -> str:
        return f"{self.run_id}-round-{index}"

    def message_id(self, index: int, role: str) -> str:
        return f"{self.run_id}-{index}-{role}"

    def operation_id(self, operation: str) -> str:
        return f"{self.run_id}-{operation}-op"


def _new_run_scope(run_id: str | None = None) -> RunScope:
    raw_run_id = run_id or f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}"
    normalized_run_id = re.sub(r"[^a-zA-Z0-9_-]", "-", raw_run_id)
    return RunScope(
        run_id=normalized_run_id,
        user_id=f"real-pressure-user-{normalized_run_id}",
        character_id=f"real-pressure-character-{normalized_run_id}",
        session_id=f"real-pressure-session-{normalized_run_id}",
    )


def _append_round(service: MemoryService, scope: RunScope, index: int, text: str) -> None:
    now = datetime(2026, 5, 4, 10, index, tzinfo=UTC)
    response = service.append(
        AppendMemoryRequest(
            request_id=scope.request_id(index),
            user_id=scope.user_id,
            character_id=scope.character_id,
            session_id=scope.session_id,
            round_id=scope.round_id(index),
            round_index=index,
            messages=[
                {
                    "message_id": scope.message_id(index, "u"),
                    "role": MessageRole.USER,
                    "content": text,
                    "timestamp": now.isoformat(),
                },
                {
                    "message_id": scope.message_id(index, "a"),
                    "role": MessageRole.ASSISTANT,
                    "content": "我会把这个上下文作为记忆资料处理。",
                    "timestamp": (now + timedelta(seconds=3)).isoformat(),
                },
            ],
            source_timestamp=(now + timedelta(seconds=3)).isoformat(),
        )
    )
    print(f"append round {index}: {response.status}, l3_events={len(response.l3_events)}")


class _ActiveMemoryRepository(Protocol):
    def active_memories(self, user_id: str, character_id: str) -> list[MemoryIndexEntry]:
        ...


def _assert_l3_generated(repository: _ActiveMemoryRepository, user_id: str, character_id: str) -> None:
    active_count = len(repository.active_memories(user_id, character_id))
    if active_count < 1:
        raise RuntimeError(
            "real validation produced no active L3 memories; "
            "check Mem0 LLM/embedding provider availability, quota, and /memories response events"
        )


def _select_delete_target(
    repository: _ActiveMemoryRepository, user_id: str, character_id: str
) -> MemoryIndexEntry:
    for memory in repository.active_memories(user_id, character_id):
        if getattr(memory, "memory_status", MemoryStatus.ACTIVE) == MemoryStatus.ACTIVE:
            return memory
    raise RuntimeError(
        f"no active L3 memory found for real validation scope user_id={user_id}, "
        f"character_id={character_id}"
    )


def main() -> None:
    load_dotenv()
    settings = Settings()
    _require_real_config(settings)
    _check_database()

    repository = SqlAlchemyMemoryRepository()
    service = MemoryService(
        repository=repository,
        backend=get_memory_backend(settings),
    )
    scope = _new_run_scope()
    print(
        "real validation scope: "
        f"run_id={scope.run_id}, user_id={scope.user_id}, "
        f"character_id={scope.character_id}, session_id={scope.session_id}"
    )

    pressure_rounds = [
        "我不喜欢被催睡觉，这会让我更焦虑。",
        "我最近在评估换工作机会，但还没有决定。",
        "我喜欢别人叫我阿鹏，这样更亲切。",
        "如果我聊到工作压力，希望你先陪我梳理，不要直接说教。",
        "我周末可能要和朋友复盘面试情况。",
    ]
    for index, text in enumerate(pressure_rounds, start=1):
        _append_round(service, scope, index, text)

    recall = service.recall(
        RecallMemoryRequest(
            user_id=scope.user_id,
            character_id=scope.character_id,
            session_id=scope.session_id,
            query="用户关于睡觉和工作压力的长期偏好",
            intent=RecallIntent.PREFERENCE,
            l3_limit=10,
        )
    )
    print(f"recall items after 5 rounds: {len(recall.items)}")
    if not recall.items:
        raise RuntimeError("real recall returned no memory items")
    _assert_l3_generated(repository, scope.user_id, scope.character_id)

    target = _select_delete_target(repository, scope.user_id, scope.character_id)
    delete_response = service.delete(
        DeleteMemoryRequest(
            request_id=f"{scope.run_id}-delete",
            user_id=scope.user_id,
            character_id=scope.character_id,
            scope=DeleteScope.MEMORY,
            operation_id=scope.operation_id("delete"),
            memory_id=target.memory_id,
        )
    )
    print(f"delete target: affected={delete_response.affected_memories}")
    if delete_response.affected_memories < 1:
        raise RuntimeError("real delete did not affect any L3 memory")
    active_backend_ids = {
        memory.backend_memory_id for memory in repository.active_memories(scope.user_id, scope.character_id)
    }
    if target.backend_memory_id in active_backend_ids:
        raise RuntimeError("deleted L3 memory is still active in business index")
    deleted_recall = service.recall(
        RecallMemoryRequest(
            user_id=scope.user_id,
            character_id=scope.character_id,
            session_id=scope.session_id,
            query=target.memory_text or "用户长期记忆",
            intent=RecallIntent.MEMORY_QUERY,
            l3_limit=10,
        )
    )
    if any(item.layer == "L3" and item.memory_id == target.backend_memory_id for item in deleted_recall.items):
        raise RuntimeError("deleted L3 memory was returned by recall")

    rebuild = service.rebuild(
        RebuildMemoryRequest(
            request_id=f"{scope.run_id}-rebuild",
            user_id=scope.user_id,
            character_id=scope.character_id,
            operation_id=scope.operation_id("rebuild"),
        )
    )
    print(f"rebuild: l2={rebuild.rebuilt_l2}, l3={rebuild.rebuilt_l3}")

    final_recall = service.recall(
        RecallMemoryRequest(
            user_id=scope.user_id,
            character_id=scope.character_id,
            session_id=scope.session_id,
            query="用户换工作和称呼偏好",
            intent=RecallIntent.MEMORY_QUERY,
            l3_limit=10,
        )
    )
    print(f"final recall items: {len(final_recall.items)}")
    if not final_recall.items:
        raise RuntimeError("real final recall returned no memory items")


if __name__ == "__main__":
    load_dotenv()
    main()
