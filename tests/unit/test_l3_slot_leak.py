"""L3 队列槽位不能泄漏。

回归缺陷：``append()`` 在 ``try`` 块之外预留 L3 队列槽位，而
``get_task`` / ``save_task`` / ``_check_retry_budget`` 也在 try 之外 ——
这些调用一旦抛异常，``release_write_slot()`` 永远不会执行，槽位只进不出。

生产配置 ``memory_l3_max_pending_tasks=256``：一次 30 秒的 DB 抖动窗口内
只要累计 256 次失败 append，该 Pod 的记忆写入能力就永久归零，只能重启恢复。
DB 抖动是最常见的生产故障，这个异常放大器非常致命。
"""

from __future__ import annotations

from typing import Any

import pytest

from thinkback.memory.backends import FakeMemoryBackend
from thinkback.memory.repositories import InMemoryMemoryRepository
from thinkback.memory.schemas import AppendMemoryRequest
from thinkback.memory.service import MemoryService


class _ExplodingRepository(InMemoryMemoryRepository):
    """在 get_task 处炸掉 —— 模拟 DB 抖动。"""

    def __init__(self) -> None:
        super().__init__()
        self.explode = True

    def get_task(self, task_id: str) -> Any:
        if self.explode:
            raise RuntimeError("database is down")
        return super().get_task(task_id)


def _request(round_id: str) -> AppendMemoryRequest:
    return AppendMemoryRequest(
        request_id=f"req-{round_id}",
        user_id="user-1",
        session_id="session-1",
        round_id=round_id,
        source_timestamp="2026-10-21T10:00:00Z",
        messages=[
            {
                "role": "user",
                "content": "测试记忆",
                "message_id": f"m-{round_id}",
                "timestamp": "2026-10-21T10:00:00Z",
            },
            {
                "role": "assistant",
                "content": "已记住",
                "message_id": f"ma-{round_id}",
                "timestamp": "2026-10-21T10:00:05Z",
            },
        ],
    )


def test_failed_appends_return_their_l3_slots() -> None:
    """连续失败的 append 必须把槽位全部归还。"""
    repository = _ExplodingRepository()
    service = MemoryService(
        repository=repository,
        backend=FakeMemoryBackend(),
        l3_write_mode="async",
        l3_max_pending_tasks=4,
    )

    for index in range(4):
        with pytest.raises(RuntimeError):
            service.append(_request(f"r-{index}"))

    assert service._l3.pending_write_count() == 0, "失败的 append 泄漏了 L3 队列槽位"


def test_l3_capacity_is_usable_again_after_failures() -> None:
    """DB 恢复后，写入能力必须回来（不是永久归零到重启为止）。"""
    repository = _ExplodingRepository()
    service = MemoryService(
        repository=repository,
        backend=FakeMemoryBackend(),
        l3_write_mode="async",
        l3_max_pending_tasks=4,
    )

    for index in range(4):
        with pytest.raises(RuntimeError):
            service.append(_request(f"r-{index}"))

    repository.explode = False
    response = service.append(_request("r-after-recovery"))
    assert response.status in {"completed", "already_done"}
    service.shutdown_l3_executor()
