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
