"""drain 必须等到完成回调真正跑完。

``concurrent.futures`` 在 worker 线程里 ``set_result`` 之后才执行 done
callback，而 ``wait()`` 在 future done 时就返回。两者之间有窗口：业务回调
里的记账（pending 计数递减、清理结束日志）如果落在 drain 返回之后，调用方
看到的就是半完成状态。
"""

from __future__ import annotations

import threading
import time

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
