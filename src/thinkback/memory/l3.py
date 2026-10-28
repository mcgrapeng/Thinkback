"""L3 后台写基础设施：线程池生命周期、队列容量限流、future 记账。

从 ``MemoryService`` 提取的纯基础设施关注点（无业务语义）：

- executor 生命周期：自建（own）或 caller 注入，幂等 shutdown；
  注入的 executor 不由本类关闭，避免 double-shutdown。
- 队列容量：``BoundedSemaphore`` 限流 + pending 槽位计数，队列满时
  快速失败（RuntimeError），防止后台写队列无界膨胀。
- future 记账：background write / cleanup 两类 future 集合，
  ``drain`` 等待并向上传播首个异常。

业务回调（任务状态流转、L3 写入流程）仍留在 ``MemoryService``，
通过 ``track_write`` / ``track_cleanup`` 的完成回调挂回。
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from threading import BoundedSemaphore, Event, Lock
from time import monotonic
from typing import Any

from loguru import logger


class L3WriteExecutor:
    """L3 后台写的执行器封装：容量、生命周期与 future 记账。

    线程安全：所有可变状态由 ``_lock`` 保护；semaphore 独立计数。
    """

    def __init__(
        self,
        *,
        executor: ThreadPoolExecutor | None = None,
        workers: int = 2,
        max_pending_tasks: int = 64,
        queue_wait_seconds: float = 0.25,
        thread_name_prefix: str = "thinkback-l3",
    ) -> None:
        # 校验文案沿用 MemoryService 构造参数名（对外报错面保持不变）。
        if workers < 1:
            raise ValueError("l3_executor_workers must be >= 1")
        if max_pending_tasks < 1:
            raise ValueError("l3_max_pending_tasks must be >= 1")
        if queue_wait_seconds < 0:
            raise ValueError("l3_queue_wait_seconds must be >= 0")
        self.workers = workers
        self.max_pending_tasks = max_pending_tasks
        self.queue_wait_seconds = queue_wait_seconds
        self.executor = executor or ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix=thread_name_prefix,
        )
        # 跟踪是否由本类拥有 executor 的生命周期。caller 注入的自定义
        # executor 由 caller 管理，shutdown() 不应主动关闭（避免
        # double-shutdown / 副作用）。
        self.owns_executor = executor is None
        self.shutdown_done = False
        self._capacity = BoundedSemaphore(max_pending_tasks)
        self._lock = Lock()
        self._pending_write_slots = 0
        self._write_futures: set[Future[Any]] = set()
        self._cleanup_futures: set[Future[Any]] = set()
        # 完成回调的完成信号：future done ≠ 回调跑完（见 drain 的说明）。
        self._callback_events: dict[Future[Any], Event] = {}
        # 回调（记账）失败：drain 要把它抛出去，不能吞掉后报告成功。
        self._callback_errors: dict[Future[Any], BaseException] = {}

    def reserve_write_slot(self, wait_seconds: float | None = None) -> None:
        """为后台 L3 写入预留一个队列槽位，队列满时快速失败。

        ``wait_seconds`` 为 0 时不等待（拿不到就失败）：L2 刷新走的是
        append 请求线程，等 5 秒会把 append 变成慢请求。
        """

        timeout = self.queue_wait_seconds if wait_seconds is None else wait_seconds
        acquired = self._capacity.acquire(timeout=timeout)
        if not acquired:
            pending = self.pending_write_count()
            logger.bind(
                pending_write_tasks=pending,
                max_pending_tasks=self.max_pending_tasks,
                queue_wait_seconds=self.queue_wait_seconds,
            ).warning("memory l3 background queue full")
            raise RuntimeError(
                f"l3 background queue full: pending={pending} max={self.max_pending_tasks}"
            )
        with self._lock:
            self._pending_write_slots += 1
            pending = self._pending_write_slots
        logger.bind(
            pending_write_tasks=pending,
            max_pending_tasks=self.max_pending_tasks,
        ).info("memory l3 background slot reserved")

    def release_write_slot(self) -> None:
        with self._lock:
            if self._pending_write_slots > 0:
                self._pending_write_slots -= 1
            pending = self._pending_write_slots
        self._capacity.release()
        logger.bind(
            pending_write_tasks=pending,
            max_pending_tasks=self.max_pending_tasks,
        ).info("memory l3 background slot released")

    def pending_write_count(self) -> int:
        with self._lock:
            return self._pending_write_slots

    def track_write(self, future: Future[Any], on_finished: Callable[[Future[Any]], None]) -> None:
        """登记后台写 future 并挂完成回调（回调负责 discard_write + 释放槽位）。"""

        self._track(future, on_finished, self._write_futures)

    def discard_write(self, future: Future[Any]) -> None:
        with self._lock:
            self._write_futures.discard(future)

    def track_cleanup(
        self, future: Future[Any], on_finished: Callable[[Future[Any]], None]
    ) -> None:
        """登记后台清理 future 并挂完成回调。"""

        self._track(future, on_finished, self._cleanup_futures)

    def _track(
        self,
        future: Future[Any],
        on_finished: Callable[[Future[Any]], None],
        target: set[Future[Any]],
    ) -> None:
        """登记 future 与它的完成回调事件（同一把锁里一次做完）。

        事件必须在 future 入册的同一次加锁里登记：分两次加锁会留下窗口 ——
        drain 可能看到 future 却看不到它的事件，于是等 future 完成后直接返回，
        完成回调（记账）还没跑。

        ``concurrent.futures`` 在 worker 线程里 ``set_result`` 之后才执行
        done callback，而 ``wait()`` 在 future done 时就返回 —— 两者之间有
        窗口。业务回调里做的是记账（pending 计数递减、清理结束日志），
        早退会让调用方看到半完成状态。
        """

        event = Event()
        with self._lock:
            target.add(future)
            self._callback_events[future] = event

        def runner(done: Future[Any]) -> None:
            try:
                on_finished(done)
            except BaseException as exc:  # noqa: BLE001 - 记账失败必须让 drain 看见
                with self._lock:
                    self._callback_errors[done] = exc
                raise
            finally:
                event.set()
                with self._lock:
                    self._callback_events.pop(done, None)

        future.add_done_callback(runner)

    def discard_cleanup(self, future: Future[Any]) -> None:
        with self._lock:
            self._cleanup_futures.discard(future)

    def cleanup_count(self) -> int:
        with self._lock:
            return len(self._cleanup_futures)

    def drain(self, timeout: float | None = None) -> None:
        """等待所有在途后台写/清理 future 完成，并等完成回调跑完。

        只等 future 不够：done callback 是 worker 线程在 ``set_result``
        之后才执行的，业务回调里的记账（pending 计数递减、清理结束日志）
        可能落在 drain 返回之后，调用方会看到半完成状态。

        ``timeout`` 是总预算：等 future 与等回调共用同一份时间。

        失败不吞：
        - future 自己的异常照旧向上抛（只是推迟到回调跑完之后）；
        - 回调（记账）失败也向上抛 —— 记账没做成却报告成功，比报错更糟；
        - 预算耗尽仍有回调没跑完：记 error 日志，不假装保证成立。
        """

        deadline = None if timeout is None else monotonic() + timeout
        with self._lock:
            futures = list(self._write_futures | self._cleanup_futures)
            events = list(self._callback_events.values())

        errors: list[BaseException] = []
        if futures:
            wait(futures, timeout=self._remaining(deadline))
            for future in futures:
                if not future.done():
                    continue
                try:
                    future.result()
                except BaseException as exc:  # noqa: BLE001 - 推迟到回调跑完后抛
                    errors.append(exc)

        pending = 0
        for event in events:
            if not event.wait(timeout=self._remaining(deadline)):
                pending += 1
        if pending:
            logger.bind(pending_callbacks=pending).error(
                "memory l3 drain timed out before completion callbacks finished"
            )

        with self._lock:
            callback_errors = list(self._callback_errors.values())
            self._callback_errors.clear()
        errors.extend(callback_errors)
        if errors:
            raise errors[0]

    @staticmethod
    def _remaining(deadline: float | None) -> float | None:
        """剩余预算：``None`` 表示不限时；用完就是 0（不阻塞）。"""

        if deadline is None:
            return None
        return max(0.0, deadline - monotonic())

    def shutdown(self, *, wait: bool = True) -> None:
        """关闭自有的 executor（幂等；注入的 executor 归 caller 管理）。"""

        if not self.owns_executor or self.shutdown_done:
            return
        self.executor.shutdown(wait=wait)
        self.shutdown_done = True
