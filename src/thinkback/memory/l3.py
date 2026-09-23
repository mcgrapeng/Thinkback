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
from threading import BoundedSemaphore, Lock
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

    def reserve_write_slot(self) -> None:
        """为后台 L3 写入预留一个队列槽位，队列满时快速失败。"""

        acquired = self._capacity.acquire(timeout=self.queue_wait_seconds)
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

        with self._lock:
            self._write_futures.add(future)
        future.add_done_callback(on_finished)

    def discard_write(self, future: Future[Any]) -> None:
        with self._lock:
            self._write_futures.discard(future)

    def track_cleanup(
        self, future: Future[Any], on_finished: Callable[[Future[Any]], None]
    ) -> None:
        """登记后台清理 future 并挂完成回调。"""

        with self._lock:
            self._cleanup_futures.add(future)
        future.add_done_callback(on_finished)

    def discard_cleanup(self, future: Future[Any]) -> None:
        with self._lock:
            self._cleanup_futures.discard(future)

    def cleanup_count(self) -> int:
        with self._lock:
            return len(self._cleanup_futures)

    def drain(self, timeout: float | None = None) -> None:
        """等待所有在途后台写/清理 future 完成，向上传播异常。"""

        with self._lock:
            futures = list(self._write_futures | self._cleanup_futures)
        if futures:
            wait(futures, timeout=timeout)
            for future in futures:
                if future.done():
                    future.result()

    def shutdown(self, *, wait: bool = True) -> None:
        """关闭自有的 executor（幂等；注入的 executor 归 caller 管理）。"""

        if not self.owns_executor or self.shutdown_done:
            return
        self.executor.shutdown(wait=wait)
        self.shutdown_done = True
