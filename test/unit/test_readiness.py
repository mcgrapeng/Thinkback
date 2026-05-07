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
    monkeypatch.setattr(readiness, "check_mem0_api", ready_check)

    payload = await readiness.collect_readiness(timeout_seconds=0.01)

    assert payload["status"] == "not_ready"
    assert payload["dependencies"]["database"] == {
        "status": "not_ready",
        "detail": "timed out after 0.01s",
    }
    assert payload["dependencies"]["redis"] == {"status": "ready", "detail": "ok"}
    assert payload["dependencies"]["qdrant"] == {"status": "ready", "detail": "ok"}
    assert payload["dependencies"]["mem0"] == {"status": "ready", "detail": "ok"}


@pytest.mark.asyncio
async def test_collect_readiness_checks_mem0_api_and_shared_qdrant(monkeypatch) -> None:
    async def ready_check() -> dict[str, str]:
        return {"status": "ready", "detail": "ok"}

    monkeypatch.setattr(readiness, "settings", Settings(mem0_api_url="https://mem0.example.internal"))
    monkeypatch.setattr(readiness, "check_database", ready_check)
    monkeypatch.setattr(readiness, "check_redis", ready_check)
    monkeypatch.setattr(readiness, "check_qdrant", ready_check)
    monkeypatch.setattr(readiness, "check_mem0_api", ready_check)

    payload = await readiness.collect_readiness()

    assert payload["status"] == "ready"
    assert payload["dependencies"]["qdrant"] == {"status": "ready", "detail": "ok"}
    assert payload["dependencies"]["mem0"] == {"status": "ready", "detail": "ok"}


@pytest.mark.asyncio
async def test_check_qdrant_uses_shared_endpoint_for_diagnostics_only(monkeypatch) -> None:
    calls: list[dict[str, str]] = []

    class FakeQdrantClient:
        def __init__(
            self, *, url: str, api_key: str | None = None, timeout_seconds: float = 2.0
        ) -> None:
            _ = timeout_seconds
            calls.append({"url": url, "api_key": api_key or ""})

        def get_collections(self) -> object:
            return object()

    monkeypatch.setattr(readiness, "QdrantClient", FakeQdrantClient)
    monkeypatch.setattr(
        readiness,
        "settings",
        Settings(qdrant_url="https://qdrant.example.internal", qdrant_api_key="qdrant-secret"),
    )

    payload = await readiness.check_qdrant()

    assert payload == {"status": "ready", "detail": "ok"}
    assert calls == [{"url": "https://qdrant.example.internal", "api_key": "qdrant-secret"}]
