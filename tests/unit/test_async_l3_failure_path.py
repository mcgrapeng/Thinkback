"""异步 L3 写入的失败路径：失败必须被记账，不能变成静默的数据丢失。

async 模式下 append 已经返回 ``accepted``，调用方认为「写进去了」。此后的
失败没有调用方可以接，必须由任务状态本身承担：

1. **后端写失败** → 任务标 ``FAILED`` + ``last_error``。若任务停在 RUNNING，
   客户端会一直轮询，而那条长期记忆永远缺失。
2. **失败状态本身写不进去**（仓储不可用）→ 异常不得逃出后台线程（逃出去会被
   线程池吞掉，连日志都没有），任务停在 RUNNING，交给孤儿回收流程处置。
   这是刻意的取舍：宁可任务显示「还在跑」，也不谎报「已完成」。
3. **旧 attempt 不得覆盖新 attempt 的状态**：任务被回收并由别人重试后，
   旧执行者的终态写必须被 attempt 号挡住，否则会出现
   「RUNNING → FAILED → COMPLETED」的翻转，客户端据此重试就会重复写。

这三条分支在历史覆盖里一次都没被执行过。
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from thinkback.domain.enums import TaskStatus
from thinkback.memory.backends import FakeMemoryBackend
from thinkback.memory.repositories import InMemoryMemoryRepository
from thinkback.memory.schemas import AppendMemoryRequest
from thinkback.memory.service import MemoryService


class _FailingAddBackend(FakeMemoryBackend):
    def add(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        raise RuntimeError("mem0 library add failed")


class _BlockingFailingAddBackend(FakeMemoryBackend):
    """卡在 add 里失败，用来在失败发生前制造一个「任务被回收重试」的窗口。"""

    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def add(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        self.started.set()
        self.release.wait(timeout=10)
        raise RuntimeError("mem0 library add failed")


class _BlockingSuccessAddBackend(FakeMemoryBackend):
    """卡在 add 里然后成功：写成功但终态写被 attempt 挡住的那条路。"""

    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def add(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        self.started.set()
        self.release.wait(timeout=10)
        return super().add(*args, **kwargs)


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


def test_async_l3_write_failure_marks_the_task_failed() -> None:
    """后端写失败 → 任务标 FAILED 并留下错误原因。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(
        repository=repository,
        backend=_FailingAddBackend(),
        l3_write_mode="async",
    )

    response = service.append(_request("round-async-fail"))
    assert response.status == "accepted"

    # drain 会向上抛后台 future 的首个异常；这里不得抛 —— 失败已被任务状态接住。
    service.drain_l3_background_tasks(timeout=5)

    task = repository.get_task(response.task_id)
    assert task is not None
    assert task.status is TaskStatus.FAILED
    assert task.last_error is not None
    assert "mem0 library add failed" in task.last_error


def test_async_l3_failure_does_not_escape_the_background_thread() -> None:
    """失败不得从后台线程逃出去：逃出去会被线程池吞掉，连日志都不留。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(
        repository=repository,
        backend=_FailingAddBackend(),
        l3_write_mode="async",
    )

    response = service.append(_request("round-async-no-escape"))
    service.drain_l3_background_tasks(timeout=5)

    task = repository.get_task(response.task_id)
    assert task is not None
    assert task.status is TaskStatus.FAILED
    assert service.l3_background_status()["pending_write_tasks"] == 0


def test_async_l3_failure_that_cannot_be_persisted_leaves_the_task_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """失败状态写不进去时，任务停在 RUNNING 等孤儿回收 —— 不得谎报已完成。"""
    repository = InMemoryMemoryRepository()
    backend = _BlockingFailingAddBackend()
    service = MemoryService(
        repository=repository,
        backend=backend,
        l3_write_mode="async",
    )

    response = service.append(_request("round-async-persist-fail"))
    # 等后台线程真的进了 add 才动仓储：否则它可能已经把 FAILED 写下去了，
    # 「终态写失败」这条路径根本没被走到，用例就成了竞态（发布门禁上偶发红）。
    assert backend.started.wait(timeout=5), "后台写入没有进入 add"

    def _boom(task: Any) -> Any:
        raise RuntimeError("pg unreachable")

    monkeypatch.setattr(repository, "save_task", _boom)

    backend.release.set()
    service.drain_l3_background_tasks(timeout=5)

    task = repository.get_task(response.task_id)
    assert task is not None
    assert task.status is TaskStatus.RUNNING, "终态写失败时不得把任务标成已完成"
    assert task.last_error is None


def test_stale_async_attempt_does_not_overwrite_the_newer_attempt_state() -> None:
    """旧 attempt 的失败不得覆盖新 attempt 的状态（防止状态翻转）。"""
    repository = InMemoryMemoryRepository()
    backend = _BlockingFailingAddBackend()
    service = MemoryService(
        repository=repository,
        backend=backend,
        l3_write_mode="async",
        task_heartbeat_seconds=30.0,
    )

    response = service.append(_request("round-stale-attempt"))
    assert backend.started.wait(timeout=5), "后台写入没有进入 add"

    # 模拟「任务被孤儿回收并由别人重试」：attempt 号前进。
    task = repository.get_task(response.task_id)
    assert task is not None
    task.result = {**task.result, "attempt": int(task.result.get("attempt", 0) or 0) + 1}
    repository.save_task(task)

    backend.release.set()
    service.drain_l3_background_tasks(timeout=5)

    final = repository.get_task(response.task_id)
    assert final is not None
    assert final.status is TaskStatus.RUNNING, "旧 attempt 覆盖了新 attempt 的任务状态"
    assert final.last_error is None


def test_stale_async_attempt_does_not_mark_completion_after_a_newer_attempt_started() -> None:
    """旧 attempt 写成功也不得标 COMPLETED —— 否则会翻转成 RUNNING → COMPLETED。

    客户端看到 COMPLETED 就会认为这条已经落地，若随后再重试就会重复写。
    """
    repository = InMemoryMemoryRepository()
    backend = _BlockingSuccessAddBackend()
    service = MemoryService(
        repository=repository,
        backend=backend,
        l3_write_mode="async",
        task_heartbeat_seconds=30.0,
    )

    response = service.append(_request("round-stale-success"))
    assert backend.started.wait(timeout=5), "后台写入没有进入 add"

    task = repository.get_task(response.task_id)
    assert task is not None
    task.result = {**task.result, "attempt": int(task.result.get("attempt", 0) or 0) + 1}
    repository.save_task(task)

    backend.release.set()
    service.drain_l3_background_tasks(timeout=5)

    final = repository.get_task(response.task_id)
    assert final is not None
    assert final.status is TaskStatus.RUNNING, "旧 attempt 把任务标成了已完成"
    assert final.result.get("l3_events") is None
