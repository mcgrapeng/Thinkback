"""记忆类型策略与降级召回的回归测试。

- ``conflict=append`` 的类型不得被同槽位新事实 supersede；
- 后端不可用时，复合查询要把每个请求槽位都从本地回填。
"""

from __future__ import annotations

from typing import Any

from thinkback.memory.backends import FakeMemoryBackend
from thinkback.memory.repositories import InMemoryMemoryRepository
from thinkback.memory.schemas import (
    AppendMemoryRequest,
    MessageRole,
    RecallMemoryRequest,
)
from thinkback.memory.service import MemoryService


def _append(round_id: str, content: str, *, memory_type: str | None = None) -> AppendMemoryRequest:
    metadata = {"memory_type": memory_type} if memory_type else {}
    return AppendMemoryRequest(
        request_id=f"req-{round_id}",
        user_id="user-1",
        session_id="session-1",
        round_id=round_id,
        messages=[
            {
                "message_id": f"{round_id}-u",
                "role": MessageRole.USER,
                "content": content,
                "timestamp": "2026-05-04T10:00:00Z",
            },
            {
                "message_id": f"{round_id}-a",
                "role": MessageRole.ASSISTANT,
                "content": "好的。",
                "timestamp": "2026-05-04T10:00:03Z",
            },
        ],
        source_timestamp="2026-05-04T10:00:03Z",
        metadata=metadata,
    )


def test_event_type_append_keeps_both_facts() -> None:
    """event 类型声明 conflict=append：同槽位的新事实不得把旧事实 supersede。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())

    service.append(_append("r-event-1", "我现在住在杭州。", memory_type="event"))
    service.append(_append("r-event-2", "我现在住在上海。", memory_type="event"))

    active = repository.active_memories("user-1", "thinkback")
    assert len(active) == 2, f"append 类型应保留两条事件，实际 {len(active)}"


def test_profile_type_overwrite_still_supersedes() -> None:
    """默认 profile 类型（overwrite）保持「同槽位新事实覆盖旧事实」。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())

    service.append(_append("r-profile-1", "我现在住在杭州。"))
    service.append(_append("r-profile-2", "我现在住在上海。"))

    active = repository.active_memories("user-1", "thinkback")
    assert len(active) == 1
    assert "上海" in active[0].memory_text


class _FailingSearchBackend(FakeMemoryBackend):
    def search(
        self,
        query: str,
        *,
        user_id: str,
        memory_scope_id: str,
        limit: int,
        threshold: float | None = None,
        explain: bool = False,
    ) -> list[dict[str, Any]]:
        raise RuntimeError("simulated backend outage")


def test_degraded_recall_backfills_all_requested_slots() -> None:
    """后端不可用时，复合查询要把每个槽位都从本地回填，不能只给第一个。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=_FailingSearchBackend())
    service.append(_append("r-loc", "我现在住在杭州。"))
    service.append(_append("r-food", "我喜欢吃寿司。"))

    response = service.recall(
        RecallMemoryRequest(
            user_id="user-1",
            session_id="session-1",
            query="根据我的 location 和 food preference 推荐晚餐",
        )
    )

    assert response.degraded is True
    l3_texts = [item.content for item in response.items if item.layer == "L3"]
    assert len(l3_texts) == 2, f"复合查询应回填两个槽位，实际 {l3_texts}"
