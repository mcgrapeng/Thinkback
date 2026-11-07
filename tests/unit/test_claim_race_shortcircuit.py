"""**抢不到任务执行权**时的三条短路路径（delete / update / rebuild）。

同一把 ``operation_id`` 被并发调用两次时，只有一个人能 ``claim_task`` 成功；
另一个抢不到，就走「回放对方的状态」这条路 —— 不重复执行副作用。

三种操作各有自己的回放函数（``_existing_delete_response`` /
``_existing_update_response`` / ``_existing_rebuild_response``），每个都返回
「已有任务的真实状态」或 ``None``（表示没有可回放的状态，交给后续流程）。

**这些分支此前从未被走到**：现有用例都是单调用，走的是 ``claim_task`` 成功那条路。
后果是「抢不到执行权时该回放什么状态」这条口径完全没被验证 ——
而它决定了并发重复请求时客户端收到的是 ``completed`` / ``running`` / ``failed``
中的哪一个。口径错了，客户端就会误判「我的操作完成了」而实际没完成。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from thinkback.domain.enums import TaskStatus
from thinkback.memory.backends import FakeMemoryBackend
from thinkback.memory.repositories import InMemoryMemoryRepository
from thinkback.memory.schemas import (
    AppendMemoryRequest,
    DeleteMemoryRequest,
    MessageRole,
    RebuildMemoryRequest,
    UpdateMemoryRequest,
)
from thinkback.memory.service import MemoryService

_TS = datetime(2026, 11, 5, tzinfo=UTC)


def _service() -> tuple[MemoryService, InMemoryMemoryRepository]:
    repository = InMemoryMemoryRepository()
    return (
        MemoryService(
            repository=repository,
            backend=FakeMemoryBackend(),
            l3_write_mode="sync",
        ),
        repository,
    )


def _append(service: MemoryService, round_id: str = "r1") -> None:
    service.append(
        AppendMemoryRequest(
            request_id=f"req-{round_id}",
            user_id="u1",
            session_id="s1",
            round_id=round_id,
            messages=[
                {
                    "message_id": f"m-{round_id}",
                    "role": MessageRole.USER,
                    "content": "我叫麻薯",
                    "timestamp": _TS,
                }
            ],
            source_timestamp=_TS,
            metadata={},
        )
    )


def _lose_the_claim(service: MemoryService, status: TaskStatus) -> None:
    """把 ``claim_task`` 换成「永远抢不到」，并让对方任务是给定状态。"""
    from thinkback.domain.entities import TaskEntry

    def _claim(task: Any) -> tuple[Any, bool]:
        winner = TaskEntry(
            task_id=task.task_id,
            request_id="other-writer",
            op_type=task.op_type,
            scope=task.scope,
            status=status,
            result={"affected_count": 3},
        )
        return winner, False

    service.repository.claim_task = _claim  # type: ignore[method-assign]


def _delete_request(operation_id: str = "op-del") -> DeleteMemoryRequest:
    return DeleteMemoryRequest(
        request_id=f"req-{operation_id}",
        user_id="u1",
        session_id="s1",
        scope="session",
        operation_id=operation_id,
        namespace="thinkback",
    )


def _update_request(operation_id: str = "op-upd", memory_id: str = "m-1") -> UpdateMemoryRequest:
    return UpdateMemoryRequest(
        request_id=f"req-{operation_id}",
        user_id="u1",
        memory_id=memory_id,
        operation_id=operation_id,
        content="新内容",
        namespace="thinkback",
    )


def _existing_memory_id(service: MemoryService) -> str:
    """取一条真实存在的记忆 id。

    update 的回放路径要**读出那条记忆**（读不到就抛 "memory not found"），
    所以必须用真的 id，不能编一个。
    """
    items = service.list_memory_items(user_id="u1", namespace="thinkback")
    assert items.items, "没有可管理的记忆 —— 先 append 一轮"
    return items.items[0].memory_id


def _rebuild_request(operation_id: str = "op-rb") -> RebuildMemoryRequest:
    return RebuildMemoryRequest(
        request_id=f"req-{operation_id}",
        user_id="u1",
        session_id="s1",
        operation_id=operation_id,
        rebuild_l2=True,
        rebuild_l3=True,
        namespace="thinkback",
    )


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (TaskStatus.RUNNING, "running"),
        (TaskStatus.PENDING, "running"),
        (TaskStatus.DEAD_LETTER, "failed"),
    ],
)
def test_delete_losing_the_claim_replays_the_winners_status(
    status: TaskStatus, expected: str
) -> None:
    """delete 抢不到执行权 → 回放对方的状态，不重复执行。

    客户端据此知道「你的删除已经在跑」还是「已经失败」——
    口径错了客户端就会误判。
    """
    service, _repository = _service()
    _append(service)
    _lose_the_claim(service, status)

    response = service.delete(_delete_request())

    assert response.status == expected, (
        f"抢不到执行权时回放的状态不对：期望 {expected}，实际 {response.status}"
    )


def test_delete_losing_the_claim_to_a_failed_task_falls_through_to_the_retry_path() -> None:
    """对方任务是 **FAILED** → 没有可回放的状态 → 走重试路径。

    ``_existing_delete_response`` 只对 DEAD_LETTER / PENDING / RUNNING /
    COMPLETED 有话可说；FAILED 意味着「上次失败了，这次该接着做」——
    所以返回 None，让流程继续往下走（重新执行删除）。

    这条与上面那条互补：上面测「有状态可回放」，这条测「没有 → 不短路」。
    """
    service, repository = _service()
    _append(service)
    _lose_the_claim(service, TaskStatus.FAILED)

    response = service.delete(_delete_request())

    # 落到重试路径 → 任务被重新执行，状态不再是「回放对方」
    assert response.status != "running", "FAILED 的对方任务不该被回放成 running"
    assert repository.get_task("memory-delete:op-del") is not None


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (TaskStatus.RUNNING, "running"),
        (TaskStatus.PENDING, "running"),
        (TaskStatus.DEAD_LETTER, "failed"),
    ],
)
def test_update_losing_the_claim_replays_the_winners_status(
    status: TaskStatus, expected: str
) -> None:
    """update 抢不到执行权 → 回放对方的状态。"""
    service, _repository = _service()
    _append(service)
    memory_id = _existing_memory_id(service)
    _lose_the_claim(service, status)

    response = service.update_memory(_update_request(memory_id=memory_id))

    assert response.status == expected, (
        f"抢不到执行权时回放的状态不对：期望 {expected}，实际 {response.status}"
    )


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (TaskStatus.RUNNING, "running"),
        (TaskStatus.PENDING, "running"),
        (TaskStatus.DEAD_LETTER, "failed"),
    ],
)
def test_rebuild_losing_the_claim_replays_the_winners_status(
    status: TaskStatus, expected: str
) -> None:
    """rebuild 抢不到执行权 → 回放对方的状态。"""
    service, _repository = _service()
    _append(service)
    _lose_the_claim(service, status)

    response = service.rebuild(_rebuild_request())

    assert response.status == expected, (
        f"抢不到执行权时回放的状态不对：期望 {expected}，实际 {response.status}"
    )


def test_delete_losing_the_claim_does_not_repeat_the_deletion() -> None:
    """抢不到执行权 → **不重复删**。

    重复删会重复计数（``affected_memories``）并把审计写两遍。
    """
    service, _repository = _service()
    _append(service)
    _lose_the_claim(service, TaskStatus.RUNNING)

    response = service.delete(_delete_request())

    # 回放的响应带的是「对方任务」里的计数，不是本次新算的
    assert response.affected_memories == 3, (
        f"回放时 affected_memories 不是对方的计数：{response.affected_memories}"
    )


def test_update_losing_the_claim_does_not_write_a_pending_backend_update() -> None:
    """update 抢不到执行权 → **没有**待写的后端更新。

    有了就等于两个写入者都在改同一条记忆。
    """
    service, _repository = _service()
    _append(service)
    memory_id = _existing_memory_id(service)
    _lose_the_claim(service, TaskStatus.RUNNING)

    response = service.update_memory(_update_request(memory_id=memory_id))

    # 回放的响应不带待写后端更新 —— 抢不到执行权就不能改记忆
    assert response.status == "running"
