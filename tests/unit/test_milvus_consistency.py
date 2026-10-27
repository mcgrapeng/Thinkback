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


def test_passes_configured_database_to_client(monkeypatch: Any) -> None:
    """必须显式传 db_name：不传就是在 default 库里找集合。

    mem0 写入用的是配置里的 ``db_name``；一致性钩子若连的是 default 库，
    改的集合根本不是 mem0 在写的那一个，Strong 永远落不下去。
    """
    observed: dict[str, Any] = {}

    def fake_client(**kwargs: Any) -> _FakeClient:
        observed.update(kwargs)
        return _FakeClient(has=True, level="Bounded")

    import pymilvus

    monkeypatch.setattr(pymilvus, "MilvusClient", fake_client)

    result = milvus_infra.ensure_milvus_collection_consistency(
        milvus_url="http://milvus:19530",
        milvus_database="thinkback",
        collection_name="agent_semantic_memory_v1",
    )

    assert result == "Strong"
    assert observed["db_name"] == "thinkback"
    assert observed["uri"] == "http://milvus:19530"


def test_returns_failed_when_client_construction_fails(monkeypatch: Any) -> None:
    """构造客户端失败也算失败：异常不能从一致性钩子里逃出去打断写入。"""

    def boom(**kwargs: Any) -> Any:
        _ = kwargs
        raise RuntimeError("connect refused")

    import pymilvus

    monkeypatch.setattr(pymilvus, "MilvusClient", boom)

    result = milvus_infra.ensure_milvus_collection_consistency(
        milvus_url="http://milvus:19530",
        milvus_database="thinkback",
        collection_name="agent_semantic_memory_v1",
    )

    assert result == "failed"
