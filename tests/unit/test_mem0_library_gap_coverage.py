"""Mem0 Library 适配器的行覆盖缺口收口（确定性，无网络 / 无真实 mem0 调用）。

逐行钉住此前没有测试触达的分支：
- 构造与导入守卫（并发上限、mem0 未安装时的报错）；
- search 的 explain 传参形状（默认不传，要分解时才传）；
- 引擎侧历史清理的 SQLite 错误分类（非锁错误立刻上抛、持续锁重试耗尽后 RuntimeError）；
- 预热成功路径与镜像集合（迁移窗口）的懒构造、双写重试、双读回落各分支；
- 结果摘要 / memory_id 判定 / 内联执行器与 concurrent 代理这些纯函数小分支。

mem0 已安装的分支用 ``sys.modules[...] = None`` 触发 ImportError；
Milvus 客户端一律用替身，绝不连网。
"""

from __future__ import annotations

import sqlite3
import sys
from copy import deepcopy
from typing import Any

import pytest

import thinkback.memory.backends.mem0_library as mem0_module
from thinkback.infra import milvus_collection
from thinkback.infra.milvus_collection import MilvusMirror
from thinkback.memory.backends import Mem0LibraryMemoryBackend
from thinkback.memory.backends.mem0_library import (
    _InlineThreadPoolExecutor,
    _mem0_inline_threadpool,
    _Mem0ConcurrentProxy,
    register_mem0_embedder_provider,
)

_MIRROR = MilvusMirror(
    url="http://mirror:19530", database="mirror-db", collection="legacy", token=""
)


class _RecordingSearchClient:
    """记录 search 调用形状的替身（**kwargs 签名走 filters 路径）。"""

    def __init__(self, results: list[dict[str, Any]] | None = None) -> None:
        self.search_calls: list[dict[str, Any]] = []
        self._results = list(results or [])

    def search(self, query: str, **kwargs: Any) -> dict[str, Any]:
        self.search_calls.append(dict(kwargs))
        return {"results": [dict(item) for item in self._results]}


class _ExplodingSearchClient:
    def search(self, query: str, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("mirror down")


def _backend(
    *,
    mirror: MilvusMirror | None = None,
    config: dict[str, Any] | None = None,
) -> Mem0LibraryMemoryBackend:
    full_config: dict[str, Any] = {
        "vector_store": {
            "config": {
                "collection_name": "primary",
                "url": "http://primary:19530",
                "db_name": "db",
                "token": "tok",
            }
        }
    }
    full_config.update(config or {})
    return Mem0LibraryMemoryBackend(
        config=full_config,
        memory_client=_RecordingSearchClient(),
        mirror=mirror,
    )


# ── 导入守卫与构造校验 ────────────────────────────────────────────────


def test_register_mem0_embedder_provider_without_mem0_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """mem0 未安装时注册 embedder 要给出可行动的报错，而不是裸 ImportError。"""
    monkeypatch.setitem(sys.modules, "mem0.utils.factory", None)

    with pytest.raises(RuntimeError, match="mem0 library is not installed; install mem0ai"):
        register_mem0_embedder_provider()


def test_backend_rejects_non_positive_max_concurrent_calls() -> None:
    """并发上限小于 1 会让信号量永远拿不到名额，必须在构造期拒绝。"""
    with pytest.raises(ValueError, match="max_concurrent_calls must be >= 1"):
        Mem0LibraryMemoryBackend(config={}, memory_client=object(), max_concurrent_calls=0)


# ── search 的 explain 传参形状 ────────────────────────────────────────


def test_search_passes_explain_only_when_requested() -> None:
    """explain 默认不传（不改调用形状），要分数分解时才显式传 True。"""
    client = _RecordingSearchClient(results=[{"id": "m-1", "memory": "x"}])
    backend = Mem0LibraryMemoryBackend(config={}, memory_client=client)

    backend.search("q", user_id="u", memory_scope_id="s", limit=3, explain=True)
    backend.search("q", user_id="u", memory_scope_id="s", limit=3)

    assert client.search_calls[0]["explain"] is True
    assert "explain" not in client.search_calls[1]


# ── 引擎侧历史清理的 SQLite 错误分类 ─────────────────────────────────


def _purge_backend(tmp_path: Any) -> Mem0LibraryMemoryBackend:
    return Mem0LibraryMemoryBackend(
        config={"history_db_path": str(tmp_path / "history.db")},
        memory_client=object(),
    )


def test_purge_history_reraises_non_lock_sqlite_errors_without_retry(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """非锁类 OperationalError（如表不存在）不是瞬时冲突，重试没有意义。"""
    backend = _purge_backend(tmp_path)
    calls: list[int] = []

    def boom(**kwargs: Any) -> dict[str, int]:
        calls.append(1)
        raise sqlite3.OperationalError("no such table: history")

    monkeypatch.setattr(backend, "_purge_history_once", boom)

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        backend.purge_history(
            user_id="u",
            memory_ids=[],
            namespace="",
            message_texts=[],
            purge_all_messages=True,
        )
    assert len(calls) == 1


def test_purge_history_raises_runtime_error_after_exhausting_lock_retries(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """持续锁冲突按退避序列重试，耗尽后上抛 RuntimeError（擦除必须可判失败）。"""
    backend = _purge_backend(tmp_path)
    calls: list[int] = []

    def locked(**kwargs: Any) -> dict[str, int]:
        calls.append(1)
        raise sqlite3.OperationalError("database is locked")

    sleeps: list[float] = []
    monkeypatch.setattr(backend, "_purge_history_once", locked)
    monkeypatch.setattr(mem0_module.time, "sleep", sleeps.append)

    with pytest.raises(RuntimeError, match="history purge failed after 3 attempts"):
        backend.purge_history(
            user_id="u",
            memory_ids=[],
            namespace="",
            message_texts=[],
            purge_all_messages=True,
        )
    assert len(calls) == 3
    assert sleeps == pytest.approx([0.2, 0.4, 0.6])


# ── 预热与客户端构造 ─────────────────────────────────────────────────


def test_warmup_success_builds_client_and_returns_quietly() -> None:
    """预热成功路径：客户端已注入时 warmup 直接完成，不抛异常。"""
    client = _RecordingSearchClient()
    backend = Mem0LibraryMemoryBackend(config={}, memory_client=client)

    backend.warmup()

    assert backend.memory_client is client


def test_build_client_for_without_mem0_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "mem0", None)
    backend = Mem0LibraryMemoryBackend(config={}, memory_client=object())

    with pytest.raises(RuntimeError, match="mem0 library is not installed; install mem0ai"):
        backend._build_client_for({})


# ── 迁移窗口：镜像句柄与主集合句柄 ──────────────────────────────────


def test_mirror_handle_without_mirror_raises() -> None:
    backend = _backend()

    with pytest.raises(RuntimeError, match="mirror collection is not configured"):
        backend._mirror_handle()


def test_mirror_handle_lazily_builds_and_caches_mirror_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """镜像 Milvus 客户端懒构造且只构造一次（重复取拿到同一实例）。"""
    backend = _backend(mirror=_MIRROR)
    built: list[str] = []
    monkeypatch.setattr(MilvusMirror, "client", lambda self: built.append("x") or "handle")

    assert backend._mirror_handle() == "handle"
    assert backend._mirror_handle() == "handle"
    assert built == ["x"]


def test_primary_handle_lazily_connects_and_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    """主集合句柄从 infra 的 connect 工厂懒构造，URL / 库 / token 来自配置。"""
    backend = _backend()
    connect_calls: list[dict[str, Any]] = []

    def fake_connect(**kwargs: Any) -> str:
        connect_calls.append(kwargs)
        return "primary-handle"

    monkeypatch.setattr(milvus_collection, "connect", fake_connect)

    assert backend._primary_handle() == "primary-handle"
    assert backend._primary_handle() == "primary-handle"
    assert connect_calls == [{"url": "http://primary:19530", "database": "db", "token": "tok"}]


def test_mirror_scope_ids_without_mirror_returns_empty() -> None:
    """没配镜像就没有镜像作用域 ID，调用方（擦除复核）据此跳过镜像核对。"""
    backend = _backend()

    assert backend._mirror_scope_ids(user_id="u", memory_scope_id="s", limit=5) == []


def test_primary_present_ids_with_empty_candidates_returns_empty_set() -> None:
    backend = _backend()

    assert backend._primary_present_ids([]) == set()


# ── 迁移窗口：双写 / 双删 ───────────────────────────────────────────


def test_mirror_write_retries_when_rows_not_yet_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """镜像搬运遇到 growing-segment 可见性窗口时退避重试，搬齐为止。"""
    backend = _backend(mirror=_MIRROR)
    moves = [0, 1]  # 第一次搬 0 行（尚不可见），第二次搬齐。

    def fake_mirror_rows(**kwargs: Any) -> int:
        return moves.pop(0)

    sleeps: list[float] = []
    monkeypatch.setattr(MilvusMirror, "client", lambda self: "mirror-client")
    monkeypatch.setattr(mem0_module, "mirror_rows", fake_mirror_rows)
    monkeypatch.setattr(mem0_module.time, "sleep", sleeps.append)

    backend._mirror_write(["m-1"])

    assert moves == []
    assert sleeps == [0.5]


def test_mirror_delete_success_completes_without_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """镜像删除成功路径：删完即返回，不把异常抛给已经删完主集合的调用方。"""
    backend = _backend(mirror=_MIRROR)
    deleted: list[tuple[str, list[str]]] = []

    def fake_delete_mirror_rows(**kwargs: Any) -> int:
        deleted.append((kwargs["target"], list(kwargs["ids"])))
        return len(kwargs["ids"])

    monkeypatch.setattr(MilvusMirror, "client", lambda self: "mirror-client")
    monkeypatch.setattr(mem0_module, "delete_mirror_rows", fake_delete_mirror_rows)

    backend._mirror_delete(["m-1"])

    assert deleted == [("legacy", ["m-1"])]


# ── 迁移窗口：镜像 mem0 客户端与双读回落 ────────────────────────────


def test_mirror_memory_client_without_mirror_raises() -> None:
    backend = _backend()

    with pytest.raises(RuntimeError, match="mirror collection is not configured"):
        backend._mirror_memory_client()


def test_mirror_memory_client_overrides_collection_and_caches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """镜像 mem0 客户端复用同一条构造路径，但集合名换成镜像集合；只构造一次。"""
    backend = _backend(mirror=_MIRROR)
    built: list[dict[str, Any]] = []

    def fake_build(config: dict[str, Any]) -> str:
        built.append(deepcopy(config))
        return "mirror-memory"

    monkeypatch.setattr(backend, "_build_client_for", fake_build)

    assert backend._mirror_memory_client() == "mirror-memory"
    assert backend._mirror_memory_client() == "mirror-memory"
    assert len(built) == 1
    assert built[0]["vector_store"]["config"]["collection_name"] == "legacy"
    # 传入的是深拷贝：镜像构造不改主配置。
    assert backend.config["vector_store"]["config"]["collection_name"] == "primary"


def test_merge_mirror_search_appends_only_primary_alive_and_passes_explain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回落补位：镜像结果去重后追加，且核对过主集合还在；explain 形状透传。"""
    backend = _backend(mirror=_MIRROR)
    mirror_client = _RecordingSearchClient(results=[{"id": "m-1", "memory": "mirror"}])
    backend._mirror_memory = mirror_client
    monkeypatch.setattr(backend, "_primary_present_ids", lambda ids: {"m-1"})

    merged = backend._merge_mirror_search(
        "q",
        [{"id": "p-1", "memory": "primary"}],
        user_id="u",
        memory_scope_id="s",
        limit=5,
        threshold=None,
        explain=True,
    )

    assert [item["id"] for item in merged] == ["p-1", "m-1"]
    assert mirror_client.search_calls[0]["explain"] is True


def test_merge_mirror_search_returns_primary_when_mirror_client_fails() -> None:
    """镜像检索失败只影响回落，主集合结果原样返回。"""
    backend = _backend(mirror=_MIRROR)
    backend._mirror_memory = _ExplodingSearchClient()

    merged = backend._merge_mirror_search(
        "q",
        [{"id": "p-1", "memory": "primary"}],
        user_id="u",
        memory_scope_id="s",
        limit=5,
        threshold=None,
        explain=False,
    )

    assert [item["id"] for item in merged] == ["p-1"]


def test_merge_mirror_search_returns_primary_when_mirror_has_no_hits() -> None:
    backend = _backend(mirror=_MIRROR)
    backend._mirror_memory = _RecordingSearchClient(results=[])

    merged = backend._merge_mirror_search(
        "q",
        [{"id": "p-1", "memory": "primary"}],
        user_id="u",
        memory_scope_id="s",
        limit=5,
        threshold=None,
        explain=False,
    )

    assert [item["id"] for item in merged] == ["p-1"]


def test_merge_mirror_search_skips_ids_already_in_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """镜像结果里主集合已有的 id 跳过（去重），只补新的。"""
    backend = _backend(mirror=_MIRROR)
    mirror_client = _RecordingSearchClient(
        results=[{"id": "p-1", "memory": "dup"}, {"id": "m-1", "memory": "new"}]
    )
    backend._mirror_memory = mirror_client
    monkeypatch.setattr(backend, "_primary_present_ids", lambda ids: {"m-1"})

    merged = backend._merge_mirror_search(
        "q",
        [{"id": "p-1", "memory": "primary"}],
        user_id="u",
        memory_scope_id="s",
        limit=5,
        threshold=None,
        explain=False,
    )

    assert [item["id"] for item in merged] == ["p-1", "m-1"]


def test_merge_mirror_search_returns_primary_when_mirror_only_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """镜像全是主集合已有的 id（无新候选）时直接返回主结果。"""
    backend = _backend(mirror=_MIRROR)
    backend._mirror_memory = _RecordingSearchClient(results=[{"id": "p-1", "memory": "dup"}])

    merged = backend._merge_mirror_search(
        "q",
        [{"id": "p-1", "memory": "primary"}],
        user_id="u",
        memory_scope_id="s",
        limit=5,
        threshold=None,
        explain=False,
    )

    assert [item["id"] for item in merged] == ["p-1"]


def test_merge_mirror_search_skips_fallback_when_presence_check_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """主集合核对失败就放弃回落：少补几条也比放进一条已擦除的事实安全。"""
    backend = _backend(mirror=_MIRROR)
    backend._mirror_memory = _RecordingSearchClient(results=[{"id": "m-1", "memory": "x"}])

    def boom(ids: list[str]) -> set[str]:
        raise RuntimeError("primary check down")

    monkeypatch.setattr(backend, "_primary_present_ids", boom)

    merged = backend._merge_mirror_search(
        "q",
        [{"id": "p-1", "memory": "primary"}],
        user_id="u",
        memory_scope_id="s",
        limit=5,
        threshold=None,
        explain=False,
    )

    assert [item["id"] for item in merged] == ["p-1"]


# ── 纯函数小分支 ─────────────────────────────────────────────────────


def test_result_summary_counts_list_results() -> None:
    assert Mem0LibraryMemoryBackend._result_summary([{"id": "1"}]) == {
        "result_type": "list",
        "result_count": 1,
    }


def test_is_backend_memory_id_rejects_empty_and_local_ids() -> None:
    is_backend_id = Mem0LibraryMemoryBackend._is_backend_memory_id

    assert is_backend_id("") is False
    assert is_backend_id("local-p0:preferred_nickname") is False
    assert is_backend_id("mem0-1") is True


def test_inline_executor_captures_exceptions_in_future() -> None:
    """内联执行器把异常塞进 future（异步语义），而不是当场炸掉调用线程。"""

    def _boom() -> None:
        raise RuntimeError("inline boom")

    future = _InlineThreadPoolExecutor().submit(_boom)

    with pytest.raises(RuntimeError, match="inline boom"):
        future.result()


def test_mem0_concurrent_proxy_passes_through_other_attributes() -> None:
    """代理只换 futures.ThreadPoolExecutor，其它属性原样透传。"""

    class _Original:
        futures = object()
        version = "1.0"

    proxy = _Mem0ConcurrentProxy(_Original())

    assert proxy.version == "1.0"
    assert isinstance(proxy.futures, mem0_module._Mem0FuturesProxy)


def test_inline_threadpool_yields_without_mem0(monkeypatch: pytest.MonkeyPatch) -> None:
    """mem0 未安装时上下文直接 yield，不碰全局替换状态。"""
    monkeypatch.setitem(sys.modules, "mem0.memory.main", None)

    assert mem0_module._mem0_inline_threadpool_depth == 0
    with _mem0_inline_threadpool():
        assert mem0_module._mem0_inline_threadpool_depth == 0
    assert mem0_module._mem0_inline_threadpool_original is None
