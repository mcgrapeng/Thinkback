"""Milvus collection 基础设施的行覆盖缺口收口（确定性，不打真实 Milvus）。

覆盖此前没被触达的分支：
- connect / MilvusMirror.client 的空 token 归一化（token="" → None）；
- present_ids / mirror_rows / delete_mirror_rows / copy_rows / row_count 的空输入边界；
- analyzer_of 对坏 JSON 的容错（返回 None，由调用方按 drifted 处理）；
- pymilvus 缺失与建表失败的导入守卫 / 降级路径。
"""

from __future__ import annotations

import sys
from typing import Any

import pymilvus
import pytest

from thinkback.infra.milvus_collection import (
    STATE_FAILED,
    MilvusMirror,
    analyzer_of,
    connect,
    copy_rows,
    delete_mirror_rows,
    ensure_collection,
    mirror_rows,
    present_ids,
    row_count,
)


class _Client:
    """够用的 MilvusClient 替身：记录调用，不连网。"""

    query_rows: list[Any]

    def __init__(self) -> None:
        self.query_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []
        self.delete_calls: list[list[str]] = []
        self.closed = False
        self.exists = False
        self.create_error: Exception | None = None

    def has_collection(self, name: str) -> bool:
        return self.exists

    def prepare_index_params(self) -> Any:
        class _IndexParams:
            def __init__(self) -> None:
                self.indexes: list[dict[str, Any]] = []

            def add_index(self, **kwargs: Any) -> None:
                self.indexes.append(kwargs)

        return _IndexParams()

    def create_collection(self, **kwargs: Any) -> None:
        if self.create_error is not None:
            raise self.create_error

    def query(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.query_calls.append(kwargs)
        return getattr(self, "query_rows", [])

    def get(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.get_calls.append(kwargs)
        return []

    def upsert(self, **kwargs: Any) -> None:
        _ = kwargs

    def delete(self, **kwargs: Any) -> None:
        self.delete_calls.append(list(kwargs.get("ids", [])))

    def flush(self, **kwargs: Any) -> None:
        _ = kwargs

    def close(self) -> None:
        self.closed = True


def _install(monkeypatch: pytest.MonkeyPatch, client: _Client) -> None:
    monkeypatch.setattr(pymilvus, "MilvusClient", lambda *args, **kwargs: client)


def _ensure(client: _Client, monkeypatch: pytest.MonkeyPatch) -> Any:
    _install(monkeypatch, client)
    return ensure_collection(
        milvus_url="http://localhost:19530",
        milvus_database="default",
        collection_name="thinkback_memories",
        dim=4,
    )


def test_connect_normalizes_empty_token_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """空 token 归一化成 None，Milvus SDK 才不会拿空串去鉴权。"""
    created: list[dict[str, Any]] = []

    def fake_client(**kwargs: Any) -> str:
        created.append(kwargs)
        return "client"

    monkeypatch.setattr(pymilvus, "MilvusClient", fake_client)

    assert connect(url="http://m:19530", database="db", token="") == "client"
    assert created == [{"uri": "http://m:19530", "token": None, "db_name": "db"}]


def test_mirror_client_passes_url_database_and_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """镜像客户端沿同一条工厂路径构造，凭据逐项传对。"""
    created: list[dict[str, Any]] = []

    def fake_client(**kwargs: Any) -> str:
        created.append(kwargs)
        return "mirror-client"

    monkeypatch.setattr(pymilvus, "MilvusClient", fake_client)
    mirror = MilvusMirror(
        url="http://m:19530", database="legacy-db", collection="legacy", token="tok"
    )

    assert mirror.client() == "mirror-client"
    assert created == [{"uri": "http://m:19530", "token": "tok", "db_name": "legacy-db"}]


def test_present_ids_with_empty_candidates_returns_empty_set() -> None:
    assert present_ids(client=_Client(), collection="c", memory_ids=[]) == set()


def test_analyzer_of_returns_none_for_malformed_analyzer_json() -> None:
    """analyzer_params 是坏 JSON 时读不出分析器（None → 调用方按 drifted 处理）。"""
    description = {"fields": [{"name": "text", "params": {"analyzer_params": "{not-json"}}]}

    assert analyzer_of(description) is None


def test_ensure_collection_without_pymilvus_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "pymilvus", None)

    with pytest.raises(RuntimeError, match="pymilvus is not installed; install pymilvus"):
        ensure_collection(
            milvus_url="http://m:19530",
            milvus_database="default",
            collection_name="c",
            dim=4,
        )


def test_ensure_collection_create_failure_degrades_to_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """建表失败降级为 STATE_FAILED（带错误详情），且客户端被关闭。"""
    client = _Client()
    client.create_error = RuntimeError("disk full")

    state = _ensure(client, monkeypatch)

    assert state.state == STATE_FAILED
    assert "disk full" in state.detail
    assert client.closed is True


def test_copy_rows_from_empty_source_copies_nothing() -> None:
    client = _Client()
    client.query_rows = []

    assert copy_rows(client=client, source="old", target="new") == (0, 0)
    assert len(client.query_calls) == 1


def test_mirror_rows_without_ids_is_noop() -> None:
    client = _Client()

    assert mirror_rows(client=client, source="primary", target="legacy", ids=[]) == 0
    assert client.get_calls == []


def test_delete_mirror_rows_without_ids_is_noop() -> None:
    client = _Client()

    assert delete_mirror_rows(client=client, target="legacy", ids=[]) == 0
    assert client.delete_calls == []


def test_row_count_of_empty_collection_is_zero() -> None:
    client = _Client()
    client.query_rows = []

    assert row_count(client=client, collection="c") == 0
