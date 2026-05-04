from memory.backends import FakeMemoryBackend, Mem0MemoryBackend
from memory.mem0_client import build_mem0_config


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


def test_mem0_config_contains_qdrant_dimensions_and_openai_base_url() -> None:
    config = build_mem0_config()

    assert config["version"] == "v1.1"
    assert config["vector_store"]["provider"] == "qdrant"
    assert config["vector_store"]["config"]["embedding_model_dims"] == 1536
    assert "openai_base_url" in config["llm"]["config"]
    assert "openai_base_url" in config["embedder"]["config"]


def test_mem0_backend_forwards_user_character_scope(mocker) -> None:
    memory = mocker.Mock()
    memory.add.return_value = {"results": [{"id": "m1", "memory": "用户喜欢猫", "event": "ADD"}]}
    backend = Mem0MemoryBackend(memory=memory)

    events = backend.add(
        [{"role": "user", "content": "我喜欢猫"}],
        user_id="user-1",
        character_id="char-1",
        metadata={"memory_type": "preference"},
    )

    assert events == [{"id": "m1", "memory": "用户喜欢猫", "event": "ADD"}]
    memory.add.assert_called_once()
    _, kwargs = memory.add.call_args
    assert kwargs["user_id"] == "user-1"
    assert kwargs["agent_id"] == "char-1"
    assert kwargs["metadata"]["character_id"] == "char-1"


def test_mem0_backend_scoped_delete_all_never_calls_mem0_delete_all(mocker) -> None:
    memory = mocker.Mock()
    memory.get_all.return_value = {
        "results": [
            {"id": "m1", "memory": "用户喜欢猫"},
            {"id": "m2", "memory": "用户不喜欢熬夜"},
        ]
    }
    backend = Mem0MemoryBackend(memory=memory)

    deleted = backend.delete_all(user_id="user-1", character_id="char-1")

    assert deleted == 2
    memory.get_all.assert_called_once_with(user_id="user-1", agent_id="char-1", limit=1000)
    memory.delete.assert_has_calls([mocker.call("m1"), mocker.call("m2")])
    memory.delete_all.assert_not_called()


def test_mem0_http_backend_maps_add_search_update_and_delete(mocker) -> None:
    from memory.backends import Mem0HttpMemoryBackend

    transport = mocker.Mock()
    transport.side_effect = [
        {"results": [{"id": "m1", "memory": "用户喜欢猫", "event": "ADD"}]},
        {"results": [{"id": "m1", "memory": "用户喜欢猫", "score": 0.9}]},
        {"message": "updated"},
        {"message": "deleted"},
    ]
    backend = Mem0HttpMemoryBackend(
        api_url="http://localhost:8889",
        api_key="test-key",
        transport=transport,
    )

    add_events = backend.add(
        [{"role": "user", "content": "我喜欢猫"}],
        user_id="user-1",
        character_id="char-1",
        metadata={"memory_type": "preference"},
    )
    search_results = backend.search("猫", user_id="user-1", character_id="char-1", limit=3)
    backend.update("m1", "用户喜欢猫")
    backend.delete("m1")

    assert add_events == [{"id": "m1", "memory": "用户喜欢猫", "event": "ADD"}]
    assert search_results == [{"id": "m1", "memory": "用户喜欢猫", "score": 0.9}]
    assert transport.call_args_list == [
        mocker.call(
            "POST",
            "/memories",
            json_body={
                "messages": [{"role": "user", "content": "我喜欢猫"}],
                "user_id": "user-1",
                "agent_id": "char-1",
                "metadata": {"memory_type": "preference", "character_id": "char-1"},
            },
        ),
        mocker.call(
            "POST",
            "/search",
            json_body={
                "query": "猫",
                "filters": {"user_id": "user-1", "agent_id": "char-1"},
                "top_k": 3,
            },
        ),
        mocker.call("PUT", "/memories/m1", json_body={"text": "用户喜欢猫"}),
        mocker.call("DELETE", "/memories/m1", json_body=None),
    ]


def test_mem0_http_backend_scoped_delete_all_lists_then_deletes(mocker) -> None:
    from memory.backends import Mem0HttpMemoryBackend

    transport = mocker.Mock()
    transport.side_effect = [
        {"results": [{"id": "m1"}, {"id": "m2"}]},
        {"message": "deleted"},
        {"message": "deleted"},
    ]
    backend = Mem0HttpMemoryBackend(
        api_url="http://localhost:8889/",
        api_key="test-key",
        transport=transport,
    )

    deleted = backend.delete_all(user_id="user-1", character_id="char-1")

    assert deleted == 2
    assert transport.call_args_list == [
        mocker.call("GET", "/memories?user_id=user-1&agent_id=char-1", json_body=None),
        mocker.call("DELETE", "/memories/m1", json_body=None),
        mocker.call("DELETE", "/memories/m2", json_body=None),
    ]


def test_mem0_http_backend_health_check_uses_remote_config_endpoint(mocker) -> None:
    from memory.backends import Mem0HttpMemoryBackend

    transport = mocker.Mock(return_value={"configured": True})
    backend = Mem0HttpMemoryBackend(
        api_url="https://mem0.example.internal",
        api_key="test-key",
        transport=transport,
    )

    status = backend.health_check()

    assert status == {"status": "ready", "detail": "ok"}
    transport.assert_called_once_with("GET", "/configure", json_body=None)


def test_mem0_http_backend_wraps_socket_timeout(mocker) -> None:
    from memory.backends import Mem0HttpMemoryBackend

    backend = Mem0HttpMemoryBackend(api_url="http://localhost:8889", api_key="test-key")
    mocker.patch("memory.backends.urlopen", side_effect=TimeoutError("timed out"))

    try:
        backend._request("POST", "/memories", json_body={"messages": []})
    except RuntimeError as exc:
        assert "mem0 http POST /memories failed" in str(exc)
        assert "timed out" in str(exc)
    else:
        raise AssertionError("expected mem0 timeout to be wrapped")

    mocker.patch("memory.backends.urlopen", side_effect=TimeoutError("read timed out"))

    try:
        backend._request("POST", "/search", json_body={"query": "猫"})
    except RuntimeError as exc:
        assert "mem0 http POST /search failed" in str(exc)
        assert "read timed out" in str(exc)
    else:
        raise AssertionError("expected socket timeout to be wrapped")
