"""metrics.py 滑动窗口单元测试：append/recall × success/failure 的 5min 计数 + per-minute 速率。"""

from __future__ import annotations

import pytest

from thinkback.api.metrics import MetricsRegistry


def test_metrics_registry_starts_empty() -> None:
    reg = MetricsRegistry(window_seconds=300.0)
    snap = reg.snapshot()
    for kind in ("append_ok", "append_fail", "recall_ok", "recall_fail"):
        assert snap[kind] == {"count": 0, "per_minute": 0}


def test_metrics_registry_counts_within_window() -> None:
    """5min 内的事件都被记录，count 与 per_minute 正确。"""
    reg = MetricsRegistry(window_seconds=300.0)
    base = 1000.0
    for _ in range(10):
        reg.record("append_ok", at=base)
        base += 1
    for _ in range(2):
        reg.record("append_fail", at=base)
        base += 1
    # 查询时 at = base + 1 → 所有事件都在 5min 内
    snap = reg.snapshot(now=base + 1)
    assert snap["append_ok"]["count"] == 10
    assert snap["append_fail"]["count"] == 2
    # per_minute = count / 300 * 60 = count / 5
    assert snap["append_ok"]["per_minute"] == 2.0
    assert snap["append_fail"]["per_minute"] == 0.4


def test_metrics_registry_prunes_outside_window() -> None:
    """超过 window_seconds 的事件被剪掉，count 不累计。"""
    reg = MetricsRegistry(window_seconds=60.0)
    # 在 t=0 写 5 条
    for _ in range(5):
        reg.record("append_ok", at=0.0)
    # 在 t=30 写 3 条（窗口内）
    for _ in range(3):
        reg.record("append_ok", at=30.0)
    # 在 t=200 写 2 条（前面 5 条全部过期）
    for _ in range(2):
        reg.record("append_ok", at=200.0)
    snap = reg.snapshot(now=200.0)
    # 200 - 60 = 140：t=0 的 5 条不在窗口
    # 200 - 60 = 140：t=30 的 3 条不在窗口（30 < 140）
    # t=200 的 2 条在窗口
    assert snap["append_ok"]["count"] == 2
    # per_minute = 2 / 60 * 60 = 2.0
    assert snap["append_ok"]["per_minute"] == 2.0


def test_metrics_registry_thread_safe() -> None:
    """并发写入不丢数。"""
    import threading

    reg = MetricsRegistry(window_seconds=300.0)
    base = 0.0

    def writer() -> None:
        for _ in range(100):
            reg.record("append_ok", at=base)

    threads = [threading.Thread(target=writer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    snap = reg.snapshot(now=base + 1)
    # 4 threads × 100 writes = 400
    assert snap["append_ok"]["count"] == 400


def test_metrics_registry_per_kind_isolation() -> None:
    """4 个 kind 互相独立，互不串扰。"""
    reg = MetricsRegistry(window_seconds=300.0)
    reg.record("append_ok", at=0.0)
    reg.record("append_fail", at=0.0)
    reg.record("recall_ok", at=0.0)
    reg.record("recall_fail", at=0.0)
    snap = reg.snapshot(now=1.0)
    assert snap["append_ok"]["count"] == 1
    assert snap["append_fail"]["count"] == 1
    assert snap["recall_ok"]["count"] == 1
    assert snap["recall_fail"]["count"] == 1


def test_metrics_registry_zero_events_zero_rate() -> None:
    """空窗口的 per_minute 必须为 0 而不是被 0 除。"""
    reg = MetricsRegistry(window_seconds=300.0)
    snap = reg.snapshot()
    for kind in snap.values():
        assert kind["per_minute"] == 0
        assert kind["count"] == 0
