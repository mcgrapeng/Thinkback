from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event, Lock, get_ident
from time import sleep
from typing import Any

import pytest
from loguru import logger

from thinkback.domain.ports import MEMORY_LIST_PAGE_LIMIT
from thinkback.domain.prompts import DEFAULT_EXTRACTION_INSTRUCTIONS
from thinkback.memory.backends import FakeMemoryBackend


def test_fake_backend_add_search_delete_all_are_scoped() -> None:
    backend = FakeMemoryBackend()
    messages = [{"role": "user", "content": "用户不喜欢被催睡觉"}]

    result = backend.add(
        messages,
        user_id="user-1",
        memory_scope_id="thinkback",
        metadata={"source_refs": [{"session_id": "s1", "round_id": "r1"}]},
    )

    assert result[0]["event"] == "ADD"
    assert backend.search("睡觉", user_id="user-1", memory_scope_id="thinkback", limit=5)
    assert backend.search("睡觉", user_id="user-1", memory_scope_id="other-session", limit=5) == []

    deleted = backend.delete_all(user_id="user-1", memory_scope_id="thinkback")

    # 返回被删的 ID 列表（不是条数）：调用方要按这些 ID 清引擎侧历史
    assert len(deleted) == 1
    assert all(memory_id.startswith("fake-") for memory_id in deleted)
    assert backend.search("睡觉", user_id="user-1", memory_scope_id="thinkback", limit=5) == []


def test_fake_backend_update_on_missing_id_raises_like_production() -> None:
    """fake 不能比生产后端宽松。

    生产后端对不存在的 memory_id 重试几次后抛 RuntimeError；fake 原来静默
    no-op —— 一条针对「更新不存在的记忆」的用例在 fake 上通过，到生产才炸，
    而且看起来像 mem0 的 bug 而不是 fake 在骗人。
    """
    backend = FakeMemoryBackend()

    with pytest.raises(RuntimeError):
        backend.update("missing-id", "新内容")


def test_fake_backend_delete_many_returns_attempted_count() -> None:
    """delete_many 返回尝试删除的条数（幂等删除，条数不等于删除数）。"""
    backend = FakeMemoryBackend()
    backend.add(
        [{"role": "user", "content": "用户不喜欢被催睡觉"}],
        user_id="user-1",
        memory_scope_id="thinkback",
    )

    attempted = backend.delete_many(["fake-missing", "", "fake-present"])

    # 三个 ID 里两个非空，其中一个不存在 —— 尝试 2 个，删掉 1 个。
    assert attempted == 2


def test_mem0_library_config_builder_targets_milvus_and_openai() -> None:
    from thinkback.memory.backends import build_mem0_library_config

    config = build_mem0_library_config(
        llm_api_key="openai-secret",
        llm_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        embedding_api_key="",
        embedding_base_url="http://embedding.example.internal:7345/v1",
        milvus_url="http://localhost:19530",
        milvus_token="milvus-user:milvus-secret",
        milvus_database="thinkback",
        collection_name="thinkback_memories",
        llm_model="qwen-plus-latest",
        embedding_model="default-embedding",
        history_db_path=".mem0/history.db",
        embedding_model_dims=1536,
    )

    assert config == {
        "vector_store": {
            "provider": "milvus",
            "config": {
                "collection_name": "thinkback_memories",
                "url": "http://localhost:19530",
                "token": "milvus-user:milvus-secret",
                "db_name": "thinkback",
                "embedding_model_dims": 1536,
                "metric_type": "COSINE",
            },
        },
        "llm": {
            "provider": "openai",
            "config": {
                "model": "qwen-plus-latest",
                "api_key": "openai-secret",
                "openai_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            },
        },
        "embedder": {
            "provider": "openai",
            "config": {
                "model": "default-embedding",
                "api_key": "",
                "openai_base_url": "http://embedding.example.internal:7345/v1",
                "embedding_dims": 1536,
            },
        },
        "history_db_path": ".mem0/history.db",
        "custom_instructions": config["custom_instructions"],
    }


def test_mem0_library_config_builder_supports_unauthenticated_milvus() -> None:
    from thinkback.memory.backends import build_mem0_library_config

    config = build_mem0_library_config(
        llm_api_key="openai-secret",
        llm_base_url="",
        embedding_api_key="",
        embedding_base_url="",
        milvus_url="http://milvus.internal:19530",
        milvus_token="",
        milvus_database="default",
        collection_name="thinkback_memories",
        llm_model="gpt-5-mini",
        embedding_model="text-embedding-3-small",
        history_db_path=".mem0/history.db",
        embedding_model_dims=1536,
    )

    assert config["vector_store"]["config"] == {
        "collection_name": "thinkback_memories",
        "url": "http://milvus.internal:19530",
        "token": "",
        "db_name": "default",
        "embedding_model_dims": 1536,
        "metric_type": "COSINE",
    }


def test_openai_compatible_embedding_omits_dimensions_and_uses_dummy_key(monkeypatch) -> None:
    from mem0.configs.embeddings.base import BaseEmbedderConfig

    from thinkback.memory import embeddings
    from thinkback.memory.embeddings import OpenAICompatibleEmbeddingNoDimensions

    observed: dict[str, Any] = {}

    class FakeEmbeddings:
        def create(self, **kwargs):  # type: ignore[no-untyped-def]
            observed["create_kwargs"] = kwargs

            class FakeDatum:
                embedding = [0.1, 0.2, 0.3]

            class FakeResponse:
                data = [FakeDatum()]

            return FakeResponse()

    class FakeOpenAI:
        def __init__(self, **kwargs):  # type: ignore[no-untyped-def]
            observed["client_kwargs"] = kwargs
            self.embeddings = FakeEmbeddings()

    monkeypatch.setenv("MEMORY_LLM_KEY", "llm-secret")
    monkeypatch.setattr(embeddings, "OpenAI", FakeOpenAI)

    embedder = OpenAICompatibleEmbeddingNoDimensions(
        BaseEmbedderConfig(
            model="default-embedding",
            api_key="",
            openai_base_url="http://embedding.example.internal:7345/v1",
            embedding_dims=1024,
        )
    )

    result = embedder.embed("ping\npong", memory_action="search")

    assert result == [0.1, 0.2, 0.3]
    client_kwargs = observed["client_kwargs"]
    assert client_kwargs["api_key"] == "not-required"
    assert client_kwargs["base_url"] == "http://embedding.example.internal:7345/v1"
    # 内网网关不该被环境里的代理劫持 → 客户端不信任环境代理
    assert client_kwargs["http_client"].trust_env is False
    assert observed["create_kwargs"] == {
        "input": ["ping pong"],
        "model": "default-embedding",
    }


def test_mem0_library_backend_registers_custom_embedder_provider(monkeypatch, tmp_path) -> None:
    from mem0.utils.factory import EmbedderFactory

    from thinkback.memory.backends import Mem0LibraryMemoryBackend

    observed: dict[str, Any] = {}
    original_mapping = dict(EmbedderFactory.provider_to_class)

    class FakeMemory:
        @classmethod
        def from_config(cls, config):  # type: ignore[no-untyped-def]
            observed["config"] = config
            observed["provider_path"] = EmbedderFactory.provider_to_class.get("openai")
            return cls()

    monkeypatch.setitem(
        __import__("sys").modules, "mem0", type("FakeMem0", (), {"Memory": FakeMemory})
    )

    try:
        backend = Mem0LibraryMemoryBackend(
            config={
                "history_db_path": str(tmp_path / "history.db"),
                "embedder": {"provider": "openai", "config": {}},
            }
        )

        assert isinstance(backend.memory_client, FakeMemory)
        assert observed["provider_path"] == (
            "thinkback.memory.embeddings.OpenAICompatibleEmbeddingNoDimensions"
        )
        assert observed["config"]["embedder"]["provider"] == "openai"
    finally:
        EmbedderFactory.provider_to_class.clear()
        EmbedderFactory.provider_to_class.update(original_mapping)


def test_mem0_library_backend_logs_english_call_summaries_without_content() -> None:
    from thinkback.memory.backends import Mem0LibraryMemoryBackend

    class FakeMemoryClient:
        def add(self, messages, **kwargs):  # type: ignore[no-untyped-def]
            _ = messages
            _ = kwargs
            return {"results": [{"id": "m1", "memory": "private memory", "event": "ADD"}]}

        def search(self, query, **kwargs):  # type: ignore[no-untyped-def]
            _ = query
            _ = kwargs
            return {"results": []}

    sink: list[str] = []
    handler_id = logger.add(sink.append, level="INFO", format="{message} {extra}")
    backend = Mem0LibraryMemoryBackend(
        config={"history_db_path": ".mem0/history.db"},
        memory_client=FakeMemoryClient(),
        max_concurrent_calls=2,
    )
    try:
        backend.add(
            [{"role": "user", "content": "backend secret text"}],
            user_id="user-1",
            memory_scope_id="thinkback",
        )
        backend.search(
            "backend query text",
            user_id="user-1",
            memory_scope_id="thinkback",
            limit=3,
        )
    finally:
        logger.remove(handler_id)

    logs = "\n".join(sink)
    assert "memory backend call started" in logs
    assert "memory backend call completed" in logs
    assert "operation" in logs
    assert "max_concurrent_calls" in logs
    assert "backend secret text" not in logs
    assert "backend query text" not in logs


def test_mem0_library_backend_disables_mem0_telemetry_capture(monkeypatch) -> None:
    import mem0
    import mem0.memory.main as mem0_main
    import mem0.memory.telemetry as mem0_telemetry

    from thinkback.memory.backends import Mem0LibraryMemoryBackend

    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def leaky_capture_event(*args: Any, **kwargs: Any) -> None:
        calls.append((args, kwargs))

    class FakeMemory:
        @classmethod
        def from_config(cls, config):  # type: ignore[no-untyped-def]
            _ = config
            mem0_main.capture_event("mem0.init", cls())
            mem0_telemetry.capture_event("mem0.add", cls())
            return cls()

    monkeypatch.setattr(mem0, "Memory", FakeMemory)
    monkeypatch.setattr(mem0_main, "capture_event", leaky_capture_event)
    monkeypatch.setattr(mem0_telemetry, "capture_event", leaky_capture_event)

    backend = Mem0LibraryMemoryBackend(config={"history_db_path": ".mem0/history.db"})

    assert isinstance(backend.memory_client, FakeMemory)
    assert calls == []


def test_disable_mem0_telemetry_is_thread_stable() -> None:
    import threading

    import mem0.memory.telemetry as mem0_telemetry

    from thinkback.memory.backends import disable_mem0_telemetry

    disable_mem0_telemetry()
    before = len(threading.enumerate())

    for _ in range(20):
        mem0_telemetry.capture_event("mem0.add", object())

    assert len(threading.enumerate()) == before


def test_mem0_library_backend_maps_add_search_update_delete_and_scoped_delete_all_with_limit() -> (
    None
):
    from thinkback.memory.backends import Mem0LibraryMemoryBackend

    class FakeMemoryClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple, dict]] = []
            self.delete_all_called = False

        def add(self, messages, **kwargs):  # type: ignore[no-untyped-def]
            self.calls.append(("add", (messages,), kwargs))
            return {"results": [{"id": "m1", "memory": "用户喜欢猫", "event": "ADD", "score": 1.0}]}

        def search(self, query, *, filters, limit, threshold):  # type: ignore[no-untyped-def]
            kwargs = {"filters": filters, "limit": limit, "threshold": threshold}
            self.calls.append(("search", (query,), kwargs))
            return {"results": [{"id": "m1", "memory": "用户喜欢猫", "score": 0.9}]}

        def get_all(self, *, filters, limit):  # type: ignore[no-untyped-def]
            kwargs = {"filters": filters, "limit": limit}
            self.calls.append(("get_all", (), kwargs))
            return {"results": [{"id": "m1"}, {"id": "m2"}]}

        def update(self, memory_id, data):  # type: ignore[no-untyped-def]
            self.calls.append(("update", (memory_id, data), {}))
            return {"message": "Memory updated successfully!"}

        def delete(self, memory_id):  # type: ignore[no-untyped-def]
            self.calls.append(("delete", (memory_id,), {}))
            return {"message": "Memory deleted successfully!"}

        def delete_all(self, **kwargs):  # type: ignore[no-untyped-def]
            self.delete_all_called = True
            raise AssertionError("library backend must not call mem0 delete_all() directly")

    client = FakeMemoryClient()
    backend = Mem0LibraryMemoryBackend(
        config={"history_db_path": ".mem0/history.db"}, memory_client=client
    )

    add_events = backend.add(
        [{"role": "user", "content": "我喜欢猫"}],
        user_id="user-1",
        memory_scope_id="thinkback",
        metadata={"memory_type": "preference"},
    )
    search_results = backend.search("猫", user_id="user-1", memory_scope_id="thinkback", limit=3)
    threshold_results = backend.search(
        "猫", user_id="user-1", memory_scope_id="thinkback", limit=3, threshold=0.7
    )
    backend.update("m1", "用户喜欢猫")
    backend.delete("m1")
    deleted = backend.delete_all(user_id="user-1", memory_scope_id="thinkback")

    assert add_events == [{"id": "m1", "memory": "用户喜欢猫", "event": "ADD", "score": 1.0}]
    assert search_results == [{"id": "m1", "memory": "用户喜欢猫", "score": 0.9}]
    assert threshold_results == [{"id": "m1", "memory": "用户喜欢猫", "score": 0.9}]
    assert deleted == ["m1", "m2"]
    assert client.delete_all_called is False
    assert client.calls == [
        (
            "add",
            ([{"role": "user", "content": "我喜欢猫"}],),
            {
                "user_id": "user-1",
                "agent_id": "thinkback",
                "metadata": {"memory_type": "preference", "memory_scope_id": "thinkback"},
                "infer": True,
                # 抽取指令随每次写入传：引擎的 prompt 覆盖配置级默认值，
                # 没给就等于裸奔（ADR-0014）。
                "prompt": DEFAULT_EXTRACTION_INSTRUCTIONS,
            },
        ),
        (
            "search",
            ("猫",),
            {
                "filters": {"user_id": "user-1", "agent_id": "thinkback"},
                "limit": 3,
                "threshold": None,
            },
        ),
        (
            "search",
            ("猫",),
            {
                "filters": {"user_id": "user-1", "agent_id": "thinkback"},
                "limit": 3,
                "threshold": 0.7,
            },
        ),
        ("update", ("m1", "用户喜欢猫"), {}),
        ("delete", ("m1",), {}),
        (
            "get_all",
            (),
            {
                "filters": {"user_id": "user-1", "agent_id": "thinkback"},
                "limit": MEMORY_LIST_PAGE_LIMIT,
            },
        ),
        ("delete", ("m1",), {}),
        ("delete", ("m2",), {}),
    ]


@pytest.mark.parametrize("first_result", ["skipped", "failed"])
def test_mem0_library_backend_retries_collection_consistency_until_succeeded(
    monkeypatch: pytest.MonkeyPatch,
    first_result: str,
) -> None:
    """集合还没建好/改级失败时不能记为已完成：下一次写入必须再试一次。

    记成完成会让集合永远停在 Bounded —— 写后立刻读不到刚写的向量。
    """
    from thinkback.memory.backends import Mem0LibraryMemoryBackend

    results = [first_result, "Strong"]
    calls: list[dict[str, Any]] = []

    def fake_ensure(**kwargs: Any) -> str:
        calls.append(kwargs)
        return results[min(len(calls) - 1, len(results) - 1)]

    monkeypatch.setattr("thinkback.infra.milvus.ensure_milvus_collection_consistency", fake_ensure)

    class FakeMemoryClient:
        def add(self, messages, **kwargs):  # type: ignore[no-untyped-def]
            _ = messages
            _ = kwargs
            return {"results": [{"id": "m1", "memory": "用户喜欢猫", "event": "ADD"}]}

    backend = Mem0LibraryMemoryBackend(
        config={
            "history_db_path": ".mem0/history.db",
            "vector_store": {
                "config": {
                    "collection_name": "thinkback_memories",
                    "url": "http://milvus:19530",
                    "db_name": "thinkback",
                    "token": "",
                }
            },
        },
        memory_client=FakeMemoryClient(),
    )

    backend.add(
        [{"role": "user", "content": "我喜欢猫"}],
        user_id="user-1",
        memory_scope_id="thinkback",
    )

    assert len(calls) == 1
    assert calls[0]["collection_name"] == "thinkback_memories"
    assert calls[0]["milvus_database"] == "thinkback"
    assert calls[0]["level"] == "Strong"
    assert backend._consistency_ensured is False

    backend.add(
        [{"role": "user", "content": "我喜欢猫"}],
        user_id="user-1",
        memory_scope_id="thinkback",
    )

    assert len(calls) == 2
    assert backend._consistency_ensured is True

    # 提到 Strong 之后不再重复调用。
    backend.add(
        [{"role": "user", "content": "我喜欢猫"}],
        user_id="user-1",
        memory_scope_id="thinkback",
    )

    assert len(calls) == 2


def test_mem0_library_backend_delete_ignores_missing_milvus_vector() -> None:
    from thinkback.memory.backends import Mem0LibraryMemoryBackend

    class FakeMemoryClient:
        def delete(self, memory_id):  # type: ignore[no-untyped-def]
            _ = memory_id
            raise IndexError("list index out of range")

    backend = Mem0LibraryMemoryBackend(
        config={"history_db_path": ".mem0/history.db"},
        memory_client=FakeMemoryClient(),
    )

    backend.delete("already-missing")


def test_mem0_library_backend_delete_still_raises_unexpected_errors() -> None:
    from thinkback.memory.backends import Mem0LibraryMemoryBackend

    class FakeMemoryClient:
        def delete(self, memory_id):  # type: ignore[no-untyped-def]
            _ = memory_id
            raise RuntimeError("milvus unavailable")

    backend = Mem0LibraryMemoryBackend(
        config={"history_db_path": ".mem0/history.db"},
        memory_client=FakeMemoryClient(),
    )

    with pytest.raises(RuntimeError, match="milvus unavailable"):
        backend.delete("m1")


def test_mem0_library_backend_update_retries_not_found_visibility_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """add 后紧邻 update 的 pk 点查不可见窗口：not found 应退避重试后成功。"""

    from thinkback.memory.backends import Mem0LibraryMemoryBackend

    class FlakyMemoryClient:
        def __init__(self) -> None:
            self.update_calls = 0

        def update(self, memory_id, data):  # type: ignore[no-untyped-def]
            self.update_calls += 1
            if self.update_calls <= 2:
                raise ValueError(
                    f"Memory with id {memory_id} not found. Please provide a valid 'memory_id'"
                )
            return {"message": "Memory updated successfully!"}

    sleeps: list[float] = []
    monkeypatch.setattr("thinkback.memory.backends.mem0_library.time.sleep", sleeps.append)

    client = FlakyMemoryClient()
    backend = Mem0LibraryMemoryBackend(
        config={"history_db_path": ".mem0/history.db"}, memory_client=client
    )

    backend.update("m1", "用户喜欢猫")

    assert client.update_calls == 3
    assert sleeps == [0.5, 1.0]


def test_mem0_library_backend_update_does_not_retry_unrelated_valueerrors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """not found 之外的 ValueError（真实缺失/配置错误）不重试，直接上抛。"""

    from thinkback.memory.backends import Mem0LibraryMemoryBackend

    class FakeMemoryClient:
        def update(self, memory_id, data):  # type: ignore[no-untyped-def]
            raise ValueError("expiration_date must be YYYY-MM-DD")

    sleeps: list[float] = []
    monkeypatch.setattr("thinkback.memory.backends.mem0_library.time.sleep", sleeps.append)

    backend = Mem0LibraryMemoryBackend(
        config={"history_db_path": ".mem0/history.db"}, memory_client=FakeMemoryClient()
    )

    with pytest.raises(RuntimeError, match="expiration_date"):
        backend.update("m1", "用户喜欢猫")
    assert sleeps == []


def test_mem0_library_backend_update_gives_up_after_bounded_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """持续 not found 时按退避序列重试满后上抛，不无限循环。"""

    from thinkback.memory.backends import Mem0LibraryMemoryBackend

    class AlwaysMissingClient:
        def update(self, memory_id, data):  # type: ignore[no-untyped-def]
            raise ValueError(f"Memory with id {memory_id} not found")

    sleeps: list[float] = []
    monkeypatch.setattr("thinkback.memory.backends.mem0_library.time.sleep", sleeps.append)

    client = AlwaysMissingClient()
    backend = Mem0LibraryMemoryBackend(
        config={"history_db_path": ".mem0/history.db"}, memory_client=client
    )

    with pytest.raises(RuntimeError, match="not found"):
        backend.update("m1", "用户喜欢猫")
    assert sleeps == [0.5, 1.0, 2.0]


def test_mem0_library_backend_supports_legacy_entity_scope_kwargs() -> None:
    from thinkback.memory.backends import Mem0LibraryMemoryBackend

    class FakeLegacyMemoryClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple, dict]] = []

        def search(self, query, *, user_id, agent_id, limit, filters, threshold):  # type: ignore[no-untyped-def]
            kwargs = {
                "user_id": user_id,
                "agent_id": agent_id,
                "limit": limit,
                "filters": filters,
                "threshold": threshold,
            }
            self.calls.append(("search", (query,), kwargs))
            return {"results": [{"id": "m1", "memory": "用户喜欢猫", "score": 0.9}]}

        def get_all(self, *, user_id, agent_id, limit, filters):  # type: ignore[no-untyped-def]
            kwargs = {"user_id": user_id, "agent_id": agent_id, "limit": limit, "filters": filters}
            self.calls.append(("get_all", (), kwargs))
            return {"results": [{"id": "m1"}]}

        def delete(self, memory_id):  # type: ignore[no-untyped-def]
            self.calls.append(("delete", (memory_id,), {}))
            return {"message": "Memory deleted successfully!"}

    client = FakeLegacyMemoryClient()
    backend = Mem0LibraryMemoryBackend(
        config={"history_db_path": ".mem0/history.db"}, memory_client=client
    )

    assert backend.search("猫", user_id="user-1", memory_scope_id="thinkback", limit=3) == [
        {"id": "m1", "memory": "用户喜欢猫", "score": 0.9}
    ]
    assert backend.delete_all(user_id="user-1", memory_scope_id="thinkback") == ["m1"]
    assert client.calls == [
        (
            "search",
            ("猫",),
            {
                "user_id": "user-1",
                "agent_id": "thinkback",
                "limit": 3,
                "filters": None,
                "threshold": None,
            },
        ),
        (
            "get_all",
            (),
            {
                "user_id": "user-1",
                "agent_id": "thinkback",
                "limit": MEMORY_LIST_PAGE_LIMIT,
                "filters": None,
            },
        ),
        ("delete", ("m1",), {}),
    ]


def test_mem0_library_backend_supports_top_k_mem0_versions() -> None:
    from thinkback.memory.backends import Mem0LibraryMemoryBackend

    class FakeTopKMemoryClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple, dict]] = []

        def search(self, query, *, filters, top_k, threshold):  # type: ignore[no-untyped-def]
            kwargs = {"filters": filters, "top_k": top_k, "threshold": threshold}
            self.calls.append(("search", (query,), kwargs))
            return {"results": [{"id": "m1", "memory": "用户喜欢猫", "score": 0.9}]}

        def get_all(self, *, filters, top_k):  # type: ignore[no-untyped-def]
            kwargs = {"filters": filters, "top_k": top_k}
            self.calls.append(("get_all", (), kwargs))
            return {"results": [{"id": "m1"}]}

        def delete(self, memory_id):  # type: ignore[no-untyped-def]
            self.calls.append(("delete", (memory_id,), {}))
            return {"message": "Memory deleted successfully!"}

    client = FakeTopKMemoryClient()
    backend = Mem0LibraryMemoryBackend(
        config={"history_db_path": ".mem0/history.db"}, memory_client=client
    )

    assert backend.search("猫", user_id="user-1", memory_scope_id="thinkback", limit=3) == [
        {"id": "m1", "memory": "用户喜欢猫", "score": 0.9}
    ]
    assert backend.delete_all(user_id="user-1", memory_scope_id="thinkback") == ["m1"]
    assert client.calls == [
        (
            "search",
            ("猫",),
            {
                "filters": {"user_id": "user-1", "agent_id": "thinkback"},
                "top_k": 3,
                "threshold": None,
            },
        ),
        (
            "get_all",
            (),
            {
                "filters": {"user_id": "user-1", "agent_id": "thinkback"},
                "top_k": MEMORY_LIST_PAGE_LIMIT,
            },
        ),
        ("delete", ("m1",), {}),
    ]


def test_mem0_library_backend_health_check_uses_read_only_query() -> None:
    """探活只读：不得写任何数据。

    原实现真的 add 一条探针记忆再 delete —— k8s 探针 10s 一次 = 一次 LLM +
    一次 embedding + 一次 Milvus 写 + 一次删。
    """

    from thinkback.memory.backends import Mem0LibraryMemoryBackend

    class FakeMemoryClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple, dict]] = []

        def add(self, messages, **kwargs):  # type: ignore[no-untyped-def]
            self.calls.append(("add", (messages,), kwargs))
            return {"results": [{"id": "health-memory", "memory": "health", "event": "ADD"}]}

        def delete(self, memory_id):  # type: ignore[no-untyped-def]
            self.calls.append(("delete", (memory_id,), {}))
            return {"message": "Memory deleted successfully!"}

        def get_all(self, *, filters, limit):  # type: ignore[no-untyped-def]
            self.calls.append(("get_all", (), {"filters": filters, "limit": limit}))
            return {"results": []}

    client = FakeMemoryClient()
    backend = Mem0LibraryMemoryBackend(
        config={"history_db_path": ".mem0/history.db"}, memory_client=client
    )

    status = backend.health_check()

    assert status == {"status": "ready", "detail": "ok"}
    assert [call[0] for call in client.calls] == ["get_all"]
    assert client.calls[0][2]["filters"]["user_id"] == "thinkback-health"
    assert client.calls[0][2]["filters"]["agent_id"] == "thinkback-health"
    assert client.calls[0][2]["limit"] == 1


def test_mem0_library_backend_health_check_reports_query_failure() -> None:
    from thinkback.memory.backends import Mem0LibraryMemoryBackend

    class FakeMemoryClient:
        def get_all(self, **kwargs):  # type: ignore[no-untyped-def]
            _ = kwargs
            raise RuntimeError("connection refused")

    backend = Mem0LibraryMemoryBackend(
        config={"history_db_path": ".mem0/history.db"}, memory_client=FakeMemoryClient()
    )

    status = backend.health_check()

    assert status["status"] == "not_ready"
    assert "mem0 library get_all failed" in status["detail"]
    assert "connection refused" in status["detail"]


def test_mem0_library_backend_health_check_never_writes() -> None:
    """回归：探活路径上不允许出现任何写操作。"""

    from thinkback.memory.backends import Mem0LibraryMemoryBackend

    class FakeMemoryClient:
        def __init__(self) -> None:
            self.writes: list[str] = []

        def add(self, messages, **kwargs):  # type: ignore[no-untyped-def]
            self.writes.append("add")
            return {"results": []}

        def delete(self, memory_id):  # type: ignore[no-untyped-def]
            self.writes.append("delete")
            return {}

        def update(self, memory_id, data):  # type: ignore[no-untyped-def]
            self.writes.append("update")
            return {}

        def delete_all(self, **kwargs):  # type: ignore[no-untyped-def]
            self.writes.append("delete_all")
            return {}

        def get_all(self, **kwargs):  # type: ignore[no-untyped-def]
            return {"results": []}

    client = FakeMemoryClient()
    backend = Mem0LibraryMemoryBackend(
        config={"history_db_path": ".mem0/history.db"}, memory_client=client
    )

    status = backend.health_check()

    assert status == {"status": "ready", "detail": "ok"}
    assert client.writes == []


def test_mem0_library_backend_creates_history_db_parent_directory(tmp_path) -> None:
    from thinkback.memory.backends import Mem0LibraryMemoryBackend

    history_db_path = tmp_path / "nested" / "history.db"
    observed: dict[str, bool] = {}

    class FakeMemoryClient:
        pass

    def memory_factory(config):  # type: ignore[no-untyped-def]
        observed["parent_exists"] = history_db_path.parent.exists()
        observed["history_db_path"] = config["history_db_path"]
        return FakeMemoryClient()

    backend = Mem0LibraryMemoryBackend(
        config={"history_db_path": str(history_db_path)},
        memory_factory=memory_factory,
    )

    assert backend.memory_client
    assert observed == {
        "parent_exists": True,
        "history_db_path": str(history_db_path),
    }


def test_mem0_library_backend_builds_lazy_client_once_under_concurrent_access() -> None:
    from thinkback.memory.backends import Mem0LibraryMemoryBackend

    class FakeMemoryClient:
        pass

    calls = 0
    calls_lock = Lock()

    def memory_factory(_config):  # type: ignore[no-untyped-def]
        nonlocal calls
        with calls_lock:
            calls += 1
        sleep(0.05)
        return FakeMemoryClient()

    backend = Mem0LibraryMemoryBackend(
        config={"history_db_path": ".mem0/history.db"},
        memory_factory=memory_factory,
    )

    with ThreadPoolExecutor(max_workers=8) as executor:
        clients = list(executor.map(lambda _index: backend.memory_client, range(8)))

    assert calls == 1
    assert len({id(client) for client in clients}) == 1


def test_mem0_library_backend_allows_calls_to_initialized_client_concurrently() -> None:
    from thinkback.memory.backends import Mem0LibraryMemoryBackend

    class BlockingMemoryClient:
        def __init__(self) -> None:
            self.active_calls = 0
            self.max_active_calls = 0
            self.lock = Lock()
            self.add_entered = Event()
            self.search_entered = Event()
            self.release_add = Event()

        def add(self, messages, **kwargs):  # type: ignore[no-untyped-def]
            _ = messages
            _ = kwargs
            self._enter()
            self.add_entered.set()
            try:
                self.release_add.wait(timeout=5)
                return {"results": [{"id": "m1", "memory": "用户喜欢茶", "event": "ADD"}]}
            finally:
                self._exit()

        def search(self, query, *, filters, limit, threshold):  # type: ignore[no-untyped-def]
            _ = query
            _ = filters
            _ = limit
            _ = threshold
            self._enter()
            self.search_entered.set()
            try:
                return {"results": [{"id": "m1", "memory": "用户喜欢茶"}]}
            finally:
                self._exit()

        def _enter(self) -> None:
            with self.lock:
                self.active_calls += 1
                self.max_active_calls = max(self.max_active_calls, self.active_calls)

        def _exit(self) -> None:
            with self.lock:
                self.active_calls -= 1

    client = BlockingMemoryClient()
    backend = Mem0LibraryMemoryBackend(
        config={"history_db_path": ".mem0/history.db"},
        memory_client=client,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        add_future = executor.submit(
            backend.add,
            [{"role": "user", "content": "我喜欢茶"}],
            user_id="user-1",
            memory_scope_id="thinkback",
        )
        assert client.add_entered.wait(timeout=1)

        search_future = executor.submit(
            backend.search,
            "茶",
            user_id="user-1",
            memory_scope_id="thinkback",
            limit=1,
        )
        assert client.search_entered.wait(timeout=1)

        client.release_add.set()
        assert add_future.result(timeout=1)[0]["event"] == "ADD"
        assert search_future.result(timeout=1)[0]["memory"] == "用户喜欢茶"

    assert client.max_active_calls == 2


def test_mem0_library_backend_runs_mem0_internal_thread_pool_inline() -> None:
    import concurrent.futures

    import mem0.memory.main as mem0_main

    from thinkback.memory.backends import Mem0LibraryMemoryBackend

    original_mem0_concurrent = mem0_main.concurrent
    original_std_executor = concurrent.futures.ThreadPoolExecutor

    class FakeMem0Client:
        __module__ = "mem0.memory.main"

        def add(self, messages, **kwargs):  # type: ignore[no-untyped-def]
            _ = messages
            _ = kwargs
            with mem0_main.concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                future = executor.submit(get_ident)
                mem0_main.concurrent.futures.wait([future])
                worker_thread_id = future.result(timeout=1)
            return {"results": [{"id": "m1", "memory": str(worker_thread_id), "event": "ADD"}]}

    try:
        caller_thread_id = get_ident()
        backend = Mem0LibraryMemoryBackend(
            config={"history_db_path": ".mem0/history.db"},
            memory_client=FakeMem0Client(),
        )

        result = backend.add(
            [{"role": "user", "content": "我喜欢茶"}],
            user_id="user-1",
            memory_scope_id="thinkback",
        )

        assert result[0]["memory"] == str(caller_thread_id)
        assert concurrent.futures.ThreadPoolExecutor is original_std_executor
    finally:
        mem0_main.concurrent = original_mem0_concurrent


def test_mem0_library_backend_limits_concurrent_mem0_calls() -> None:
    from thinkback.memory.backends import Mem0LibraryMemoryBackend

    class BlockingMemoryClient:
        def __init__(self) -> None:
            self.entered = 0
            self.active_calls = 0
            self.max_active_calls = 0
            self.lock = Lock()
            self.first_two_entered = Event()
            self.release_calls = Event()

        def search(self, query, *, filters, limit, threshold):  # type: ignore[no-untyped-def]
            _ = query
            _ = filters
            _ = limit
            _ = threshold
            with self.lock:
                self.entered += 1
                self.active_calls += 1
                self.max_active_calls = max(self.max_active_calls, self.active_calls)
                if self.entered == 2:
                    self.first_two_entered.set()
            try:
                self.release_calls.wait(timeout=5)
                return {"results": [{"id": "m1", "memory": "用户喜欢茶"}]}
            finally:
                with self.lock:
                    self.active_calls -= 1

    client = BlockingMemoryClient()
    backend = Mem0LibraryMemoryBackend(
        config={"history_db_path": ".mem0/history.db"},
        memory_client=client,
        max_concurrent_calls=2,
    )
    start = Barrier(4)

    def search() -> list[dict[str, Any]]:
        start.wait(timeout=3)
        return backend.search(
            "茶",
            user_id="user-1",
            memory_scope_id="thinkback",
            limit=1,
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(search) for _ in range(8)]
        assert client.first_two_entered.wait(timeout=1)
        sleep(0.05)
        with client.lock:
            entered_before_release = client.entered
        client.release_calls.set()

        assert all(future.result(timeout=1)[0]["memory"] == "用户喜欢茶" for future in futures)

    assert entered_before_release == 2
    assert client.max_active_calls == 2


def test_mem0_library_backend_does_not_spawn_mem0_threads_during_concurrent_calls() -> None:
    import threading

    import mem0.memory.main as mem0_main

    from thinkback.memory.backends import Mem0LibraryMemoryBackend

    original_mem0_concurrent = mem0_main.concurrent
    thread_ids: set[int] = set()
    thread_ids_lock = Lock()

    class FakeMem0Client:
        def search(self, query, *, filters, limit, threshold):  # type: ignore[no-untyped-def]
            _ = query
            _ = filters
            _ = limit
            _ = threshold
            with mem0_main.concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                futures = [executor.submit(threading.get_ident) for _ in range(8)]
                mem0_main.concurrent.futures.wait(futures)
                with thread_ids_lock:
                    thread_ids.update(future.result(timeout=1) for future in futures)
            return {"results": [{"id": "m1", "memory": "用户喜欢茶"}]}

    backend = Mem0LibraryMemoryBackend(
        config={"history_db_path": ".mem0/history.db"},
        memory_client=FakeMem0Client(),
        max_concurrent_calls=4,
    )
    start = Barrier(4)

    def search() -> list[dict[str, Any]]:
        start.wait(timeout=3)
        return backend.search(
            "茶",
            user_id="user-1",
            memory_scope_id="thinkback",
            limit=1,
        )

    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(search) for _ in range(4)]
            caller_thread_ids = {future.result(timeout=3)[0]["memory"] for future in futures}

        assert caller_thread_ids == {"用户喜欢茶"}
        assert len(thread_ids) <= 4
    finally:
        mem0_main.concurrent = original_mem0_concurrent


def test_mem0_library_backend_delete_retries_not_found_then_converges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """mem0 v2 窗口性 not found：退避重试后成功删除（supersede 清理场景）。"""

    from thinkback.memory.backends import Mem0LibraryMemoryBackend

    class FlakyMemoryClient:
        def __init__(self) -> None:
            self.delete_calls = 0

        def delete(self, memory_id):  # type: ignore[no-untyped-def]
            self.delete_calls += 1
            if self.delete_calls == 1:
                raise ValueError(f"Memory with id {memory_id} not found")
            return {"message": "Memory deleted successfully!"}

    sleeps: list[float] = []
    monkeypatch.setattr("thinkback.memory.backends.mem0_library.time.sleep", sleeps.append)

    client = FlakyMemoryClient()
    backend = Mem0LibraryMemoryBackend(
        config={"history_db_path": ".mem0/history.db"}, memory_client=client
    )

    backend.delete("m1")

    assert client.delete_calls == 2
    assert sleeps == [0.5]


def test_mem0_library_backend_delete_not_found_after_retries_is_idempotent_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重试预算耗尽仍 not found：与并发已删除不可区分，幂等跳过不上抛。"""

    from thinkback.memory.backends import Mem0LibraryMemoryBackend

    class AlwaysMissingClient:
        def delete(self, memory_id):  # type: ignore[no-untyped-def]
            raise ValueError(f"Memory with id {memory_id} not found")

    sleeps: list[float] = []
    monkeypatch.setattr("thinkback.memory.backends.mem0_library.time.sleep", sleeps.append)

    backend = Mem0LibraryMemoryBackend(
        config={"history_db_path": ".mem0/history.db"}, memory_client=AlwaysMissingClient()
    )

    backend.delete("m1")  # 不抛即通过

    assert sleeps == [0.5, 1.0, 2.0]


def test_mem0_library_backend_delete_does_not_retry_unrelated_valueerrors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """not found 之外的 ValueError 不重试直接上抛（真实故障要暴露）。"""

    from thinkback.memory.backends import Mem0LibraryMemoryBackend

    class FakeMemoryClient:
        def delete(self, memory_id):  # type: ignore[no-untyped-def]
            raise ValueError("vector store connection refused")

    sleeps: list[float] = []
    monkeypatch.setattr("thinkback.memory.backends.mem0_library.time.sleep", sleeps.append)

    backend = Mem0LibraryMemoryBackend(
        config={"history_db_path": ".mem0/history.db"}, memory_client=FakeMemoryClient()
    )

    with pytest.raises(RuntimeError, match="connection refused"):
        backend.delete("m1")
    assert sleeps == []


def test_mem0_library_backend_purge_history_removes_history_and_messages(
    tmp_path: Path,
) -> None:
    """物理擦除引擎侧残留：按 memory_id 删历史，按消息正文删原始消息。"""
    import sqlite3

    from thinkback.memory.backends import Mem0LibraryMemoryBackend

    db_path = tmp_path / "history.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        """
        CREATE TABLE history (
            id TEXT PRIMARY KEY, memory_id TEXT, old_memory TEXT, new_memory TEXT,
            event TEXT, created_at DATETIME, updated_at DATETIME,
            is_deleted INTEGER, actor_id TEXT, role TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE messages (
            id TEXT PRIMARY KEY, session_scope TEXT, role TEXT,
            content TEXT, name TEXT, created_at DATETIME
        )
        """
    )
    connection.execute(
        "INSERT INTO history (id, memory_id, old_memory, new_memory, event)"
        " VALUES ('h1', 'm1', '旧事实 SECRET', '新事实', 'UPDATE')"
    )
    connection.execute(
        "INSERT INTO history (id, memory_id, old_memory, new_memory, event)"
        " VALUES ('h2', 'm2', '别人的事实', '别的', 'ADD')"
    )
    connection.execute(
        "INSERT INTO messages (id, session_scope, role, content)"
        " VALUES ('msg1', 'agent_id=thinkback&user_id=u1', 'user', '原始对话 SECRET')"
    )
    connection.execute(
        "INSERT INTO messages (id, session_scope, role, content)"
        " VALUES ('msg2', 'agent_id=thinkback&user_id=u1', 'user', '别的会话的对话')"
    )
    connection.execute(
        "INSERT INTO messages (id, session_scope, role, content)"
        " VALUES ('msg3', 'agent_id=other&user_id=u1', 'user', '别人的对话')"
    )
    connection.commit()
    connection.close()

    backend = Mem0LibraryMemoryBackend(config={"history_db_path": str(db_path)})
    counts = backend.purge_history(
        user_id="u1",
        memory_ids=["m1"],
        namespace="thinkback",
        message_texts=["原始对话 SECRET"],
        purge_all_messages=False,
    )

    assert counts == {"history": 1, "messages": 1, "supported": True}

    connection = sqlite3.connect(db_path)
    try:
        history_ids = [row[0] for row in connection.execute("SELECT id FROM history")]
        message_ids = [row[0] for row in connection.execute("SELECT id FROM messages")]
    finally:
        connection.close()
    # 目标行没了；同一 namespace 下别的会话的消息、以及别的 namespace 的消息都在
    assert history_ids == ["h2"]
    assert message_ids == ["msg2", "msg3"]


def test_mem0_library_backend_purge_history_fails_visible_on_schema_drift(
    tmp_path: Path,
) -> None:
    """引擎历史表结构漂移时必须报错，不能静默跳过后仍报「已擦除」。

    mem0 升级把 history 表改名 → 旧写法是「表不在就跳过」，擦除任务照样
    标 completed，而旧事实原文还躺在引擎历史里 —— 「物理擦除」这一档
    对外承诺就是假的。契约要求 fail-visible（插件化设计 §7）。
    """
    import sqlite3

    from thinkback.memory.backends import Mem0LibraryMemoryBackend

    db_path = tmp_path / "history.db"
    connection = sqlite3.connect(db_path)
    # 表被改名了（mem0 升级漂移的模拟）
    connection.execute("CREATE TABLE memory_history (memory_id TEXT, old_memory TEXT)")
    connection.execute("INSERT INTO memory_history VALUES ('m1', '旧事实 SECRET')")
    connection.commit()
    connection.close()

    backend = Mem0LibraryMemoryBackend(config={"history_db_path": str(db_path)})

    with pytest.raises(RuntimeError, match="history table is missing"):
        backend.purge_history(
            user_id="u1",
            memory_ids=["m1"],
            namespace="thinkback",
            message_texts=[],
            purge_all_messages=False,
        )

    # 关键：原文必须还在（没有"顺手删掉"），且调用方拿不到「已清理」的计数
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute("SELECT old_memory FROM memory_history").fetchall()
    finally:
        connection.close()
    assert rows == [("旧事实 SECRET",)]


def test_mem0_library_backend_purge_history_fails_visible_on_column_drift(
    tmp_path: Path,
) -> None:
    """表还在但列名变了（memory_id → id）同样 fail-visible。"""
    import sqlite3

    from thinkback.memory.backends import Mem0LibraryMemoryBackend

    db_path = tmp_path / "history.db"
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE history (id TEXT, old_memory TEXT)")
    connection.execute("INSERT INTO history VALUES ('m1', '旧事实 SECRET')")
    connection.commit()
    connection.close()

    backend = Mem0LibraryMemoryBackend(config={"history_db_path": str(db_path)})

    with pytest.raises(RuntimeError, match="schema is incompatible"):
        backend.purge_history(
            user_id="u1",
            memory_ids=["m1"],
            namespace="thinkback",
            message_texts=[],
            purge_all_messages=False,
        )


def test_mem0_library_backend_purge_history_all_scope_clears_namespace_messages(
    tmp_path: Path,
) -> None:
    """ALL 范围清空该 namespace 下的全部原始消息。"""
    import sqlite3

    from thinkback.memory.backends import Mem0LibraryMemoryBackend

    db_path = tmp_path / "history.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        """
        CREATE TABLE messages (
            id TEXT PRIMARY KEY, session_scope TEXT, role TEXT,
            content TEXT, name TEXT, created_at DATETIME
        )
        """
    )
    connection.execute(
        "INSERT INTO messages (id, session_scope, role, content)"
        " VALUES ('msg1', 'agent_id=thinkback&user_id=u1', 'user', '对话一')"
    )
    connection.execute(
        "INSERT INTO messages (id, session_scope, role, content)"
        " VALUES ('msg2', 'agent_id=thinkback&user_id=u1', 'user', '对话二')"
    )
    connection.execute(
        "INSERT INTO messages (id, session_scope, role, content)"
        " VALUES ('msg3', 'agent_id=other&user_id=u1', 'user', '别人的对话')"
    )
    connection.commit()
    connection.close()

    backend = Mem0LibraryMemoryBackend(config={"history_db_path": str(db_path)})
    counts = backend.purge_history(
        user_id="u1",
        memory_ids=[],
        namespace="thinkback",
        message_texts=[],
        purge_all_messages=True,
    )

    assert counts["messages"] == 2

    connection = sqlite3.connect(db_path)
    try:
        message_ids = [row[0] for row in connection.execute("SELECT id FROM messages")]
    finally:
        connection.close()
    assert message_ids == ["msg3"]


def test_mem0_library_backend_purge_history_without_history_db_is_noop() -> None:
    """没有配置 history_db_path 时不报错，返回零计数并声明不支持。"""
    from thinkback.memory.backends import Mem0LibraryMemoryBackend

    backend = Mem0LibraryMemoryBackend(config={})
    assert backend.purge_history(
        user_id="u1",
        memory_ids=["m1"],
        namespace="thinkback",
        message_texts=[],
        purge_all_messages=False,
    ) == {
        "history": 0,
        "messages": 0,
        "supported": False,
    }


def test_purge_history_retries_when_sqlite_file_is_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """mem0 自己开着一条连接写 history.db，我们的 DELETE 可能撞上瞬时锁。

    回滚日志模式下「先读后写、期间有别的连接写过」会立刻报
    ``database is locked``，busy_timeout 对这种场景不起作用。锁是瞬时的
    （写事务只持锁几毫秒），重试即可 —— 擦除不该因为它失败：向量已经删掉了，
    这时失败等于把用户卡在「删了一半」的状态。
    """
    import sqlite3

    from thinkback.memory.backends import Mem0LibraryMemoryBackend, mem0_library

    db_path = tmp_path / "history.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        "CREATE TABLE history (id TEXT PRIMARY KEY, memory_id TEXT, old_memory TEXT,"
        " new_memory TEXT, event TEXT)"
    )
    connection.execute(
        "INSERT INTO history (id, memory_id, old_memory, new_memory, event)"
        " VALUES ('h1', 'm1', '旧事实', '新事实', 'UPDATE')"
    )
    connection.commit()
    connection.close()

    class _LockingConnection:
        """第一次 execute 抛「database is locked」，之后放行。"""

        def __init__(self, real: sqlite3.Connection, state: dict[str, bool]) -> None:
            self._real = real
            self._state = state

        def execute(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
            if not self._state["failed"]:
                self._state["failed"] = True
                raise sqlite3.OperationalError("database is locked")
            return self._real.execute(*args, **kwargs)

        def __enter__(self) -> "_LockingConnection":
            self._real.__enter__()
            return self

        def __exit__(self, *exc: Any) -> bool:
            return bool(self._real.__exit__(*exc))

        def close(self) -> None:
            self._real.close()

    class _SqliteShim:
        Error = sqlite3.Error
        OperationalError = sqlite3.OperationalError

        def __init__(self) -> None:
            self.state = {"failed": False}

        def connect(self, *args: Any, **kwargs: Any) -> _LockingConnection:
            return _LockingConnection(sqlite3.connect(*args, **kwargs), self.state)

    shim = _SqliteShim()
    monkeypatch.setattr(mem0_library, "sqlite3", shim)

    backend = Mem0LibraryMemoryBackend(config={"history_db_path": str(db_path)})
    counts = backend.purge_history(
        user_id="u1",
        memory_ids=["m1"],
        namespace="thinkback",
        message_texts=[],
        purge_all_messages=False,
    )

    assert counts["history"] == 1
    assert shim.state["failed"] is True
    remaining = sqlite3.connect(db_path).execute("SELECT memory_id FROM history").fetchall()
    assert remaining == []


def test_delete_all_deletes_expired_records_too() -> None:
    """过期记录也必须删得掉：mem0 的 get_all 默认隐藏过期行。

    看不见就列不出来、列不出来就删不掉，擦除却报「已完成」—— 记忆活得
    越久越擦不掉，与保留策略的方向正好相反。删除路径要显式要 show_expired。
    """
    from thinkback.memory.backends import Mem0LibraryMemoryBackend

    class FakeExpiringMemoryClient:
        def __init__(self) -> None:
            self.rows = {
                "active": {"id": "active", "memory": "still valid"},
                "expired": {"id": "expired", "memory": "expired fact"},
            }
            self.deleted: list[str] = []
            self.show_expired_calls: list[bool] = []

        def get_all(self, *, filters, top_k, show_expired=False):  # type: ignore[no-untyped-def]
            self.show_expired_calls.append(show_expired)
            rows = list(self.rows.values())
            if not show_expired:
                rows = [row for row in rows if row["id"] != "expired"]
            return {"results": rows}

        def delete(self, memory_id):  # type: ignore[no-untyped-def]
            self.deleted.append(memory_id)
            self.rows.pop(memory_id, None)
            return {"message": "Memory deleted successfully!"}

    client = FakeExpiringMemoryClient()
    backend = Mem0LibraryMemoryBackend(
        config={"history_db_path": ".mem0/history.db"}, memory_client=client
    )

    deleted = backend.delete_all(user_id="user-1", memory_scope_id="thinkback")

    assert sorted(deleted) == ["active", "expired"]
    assert client.show_expired_calls == [True]


def test_concurrent_adds_for_one_user_are_serialised() -> None:
    """同一用户的并发写必须串行：mem0 不是线程安全的。

    mem0 的 entity_store 是实例级懒加载属性，实体链接是 linked_memory_ids 上的
    读-改-写 —— 两次并发写会互相覆盖、丢掉一条记忆 id，而且不报错。
    """
    import time
    from threading import Lock, Thread

    from thinkback.memory.backends import Mem0LibraryMemoryBackend

    class FakeConcurrentMemoryClient:
        def __init__(self) -> None:
            self.guard = Lock()
            self.active = 0
            self.max_active = 0

        def add(self, messages, *, user_id, agent_id, metadata, infer, prompt):  # type: ignore[no-untyped-def]
            with self.guard:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            time.sleep(0.05)
            with self.guard:
                self.active -= 1
            return {"results": [{"id": "m1", "memory": "一条事实"}]}

    client = FakeConcurrentMemoryClient()
    backend = Mem0LibraryMemoryBackend(
        config={"history_db_path": ".mem0/history.db"}, memory_client=client
    )

    def _write() -> None:
        backend.add(
            [{"role": "user", "content": "一条事实"}],
            user_id="user-1",
            memory_scope_id="thinkback",
        )

    threads = [Thread(target=_write) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert client.max_active == 1
