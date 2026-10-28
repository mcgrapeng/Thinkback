from fastapi import FastAPI
from loguru import logger

from thinkback.api.app import create_app
from thinkback.infra import logging as logging_infra


def test_create_app_returns_fastapi_app() -> None:
    app = create_app(run_startup_tasks=False)

    assert isinstance(app, FastAPI)
    assert app.title == "thinkback Memory Service API"


def test_root_endpoint(client) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert response.json() == {
        "name": "thinkback",
        "version": "0.1.0",
        "status": "running",
        "scope": "memory-service",
    }


def test_health_endpoint(client) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "healthy",
        "version": "0.1.0",
        "environment": "development",
    }


def test_liveness_endpoint(client) -> None:
    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


def test_liveness_probe_does_not_emit_info_logs(client) -> None:
    sink: list[str] = []
    handler_id = logger.add(sink.append, level="INFO", format="{message} {extra}")
    logging_infra.logger.configure(patcher=logging_infra.patch_log_record)

    try:
        response = client.get("/health/live", headers={"X-Request-Id": "trace-health-live"})
    finally:
        logger.remove(handler_id)
        logging_infra.logger.configure(patcher=logging_infra.patch_log_record)

    assert response.status_code == 200
    logs = "\n".join(sink)
    assert "liveness check requested" not in logs
    assert "HTTP request completed" not in logs


def test_validation_error_does_not_echo_input(client) -> None:
    """校验失败时，响应体与日志都不得回显请求正文（含凭据）。"""
    secret = "sk-TEST-SECRET-VALUE"
    sink: list[str] = []
    handler_id = logger.add(sink.append, level="WARNING", format="{message}")

    try:
        response = client.post(
            "/v2/memory/events",
            json={
                "request_id": "req-validation-secret",
                "user_id": "u-validation-secret",
                "session_id": "s-validation-secret",
                "round_id": "r-validation-secret",
                # 时间戳非法 → 422；正文里带着 secret，响应与日志都不能回显它。
                "source_timestamp": "not-a-timestamp",
                "messages": [
                    {
                        "message_id": "m1",
                        "role": "user",
                        "content": secret,
                        "timestamp": "2026-10-27T10:00:00Z",
                    },
                ],
            },
        )
    finally:
        logger.remove(handler_id)

    assert response.status_code == 422
    assert secret not in response.text
    assert all(secret not in line for line in sink)


def test_trace_id_header_is_returned(client) -> None:
    response = client.get("/health", headers={"X-Request-Id": "req-tests"})

    assert response.status_code == 200
    assert response.headers["X-Trace-Id"] == "req-tests"


def test_readiness_endpoint_returns_ready(client, monkeypatch) -> None:
    async def fake_collect_readiness():
        return {
            "status": "ready",
            "dependencies": {
                "database": {"status": "ready", "detail": "ok"},
                "mem0": {"status": "ready", "detail": "ok"},
            },
        }

    monkeypatch.setattr("thinkback.api.health.collect_readiness", fake_collect_readiness)

    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_ready_probe_success_does_not_emit_info_logs(client, monkeypatch) -> None:
    async def fake_collect_readiness():
        return {
            "status": "ready",
            "dependencies": {
                "database": {"status": "ready", "detail": "ok"},
                "mem0": {"status": "ready", "detail": "ok"},
            },
        }

    monkeypatch.setattr("thinkback.api.health.collect_readiness", fake_collect_readiness)
    sink: list[str] = []
    handler_id = logger.add(sink.append, level="INFO", format="{message} {extra}")
    logging_infra.logger.configure(patcher=logging_infra.patch_log_record)

    try:
        response = client.get("/health/ready", headers={"X-Request-Id": "trace-health-ready"})
    finally:
        logger.remove(handler_id)
        logging_infra.logger.configure(patcher=logging_infra.patch_log_record)

    assert response.status_code == 200
    logs = "\n".join(sink)
    assert "readiness check requested" not in logs
    assert "readiness check completed" not in logs
    assert "HTTP request completed" not in logs


def test_readiness_endpoint_returns_503_when_dependency_is_not_ready(client, monkeypatch) -> None:
    async def fake_collect_readiness():
        return {
            "status": "not_ready",
            "dependencies": {
                "database": {"status": "ready", "detail": "ok"},
                "milvus": {"status": "not_ready", "detail": "connection refused"},
                "mem0": {"status": "ready", "detail": "ok"},
            },
        }

    monkeypatch.setattr("thinkback.api.health.collect_readiness", fake_collect_readiness)

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["dependencies"]["milvus"]["status"] == "not_ready"
