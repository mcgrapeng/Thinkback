"""进程内滑动窗口 metrics：append / recall 的最近 5min 速率 + 错误率。

设计动机：观察 P1「L3 队列水位」之外的另一个维度——5min 内有多少 append 在
跑、错误率多少；admin web Overview 直接消费（30s 轮询）。

实现：纯 in-memory（不持久化，重启清零），单进程粒度（多副本部署时聚合需
走 metrics 系统，本服务不在范围内）。append/recall 计数由 API 层在路由
上下文中挂钩，业务层 service.py 不感知。

S2 (P0-3 落地): 同时暴露 Prometheus 文本 exposition 格式（``/metrics``
端点），运维可在 Prometheus / VictoriaMetrics 端 scrape。计数器命名遵循
Prometheus 惯例（``thinkback_*`` 前缀，单位后缀 ``_total`` / ``_per_minute``）。
"""

from __future__ import annotations

import time
from collections import deque
from threading import Lock
from typing import Literal

WindowKind = Literal["append_ok", "append_fail", "recall_ok", "recall_fail"]

# 滑动窗口长度（秒）
WINDOW_SECONDS = 300.0


class MetricsRegistry:
    """线程安全滑动窗口 metrics：append / recall × success / failure。"""

    def __init__(self, window_seconds: float = WINDOW_SECONDS) -> None:
        self.window_seconds = window_seconds
        self._buckets: dict[WindowKind, deque[float]] = {
            "append_ok": deque(),
            "append_fail": deque(),
            "recall_ok": deque(),
            "recall_fail": deque(),
        }
        self._lock = Lock()

    def _prune_locked(self, kind: WindowKind, now: float) -> None:
        cutoff = now - self.window_seconds
        bucket = self._buckets[kind]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()

    def record(self, kind: WindowKind, *, at: float | None = None) -> None:
        now = at if at is not None else time.monotonic()
        with self._lock:
            self._prune_locked(kind, now)
            self._buckets[kind].append(now)

    def snapshot(self, *, now: float | None = None) -> dict[str, dict[str, float]]:
        """返回各 kind 的窗口内计数 + 5min 内速率（per minute）。

        速率 = count / window_seconds * 60；0 → 0。
        """
        now = now if now is not None else time.monotonic()
        return self._snapshot_at(now)

    def _snapshot_at(self, now: float) -> dict[str, dict[str, float]]:
        result: dict[str, dict[str, float]] = {}
        with self._lock:
            for kind, bucket in self._buckets.items():
                self._prune_locked(kind, now)
                count = len(bucket)
                rate = round(count / self.window_seconds * 60, 1) if count else 0
                result[kind] = {"count": count, "per_minute": rate}
        return result

    def render_prometheus(self, *, now: float | None = None) -> str:
        """S2: 输出 Prometheus 文本 exposition 格式。

        标准 Counter + per-window metric。零依赖、纯文本。
        """
        if now is None:
            now = time.monotonic()
        snap = self._snapshot_at(now)
        lines: list[str] = []
        # HELP / TYPE 头各仅加一次。
        lines.append("# HELP thinkback_request_total Request outcomes (since process start).")
        lines.append("# TYPE thinkback_request_total counter")
        for kind, value in snap.items():
            lines.append(f'thinkback_request_total{{kind="{kind}"}} {int(value["count"])}')
        lines.append("# HELP thinkback_request_per_minute Rolling 5min per-minute rate.")
        lines.append("# TYPE thinkback_request_per_minute gauge")
        for kind, value in snap.items():
            lines.append(f'thinkback_request_per_minute{{kind="{kind}"}} {value["per_minute"]}')
        return "\n".join(lines) + "\n"

    def _snapshot_text(self, *, now: float | None = None) -> str:
        """测试钩子：render_prometheus 但显式传时间。"""
        if now is None:
            now = time.monotonic()
        return self.render_prometheus(now=now)


# 进程单例
_REGISTRY = MetricsRegistry()


def record_event(kind: WindowKind) -> None:
    """路由层调用：成功 / 失败事件入桶。"""

    _REGISTRY.record(kind)


def snapshot() -> dict[str, dict[str, float]]:
    """Overview / admin 端点消费：4 个 kind 的窗口计数 + 速率。"""

    return _REGISTRY.snapshot()


def render_prometheus() -> str:
    """``/metrics`` 端点消费：Prometheus 文本 exposition 格式。"""

    return _REGISTRY.render_prometheus()
