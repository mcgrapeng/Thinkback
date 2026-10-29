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


# ---------------------------------------------------------------------------
# 构造参数校验与 drain 的无预算路径（此前 0 覆盖）
# ---------------------------------------------------------------------------


def test_l3_executor_rejects_invalid_construction_parameters() -> None:
    """三个容量参数的下界必须在构造时挡住。

    ``workers=0`` 会让 ThreadPoolExecutor 起不来线程；``max_pending_tasks=0``
    会让 BoundedSemaphore(0) 永远拿不到槽位（每次 append 都 500）；
    ``queue_wait_seconds<0`` 会让 Semaphore.acquire 直接抛 ValueError。
    这三个值都来自配置 —— 配置写错必须在启动时炸，而不是在服务跑起来之后
    以「append 一直失败」的形式出现。
    """
    with pytest.raises(ValueError, match="l3_executor_workers must be >= 1"):
        L3WriteExecutor(workers=0)
    with pytest.raises(ValueError, match="l3_max_pending_tasks must be >= 1"):
        L3WriteExecutor(max_pending_tasks=0)
    with pytest.raises(ValueError, match="l3_queue_wait_seconds must be >= 0"):
        L3WriteExecutor(queue_wait_seconds=-0.1)


def test_drain_without_a_timeout_budget_still_waits_for_the_callback() -> None:
    """``drain()`` 不给预算时是「无限等」，不是「不等」。

    关停路径用的就是无预算 drain：它必须等到完成回调真的跑完才返回。
    之前所有用例都传了 timeout，无预算分支（``_remaining`` 返回 None）
    从没被执行过 —— 而它恰好是关停语义本身。
    """
    tracker = L3WriteExecutor(workers=2)
    finished = threading.Event()

    def on_finished(_done: object) -> None:
        finished.set()

    gate, _ = _tracked_future(tracker, on_finished)
    gate.set()

    tracker.drain()

    assert finished.is_set(), "drain() 没等完成回调就返回了"
    tracker.shutdown()


def test_drain_tolerates_a_future_that_is_still_running() -> None:
    """预算耗尽时任务还在跑 → drain 不抛异常，只是放弃等待。

    future 未完成时不能去 ``result()``（会阻塞到天荒地老）；也不能把
    「没等到」当成失败抛出去 —— drain 的契约是「尽量等」，等不到就返回，
    由关停方决定要不要强杀。
    """
    tracker = L3WriteExecutor(workers=2)
    release = threading.Event()

    future = tracker.executor.submit(release.wait, 30)
    tracker.track_cleanup(future, lambda _done: None)

    tracker.drain(timeout=0.2)  # 不得抛出

    release.set()
    tracker.shutdown()
