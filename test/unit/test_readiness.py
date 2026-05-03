import asyncio

import pytest

from infra import readiness


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
