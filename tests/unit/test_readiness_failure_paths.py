"""readiness 探针的失败路径：探针出错必须报 not_ready，不能抛异常。

readiness 端点挂了就等于 Pod 不再接流量。探针函数抛异常 → 端点 500 →
K8s 拿不到 503 的语义；探针返回 not_ready → 端点 503 → Pod 被摘流。
两者对流量处理完全不同，所以失败路径必须单独钉住。

另外两条硬要求：
- Milvus 探针失败后客户端必须关闭（否则连接泄漏）；
- 依赖卡死要按超时算 not_ready，不能把 readiness 请求拖成挂起。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from thinkback.infra import readiness
from thinkback.infra.config import Settings


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "memory_llm_key": "openai-secret",
        "milvus_url": "http://milvus.example.internal:19530",
        "memory_milvus_collection": "thinkback",
    }
    base.update(overrides)
    return Settings(**base)


# ---------------------------------------------------------------------------
# Milvus 探针
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_milvus_reports_not_ready_when_the_probe_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Milvus 不可达 → not_ready，不抛。"""

    class FailingMilvusClient:
        def __init__(self, *, uri: str, token: str, db_name: str) -> None:
            _ = (uri, token, db_name)

        def list_collections(self) -> object:
            raise RuntimeError("milvus unreachable")

        def close(self) -> None:
            return None

    monkeypatch.setattr(readiness, "MilvusClient", FailingMilvusClient)
    monkeypatch.setattr(readiness, "settings", _settings())

    payload = await readiness.check_milvus()

    assert payload["status"] == "not_ready"
    assert "milvus unreachable" in payload["detail"]


@pytest.mark.asyncio
async def test_check_milvus_closes_the_client_when_the_probe_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """探针失败后客户端必须关闭：否则每次探针泄漏一条连接。"""
    closed: list[bool] = []

    class FailingMilvusClient:
        def __init__(self, *, uri: str, token: str, db_name: str) -> None:
            _ = (uri, token, db_name)

        def list_collections(self) -> object:
            raise RuntimeError("milvus unreachable")

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(readiness, "MilvusClient", FailingMilvusClient)
    monkeypatch.setattr(readiness, "settings", _settings())

    await readiness.check_milvus()

    assert closed == [True]


@pytest.mark.asyncio
async def test_check_milvus_reports_not_ready_when_pymilvus_is_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """打包坏了（pymilvus 没装）→ not_ready：不能当成 ready。"""
    monkeypatch.setattr(readiness, "MilvusClient", None)
    monkeypatch.setattr(readiness, "settings", _settings())

    payload = await readiness.check_milvus()

    assert payload["status"] == "not_ready"
    assert "pymilvus" in payload["detail"]


# ---------------------------------------------------------------------------
# mem0 / LLM 探针
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_mem0_library_reports_not_ready_when_the_llm_probe_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM 探不通 → not_ready：抽取不可用的服务不该接流量。"""

    class FailingOpenAI:
        def __init__(self, *, api_key: str, base_url: str | None) -> None:
            _ = (api_key, base_url)

        @property
        def models(self) -> Any:
            class _Models:
                def list(self) -> object:
                    raise RuntimeError("llm unreachable")

            return _Models()

        def close(self) -> None:
            return None

    monkeypatch.setattr("openai.OpenAI", FailingOpenAI)
    monkeypatch.setattr(readiness, "settings", _settings())

    payload = await readiness.check_mem0_library()

    assert payload["status"] == "not_ready"
    assert "llm unreachable" in payload["detail"]


# ---------------------------------------------------------------------------
# 单依赖探针执行器
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dependency_check_exception_becomes_not_ready() -> None:
    """探针抛异常 → 归一为 not_ready，不把异常抖到 readiness 端点。"""

    async def _boom() -> dict[str, str]:
        raise RuntimeError("probe exploded")

    name, result = await readiness._run_dependency_check("database", _boom, 1.0)

    assert name == "database"
    assert result["status"] == "not_ready"
    assert "probe exploded" in result["detail"]


@pytest.mark.asyncio
async def test_dependency_check_timeout_becomes_not_ready() -> None:
    """探针卡死 → 按超时算 not_ready：不能把 readiness 请求拖成挂起。"""

    async def _hang() -> dict[str, str]:
        await asyncio.sleep(10)
        return {"status": "ready", "detail": "ok"}

    _, result = await readiness._run_dependency_check("milvus", _hang, 0.05)

    assert result["status"] == "not_ready"
    assert "timed out" in result["detail"]


@pytest.mark.asyncio
async def test_dependency_check_not_ready_status_is_passed_through() -> None:
    """探针自己报 not_ready → 原样透出，别改成 ready。"""

    async def _not_ready() -> dict[str, str]:
        return {"status": "not_ready", "detail": "schema missing"}

    _, result = await readiness._run_dependency_check("mem0", _not_ready, 1.0)

    assert result == {"status": "not_ready", "detail": "schema missing"}
