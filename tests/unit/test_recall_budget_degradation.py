"""预算装不下任何一条记忆时，召回必须说清楚是「预算不够」。

背景：召回有「每层至少留一条」的兜底（``domain/recall_policy.py``），
用全局余额给空层补一条。但余额本身也可能不够 —— 预算比最便宜的一条记忆
还小时，物理上装不下任何一条，召回只能返回空包。

那时候如果 ``degraded=false``，调用方拿到空包会理解成「用户没有相关记忆」，
于是要么不再调召回、要么以为记忆没生效 —— 两种反应都会让记忆白记。
"""

from __future__ import annotations

from typing import Any

from thinkback.memory.repositories import InMemoryMemoryRepository
from thinkback.memory.schemas import RecallMemoryRequest
from thinkback.memory.service import MemoryService


class _StubBackend:
    """只实现 recall 用到的 search（返回预置的 L3 命中）。"""

    def __init__(self, results: list[dict[str, Any]]) -> None:
        self.results = results

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
        _ = (query, user_id, memory_scope_id, limit, threshold)
        return list(self.results)

    def _call(self, operation: str, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"unexpected backend call: {operation}")


def _service_with_one_long_memory() -> tuple[MemoryService, InMemoryMemoryRepository]:
    repository = InMemoryMemoryRepository()
    backend = _StubBackend([{"id": "b-1", "score": 0.9}])
    service = MemoryService(repository=repository, backend=backend, l3_write_mode="sync")
    repository.add_memory_index(
        backend_memory_id="b-1",
        user_id="user-1",
        memory_scope_id="thinkback",
        source_refs=[],
        memory_text="用户的长期偏好与工作背景说明。" * 50,
    )
    return service, repository


def test_budget_smaller_than_one_memory_reports_degradation() -> None:
    """预算装不下任何一条 → 空包 + degraded 标记 + 可读的降级原因。"""
    service, _ = _service_with_one_long_memory()

    response = service.recall(
        RecallMemoryRequest(
            user_id="user-1",
            session_id="session-1",
            query="用户偏好是什么",
            # 契约允许的最小预算（schemas.py: ge=100），而这条记忆的成本远超它。
            token_budget=100,
            l3_limit=5,
            l3_score_threshold=0.0,
        )
    )

    assert response.items == [], "预算确实装不下任何一条"
    assert response.degraded is True, "空包必须标明是降级，不是「没有记忆」"
    assert "token_budget_too_small" in response.degradation_reasons


def test_no_memories_is_not_reported_as_degradation() -> None:
    """真的没有记忆时不该标降级 —— 那会把「没记过」和「装不下」混成一件事。"""
    repository = InMemoryMemoryRepository()
    backend = _StubBackend([])
    service = MemoryService(repository=repository, backend=backend, l3_write_mode="sync")

    response = service.recall(
        RecallMemoryRequest(
            user_id="user-1",
            session_id="session-1",
            query="用户偏好是什么",
            token_budget=1200,
            l3_limit=5,
            l3_score_threshold=0.0,
        )
    )

    assert response.items == []
    assert response.degraded is False
    assert "token_budget_too_small" not in response.degradation_reasons
