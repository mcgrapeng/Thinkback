"""S3 regression tests: Multi-replica CAS auto-replay.

P1a 路线图：row_version CAS 已落库；S3 落地跨副本 CAS 冲突时由
``_mutate_task_with_retry`` 重读快照重放，避免 409 把可收敛的中间态上抛给客户端。
"""

from __future__ import annotations

from thinkback.domain.enums import OperationType, TaskStatus
from thinkback.domain.errors import TaskStaleWriteError
from thinkback.memory.backends import FakeMemoryBackend
from thinkback.memory.repositories import (
    InMemoryMemoryRepository,
    TaskEntry,
)


class _StaleWriteMemoryRepository(InMemoryMemoryRepository):
    """仓储桩：第一次 save_task（=mutate_with_retry 的第二次）抛 TaskStaleWriteError。

    复现 S3 目标：CAS 冲突 → mutate 重读快照重放 → 第二次 save_task 成功。
    注：测试 setup 里调用 ``repository.save_task(task)`` 是第一次（应成功），
    进入 mutate_with_retry 后再调 save_task 才是第二次（应触发冲突）。
    """

    def __init__(self) -> None:
        super().__init__()
        self._save_count = 0
        self._conflicts_emitted = 0

    def save_task(self, task: TaskEntry) -> TaskEntry:  # type: ignore[override]
        self._save_count += 1
        # 第一次是测试 setup 的预存；之后是 mutate_with_retry 内部的提交循环。
        # 第 2 次 save_task 触发冲突（模拟另一副本抢先提交），后续重放应该成功。
        if self._save_count == 2:
            self._conflicts_emitted += 1
            raise TaskStaleWriteError(task_id=task.task_id, expected_version=task.row_version)
        return super().save_task(task)


def test_mutate_task_with_retry_retries_on_stale_write() -> None:
    """S3: 第一次 save_task 抛 TaskStaleWriteError，自动重放，第二次成功。"""
    from thinkback.memory.service import MemoryService

    repository = _StaleWriteMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())

    task = TaskEntry(
        task_id="test-task-1",
        request_id="req-1",
        op_type=OperationType.WRITE_ROUND,
        scope={"user_id": "u"},
        status=TaskStatus.RUNNING,
    )
    repository.save_task(task)

    def mutate(t: TaskEntry) -> None:
        t.status = TaskStatus.COMPLETED

    result = service._mutate_task_with_retry("test-task-1", mutate)

    assert result is not None
    assert result.status is TaskStatus.COMPLETED
    assert repository._conflicts_emitted == 1


def test_mutate_task_with_retry_gives_up_after_attempts() -> None:
    """S3: 持续冲突（3 次都失败）放弃并 warning，不阻塞主链路。"""

    class _AlwaysStaleMemoryRepository(_StaleWriteMemoryRepository):
        def save_task(self, task: TaskEntry) -> TaskEntry:  # type: ignore[override]
            self._save_count += 1
            # 第一次（setup 预存）成功入桶；之后 mutate_with_retry 内的所有 save 都失败。
            # 这让 attempts 跑满后 give up 返回 None。
            if self._save_count == 1:
                return InMemoryMemoryRepository.save_task(self, task)
            self._conflicts_emitted += 1
            raise TaskStaleWriteError(task_id=task.task_id, expected_version=task.row_version)

    from thinkback.memory.service import MemoryService

    repository = _AlwaysStaleMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    task = TaskEntry(
        task_id="test-task-2",
        request_id="req-2",
        op_type=OperationType.WRITE_ROUND,
        scope={"user_id": "u"},
        status=TaskStatus.RUNNING,
    )
    repository.save_task(task)

    def mutate(t: TaskEntry) -> None:
        t.status = TaskStatus.COMPLETED

    result = service._mutate_task_with_retry("test-task-2", mutate, attempts=3)
    assert result is None
    assert repository._conflicts_emitted == 3


def test_mutate_task_with_retry_returns_none_for_missing_task() -> None:
    """S3: 任务不存在（被另一副本删除）→ 返回 None，不抛异常。"""
    from thinkback.memory.service import MemoryService

    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())

    def mutate(t: TaskEntry) -> None:
        t.status = TaskStatus.COMPLETED

    result = service._mutate_task_with_retry("nonexistent", mutate)
    assert result is None
