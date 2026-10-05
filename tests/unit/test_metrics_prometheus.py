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


def test_metrics_endpoint_returns_prometheus_text() -> None:
    """S2: GET /metrics 返回 text/plain; version=0.0.4 格式。"""
    app = create_app()
    record_event("append_ok")
    with TestClient(app) as client:
        response = client.get("/health/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "thinkback_request_total" in response.text
