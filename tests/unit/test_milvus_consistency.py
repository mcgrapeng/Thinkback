"""ensure_milvus_collection_consistency 的单元测试（不打真实 Milvus）。"""

from __future__ import annotations

from typing import Any

from thinkback.infra import milvus as milvus_infra


class _FakeClient:
    def __init__(self, *, has: bool, level: str, fail_on_alter: bool = False) -> None:
        self._has = has
        self._level = level
        self._fail_on_alter = fail_on_alter
        self.altered: list[tuple[str, dict[str, Any]]] = []

    def has_collection(self, name: str) -> bool:
        return self._has

    def describe_collection(self, name: str) -> dict[str, Any]:
        return {"consistency_level_name": self._level}

    def alter_collection_properties(self, name: str, properties: dict[str, Any]) -> None:
        if self._fail_on_alter:
            raise RuntimeError("alter failed")
        self.altered.append((name, properties))

    def close(self) -> None:
        pass


def _install(monkeypatch: Any, client: _FakeClient) -> None:
    import pymilvus

    monkeypatch.setattr(pymilvus, "MilvusClient", lambda *args, **kwargs: client)


def test_raises_collection_consistency_when_below_strong(monkeypatch: Any) -> None:
    client = _FakeClient(has=True, level="Bounded")
    _install(monkeypatch, client)

    result = milvus_infra.ensure_milvus_collection_consistency(
        milvus_url="http://milvus:19530",
        milvus_database="thinkback",
        collection_name="agent_semantic_memory_v1",
    )

    assert result == "Strong"
    assert client.altered == [("agent_semantic_memory_v1", {"consistency_level": "Strong"})]


def test_skips_when_already_strong(monkeypatch: Any) -> None:
    client = _FakeClient(has=True, level="Strong")
    _install(monkeypatch, client)

    result = milvus_infra.ensure_milvus_collection_consistency(
        milvus_url="http://milvus:19530",
        milvus_database="thinkback",
        collection_name="agent_semantic_memory_v1",
    )

    assert result == "Strong"
    assert client.altered == []


def test_skips_when_collection_not_created_yet(monkeypatch: Any) -> None:
    client = _FakeClient(has=False, level="")
    _install(monkeypatch, client)

    result = milvus_infra.ensure_milvus_collection_consistency(
        milvus_url="http://milvus:19530",
        milvus_database="thinkback",
        collection_name="agent_semantic_memory_v1",
    )

    assert result == "skipped"
    assert client.altered == []


def test_returns_failed_without_raising(monkeypatch: Any) -> None:
    client = _FakeClient(has=True, level="Bounded", fail_on_alter=True)
    _install(monkeypatch, client)

    result = milvus_infra.ensure_milvus_collection_consistency(
        milvus_url="http://milvus:19530",
        milvus_database="thinkback",
        collection_name="agent_semantic_memory_v1",
    )

    assert result == "failed"
