import asyncio

import pytest

from infra import readiness
from infra.config import Settings


@pytest.mark.asyncio
async def test_collect_readiness_marks_timed_out_dependency_not_ready(monkeypatch) -> None:
    async def slow_database_check() -> dict[str, str]:
        await asyncio.sleep(1)
        return {"status": "ready", "detail": "late"}

    async def ready_check() -> dict[str, str]:
        return {"status": "ready", "detail": "ok"}

    monkeypatch.setattr(readiness, "check_database", slow_database_check)
    monkeypatch.setattr(readiness, "check_redis", ready_check)
    monkeypatch.setattr(readiness, "check_qdrant", ready_check)

    payload = await readiness.collect_readiness(timeout_seconds=0.01)

    assert payload["status"] == "not_ready"
    assert payload["dependencies"]["database"] == {
        "status": "not_ready",
        "detail": "timed out after 0.01s",
    }
    assert payload["dependencies"]["redis"] == {"status": "ready", "detail": "ok"}
    assert payload["dependencies"]["qdrant"] == {"status": "ready", "detail": "ok"}


@pytest.mark.asyncio
async def test_collect_readiness_checks_mem0_api_in_http_mode_instead_of_qdrant(monkeypatch) -> None:
    async def ready_check() -> dict[str, str]:
        return {"status": "ready", "detail": "ok"}

    async def qdrant_should_not_run() -> dict[str, str]:
        raise AssertionError("http_api mode must not require direct qdrant readiness")

    monkeypatch.setattr(
        readiness,
        "settings",
        Settings(mem0_backend_mode="http_api", mem0_api_url="https://mem0.example.internal"),
    )
    monkeypatch.setattr(readiness, "check_database", ready_check)
    monkeypatch.setattr(readiness, "check_redis", ready_check)
    monkeypatch.setattr(readiness, "check_mem0_api", ready_check)
    monkeypatch.setattr(readiness, "check_qdrant", qdrant_should_not_run)

    payload = await readiness.collect_readiness()

    assert payload["status"] == "ready"
    assert payload["dependencies"]["mem0"] == {"status": "ready", "detail": "ok"}
    assert "qdrant" not in payload["dependencies"]


@pytest.mark.asyncio
async def test_collect_readiness_checks_qdrant_in_local_sdk_mode(monkeypatch) -> None:
    async def ready_check() -> dict[str, str]:
        return {"status": "ready", "detail": "ok"}

    async def mem0_should_not_run() -> dict[str, str]:
        raise AssertionError("local_sdk mode must not call remote mem0 readiness")

    monkeypatch.setattr(readiness, "settings", Settings(mem0_backend_mode="local_sdk"))
    monkeypatch.setattr(readiness, "check_database", ready_check)
    monkeypatch.setattr(readiness, "check_redis", ready_check)
    monkeypatch.setattr(readiness, "check_qdrant", ready_check)
    monkeypatch.setattr(readiness, "check_mem0_api", mem0_should_not_run)

    payload = await readiness.collect_readiness()

    assert payload["status"] == "ready"
    assert payload["dependencies"]["qdrant"] == {"status": "ready", "detail": "ok"}
    assert "mem0" not in payload["dependencies"]
