"""append 的写入归属：谁有权执行这次写入，撞车时怎么收场。

两条真实竞态，代码里都写了处理分支，但历史覆盖里从未被执行过：

1. **DEAD_LETTER 短路**：重试预算耗尽后任务进终态 DEAD_LETTER。再打同一个
   round_id 时必须原地短路返回「失败」，不得把任务复活回 RUNNING。生产事故
   记录：连打 19 次，``retry_count`` 涨到 11（MAX_RETRIES=5）—— 预算检查被
   绕过去，等于无限重试。

2. **认领撞车**：append 先查任务、再认领任务。两次访问之间别的写入者抢先
   认领了同一个 task_id（多副本 / 读延迟都会造出这个窗口）。撞车的一方必须
   回放对方的状态、归还队列槽位，**不得重复执行后端写入**。
"""

from __future__ import annotations

from contextlib import suppress
from typing import Any

import pytest

from thinkback.domain.entities import TaskEntry
from thinkback.domain.enums import DeleteScope, OperationType, TaskStatus
from thinkback.memory.backends import FakeMemoryBackend
from thinkback.memory.repositories import InMemoryMemoryRepository
from thinkback.memory.schemas import AppendMemoryRequest, DeleteMemoryRequest
from thinkback.memory.service import MemoryService


class _AlwaysFailingAddBackend(FakeMemoryBackend):
    def add(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        raise RuntimeError("mem0 add failed")


class _ReadLagRepository(InMemoryMemoryRepository):
    """模拟读延迟：``get_task`` 读到还没追上的副本（查不到），
    ``claim_task`` 打到主库（撞上已有的行）。

    这就是真实的竞态窗口，不是造出来的：PG 有读副本时，append 的
    「查任务」与「认领任务」可能落在不同节点上。
    """

    def get_task(self, task_id: str) -> Any:
        return None


class _StagedReadRepository(InMemoryMemoryRepository):
    """前 N 次 ``get_task`` 返回 None，之后走真实仓储。

    模拟另一种竞态：append 第一次查任务时任务还不存在；等它走到认领那一步时，
    别的写入者已经把任务建好了。append 里正好两次 get_task（先查后认领），
    所以隐藏第一次读就能落进「任务已存在但本副本刚查不到」这个窗口。
    """

    def __init__(self, hidden_reads: int = 1) -> None:
        super().__init__()
        self.hidden_reads = hidden_reads
        self.reads = 0

    def get_task(self, task_id: str) -> Any:
        self.reads += 1
        if self.reads <= self.hidden_reads:
            return None
        return super().get_task(task_id)


def _request(round_id: str) -> AppendMemoryRequest:
    return AppendMemoryRequest(
        request_id=f"req-{round_id}",
        user_id="user-1",
        session_id="session-1",
        round_id=round_id,
        source_timestamp="2026-10-29T10:00:00Z",
        messages=[
            {
                "role": "user",
                "content": "我养了一只狗，名字叫豆包。",
                "message_id": f"m-{round_id}",
                "timestamp": "2026-10-29T10:00:00Z",
            },
            {
                "role": "assistant",
                "content": "记住了",
                "message_id": f"ma-{round_id}",
                "timestamp": "2026-10-29T10:00:05Z",
            },
        ],
    )


def _seed_task(repository: InMemoryMemoryRepository, round_id: str, status: TaskStatus) -> str:
    """在仓储里预置一个任务行，模拟别的写入者先认领了。"""
    task_id = f"memory-extract:{round_id}"
    repository.claim_task(
        TaskEntry(
            task_id=task_id,
            request_id=f"req-other-{round_id}",
            op_type=OperationType.WRITE_ROUND,
            scope={
                "user_id": "user-1",
                "session_id": "session-1",
                "round_id": round_id,
            },
            status=status,
        )
    )
    return task_id


# ---------------------------------------------------------------------------
# DEAD_LETTER 短路
# ---------------------------------------------------------------------------


def test_append_on_dead_letter_task_short_circuits_instead_of_reviving_it() -> None:
    """DEAD_LETTER 是终态：再打同一个 round_id 必须原地短路。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=_AlwaysFailingAddBackend())

    max_retries = MemoryService.MAX_RETRIES
    for _ in range(max_retries + 1):
        with suppress(RuntimeError, ValueError):
            service.append(_request("round-dead"))

    dead = repository.get_task("memory-extract:round-dead")
    assert dead is not None
    assert dead.status is TaskStatus.DEAD_LETTER
    retry_count_at_dead = dead.retry_count

    # 再打 19 次（复现生产事故的连打次数）：状态不变、预算不再涨。
    for _ in range(19):
        response = service.append(_request("round-dead"))
        assert response.status == "failed"

    after = repository.get_task("memory-extract:round-dead")
    assert after is not None
    assert after.status is TaskStatus.DEAD_LETTER, "DEAD_LETTER 任务被复活了"
    assert after.retry_count == retry_count_at_dead, "短路路径绕过了重试预算检查"


def test_append_on_dead_letter_task_does_not_touch_the_backend() -> None:
    """短路时不得再碰后端：预算耗尽的写入不该继续消耗引擎资源。"""
    repository = InMemoryMemoryRepository()
    backend = _AlwaysFailingAddBackend()
    service = MemoryService(repository=repository, backend=backend)

    max_retries = MemoryService.MAX_RETRIES
    for _ in range(max_retries + 1):
        with suppress(RuntimeError, ValueError):
            service.append(_request("round-dead-backend"))

    backend.add = lambda *args, **kwargs: []  # type: ignore[method-assign]
    service.append(_request("round-dead-backend"))

    assert repository.get_task("memory-extract:round-dead-backend") is not None


# ---------------------------------------------------------------------------
# 认领撞车
# ---------------------------------------------------------------------------


def test_append_does_not_write_twice_when_another_writer_already_claimed_the_task() -> None:
    """认领撞车：回放对方的状态，不重复执行后端写入。"""
    repository = _ReadLagRepository()
    backend = FakeMemoryBackend()
    task_id = "memory-extract:round-race"
    claimed, created = repository.claim_task(
        TaskEntry(
            task_id=task_id,
            request_id="req-other-writer",
            op_type=OperationType.WRITE_ROUND,
            scope={
                "user_id": "user-1",
                "session_id": "session-1",
                "round_id": "round-race",
            },
            status=TaskStatus.COMPLETED,
        )
    )
    assert created is True
    assert claimed.task_id == task_id

    service = MemoryService(repository=repository, backend=backend, l3_write_mode="async")
    response = service.append(_request("round-race"))

    assert response.status == "already_done"
    assert response.task_id == task_id
    assert backend.memories == {}, "认领撞车时重复执行了后端写入"


def test_append_returns_its_l3_slot_when_task_claim_is_lost() -> None:
    """认领失败必须归还 L3 队列槽位：槽位只进不出会把写入能力永久归零。"""
    repository = _ReadLagRepository()
    service = MemoryService(
        repository=repository,
        backend=FakeMemoryBackend(),
        l3_write_mode="async",
        l3_max_pending_tasks=4,
    )
    repository.claim_task(
        TaskEntry(
            task_id="memory-extract:round-slot",
            request_id="req-other-writer",
            op_type=OperationType.WRITE_ROUND,
            scope={
                "user_id": "user-1",
                "session_id": "session-1",
                "round_id": "round-slot",
            },
            status=TaskStatus.COMPLETED,
        )
    )

    service.append(_request("round-slot"))

    assert service._l3.pending_write_count() == 0, "认领失败泄漏了 L3 队列槽位"
    assert service.l3_background_status()["available_capacity"] == 4


# ---------------------------------------------------------------------------
# 任务已存在时的回放（第一次查不到、认领前别人抢先建好了任务）
# ---------------------------------------------------------------------------


def test_append_replays_running_task_when_another_writer_already_owns_it() -> None:
    """任务已被别人认领且在跑 → 回放「running」，不得说成已完成，也不得重复写。"""
    repository = _StagedReadRepository(hidden_reads=1)
    backend = FakeMemoryBackend()
    request = _request("round-running")
    # 轮次行必须在：append 的「已写过没有」快查只在轮次存在时才做，
    # 那正是竞态的入口 —— 第一次查不到任务，第二次查到了。
    repository.save_round(request)
    task_id = _seed_task(repository, "round-running", TaskStatus.RUNNING)

    service = MemoryService(repository=repository, backend=backend, l3_write_mode="async")
    response = service.append(request)

    assert response.status == "running"
    assert response.task_id == task_id
    assert backend.memories == {}, "别人在写时不该重复执行后端写入"
    assert service._l3.pending_write_count() == 0


def test_append_replays_completed_task_when_another_writer_finished_first() -> None:
    """任务已被别人跑完 → 回放「already_done」，不得重复写。"""
    repository = _StagedReadRepository(hidden_reads=1)
    backend = FakeMemoryBackend()
    request = _request("round-completed")
    repository.save_round(request)
    task_id = _seed_task(repository, "round-completed", TaskStatus.COMPLETED)

    service = MemoryService(repository=repository, backend=backend, l3_write_mode="async")
    response = service.append(request)

    assert response.status == "already_done"
    assert response.task_id == task_id
    assert backend.memories == {}, "别人已写完时不该重复执行后端写入"
    assert service._l3.pending_write_count() == 0


# ---------------------------------------------------------------------------
# 已擦除的轮次不得原样写回来
# ---------------------------------------------------------------------------


def test_append_rejects_a_round_that_was_physically_erased() -> None:
    """已被物理擦除的轮次不得原样写回来：原文会跟着回来。

    擦除承诺的是「原文从每一个存储位置清掉」。同一个 round_id 再写一次，
    等于把刚擦掉的原文重新落库 —— 承诺就废了。调用方必须换新的 round_id。
    """
    repository = InMemoryMemoryRepository()
    backend = FakeMemoryBackend()
    round_entry = repository.save_round(_request("round-erased"))
    repository.update_l1(round_entry)
    memory = repository.add_memory_index(
        backend_memory_id="erased-memory",
        user_id="user-1",
        memory_scope_id="thinkback",
        source_refs=[{"session_id": "session-1", "round_id": "round-erased"}],
        memory_text="User has a dog named 豆包",
    )
    backend.memories[memory.backend_memory_id] = {
        "id": memory.backend_memory_id,
        "memory": memory.memory_text,
        "event": "ADD",
        "user_id": "user-1",
        "agent_id": "thinkback",
        "metadata": {},
        "score": 1.0,
    }
    service = MemoryService(repository=repository, backend=backend)

    service.erase(
        DeleteMemoryRequest(
            request_id="req-erase",
            user_id="user-1",
            scope=DeleteScope.SESSION,
            session_id="session-1",
            operation_id="op-erase",
        )
    )

    with pytest.raises(ValueError, match="source round was erased"):
        service.append(_request("round-erased"))
