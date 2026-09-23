import asyncio

import pytest

from thinkback.infra import readiness
from thinkback.infra.config import Settings


@pytest.mark.asyncio
async def test_collect_readiness_marks_timed_out_dependency_not_ready(monkeypatch) -> None:
    async def slow_database_check() -> dict[str, str]:
        await asyncio.sleep(1)
        return {"status": "ready", "detail": "late"}

    async def ready_check() -> dict[str, str]:
        return {"status": "ready", "detail": "ok"}

    monkeypatch.setattr(readiness, "check_database", slow_database_check)
    monkeypatch.setattr(readiness, "check_milvus", ready_check)
    monkeypatch.setattr(readiness, "check_mem0_library", ready_check)

    payload = await readiness.collect_readiness(timeout_seconds=0.01)

    assert payload["status"] == "not_ready"
    assert payload["dependencies"]["database"] == {
        "status": "not_ready",
        "detail": "timed out after 0.01s",
    }
    assert payload["dependencies"]["milvus"] == {"status": "ready", "detail": "ok"}
    assert payload["dependencies"]["mem0"] == {"status": "ready", "detail": "ok"}


@pytest.mark.asyncio
async def test_collect_readiness_checks_mem0_library_and_milvus(monkeypatch) -> None:
    async def ready_check() -> dict[str, str]:
        return {"status": "ready", "detail": "ok"}

    monkeypatch.setattr(
        readiness,
        "settings",
        Settings(
            openai_api_key="openai-secret",
            milvus_url="http://milvus.example.internal:19530",
        ),
    )
    monkeypatch.setattr(readiness, "check_database", ready_check)
    monkeypatch.setattr(readiness, "check_milvus", ready_check)
    monkeypatch.setattr(readiness, "check_mem0_library", ready_check)

    payload = await readiness.collect_readiness()

    assert payload["status"] == "ready"
    assert payload["dependencies"]["milvus"] == {"status": "ready", "detail": "ok"}
    assert payload["dependencies"]["mem0"] == {"status": "ready", "detail": "ok"}


@pytest.mark.asyncio
async def test_collect_readiness_uses_configured_timeout_by_default(monkeypatch) -> None:
    async def slow_but_ready_check() -> dict[str, str]:
        await asyncio.sleep(0.02)
        return {"status": "ready", "detail": "ok"}

    monkeypatch.setattr(
        readiness,
        "settings",
        Settings(readiness_timeout_seconds=0.001),
    )
    monkeypatch.setattr(readiness, "check_database", slow_but_ready_check)
    monkeypatch.setattr(readiness, "check_milvus", slow_but_ready_check)
    monkeypatch.setattr(readiness, "check_mem0_library", slow_but_ready_check)

    payload = await readiness.collect_readiness()

    assert payload["status"] == "not_ready"
    assert payload["dependencies"]["mem0"]["detail"] == "timed out after 0.001s"


@pytest.mark.asyncio
async def test_check_milvus_uses_configured_endpoint_for_diagnostics_only(monkeypatch) -> None:
    calls: list[dict[str, str]] = []

    class FakeMilvusClient:
        def __init__(self, *, uri: str, token: str, db_name: str) -> None:
            calls.append({"uri": uri, "token": token, "db_name": db_name})

        def list_collections(self) -> object:
            return []

    monkeypatch.setattr(readiness, "MilvusClient", FakeMilvusClient)
    monkeypatch.setattr(
        readiness,
        "settings",
        Settings(
            openai_api_key="openai-secret",
            milvus_url="http://milvus.example.internal:19530",
            milvus_user="milvus-user",
            milvus_password="milvus-secret",
            milvus_database="innies",
        ),
    )

    payload = await readiness.check_milvus()

    assert payload == {"status": "ready", "detail": "ok"}
    assert calls == [
        {
            "uri": "http://milvus.example.internal:19530",
            "token": "milvus-user:milvus-secret",
            "db_name": "innies",
        }
    ]


@pytest.mark.asyncio
async def test_check_mem0_library_does_not_write_probe_memory(monkeypatch) -> None:
    """readiness check uses a lightweight OpenAI probe, not a full Mem0 backend."""

    class FakeOpenAIClient:
        closed = False

        def __init__(self, **kwargs: object) -> None:
            _ = kwargs

        class _Models:
            @staticmethod
            def list() -> list[str]:
                return ["model-a"]

        models = _Models()

        def close(self) -> None:
            FakeOpenAIClient.closed = True

    monkeypatch.setattr(readiness, "settings", Settings(openai_api_key="openai-secret"))

    import openai as _openai_module

    monkeypatch.setattr(_openai_module, "OpenAI", FakeOpenAIClient)

    payload = await readiness.check_mem0_library()

    assert payload == {"status": "ready", "detail": "configured"}
    assert FakeOpenAIClient.closed


@pytest.mark.asyncio
async def test_l3_dependencies_skipped_when_openai_api_key_missing(monkeypatch) -> None:
    """本地最小依赖闭环：未配 OPENAI_API_KEY 时 mem0 / milvus 自动跳过，readiness 仍 ready。"""
    monkeypatch.setattr(readiness, "settings", Settings(openai_api_key=""))

    class FailMilvusClient:
        def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            raise AssertionError("milvus client must not be constructed when L3 is disabled")

    monkeypatch.setattr(readiness, "MilvusClient", FailMilvusClient)

    mem0_payload = await readiness.check_mem0_library()
    milvus_payload = await readiness.check_milvus()

    assert mem0_payload["status"] == "ready"
    assert "skipped" in mem0_payload["detail"]
    assert milvus_payload["status"] == "ready"
    assert "skipped" in milvus_payload["detail"]


@pytest.mark.asyncio
async def test_production_readiness_fails_when_openai_api_key_missing(monkeypatch) -> None:
    """生产环境缺 L3 LLM 凭据时不能把 Mem0/Milvus 依赖标成 ready。"""
    monkeypatch.setattr(
        readiness, "settings", Settings(environment="production", openai_api_key="")
    )

    class FailMilvusClient:
        def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError(
                "milvus client must not be constructed when L3 credentials missing"
            )

    monkeypatch.setattr(readiness, "MilvusClient", FailMilvusClient)

    mem0_payload = await readiness.check_mem0_library()
    milvus_payload = await readiness.check_milvus()

    assert mem0_payload["status"] == "not_ready"
    assert "OPENAI_API_KEY" in mem0_payload["detail"]
    assert milvus_payload["status"] == "not_ready"
    assert "OPENAI_API_KEY" in milvus_payload["detail"]


def test_readiness_engine_is_isolated_from_business_pool() -> None:
    """R-4 回归：readiness 探测必须使用独立 NullPool 引擎。

    修复前 check_database 在 uvicorn 主循环上从共享业务池 checkout 连接，
    归还后连接仍绑定主循环；业务仓储的专属循环 checkout 到该连接时
    pre_ping 抛 "Future attached to a different loop" → 随机 500
    （k8s readinessProbe 周期探测会持续制造错绑连接）。
    """
    from sqlalchemy.pool import NullPool

    from thinkback.infra.database import engine as engine_module

    assert engine_module.readiness_engine is not engine_module.engine
    assert engine_module.readiness_engine.pool.__class__ is NullPool
