"""``memory/l3.py`` 的**精确值**契约（队列容量 / future 记账 / 关停语义）。

L3 后台写执行器是三件事的交汇点：队列容量、future 记账、关停。
三个方向写错，线上表现都很隐蔽：

- 槽位只进不出 → 队列预算被吃光，该 Pod 的记忆写入能力永久归零；
- 槽位多还 → 绕过 ``max_pending_tasks`` 直压 LLM；
- drain 提前返回 → 调用方以为「已关停」，而记账还没做完。

所以这里的用例都断言**精确值**（计数、次数、耗时上限、异常类型），
而不是「没抛异常」。
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

import pytest

from thinkback.memory.l3 import L3WriteExecutor


class _SlottedFuture(Future[Any]):
    """哈希值固定的 future —— 让 ``set`` 的遍历顺序可预测。

    drain 按「写 ∪ 清理」这个 set 的遍历顺序收集错误，而 set 的顺序由
    **哈希槽**决定（默认哈希是对象地址，每次运行都不同）。要让
    「未完成的 future 排在失败的 future 前面」这件事稳定成立，只能把
    哈希钉死 —— 否则那条用例只有约一半的概率杀掉对应的变异体
    （实测 20 次里 11 次）。
    """

    def __init__(self, slot: int) -> None:
        super().__init__()
        self._slot = slot

    def __hash__(self) -> int:
        return self._slot


# ---------------------------------------------------------------------------
# 构造参数的边界：下界挡住非法值，边界值本身必须合法
# ---------------------------------------------------------------------------


def test_the_default_capacity_is_the_documented_one() -> None:
    """默认容量就是那三个值 —— 改默认值等于改生产配置。"""
    tracker = L3WriteExecutor()

    assert tracker.workers == 2
    assert tracker.max_pending_tasks == 64
    assert tracker.queue_wait_seconds == 0.25
    # 自建线程池也要按配置来：线程数与线程名前缀是排障时看线程栈的依据。
    assert tracker.executor._max_workers == 2
    assert tracker.executor._thread_name_prefix == "thinkback-l3"

    tracker.shutdown()


def test_a_single_worker_is_allowed() -> None:
    """``workers=1`` 是合法值（串行写队列）。

    下界写成 ``< 2`` 就把单 worker 的部署挡在门外 —— 而那是合法的压测配置。
    """
    tracker = L3WriteExecutor(workers=1)

    assert tracker.workers == 1

    tracker.shutdown()


def test_a_single_pending_task_is_allowed() -> None:
    """``max_pending_tasks=1`` 是合法值（最小队列）。

    下界写成 ``< 2`` 会让「队列只有一个槽位」这种最小配置直接起不来。
    """
    tracker = L3WriteExecutor(max_pending_tasks=1)

    assert tracker.max_pending_tasks == 1

    tracker.shutdown()


def test_a_zero_queue_wait_is_allowed() -> None:
    """``queue_wait_seconds=0`` 是合法值：它就是「不等待，拿不到立刻失败」。

    L2 刷新走的是 append 请求线程，等 5 秒会把 append 变成慢请求 ——
    所以 0 必须可用，不能被判成非法。
    """
    tracker = L3WriteExecutor(queue_wait_seconds=0)

    assert tracker.queue_wait_seconds == 0

    tracker.shutdown()


# ---------------------------------------------------------------------------
# executor 生命周期：自有 vs 注入
# ---------------------------------------------------------------------------


def test_an_injected_executor_is_used_as_is() -> None:
    """注入的 executor 必须原样使用，不能被换成另一个。

    换成另一个 → caller 的限流 / 命名 / 关闭语义全部对不上。
    """
    injected = ThreadPoolExecutor(max_workers=1)
    tracker = L3WriteExecutor(executor=injected)

    assert tracker.executor is injected

    tracker.shutdown()
    injected.shutdown()


def test_shutdown_leaves_an_injected_executor_running() -> None:
    """注入的 executor 归 caller 管：``shutdown()`` 不能把它关掉。

    关掉之后 caller 再 submit 直接 RuntimeError —— 而 caller 注入它，
    就是为了自己接着用它做后续调度。
    """
    injected = ThreadPoolExecutor(max_workers=1)
    tracker = L3WriteExecutor(executor=injected)

    tracker.shutdown()

    assert tracker.shutdown_done is False
    future = injected.submit(lambda: 42)
    assert future.result(timeout=5) == 42

    injected.shutdown()


def test_shutdown_closes_an_owned_executor() -> None:
    """自建的 executor 必须被关掉，且关停标记为真。

    不关 → 进程退出时线程池的 atexit 钩子还在等任务；标记不置真 →
    关停状态对外不可见。
    """
    tracker = L3WriteExecutor(workers=1)

    tracker.shutdown()

    assert tracker.shutdown_done is True
    with pytest.raises(RuntimeError):
        tracker.executor.submit(lambda: 1)


def test_shutdown_without_waiting_returns_while_a_task_is_still_running() -> None:
    """``shutdown(wait=False)`` 立刻返回：不等在途任务。

    关停路径用它把「不等」显式写出来；写成 wait=True 会让关停卡在
    最慢的那个后台写上。
    """
    tracker = L3WriteExecutor(workers=1)
    release = threading.Event()
    tracker.executor.submit(release.wait, 5)

    started = time.monotonic()
    tracker.shutdown(wait=False)
    elapsed = time.monotonic() - started

    release.set()
    assert elapsed < 0.3, f"shutdown(wait=False) 却等到了任务结束：{elapsed:.3f}s"


# ---------------------------------------------------------------------------
# 槽位记账：只进不出 / 不多还 / 计数不为负
# ---------------------------------------------------------------------------


def test_reserving_and_releasing_move_the_pending_count_by_one() -> None:
    """计数是「在途后台写数」，每预留 +1、每归还 -1。"""
    tracker = L3WriteExecutor(workers=1, max_pending_tasks=4)

    assert tracker.pending_write_count() == 0
    tracker.reserve_write_slot()
    assert tracker.pending_write_count() == 1
    tracker.reserve_write_slot()
    assert tracker.pending_write_count() == 2
    tracker.release_write_slot()
    assert tracker.pending_write_count() == 1
    tracker.release_write_slot()
    assert tracker.pending_write_count() == 0

    tracker.shutdown()


def test_releasing_more_slots_than_reserved_never_makes_the_count_negative() -> None:
    """多还一次：计数不得变成 -1。

    计数变负 → 上层看到的「在途任务数」是负的 → 队列预算算错，
    监控面板上的 pending 也在说谎。
    """
    tracker = L3WriteExecutor(workers=1, max_pending_tasks=4)
    tracker.reserve_write_slot()
    tracker.release_write_slot()

    with pytest.raises(ValueError):
        tracker.release_write_slot()

    assert tracker.pending_write_count() == 0, "多还一次把计数扣成了负数"

    tracker.shutdown()


def test_reserving_from_a_full_queue_reports_the_pending_and_max_counts() -> None:
    """队列满时报错要带上「在途多少 / 上限多少」—— 那是运维定位的唯一线索。"""
    tracker = L3WriteExecutor(workers=1, max_pending_tasks=1, queue_wait_seconds=0)
    tracker.reserve_write_slot()
    _release_slot_later(tracker)

    with pytest.raises(RuntimeError, match=r"pending=1 max=1"):
        tracker.reserve_write_slot()

    tracker.shutdown()


# ---------------------------------------------------------------------------
# 等待时长：默认来自配置，显式 0 就是不等待
# ---------------------------------------------------------------------------


def _release_slot_later(tracker: L3WriteExecutor, delay: float = 0.5) -> None:
    """在 ``delay`` 秒后归还槽位（守护线程）。

    这是给「无限等」的实现准备的：正常实现等到 ``queue_wait_seconds``
    就放弃并报错；「无限等」的实现会一直等到槽位被归还 —— 于是那个
    RuntimeError 永远不抛，用例**失败**（而不是把审计挂死）。
    """

    def _release() -> None:
        time.sleep(delay)
        tracker.release_write_slot()

    threading.Thread(target=_release, daemon=True).start()


def test_the_default_reserve_waits_exactly_the_configured_time() -> None:
    """默认等待时长来自 ``queue_wait_seconds``。

    等 0 秒 → 立刻失败（append 变慢请求）；等太久 → append 被 LLM 队列拖住。
    """
    tracker = L3WriteExecutor(workers=1, max_pending_tasks=1, queue_wait_seconds=0.2)
    tracker.reserve_write_slot()
    _release_slot_later(tracker)

    started = time.monotonic()
    with pytest.raises(RuntimeError):
        tracker.reserve_write_slot()
    elapsed = time.monotonic() - started

    assert 0.15 <= elapsed < 2.0, f"默认等待时长不对：{elapsed:.3f}s"

    tracker.shutdown()


def test_the_default_reserve_never_waits_forever() -> None:
    """队列满时如果无限等，append 请求线程会被卡住 —— 整个写路径停摆。

    这里让另一个线程在 0.5 秒后归还槽位：正常实现会在 0.2 秒就放弃并报错；
    「无限等」的实现会一直等到拿到槽位 —— 那就永远等不到那个 RuntimeError。
    """
    tracker = L3WriteExecutor(workers=1, max_pending_tasks=1, queue_wait_seconds=0.2)
    tracker.reserve_write_slot()
    _release_slot_later(tracker)

    started = time.monotonic()
    with pytest.raises(RuntimeError):
        tracker.reserve_write_slot()
    elapsed = time.monotonic() - started

    assert 0.15 <= elapsed < 0.45, f"默认等待时长不对：{elapsed:.3f}s"

    tracker.shutdown()


def test_an_explicit_zero_wait_fails_immediately() -> None:
    """显式 ``wait_seconds=0`` → 不等待，拿不到立刻失败。

    L2 刷新的 reserve 就是这么调的（append 请求线程里等几秒 = 慢请求）。
    """
    tracker = L3WriteExecutor(workers=1, max_pending_tasks=1, queue_wait_seconds=1)
    tracker.reserve_write_slot()
    _release_slot_later(tracker)

    started = time.monotonic()
    with pytest.raises(RuntimeError):
        tracker.reserve_write_slot(wait_seconds=0)
    elapsed = time.monotonic() - started

    assert elapsed < 0.2, f"显式零等待却等了 {elapsed:.3f}s"

    tracker.shutdown()


# ---------------------------------------------------------------------------
# future 记账与 drain
# ---------------------------------------------------------------------------


def test_cleanup_count_tracks_the_registered_cleanup_futures() -> None:
    """已登记的清理 future 要能数得出来，也能摘掉。"""
    tracker = L3WriteExecutor(workers=1)
    assert tracker.cleanup_count() == 0

    gate = threading.Event()
    future = tracker.executor.submit(gate.wait, 5)
    tracker.track_cleanup(future, lambda _done: None)
    assert tracker.cleanup_count() == 1

    tracker.discard_cleanup(future)
    assert tracker.cleanup_count() == 0

    gate.set()
    tracker.shutdown()


def test_drain_gives_up_when_the_budget_is_exhausted_while_a_task_is_running() -> None:
    """预算耗尽时任务还在跑 → drain 立刻返回，不阻塞在取结果上。

    任务最终会结束（5 秒后），但 drain 的契约是「最多等 timeout」：
    阻塞在取结果上会把关停流程拖成不可预期时长。
    """
    tracker = L3WriteExecutor(workers=1)
    release = threading.Event()
    future = tracker.executor.submit(release.wait, 5)
    tracker.track_cleanup(future, lambda _done: None)

    started = time.monotonic()
    tracker.drain(timeout=0.05)
    elapsed = time.monotonic() - started

    release.set()
    assert elapsed < 0.3, f"drain 阻塞在未完成的任务上了：{elapsed:.3f}s"

    tracker.shutdown()


def test_track_write_registers_the_future_so_drain_waits_for_its_callback() -> None:
    """``track_write`` 必须真的登记 future 并挂上完成回调。

    这是 L3 后台**写**路径的入口（``track_cleanup`` 是清理路径）。
    漏登记 → drain 看不到这个 future → 关停时记账还没做完就返回了。
    """
    tracker = L3WriteExecutor(workers=1)
    finished = threading.Event()

    gate = threading.Event()
    future = tracker.executor.submit(gate.wait, 5)
    tracker.track_write(future, lambda _done: finished.set())
    assert future in tracker._write_futures, "track_write 没登记这个 future"
    gate.set()

    tracker.drain(timeout=5)

    assert finished.is_set(), "track_write 没挂上完成回调：drain 等不到它"

    tracker.shutdown()


def test_discard_write_removes_the_future_from_the_tracked_set() -> None:
    """``discard_write`` 要真的摘掉那个 future。

    摘不掉 → 关停时 drain 一直等一个已经结束的 future（记账表只增不减）。
    """
    tracker = L3WriteExecutor(workers=1)
    gate = threading.Event()
    future = tracker.executor.submit(gate.wait, 5)
    tracker.track_write(future, lambda _done: None)

    tracker.discard_write(future)

    assert future not in tracker._write_futures, "discard_write 没摘掉这个 future"

    gate.set()
    tracker.shutdown()


def test_drain_reports_a_failure_even_when_another_task_is_still_running() -> None:
    """一个 future 还没跑完时，另一个 future 的失败仍要报出来。

    收集错误时若遇到未完成的 future 就「跳出循环」，排在它后面的失败会被
    漏掉 —— drain 报成功，而实际有任务失败了。
    """
    tracker = L3WriteExecutor(workers=2)

    # 哈希钉死（见 _SlottedFuture）：未完成的 future 排在失败的 future
    # 前面 —— 正是漏报发生的顺序，而且每次运行都是这个顺序。
    pending = _SlottedFuture(slot=0)
    failing = _SlottedFuture(slot=1)
    failing.set_exception(ValueError("work failed"))
    tracker.track_write(pending, lambda _done: None)
    tracker.track_cleanup(failing, lambda _done: None)

    with pytest.raises(ValueError, match="work failed"):
        tracker.drain(timeout=0.2)

    tracker.shutdown()


def test_the_completion_callback_receives_the_future_it_belongs_to() -> None:
    """完成回调拿到的必须是**它自己的**那个 future。

    回调里要做的是记账（按 future 找任务状态）；拿到别的 future → 记到
    别的任务头上，或者直接 KeyError。
    """
    tracker = L3WriteExecutor(workers=1)
    seen: list[object] = []

    gate = threading.Event()
    future = tracker.executor.submit(gate.wait, 5)
    tracker.track_cleanup(future, seen.append)
    gate.set()

    tracker.drain(timeout=5)

    assert seen == [future], "完成回调拿到的不是它自己的 future"

    tracker.shutdown()


def test_the_callback_event_is_released_after_the_callback_runs() -> None:
    """回调跑完后它的事件要从登记表里摘掉。

    留着 → 每一次 drain 都要多等一个已经结束的事件（内存也只增不减）。
    """
    tracker = L3WriteExecutor(workers=1)

    gate = threading.Event()
    future = tracker.executor.submit(gate.wait, 5)
    tracker.track_cleanup(future, lambda _done: None)
    gate.set()

    tracker.drain(timeout=5)

    assert tracker._callback_events == {}, "回调事件没被摘掉：登记表只增不减"

    tracker.shutdown()


def test_shutdown_waits_for_running_tasks_by_default() -> None:
    """``shutdown()`` 默认要等到在途任务结束。

    不等 → 关停时后台写被砍在半路，任务状态永远收敛不了。
    """
    tracker = L3WriteExecutor(workers=1)
    finished = threading.Event()
    tracker.executor.submit(lambda: (time.sleep(0.2), finished.set()))

    tracker.shutdown()

    assert finished.is_set(), "shutdown() 没等在途任务结束"
