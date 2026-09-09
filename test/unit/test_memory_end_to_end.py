from innies_memory.memory.backends import FakeMemoryBackend
from innies_memory.memory.repositories import InMemoryMemoryRepository
from innies_memory.memory.schemas import (
    AppendMemoryRequest,
    DeleteMemoryRequest,
    DeleteScope,
    MessageRole,
    RebuildMemoryRequest,
    RecallIntent,
    RecallMemoryRequest,
)
from innies_memory.memory.service import MemoryService


def append_round(service: MemoryService, index: int, text: str) -> None:
    service.append(
        AppendMemoryRequest(
            request_id=f"req-{index}",
            user_id="user-e2e",
            session_id="session-e2e",
            round_id=f"round-{index}",
            round_index=index,
            messages=[
                {
                    "message_id": f"m{index}-u",
                    "role": MessageRole.USER,
                    "content": text,
                    "timestamp": f"2026-05-04T10:0{index}:00Z",
                },
                {
                    "message_id": f"m{index}-a",
                    "role": MessageRole.ASSISTANT,
                    "content": "我会按这个上下文继续回应。",
                    "timestamp": f"2026-05-04T10:0{index}:03Z",
                },
            ],
            source_timestamp=f"2026-05-04T10:0{index}:03Z",
        )
    )


def test_fake_backend_full_chain_append_recall_delete_rebuild_recall() -> None:
    service = MemoryService(repository=InMemoryMemoryRepository(), backend=FakeMemoryBackend())
    append_round(service, 1, "我不喜欢被催睡觉")
    append_round(service, 2, "我正在评估换工作机会")

    first_recall = service.recall(
        RecallMemoryRequest(
            user_id="user-e2e",
            session_id="session-e2e",
            query="换工作",
            intent=RecallIntent.CHAT,
        )
    )

    assert any("换工作" in item.content for item in first_recall.items)

    memory_id = next(
        memory.memory_id
        for memory in service.repository.memories.values()
        if "sleep reminder" in memory.memory_text
    )
    service.delete(
        DeleteMemoryRequest(
            request_id="delete-e2e",
            user_id="user-e2e",
            scope=DeleteScope.MEMORY,
            operation_id="delete-e2e-op",
            memory_id=memory_id,
        )
    )

    deleted_recall = service.recall(
        RecallMemoryRequest(
            user_id="user-e2e",
            session_id="session-e2e",
            query="睡觉",
            intent=RecallIntent.CHAT,
        )
    )

    assert all("睡觉" not in item.content for item in deleted_recall.items)

    service.rebuild(
        RebuildMemoryRequest(
            request_id="rebuild-e2e",
            user_id="user-e2e",
            operation_id="rebuild-e2e-op",
        )
    )
    final_recall = service.recall(
        RecallMemoryRequest(
            user_id="user-e2e",
            session_id="session-e2e",
            query="换工作",
            intent=RecallIntent.CHAT,
        )
    )

    assert any("换工作" in item.content for item in final_recall.items)
