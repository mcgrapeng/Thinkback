from fastapi import FastAPI

from api.app import create_app


def test_create_app_returns_fastapi_app() -> None:
    app = create_app()

    assert isinstance(app, FastAPI)
    assert app.title == "Thinkback"


def test_root_endpoint(client) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert response.json() == {
        "name": "Thinkback",
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
                    "redis": {"status": "ready", "detail": "ok"},
                    "mem0": {"status": "ready", "detail": "ok"},
                },
            }

    monkeypatch.setattr("api.health.collect_readiness", fake_collect_readiness)

    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_readiness_endpoint_returns_503_when_dependency_is_not_ready(client, monkeypatch) -> None:
    async def fake_collect_readiness():
        return {
            "status": "not_ready",
                "dependencies": {
                    "database": {"status": "ready", "detail": "ok"},
                    "redis": {"status": "not_ready", "detail": "connection refused"},
                    "mem0": {"status": "ready", "detail": "ok"},
                },
            }

    monkeypatch.setattr("api.health.collect_readiness", fake_collect_readiness)

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["dependencies"]["redis"]["status"] == "not_ready"
