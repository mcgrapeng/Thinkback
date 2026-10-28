"""drain 必须等到完成回调真正跑完。

``concurrent.futures`` 在 worker 线程里 ``set_result`` 之后才执行 done
callback，而 ``wait()`` 在 future done 时就返回。两者之间有窗口：业务回调
里的记账（pending 计数递减、清理结束日志）如果落在 drain 返回之后，调用方
看到的就是半完成状态。
"""

from __future__ import annotations

import threading
import time

import pytest

from thinkback.memory.l3 import L3WriteExecutor


def _tracked_future(
    tracker: L3WriteExecutor, on_finished: object
) -> tuple[threading.Event, object]:
    """提交一个「由 gate 控制完成时刻」的 future，并挂上完成回调。

    gate 保证 future 在 ``add_done_callback`` 之后才完成 —— 回调因此在
    worker 线程里执行，而不是在登记线程里同步跑完（那测不到竞态）。
    """

    gate = threading.Event()
    future = tracker.executor.submit(gate.wait)
    tracker.track_cleanup(future, on_finished)  # type: ignore[arg-type]
    return gate, future


def test_drain_waits_for_completion_callback() -> None:
    tracker = L3WriteExecutor(workers=2)
    finished = threading.Event()

    def on_finished(_done: object) -> None:
        time.sleep(0.2)
        finished.set()

    gate, _ = _tracked_future(tracker, on_finished)
    gate.set()

    tracker.drain(timeout=5)

    assert finished.is_set()
    tracker.shutdown()


def test_drain_respects_timeout_budget() -> None:
    """回调卡住时 drain 不能无限等：timeout 是总预算。"""

    tracker = L3WriteExecutor(workers=2)
    release = threading.Event()

    def on_finished(_done: object) -> None:
        release.wait(30)

    gate, _ = _tracked_future(tracker, on_finished)
    gate.set()

    started = time.monotonic()
    tracker.drain(timeout=0.5)
    elapsed = time.monotonic() - started

    release.set()
    assert elapsed < 5
    tracker.shutdown()


def test_drain_raises_when_completion_callback_fails() -> None:
    """回调（记账）失败不能被吞成「drain 成功」。

    记账失败意味着 pending 计数没减、任务状态没收敛 —— 调用方必须看见。
    """

    tracker = L3WriteExecutor(workers=2)

    def on_finished(_done: object) -> None:
        raise RuntimeError("bookkeeping failed")

    gate, _ = _tracked_future(tracker, on_finished)
    gate.set()

    with pytest.raises(RuntimeError, match="bookkeeping failed"):
        tracker.drain(timeout=5)
    tracker.shutdown()


def test_drain_raises_work_failure_after_callbacks_finished() -> None:
    """任务本身失败时，异常照旧向上抛 —— 但要先等回调跑完。

    顺序很重要：回调里做的是记账，先抛异常会让记账来不及做。
    """

    tracker = L3WriteExecutor(workers=2)
    order: list[str] = []

    def on_finished(_done: object) -> None:
        order.append("callback")

    def work() -> None:
        raise ValueError("work failed")

    future = tracker.executor.submit(work)
    tracker.track_cleanup(future, on_finished)

    with pytest.raises(ValueError, match="work failed"):
        tracker.drain(timeout=5)
    assert order == ["callback"]
    tracker.shutdown()
