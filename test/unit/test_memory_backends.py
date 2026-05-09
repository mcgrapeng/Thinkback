import pytest

from memory.backends import FakeMemoryBackend


def test_fake_backend_add_search_delete_all_are_scoped() -> None:
    backend = FakeMemoryBackend()
    messages = [{"role": "user", "content": "用户不喜欢被催睡觉"}]

    result = backend.add(
        messages,
        user_id="user-1",
        character_id="char-1",
        metadata={"source_refs": [{"session_id": "s1", "round_id": "r1"}]},
    )

    assert result[0]["event"] == "ADD"
    assert backend.search("睡觉", user_id="user-1", character_id="char-1", limit=5)
    assert backend.search("睡觉", user_id="user-1", character_id="char-2", limit=5) == []

    deleted = backend.delete_all(user_id="user-1", character_id="char-1")

    assert deleted == 1
    assert backend.search("睡觉", user_id="user-1", character_id="char-1", limit=5) == []


def test_mem0_library_config_builder_targets_shared_qdrant_and_openai() -> None:
    from memory.backends import build_mem0_library_config

    config = build_mem0_library_config(
        openai_api_key="openai-secret",
        openai_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        qdrant_url="http://localhost:6333",
        qdrant_api_key="qdrant-secret",
        collection_name="thinkback_memories",
        llm_model="gpt-5-mini",
        embedding_model="text-embedding-3-small",
        history_db_path=".mem0/history.db",
    )

    assert config == {
        "vector_store": {
            "provider": "qdrant",
            "config": {
                "collection_name": "thinkback_memories",
                "url": "http://localhost:6333",
                "api_key": "qdrant-secret",
                "embedding_model_dims": 1536,
            },
        },
        "llm": {
            "provider": "openai",
            "config": {
                "model": "gpt-5-mini",
                "api_key": "openai-secret",
                "openai_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            },
        },
        "embedder": {
            "provider": "openai",
            "config": {
                "model": "text-embedding-3-small",
                "api_key": "openai-secret",
                "openai_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "embedding_dims": 1536,
            },
        },
        "history_db_path": ".mem0/history.db",
    }


def test_mem0_library_config_builder_supports_unauthenticated_qdrant_host_port() -> None:
    from memory.backends import build_mem0_library_config

    config = build_mem0_library_config(
        openai_api_key="openai-secret",
        openai_base_url="",
        qdrant_url="http://qdrant.internal:6333",
        qdrant_api_key="",
        collection_name="thinkback_memories",
        llm_model="gpt-5-mini",
        embedding_model="text-embedding-3-small",
        history_db_path=".mem0/history.db",
    )

    assert config["vector_store"]["config"] == {
        "collection_name": "thinkback_memories",
        "host": "qdrant.internal",
        "port": 6333,
        "embedding_model_dims": 1536,
    }


def test_mem0_library_config_builder_rejects_https_qdrant_url_without_api_key() -> None:
    from memory.backends import build_mem0_library_config

    with pytest.raises(ValueError, match="QDRANT_API_KEY is required"):
        build_mem0_library_config(
            openai_api_key="openai-secret",
            openai_base_url="",
            qdrant_url="https://qdrant.example.internal",
            qdrant_api_key="",
            collection_name="thinkback_memories",
            llm_model="gpt-5-mini",
            embedding_model="text-embedding-3-small",
            history_db_path=".mem0/history.db",
        )


def test_mem0_library_backend_maps_add_search_update_delete_and_scoped_delete_all_with_limit() -> None:
    from memory.backends import Mem0LibraryMemoryBackend

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
    backend = Mem0LibraryMemoryBackend(config={"history_db_path": ".mem0/history.db"}, memory_client=client)

    add_events = backend.add(
        [{"role": "user", "content": "我喜欢猫"}],
        user_id="user-1",
        character_id="char-1",
        metadata={"memory_type": "preference"},
    )
    search_results = backend.search("猫", user_id="user-1", character_id="char-1", limit=3)
    threshold_results = backend.search(
        "猫", user_id="user-1", character_id="char-1", limit=3, threshold=0.7
    )
    backend.update("m1", "用户喜欢猫")
    backend.delete("m1")
    deleted = backend.delete_all(user_id="user-1", character_id="char-1")

    assert add_events == [{"id": "m1", "memory": "用户喜欢猫", "event": "ADD", "score": 1.0}]
    assert search_results == [{"id": "m1", "memory": "用户喜欢猫", "score": 0.9}]
    assert threshold_results == [{"id": "m1", "memory": "用户喜欢猫", "score": 0.9}]
    assert deleted == 2
    assert client.delete_all_called is False
    assert client.calls == [
        (
            "add",
            ([{"role": "user", "content": "我喜欢猫"}],),
            {
                "user_id": "user-1",
                "agent_id": "char-1",
                "metadata": {"memory_type": "preference", "character_id": "char-1"},
            },
        ),
        (
            "search",
            ("猫",),
            {
                "filters": {"user_id": "user-1", "agent_id": "char-1"},
                "limit": 3,
                "threshold": None,
            },
        ),
        (
            "search",
            ("猫",),
            {
                "filters": {"user_id": "user-1", "agent_id": "char-1"},
                "limit": 3,
                "threshold": 0.7,
            },
        ),
        ("update", ("m1", "用户喜欢猫"), {}),
        ("delete", ("m1",), {}),
        (
            "get_all",
            (),
            {"filters": {"user_id": "user-1", "agent_id": "char-1"}, "limit": 10000},
        ),
        ("delete", ("m1",), {}),
        ("delete", ("m2",), {}),
    ]


def test_mem0_library_backend_supports_legacy_entity_scope_kwargs() -> None:
    from memory.backends import Mem0LibraryMemoryBackend

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
    backend = Mem0LibraryMemoryBackend(config={"history_db_path": ".mem0/history.db"}, memory_client=client)

    assert backend.search("猫", user_id="user-1", character_id="char-1", limit=3) == [
        {"id": "m1", "memory": "用户喜欢猫", "score": 0.9}
    ]
    assert backend.delete_all(user_id="user-1", character_id="char-1") == 1
    assert client.calls == [
        (
            "search",
            ("猫",),
            {
                "user_id": "user-1",
                "agent_id": "char-1",
                "limit": 3,
                "filters": None,
                "threshold": None,
            },
        ),
        (
            "get_all",
            (),
            {"user_id": "user-1", "agent_id": "char-1", "limit": 10000, "filters": None},
        ),
        ("delete", ("m1",), {}),
    ]


def test_mem0_library_backend_supports_top_k_mem0_versions() -> None:
    from memory.backends import Mem0LibraryMemoryBackend

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
    backend = Mem0LibraryMemoryBackend(config={"history_db_path": ".mem0/history.db"}, memory_client=client)

    assert backend.search("猫", user_id="user-1", character_id="char-1", limit=3) == [
        {"id": "m1", "memory": "用户喜欢猫", "score": 0.9}
    ]
    assert backend.delete_all(user_id="user-1", character_id="char-1") == 1
    assert client.calls == [
        (
            "search",
            ("猫",),
            {
                "filters": {"user_id": "user-1", "agent_id": "char-1"},
                "top_k": 3,
                "threshold": None,
            },
        ),
        (
            "get_all",
            (),
            {"filters": {"user_id": "user-1", "agent_id": "char-1"}, "top_k": 10000},
        ),
        ("delete", ("m1",), {}),
    ]


def test_mem0_library_backend_health_check_runs_library_search_ping() -> None:
    from memory.backends import Mem0LibraryMemoryBackend

    class FakeMemoryClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple, dict]] = []

        def search(self, query, *, filters, limit, threshold):  # type: ignore[no-untyped-def]
            kwargs = {"filters": filters, "limit": limit, "threshold": threshold}
            self.calls.append(("search", (query,), kwargs))
            return {"results": []}

    client = FakeMemoryClient()
    backend = Mem0LibraryMemoryBackend(config={"history_db_path": ".mem0/history.db"}, memory_client=client)

    status = backend.health_check()

    assert status == {"status": "ready", "detail": "ok"}
    assert client.calls == [
        (
            "search",
            ("thinkback library health check",),
            {
                "filters": {"user_id": "thinkback-health", "agent_id": "thinkback-health"},
                "limit": 1,
                "threshold": None,
            },
        )
    ]


def test_mem0_library_backend_creates_history_db_parent_directory(tmp_path) -> None:
    from memory.backends import Mem0LibraryMemoryBackend

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
