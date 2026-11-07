"""异步 L3 后台写的**收尾回调**：写成功 / 写失败 / 被取消 三种结局。

``l3_write_mode="async"`` 时，写入走后台线程池，``append`` 立即返回。
后台写结束后由两个回调收尾：

- ``_finish_l3_background_write(future)`` —— 摘掉写任务、**归还写槽位**，
  并按 future 的状态记「成功 / 失败 / 被取消」；
- ``_finish_l3_cleanup(future, task_id)`` —— 摘掉清理任务，失败时把对应
  任务标 FAILED。

**「被取消」这一侧此前从未被验证**：``future.cancelled()`` 为真时，
``future.done()`` 也为真，于是 ``future.exception()`` 会抛
``CancelledError`` —— 那条 ``if`` 的**真分支**必须被覆盖到，
否则「写槽位没归还」的故障（该 Pod 写入能力归零）就没有测试守着。

之所以要专门盯这条：槽位只进不出的话，生产上限 256 次泄漏就让
该 Pod 永久写不进任何记忆，直到重启。
"""

from __future__ import annotations

import threading
from concurrent.futures import Future
from typing import Any

from thinkback.memory.backends import FakeMemoryBackend
from thinkback.memory.repositories import InMemoryMemoryRepository
from thinkback.memory.service import MemoryService


def _service() -> tuple[MemoryService, InMemoryMemoryRepository]:
    repository = InMemoryMemoryRepository()
    return (
        MemoryService(
            repository=repository,
            backend=FakeMemoryBackend(),
            l3_write_mode="async",
        ),
        repository,
    )


def _cancelled_future() -> Future[None]:
    """一个被取消的 future。"""
    future: Future[None] = Future()
    future.cancel()
    return future


def _failed_future() -> Future[None]:
    """一个带异常的 future。"""
    future: Future[None] = Future()
    future.set_exception(RuntimeError("l3 write failed"))
    return future


def _done_future() -> Future[None]:
    """一个成功完成的 future。"""
    future: Future[None] = Future()
    future.set_result(None)
    return future


def test_a_cancelled_background_write_still_returns_its_slot() -> None:
    """后台写被取消 → 写槽位**必须**归还。

    被取消是最容易漏的一条：``future.cancelled()`` 为真时，
    ``future.exception()`` 会抛 —— 如果收尾逻辑因此提前返回，
    槽位就永远还不回去。
    """
    service, _repository = _service()
    service._l3.reserve_write_slot()
    assert service._l3._pending_write_slots == 1, "前置不干净：槽位没占上"

    service._finish_l3_background_write(_cancelled_future())

    assert service._l3._pending_write_slots == 0, "被取消的后台写没有归还写槽位"


def test_a_failed_background_write_returns_its_slot() -> None:
    """后台写失败 → 写槽位也必须归还（失败不是「占着不放」的理由）。"""
    service, _repository = _service()
    service._l3.reserve_write_slot()

    service._finish_l3_background_write(_failed_future())

    assert service._l3._pending_write_slots == 0, "失败的后台写没有归还写槽位"


def test_a_successful_background_write_returns_its_slot() -> None:
    """后台写成功 → 写槽位归还。"""
    service, _repository = _service()
    service._l3.reserve_write_slot()

    service._finish_l3_background_write(_done_future())

    assert service._l3._pending_write_slots == 0


def test_a_not_yet_started_future_does_not_break_the_finish_callback() -> None:
    """future 还没跑完（既没 done 也没 cancelled）→ 收尾不能炸。

    收尾回调可能被一个尚未启动的 future 调用（提交成功但 worker 还没拿到）。
    这条覆盖 ``future.done() and not future.cancelled()`` 的**假**分支 ——
    此前只有「已完成」那一侧被走过。
    """
    service, _repository = _service()
    service._l3.reserve_write_slot()

    service._finish_l3_background_write(Future())

    assert service._l3._pending_write_slots == 0


def test_a_not_yet_started_cleanup_does_not_mark_the_task_failed() -> None:
    """清理回调拿到一个还没跑完的 future → 不把任务标 FAILED。

    与「已取消」同侧：没做完 ≠ 做失败了。
    """
    service, repository = _service()
    task_id = "memory-delete:op-pending"
    repository.save_task(_task_entry(task_id))

    service._finish_l3_cleanup(Future(), task_id)

    task = repository.get_task(task_id)
    assert task is not None
    assert task.status is not task.status.FAILED, "future 没跑完就把任务标 FAILED 了"


def test_a_cancelled_cleanup_marks_no_task_failed() -> None:
    """清理任务被取消 → **不**把任务标 FAILED。

    取消是「没做」，不是「做失败了」——标成 FAILED 会让调用方以为清理
    失败并去重试，而实际上它已经被别处取消了。
    """
    service, repository = _service()
    task_id = "memory-delete:op-cancelled"
    repository.save_task(_task_entry(task_id))

    service._finish_l3_cleanup(_cancelled_future(), task_id)

    task = repository.get_task(task_id)
    assert task is not None
    assert task.status is not task.status.FAILED, "被取消的清理不该把任务标 FAILED"


def test_a_failed_cleanup_marks_the_task_failed() -> None:
    """清理任务失败 → 任务标 FAILED（调用方需要知道）。"""
    service, repository = _service()
    task_id = "memory-delete:op-failed"
    repository.save_task(_task_entry(task_id))

    service._finish_l3_cleanup(_failed_future(), task_id)

    task = repository.get_task(task_id)
    assert task is not None
    assert task.status is task.status.FAILED, f"清理失败后任务状态是 {task.status}"
    assert "l3 write failed" in (task.last_error or ""), f"任务没记失败原因：{task.last_error!r}"


def test_a_successful_cleanup_leaves_the_task_alone() -> None:
    """清理成功 → 任务状态不被改成 FAILED。"""
    service, repository = _service()
    task_id = "memory-delete:op-ok"
    repository.save_task(_task_entry(task_id))

    service._finish_l3_cleanup(_done_future(), task_id)

    task = repository.get_task(task_id)
    assert task is not None
    assert task.status is not task.status.FAILED


def test_the_finish_callbacks_are_safe_to_call_from_a_worker_thread() -> None:
    """收尾回调跑在 worker 线程上 → 不能抛。

    回调抛异常会丢进 future，而 future 没人看 —— 槽位就漏了，且无迹可寻。

    槽位必须**先占后还**：收尾回调自己会归还一次，
    没占就还 = 信号量多放一次（实际并发上限高于配置）；
    占两次再还一次 = 多放同样一次。
    """
    service, _repository = _service()
    service._l3.reserve_write_slot()
    errors: list[BaseException] = []

    def _run() -> None:
        try:
            service._finish_l3_background_write(_cancelled_future())
        except BaseException as exc:  # noqa: BLE001 - 就是要抓住它
            errors.append(exc)

    thread = threading.Thread(target=_run)
    thread.start()
    thread.join(timeout=5)

    assert errors == [], f"收尾回调在 worker 线程里抛了：{errors}"
    assert service._l3.pending_write_count() == 0


def _task_entry(task_id: str) -> Any:
    from thinkback.domain.entities import TaskEntry
    from thinkback.domain.enums import OperationType, TaskStatus

    return TaskEntry(
        task_id=task_id,
        request_id="req-1",
        op_type=OperationType.DELETE_MEMORY,
        scope={"user_id": "u1"},
        status=TaskStatus.RUNNING,
    )
