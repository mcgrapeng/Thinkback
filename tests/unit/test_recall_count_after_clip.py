"""召回计数与预算裁剪的顺序（曝光偏差闭环）。

被去重或被预算裁掉的记忆**没有**暴露给模型，不应该被计入召回次数：
recall_count → strength → 排名提前 → 更容易召回，形成自我强化的闭环。
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
    ) -> list[dict[str, Any]]:
        _ = (query, user_id, memory_scope_id, limit, threshold)
        return list(self.results)

    def _call(self, operation: str, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"unexpected backend call: {operation}")


def _make_service() -> tuple[MemoryService, InMemoryMemoryRepository]:
    repository = InMemoryMemoryRepository()
    backend = _StubBackend(
        [
            {"id": "b-long", "score": 0.9},
            {"id": "b-short", "score": 0.8},
        ]
    )
    service = MemoryService(repository=repository, backend=backend, l3_write_mode="sync")
    return service, repository


def test_clipped_memory_is_not_counted_as_recalled() -> None:
    service, repository = _make_service()
    long_memory = repository.add_memory_index(
        backend_memory_id="b-long",
        user_id="user-1",
        memory_scope_id="thinkback",
        source_refs=[],
        memory_text="上海的工作机会很多" * 30,
    )
    short_memory = repository.add_memory_index(
        backend_memory_id="b-short",
        user_id="user-1",
        memory_scope_id="thinkback",
        source_refs=[],
        # 内容质量高于长记忆（排序键的第二位）→ 排在前面。
        # 预算裁剪取「排名前缀」：短记忆先进上下文，长记忆因超预算被裁掉。
        memory_text="2024 年 3 月我入职了上海的阿里巴巴云智能团队",
    )

    response = service.recall(
        RecallMemoryRequest(
            user_id="user-1",
            session_id="session-1",
            query="上海的天气怎么样",
            token_budget=100,
            l3_limit=5,
            l3_score_threshold=0.0,
        )
    )

    delivered = {item.memory_id for item in response.items}
    assert short_memory.memory_id in delivered
    assert long_memory.memory_id not in delivered

    # 只有真正进入上下文的记忆才计入召回次数
    assert repository.get_memory_index(short_memory.memory_id).recall_count == 1
    assert repository.get_memory_index(long_memory.memory_id).recall_count == 0
