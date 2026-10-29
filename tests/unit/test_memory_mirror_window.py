"""迁移窗口内双写双读的擦除语义。

迁移窗口里主集合（新）与镜像集合（旧）是同一份逻辑数据的两个副本。任何
一条语义漏洞都会变成「已擦除的事实又回来了」，所以这四条必须钉住：

- 镜像删除失败必须往上抛（fail-closed）：吞掉等于宣称已擦除；
- 列表要覆盖镜像：否则镜像里残留的记录既查不出也删不掉；
- 镜像回落只能补主集合还在的记录：主集合删掉的不能被镜像复活；
- 写入要把新行搬到镜像：否则回滚会丢掉窗口内的新事实。
"""

from __future__ import annotations

from typing import Any

import pytest

from thinkback.infra.milvus_collection import MilvusMirror
from thinkback.memory.backends import Mem0LibraryMemoryBackend


class _FakeMilvus:
    """够用的 pymilvus 替身。"""

    def __init__(
        self,
        *,
        rows: list[dict[str, Any]] | None = None,
        fail_delete: bool = False,
        fail_get: bool = False,
    ) -> None:
        self.rows = rows or []
        self.fail_delete = fail_delete
        self.fail_get = fail_get
        self.deleted: list[list[str]] = []
        self.upserts: list[dict[str, Any]] = []

    def query(self, **kwargs: Any) -> list[dict[str, Any]]:
        return list(self.rows)

    def get(self, **kwargs: Any) -> list[dict[str, Any]]:
        if self.fail_get:
            raise RuntimeError("milvus get failed")
        return list(self.rows)

    def upsert(self, **kwargs: Any) -> None:
        self.upserts.append(kwargs)

    def delete(self, **kwargs: Any) -> None:
        if self.fail_delete:
            raise RuntimeError("milvus delete failed")
        self.deleted.append(list(kwargs.get("ids", [])))

    def flush(self, **kwargs: Any) -> None:
        pass

    def close(self) -> None:
        pass


class _FakeMemoryClient:
    """mem0 Memory 替身：搜索结果按预设顺序返回。"""

    def __init__(self, *search_results: list[dict[str, Any]]) -> None:
        self.search_results = list(search_results)
        self.added: list[dict[str, Any]] = []
        self.deleted: list[str] = []

    def add(self, messages, **kwargs):  # type: ignore[no-untyped-def]
        self.added.append(kwargs)
        return {"results": [{"id": "m1", "memory": "用户负责 28nm 工艺整合"}]}

    def search(self, query, **kwargs):  # type: ignore[no-untyped-def]
        if not self.search_results:
            return {"results": []}
        return {"results": self.search_results.pop(0)}

    def get_all(self, **kwargs):  # type: ignore[no-untyped-def]
        return {"results": [{"id": "m1"}]}

    def delete(self, memory_id):  # type: ignore[no-untyped-def]
        self.deleted.append(memory_id)
        return {"message": "deleted"}

    def update(self, memory_id, data):  # type: ignore[no-untyped-def]
        return {"message": "updated"}


def _backend(
    mirror_client: _FakeMilvus,
    *,
    primary_client: _FakeMilvus | None = None,
    mirror_search: list[dict[str, Any]] | None = None,
):
    client = _FakeMemoryClient([{"id": "m1", "memory": "主集合命中"}])
    backend = Mem0LibraryMemoryBackend(
        config={
            "history_db_path": ".mem0/history.db",
            "vector_store": {
                "provider": "milvus",
                "config": {
                    "collection_name": "new_collection",
                    "url": "http://milvus:19530",
                    "token": "",
                    "db_name": "thinkback",
                    "embedding_model_dims": 1024,
                },
            },
        },
        memory_client=client,
        mirror=MilvusMirror(
            url="http://milvus:19530",
            database="thinkback",
            collection="old_collection",
        ),
    )
    backend._mirror_client = mirror_client  # type: ignore[assignment]
    backend._primary_client = primary_client or _FakeMilvus()  # type: ignore[assignment]
    backend._mirror_memory = _FakeMemoryClient(mirror_search or [])  # type: ignore[assignment]
    return backend, client


def test_mirror_delete_failure_propagates() -> None:
    """镜像删除失败必须抛出去：吞掉等于宣称「已擦除」，旧集合里还留着原文。"""

    backend, _ = _backend(_FakeMilvus(fail_delete=True))

    try:
        backend.delete("m1")
    except RuntimeError as exc:
        assert "milvus delete failed" in str(exc)
    else:
        raise AssertionError("镜像删除失败必须往上抛")


def test_get_all_includes_mirror_only_ids() -> None:
    """列表要覆盖镜像：镜像里残留的记录也要能被擦除复核看见。"""

    mirror = _FakeMilvus(rows=[{"id": "m9"}])
    backend, _ = _backend(mirror)

    rows = backend.get_all(user_id="user-1", memory_scope_id="thinkback")

    assert {str(row.get("id")) for row in rows} == {"m1", "m9"}


def test_mirror_fallback_skips_ids_absent_from_primary() -> None:
    """镜像回落只能补主集合还在的记录：已擦除的事实不能被镜像复活。"""

    mirror = _FakeMilvus()
    primary = _FakeMilvus(rows=[])  # 主集合里已经没有这条了
    backend, _ = _backend(
        mirror,
        primary_client=primary,
        mirror_search=[{"id": "m2", "memory": "镜像集合命中"}],
    )

    items = backend.search("光刻机", user_id="user-1", memory_scope_id="thinkback", limit=5)

    assert [str(item.get("id")) for item in items] == ["m1"]


def test_mirror_fallback_merges_ids_present_in_primary() -> None:
    """主集合还在的记录可以被镜像补进来（回填未完成的场景）。"""

    mirror = _FakeMilvus()
    primary = _FakeMilvus(rows=[{"id": "m2"}])
    backend, _ = _backend(
        mirror,
        primary_client=primary,
        mirror_search=[{"id": "m2", "memory": "镜像集合命中"}],
    )

    items = backend.search("光刻机", user_id="user-1", memory_scope_id="thinkback", limit=5)

    assert [str(item.get("id")) for item in items] == ["m1", "m2"]


def test_add_mirrors_new_rows() -> None:
    """写入要把新行搬到镜像：否则回滚会丢掉窗口内的新事实。"""

    mirror = _FakeMilvus(
        rows=[{"id": "m1", "vectors": [0.1], "metadata": {}, "text": "用户负责 28nm 工艺整合"}]
    )
    backend, _ = _backend(mirror)

    backend.add(
        [{"role": "user", "content": "我负责 28nm 工艺整合"}],
        user_id="user-1",
        memory_scope_id="thinkback",
    )

    assert mirror.upserts, "新行必须搬到镜像集合"
    assert mirror.upserts[0]["collection_name"] == "old_collection"


def test_mirror_write_failure_does_not_break_the_primary_write() -> None:
    """镜像写失败不得把主写入搞挂。

    镜像只是迁移窗口里的次副本：它失败时旧集合少几条新事实，回滚会丢，
    但主集合已经写成功、整体可用。把异常往上抛等于「因为备份坏了所以主库
    也别写」—— 一次镜像抖动会让所有用户的写入全部失败。
    """
    backend, client = _backend(_FakeMilvus(fail_get=True))

    events = backend.add(
        [{"role": "user", "content": "我负责 28nm 工艺整合"}],
        user_id="user-1",
        memory_scope_id="thinkback",
    )

    assert client.added, "主写入没有发生"
    assert events, "镜像失败把主写入的结果也吞了"


def test_mirror_write_gives_up_when_the_new_row_is_not_visible_yet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """搬不到行时放弃重试，不能把写入卡住。

    新写的行还在 growing segment 里时读不到是正常现象，重试序列有上限；
    搬不到就告警放弃 —— 主集合里的数据是好的。
    """
    monkeypatch.setattr(Mem0LibraryMemoryBackend, "_NOT_FOUND_RETRY_DELAYS", ())

    backend, client = _backend(_FakeMilvus(rows=[]))

    events = backend.add(
        [{"role": "user", "content": "我负责 28nm 工艺整合"}],
        user_id="user-1",
        memory_scope_id="thinkback",
    )

    assert client.added, "主写入没有发生"
    assert events, "镜像搬不到行把主写入的结果也吞了"
