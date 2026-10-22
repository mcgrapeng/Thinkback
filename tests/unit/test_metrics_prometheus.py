"""S2 regression tests: Prometheus exposition format + /metrics route."""

from __future__ import annotations

from fastapi.testclient import TestClient

from thinkback.api.app import create_app
from thinkback.api.metrics import (
    MetricsRegistry,
    record_event,
)


def test_render_prometheus_includes_help_type_and_series() -> None:
    """S2: 计数器命名遵循 thinkback_request_total，HELP/TYPE 头齐全。"""
    registry = MetricsRegistry()
    now = 1000.0
    registry.record("append_ok", at=now)
    registry.record("append_fail", at=now)
    registry.record("recall_ok", at=now)
    out = registry.render_prometheus(now=now)

    assert "# HELP thinkback_request_total" in out
    assert "# TYPE thinkback_request_total counter" in out
    assert "# HELP thinkback_request_per_minute" in out
    assert "# TYPE thinkback_request_per_minute gauge" in out
    assert 'thinkback_request_total{kind="append_ok"} 1' in out
    assert 'thinkback_request_total{kind="append_fail"} 1' in out
    assert 'thinkback_request_total{kind="recall_ok"} 1' in out


def test_render_prometheus_exposes_all_kinds_even_with_zero_count() -> None:
    """S2: 0 计数也输出（Prometheus 期望：时间序列始终存在便于聚合）。"""
    registry = MetricsRegistry()
    out = registry.render_prometheus()
    for kind in ("append_ok", "append_fail", "recall_ok", "recall_fail"):
        assert f'thinkback_request_total{{kind="{kind}"}} 0' in out


def test_request_total_counter_is_monotonic_across_window_boundary() -> None:
    """counter 只能增不能减：跨过 5 分钟窗口边界不得归零。

    旧实现把 300s 滑动窗口的计数当 counter 暴露，每 5 分钟归零一次 ——
    Prometheus 的 rate()/increase() 会把它当成进程重启，错误率与 SLO 全错。
    """
    registry = MetricsRegistry()
    registry.record("append_ok", at=1000.0)
    registry.record("append_ok", at=1001.0)

    early = registry.render_prometheus(now=1001.0)
    assert 'thinkback_request_total{kind="append_ok"} 2' in early

    late = registry.render_prometheus(now=1400.0)
    assert 'thinkback_request_total{kind="append_ok"} 2' in late
    # 窗口计数确实归零（窗口是滑动的），但 counter 不归零。
    assert 'thinkback_request_window_5m{kind="append_ok"} 0' in late


def test_metrics_endpoint_returns_prometheus_text() -> None:
    """S2: GET /metrics 返回 text/plain; version=0.0.4 格式。"""
    app = create_app()
    record_event("append_ok")
    with TestClient(app) as client:
        response = client.get("/health/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "thinkback_request_total" in response.text
