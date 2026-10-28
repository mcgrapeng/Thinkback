"""Thinkback 自持 Milvus collection schema 的单元测试（不打真实 Milvus）。

覆盖三件事：
- 预建出来的集合带中文分析器与 BM25 Function，字段名与 mem0 2.2.1 一致；
- 已有集合的体检结果分得清「一致」「漂移」（分析器不对 / 字段缺失）；
- 复制式回填与双写搬运的是同一批行，且不写 BM25 的输出字段。
"""

from __future__ import annotations

import json
from typing import Any

from thinkback.infra import milvus_collection as collection_infra


class _FakeClient:
    """够用的 MilvusClient 替身：记录调用，不连网。"""

    def __init__(
        self,
        *,
        exists: bool = False,
        fields: list[dict[str, Any]] | None = None,
        analyzer_params: str | None = '{"type":"chinese"}',
    ) -> None:
        self.exists = exists
        self._fields = fields
        self._analyzer_params = analyzer_params
        self.created: list[dict[str, Any]] = []
        self.upserts: list[dict[str, Any]] = []
        self.deletes: list[list[str]] = []
        self.flushed: list[str] = []
        self.closed = False

    def has_collection(self, name: str) -> bool:
        return self.exists

    def describe_collection(self, name: str) -> dict[str, Any]:
        fields = self._fields
        if fields is None:
            fields = [
                {"name": "id", "params": {}},
                {"name": "vectors", "params": {}},
                {"name": "metadata", "params": {}},
                {
                    "name": "text",
                    "params": (
                        {"analyzer_params": self._analyzer_params} if self._analyzer_params else {}
                    ),
                },
                {"name": "sparse", "params": {}},
            ]
        return {"fields": fields}

    def prepare_index_params(self) -> Any:
        class _IndexParams:
            def __init__(self) -> None:
                self.indexes: list[dict[str, Any]] = []

            def add_index(self, **kwargs: Any) -> None:
                self.indexes.append(kwargs)

        return _IndexParams()

    def create_collection(self, **kwargs: Any) -> None:
        self.created.append(kwargs)
        self.exists = True

    def query(self, **kwargs: Any) -> list[dict[str, Any]]:
        return getattr(self, "_query_rows", [])

    def get(self, **kwargs: Any) -> list[dict[str, Any]]:
        return getattr(self, "_get_rows", [])

    def upsert(self, **kwargs: Any) -> None:
        self.upserts.append(kwargs)

    def delete(self, **kwargs: Any) -> None:
        self.deletes.append(list(kwargs.get("ids", [])))

    def flush(self, **kwargs: Any) -> None:
        self.flushed.append(str(kwargs.get("collection_name")))

    def close(self) -> None:
        self.closed = True


def _install(monkeypatch: Any, client: _FakeClient) -> None:
    import pymilvus

    monkeypatch.setattr(pymilvus, "MilvusClient", lambda *args, **kwargs: client)


def _ensure(client: _FakeClient, monkeypatch: Any) -> Any:
    _install(monkeypatch, client)
    return collection_infra.ensure_collection(
        milvus_url="http://milvus:19530",
        milvus_database="thinkback",
        collection_name="agent_semantic_memory_v2",
        dim=1024,
    )


def test_created_collection_carries_chinese_analyzer(monkeypatch: Any) -> None:
    """新建的集合必须带中文分析器：这是中文 BM25 能命中的唯一前提。"""

    client = _FakeClient(exists=False)
    state = _ensure(client, monkeypatch)

    assert state.state == collection_infra.STATE_CREATED
    assert state.analyzer == "chinese"
    schema = client.created[0]["schema"]
    names = {field.name for field in schema.fields}
    assert names == set(collection_infra.REQUIRED_FIELDS)
    text_field = next(field for field in schema.fields if field.name == "text")
    # pymilvus 把 analyzer_params 存成 JSON 字符串。
    assert json.loads(text_field.params["analyzer_params"]) == {"type": "chinese"}


def test_existing_matching_collection_is_reused(monkeypatch: Any) -> None:
    client = _FakeClient(exists=True)
    state = _ensure(client, monkeypatch)

    assert state.state == collection_infra.STATE_REUSED
    assert client.created == []


def test_legacy_collection_without_analyzer_is_drifted(monkeypatch: Any) -> None:
    """mem0 自建的集合没有 analyzer_params —— 中文 BM25 在它上面恒为 0。"""

    client = _FakeClient(exists=True, analyzer_params=None)
    state = _ensure(client, monkeypatch)

    assert state.state == collection_infra.STATE_DRIFTED
    assert "standard" in state.detail


def test_collection_missing_fields_is_drifted(monkeypatch: Any) -> None:
    client = _FakeClient(
        exists=True,
        fields=[{"name": "id", "params": {}}, {"name": "vectors", "params": {}}],
    )
    state = _ensure(client, monkeypatch)

    assert state.state == collection_infra.STATE_DRIFTED
    assert "缺字段" in state.detail


def test_connection_failure_degrades_to_failed_state(monkeypatch: Any) -> None:
    import pymilvus

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("no route to host")

    monkeypatch.setattr(pymilvus, "MilvusClient", _boom)
    state = collection_infra.ensure_collection(
        milvus_url="http://milvus:19530",
        milvus_database="thinkback",
        collection_name="agent_semantic_memory_v2",
        dim=1024,
    )

    assert state.state == collection_infra.STATE_FAILED


def test_copy_rows_moves_rows_and_flushes() -> None:
    client = _FakeClient()
    client._query_rows = [
        {"id": "a", "vectors": [0.1], "metadata": {"text": "甲"}, "text": "甲"},
        {"id": "b", "vectors": [0.2], "metadata": {"text": "乙"}, "text": "乙"},
    ]

    copied, total = collection_infra.copy_rows(
        client=client, source="old", target="new", batch_size=500
    )

    assert (copied, total) == (2, 2)
    payload = client.upserts[0]["data"]
    assert [row["id"] for row in payload] == ["a", "b"]
    # BM25 的输出字段不能显式写入，由服务端按 text 重新生成。
    assert all("sparse" not in row for row in payload)
    assert client.flushed == ["new"]


def test_mirror_rows_only_moves_requested_ids() -> None:
    client = _FakeClient()
    client._get_rows = [
        {"id": "a", "vectors": [0.1], "metadata": {}, "text": "甲", "sparse": {1: 0.5}}
    ]

    moved = collection_infra.mirror_rows(client=client, source="new", target="old", ids=["a"])

    assert moved == 1
    payload = client.upserts[0]["data"]
    assert payload[0]["id"] == "a"
    assert "sparse" not in payload[0]


def test_delete_mirror_rows_removes_from_secondary() -> None:
    client = _FakeClient()

    deleted = collection_infra.delete_mirror_rows(client=client, target="old", ids=["a", "b"])

    assert deleted == 2
    assert client.deletes == [["a", "b"]]
    assert client.flushed == ["old"]


def test_row_count_counts_visible_rows_not_tombstones() -> None:
    """统计口径里的墓碑行也算进去，会让「回填前后行数一致」永远对不上。"""

    client = _FakeClient()
    client._query_rows = [{"id": str(index)} for index in range(3)]

    assert collection_infra.row_count(client=client, collection="old") == 3
