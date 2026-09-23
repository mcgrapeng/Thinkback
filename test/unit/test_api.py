from fastapi import FastAPI
from loguru import logger

from thinkback.api.app import create_app
from thinkback.infra import logging as logging_infra


def test_create_app_returns_fastapi_app() -> None:
    app = create_app()

    assert isinstance(app, FastAPI)
    assert app.title == "innies-memory Memory Service API"


def test_root_endpoint(client) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert response.json() == {
        "name": "innies-memory",
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


def test_trace_id_header_is_returned(client) -> None:
    response = client.get("/health", headers={"X-Request-Id": "req-test"})

    assert response.status_code == 200
    assert response.headers["X-Trace-Id"] == "req-test"


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
