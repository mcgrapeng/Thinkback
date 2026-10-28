"""擦除任务归属与长任务心跳的回归测试。

对应独立复审发现：

- 任务行存在但没有计划（执行者死在 claim 与落计划之间）时，擦除不能
  报 completed、更不能什么都没删；
- 计划丢失后重试要能重算计划并把擦除做完；
- 同步写入同样要有心跳：慢的同步写入超过孤儿阈值也不能被回收成 failed
  再被终态写翻回 completed。
"""

from __future__ import annotations

from threading import Event, Thread
from time import sleep

from tests.unit.test_memory_service import make_append
from thinkback.domain.entities import TaskEntry
from thinkback.domain.enums import OperationType, TaskStatus
from thinkback.memory.backends import FakeMemoryBackend
from thinkback.memory.repositories import InMemoryMemoryRepository
from thinkback.memory.schemas import DeleteMemoryRequest, DeleteScope
from thinkback.memory.service import MemoryService

ERASE_TASK_ID = "memory-erase:op-erase-owner"
ERASE_SCOPE = {
    "user_id": "user-1",
    "session_id": "session-1",
    "session_scope_id": "session-1",
    "long_term_scope_id": "thinkback",
    "delete_scope": "session",
    "memory_id": None,
}


def _erase_request(operation_id: str = "op-erase-owner") -> DeleteMemoryRequest:
    return DeleteMemoryRequest(
        request_id="req-erase-owner",
        user_id="user-1",
        session_id="session-1",
        scope=DeleteScope.SESSION,
        operation_id=operation_id,
    )


def _planless_running_task() -> TaskEntry:
    return TaskEntry(
        task_id=ERASE_TASK_ID,
        request_id="req-erase-owner",
        op_type=OperationType.ERASE,
        scope=dict(ERASE_SCOPE),
        status=TaskStatus.RUNNING,
        operation_id="op-erase-owner",
    )


def test_erase_with_planless_running_task_does_not_report_success() -> None:
    """任务行在、计划不在：不能报 completed 也不能假装擦完了。

    这是「执行者死在 claim 之后、落计划之前」的现场：此时还没有任何
    副作用发生，另一个执行者可能仍在跑。
    """
    repository = InMemoryMemoryRepository()
    service = MemoryService(
        repository=repository, backend=FakeMemoryBackend(), l3_write_mode="sync"
    )
    service.append(make_append(round_id="r-erase-owner", content="我的门禁卡号是 C12345"))
    repository.save_task(_planless_running_task())

    response = service.erase(_erase_request())

    assert response.status != "completed"
    assert response.status == "running"
    # 没有计划就没有可执行的擦除，原文必须还在（不能被「擦除成功」掩盖）。
    assert repository.get_round("r-erase-owner") is not None


def test_erase_resumes_when_plan_was_never_saved() -> None:
    """计划丢失后重试：要重算计划并把擦除做完，而不是报 0 条完成。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(
        repository=repository, backend=FakeMemoryBackend(), l3_write_mode="sync"
    )
    service.append(make_append(round_id="r-erase-resume", content="我的门禁卡号是 C12345"))
    task = _planless_running_task()
    task.task_id = "memory-erase:op-erase-resume"
    task.operation_id = "op-erase-resume"
    task.status = TaskStatus.FAILED
    repository.save_task(task)

    response = service.erase(_erase_request(operation_id="op-erase-resume"))

    assert response.status == "completed"
    assert response.affected_memories >= 1
    assert repository.get_round("r-erase-resume") is None


def test_sync_long_write_is_not_reclaimed(monkeypatch) -> None:
    """同步写入超过孤儿阈值时也不该被回收：它同样有心跳。"""
    from thinkback.infra.config import settings

    monkeypatch.setattr(settings, "task_orphan_running_seconds", 0.1, raising=False)
    repository = InMemoryMemoryRepository()
    service = MemoryService(
        repository=repository,
        backend=FakeMemoryBackend(),
        l3_write_mode="sync",
        task_heartbeat_seconds=0.02,
    )
    started = Event()
    release = Event()

    class _SlowBackend(FakeMemoryBackend):
        def add(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            started.set()
            assert release.wait(timeout=10), "add 等待超时"
            return super().add(*args, **kwargs)

    service.backend = _SlowBackend()
    worker = Thread(
        target=lambda: service.append(
            make_append(round_id="r-sync-heartbeat", content="我负责 3nm 良率提升")
        )
    )
    worker.start()
    assert started.wait(timeout=5)

    sleep(0.4)  # 超过孤儿阈值，但心跳仍在刷新任务行

    task = service.get_task("memory-extract:r-sync-heartbeat")
    assert task is not None
    assert task.status is TaskStatus.RUNNING

    release.set()
    worker.join(timeout=5)
