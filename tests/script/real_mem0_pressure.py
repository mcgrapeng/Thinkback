"""Run a real 5-round validation scenario against Mem0 Library.

This script intentionally does not support fake mode. It validates configuration
up front, then exercises append, recall, delete, rebuild, and recall again with
the real Mem0 Library adapter and shared Milvus.
"""

from __future__ import annotations

import asyncio
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import uuid4

from dotenv import load_dotenv

from thinkback.api.dependencies import get_memory_backend
from thinkback.infra.config import Settings
from thinkback.infra.database.engine import check_database
from thinkback.infra.readiness import check_milvus
from thinkback.memory.repositories import MemoryIndexEntry, SqlAlchemyMemoryRepository
from thinkback.memory.schemas import (
    AppendMemoryRequest,
    DeleteMemoryRequest,
    DeleteScope,
    MemoryStatus,
    MessageRole,
    RebuildMemoryRequest,
    RecallIntent,
    RecallMemoryRequest,
)
from thinkback.memory.service import MemoryService


class PreflightError(RuntimeError):
    """Raised when real validation cannot start because runtime prerequisites are missing."""


def _run_check(check: object) -> dict[str, str]:
    result = check()
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result


def build_preflight_report(settings: Settings) -> dict[str, object]:
    missing_config = []
    if not settings.memory_llm_key:
        missing_config.append("MEMORY_LLM_KEY")
    if not settings.milvus_url:
        missing_config.append("MILVUS_URL")

    dependencies = {
        "database": _run_check(check_database),
        "milvus": _run_check(check_milvus),
    }
    ready = not missing_config and all(
        dependency.get("status") == "ready" for dependency in dependencies.values()
    )
    return {
        "ready": ready,
        "missing_config": missing_config,
        "dependencies": dependencies,
    }


def assert_preflight_ready(settings: Settings) -> None:
    report = build_preflight_report(settings)
    if report["ready"]:
        return
    missing_config = ",".join(report["missing_config"]) or "-"
    dependencies = report["dependencies"]
    dependency_statuses = " ".join(
        _format_dependency_status(name, dependency) for name, dependency in dependencies.items()
    )
    raise PreflightError(
        f"real validation preflight failed: missing_config={missing_config} {dependency_statuses}"
    )


def _format_dependency_status(name: str, dependency: dict[str, str]) -> str:
    status = dependency.get("status", "unknown")
    detail = dependency.get("detail", "")
    if status == "ready" or not detail:
        return f"{name}={status}"
    return f"{name}={status}({detail})"


@dataclass(frozen=True)
class RunScope:
    run_id: str
    user_id: str
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
        session_id=f"real-pressure-session-{normalized_run_id}",
    )


def _append_round(service: MemoryService, scope: RunScope, index: int, text: str) -> None:
    now = datetime(2026, 5, 4, 10, index, tzinfo=UTC)
    response = service.append(
        AppendMemoryRequest(
            request_id=scope.request_id(index),
            user_id=scope.user_id,
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
    def active_memories(self, user_id: str, memory_scope_id: str) -> list[MemoryIndexEntry]: ...


def _assert_l3_generated(
    repository: _ActiveMemoryRepository, user_id: str, memory_scope_id: str
) -> None:
    active_count = len(repository.active_memories(user_id, memory_scope_id))
    if active_count < 1:
        raise RuntimeError(
            "real validation produced no active L3 memories; "
            "check Mem0 LLM/embedding provider availability, quota, and /memories response events"
        )


def _select_delete_target(
    repository: _ActiveMemoryRepository, user_id: str, memory_scope_id: str
) -> MemoryIndexEntry:
    for memory in repository.active_memories(user_id, memory_scope_id):
        if getattr(memory, "memory_status", MemoryStatus.ACTIVE) == MemoryStatus.ACTIVE:
            return memory
    raise RuntimeError(
        f"no active L3 memory found for real validation scope user_id={user_id}, "
        f"long_term_scope_id={memory_scope_id}"
    )


def main() -> None:
    load_dotenv()
    settings = Settings()
    assert_preflight_ready(settings)

    repository = SqlAlchemyMemoryRepository()
    service = MemoryService(
        repository=repository,
        backend=get_memory_backend(settings),
    )
    scope = _new_run_scope()
    print(
        "real validation scope: "
        f"run_id={scope.run_id}, user_id={scope.user_id}, "
        f"session_id={scope.session_id}"
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
            session_id=scope.session_id,
            query="用户关于睡觉和工作压力的长期偏好",
            intent=RecallIntent.CHAT,
            l3_limit=10,
        )
    )
    print(f"recall items after 5 rounds: {len(recall.items)}")
    if not recall.items:
        raise RuntimeError("real recall returned no memory items")
    _assert_l3_generated(repository, scope.user_id, MemoryService.LONG_TERM_SCOPE_ID)

    target = _select_delete_target(repository, scope.user_id, MemoryService.LONG_TERM_SCOPE_ID)
    delete_response = service.delete(
        DeleteMemoryRequest(
            request_id=f"{scope.run_id}-delete",
            user_id=scope.user_id,
            scope=DeleteScope.MEMORY,
            operation_id=scope.operation_id("delete"),
            memory_id=target.memory_id,
        )
    )
    print(f"delete target: affected={delete_response.affected_memories}")
    if delete_response.affected_memories < 1:
        raise RuntimeError("real delete did not affect any L3 memory")
    active_backend_ids = {
        memory.backend_memory_id
        for memory in repository.active_memories(scope.user_id, MemoryService.LONG_TERM_SCOPE_ID)
    }
    if target.backend_memory_id in active_backend_ids:
        raise RuntimeError("deleted L3 memory is still active in business index")
    deleted_recall = service.recall(
        RecallMemoryRequest(
            user_id=scope.user_id,
            session_id=scope.session_id,
            query=target.memory_text or "用户长期记忆",
            intent=RecallIntent.CHAT,
            l3_limit=10,
        )
    )
    if any(
        item.layer == "L3" and item.memory_id == target.backend_memory_id
        for item in deleted_recall.items
    ):
        raise RuntimeError("deleted L3 memory was returned by recall")

    rebuild = service.rebuild(
        RebuildMemoryRequest(
            request_id=f"{scope.run_id}-rebuild",
            user_id=scope.user_id,
            operation_id=scope.operation_id("rebuild"),
        )
    )
    print(f"rebuild: l2={rebuild.rebuilt_l2}, l3={rebuild.rebuilt_l3}")

    final_recall = service.recall(
        RecallMemoryRequest(
            user_id=scope.user_id,
            session_id=scope.session_id,
            query="用户换工作和称呼偏好",
            intent=RecallIntent.CHAT,
            l3_limit=10,
        )
    )
    print(f"final recall items: {len(final_recall.items)}")
    if not final_recall.items:
        raise RuntimeError("real final recall returned no memory items")


def run_cli() -> int:
    load_dotenv()
    try:
        main()
    except PreflightError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
