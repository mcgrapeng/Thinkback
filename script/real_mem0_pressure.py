"""Run a real 5-round pressure scenario against Mem0/OpenAI/Qdrant.

This script intentionally does not support fake mode. It validates configuration
up front, then exercises append, recall, delete, rebuild, and recall again with
the real Mem0 adapter.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta

from dotenv import load_dotenv
from qdrant_client import QdrantClient

from infra.config import Settings
from infra.database.engine import check_database
from memory.backends import Mem0MemoryBackend
from memory.repositories import SqlAlchemyMemoryRepository
from memory.schemas import (
    AppendMemoryRequest,
    DeleteMemoryRequest,
    DeleteScope,
    MessageRole,
    RebuildMemoryRequest,
    RecallIntent,
    RecallMemoryRequest,
)
from memory.service import MemoryService


def _require_real_config(settings: Settings) -> None:
    missing = []
    if not settings.memory_llm_api_key:
        missing.append("MEMORY_LLM_API_KEY")
    if not settings.memory_embedding_api_key:
        missing.append("MEMORY_EMBEDDING_API_KEY")
    if not settings.qdrant_url:
        missing.append("QDRANT_URL")
    if missing:
        raise RuntimeError(f"real pressure test requires: {', '.join(missing)}")


def _check_qdrant(settings: Settings) -> None:
    client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key or None)
    client.get_collections()


def _check_database() -> None:
    status = asyncio.run(check_database())
    if status["status"] != "ready":
        raise RuntimeError(f"database is not ready: {status['detail']}")


def _append_round(service: MemoryService, index: int, text: str) -> None:
    now = datetime(2026, 5, 4, 10, index, tzinfo=UTC)
    response = service.append(
        AppendMemoryRequest(
            request_id=f"real-pressure-req-{index}",
            user_id="real-pressure-user",
            character_id="real-pressure-character",
            session_id="real-pressure-session",
            round_id=f"real-pressure-round-{index}",
            round_index=index,
            messages=[
                {
                    "message_id": f"real-pressure-{index}-u",
                    "role": MessageRole.USER,
                    "content": text,
                    "timestamp": now.isoformat(),
                },
                {
                    "message_id": f"real-pressure-{index}-a",
                    "role": MessageRole.ASSISTANT,
                    "content": "我会把这个上下文作为记忆资料处理。",
                    "timestamp": (now + timedelta(seconds=3)).isoformat(),
                },
            ],
            source_timestamp=(now + timedelta(seconds=3)).isoformat(),
        )
    )
    print(f"append round {index}: {response.status}, l3_events={len(response.l3_events)}")


def main() -> None:
    load_dotenv()
    settings = Settings()
    _require_real_config(settings)
    _check_qdrant(settings)
    _check_database()

    service = MemoryService(
        repository=SqlAlchemyMemoryRepository(),
        backend=Mem0MemoryBackend(),
    )

    pressure_rounds = [
        "我不喜欢被催睡觉，这会让我更焦虑。",
        "我最近在评估换工作机会，但还没有决定。",
        "我喜欢别人叫我阿鹏，这样更亲切。",
        "如果我聊到工作压力，希望你先陪我梳理，不要直接说教。",
        "我周末可能要和朋友复盘面试情况。",
    ]
    for index, text in enumerate(pressure_rounds, start=1):
        _append_round(service, index, text)

    recall = service.recall(
        RecallMemoryRequest(
            user_id="real-pressure-user",
            character_id="real-pressure-character",
            session_id="real-pressure-session",
            query="用户关于睡觉和工作压力的长期偏好",
            intent=RecallIntent.PREFERENCE,
            l3_limit=10,
        )
    )
    print(f"recall items after 5 rounds: {len(recall.items)}")
    if not recall.items:
        raise RuntimeError("real recall returned no memory items")

    target = next(
        (
            memory
            for memory in service.repository.memories.values()
            if "睡觉" in memory.memory_text or "焦虑" in memory.memory_text
        ),
        None,
    )
    if target:
        delete_response = service.delete(
            DeleteMemoryRequest(
                request_id="real-pressure-delete",
                user_id="real-pressure-user",
                character_id="real-pressure-character",
                scope=DeleteScope.MEMORY,
                operation_id="real-pressure-delete-op",
                memory_id=target.memory_id,
            )
        )
        print(f"delete target: affected={delete_response.affected_memories}")

    rebuild = service.rebuild(
        RebuildMemoryRequest(
            request_id="real-pressure-rebuild",
            user_id="real-pressure-user",
            character_id="real-pressure-character",
            operation_id="real-pressure-rebuild-op",
        )
    )
    print(f"rebuild: l2={rebuild.rebuilt_l2}, l3={rebuild.rebuilt_l3}")

    final_recall = service.recall(
        RecallMemoryRequest(
            user_id="real-pressure-user",
            character_id="real-pressure-character",
            session_id="real-pressure-session",
            query="用户换工作和称呼偏好",
            intent=RecallIntent.MEMORY_QUERY,
            l3_limit=10,
        )
    )
    print(f"final recall items: {len(final_recall.items)}")
    if not final_recall.items:
        raise RuntimeError("real final recall returned no memory items")


if __name__ == "__main__":
    # Keep OpenAI compatibility for libraries that look at OPENAI_API_KEY.
    load_dotenv()
    if os.getenv("MEMORY_LLM_API_KEY") and not os.getenv("OPENAI_API_KEY"):
        os.environ["OPENAI_API_KEY"] = os.getenv("MEMORY_LLM_API_KEY", "")
    main()
