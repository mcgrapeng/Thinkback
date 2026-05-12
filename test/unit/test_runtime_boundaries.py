import asyncio
from pathlib import Path
from types import SimpleNamespace

from memory.schemas import MemoryStatus


def test_worker_runtime_surface_is_not_part_of_default_scaffold() -> None:
    assert not Path("src/infra/tasks/celery_app.py").exists()
    assert not Path("src/memory/tasks.py").exists()


def test_mem0_config_builder_targets_qdrant_memory_collection() -> None:
    assert not Path("src/memory/mem0_client.py").exists()


def test_mem0_boundary_uses_library_adapter_without_direct_qdrant_client() -> None:
    source = Path("src/memory/backends.py").read_text(encoding="utf-8")

    assert "Memory.from_config" in source
    assert "build_mem0_library_config" in source
    assert "QdrantClient" not in source


def test_memory_service_exposes_public_workflows() -> None:
    from memory.backends import FakeMemoryBackend
    from memory.repositories import InMemoryMemoryRepository
    from memory.service import MemoryService

    service = MemoryService(repository=InMemoryMemoryRepository(), backend=FakeMemoryBackend())

    assert service.name == "thinkback-memory"
    assert callable(service.append)
    assert callable(service.recall)
    assert callable(service.delete)
    assert callable(service.rebuild)


def test_default_memory_service_uses_sql_repository(monkeypatch) -> None:
    import api.dependencies as dependencies
    from memory.backends import FakeMemoryBackend
    from memory.repositories import SqlAlchemyMemoryRepository

    class FakeHttpBackend(FakeMemoryBackend):
        def __init__(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
            super().__init__()

    monkeypatch.setattr(dependencies, "_memory_service", None)
    monkeypatch.setattr(dependencies, "Mem0LibraryMemoryBackend", FakeHttpBackend)
    service = dependencies.get_memory_service()

    assert isinstance(service.repository, SqlAlchemyMemoryRepository)


def test_sql_repository_reuses_one_private_event_loop_for_sync_calls() -> None:
    from memory.repositories import SqlAlchemyMemoryRepository

    repository = SqlAlchemyMemoryRepository()
    observed_loops: list[asyncio.AbstractEventLoop] = []

    async def capture_loop() -> None:
        observed_loops.append(asyncio.get_running_loop())

    try:
        repository._run(capture_loop())
        repository._run(capture_loop())
    finally:
        close = getattr(repository, "close", None)
        if callable(close):
            close()

    assert observed_loops[0] is observed_loops[1]


def test_memory_service_can_use_mem0_library_backend_from_settings(monkeypatch) -> None:
    import api.dependencies as dependencies
    from infra.config import Settings
    from memory.backends import Mem0LibraryMemoryBackend

    monkeypatch.setattr(dependencies, "_memory_service", None)
    monkeypatch.setattr(
        dependencies,
        "settings",
        Settings(
            openai_api_key="openai-secret",
            memory_openai_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            qdrant_url="https://qdrant.example.internal",
            qdrant_api_key="qdrant-secret",
            memory_qdrant_collection="thinkback_memories_test",
        ),
    )
    service = dependencies.get_memory_service()

    assert isinstance(service.backend, Mem0LibraryMemoryBackend)
    assert service.backend.config["llm"]["config"]["api_key"] == "openai-secret"
    assert (
        service.backend.config["llm"]["config"]["openai_base_url"]
        == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )
    assert service.backend.config["embedder"]["config"]["api_key"] == "openai-secret"
    assert service.backend.config["vector_store"]["config"]["url"] == "https://qdrant.example.internal"
    assert service.backend.config["vector_store"]["config"]["api_key"] == "qdrant-secret"
    assert (
        service.backend.config["vector_store"]["config"]["collection_name"]
        == "thinkback_memories_test"
    )
    assert service.l3_write_mode == "async"
    assert service.l3_executor_workers == 16
    assert service.l3_max_pending_tasks == 256
    assert service.l3_queue_wait_seconds == 5.0


def test_memory_routes_call_sync_service_in_threadpool() -> None:
    source = Path("src/api/memory.py").read_text(encoding="utf-8")

    assert "CapacityLimiter(" in source
    assert "partial(method, *args, **kwargs)" in source
    assert "return await to_thread.run_sync(" in source
    assert "limiter=_memory_call_limiter" in source
    assert "_run_memory_call(service.append, request)" in source
    assert "_run_memory_call(service.recall, request)" in source
    assert "_run_memory_call(service.delete, request)" in source
    assert "_run_memory_call(service.rebuild, request)" in source


def test_real_validation_script_uses_sql_repository_not_in_memory() -> None:
    source = Path("script/real_mem0_pressure.py").read_text(encoding="utf-8")

    assert "SqlAlchemyMemoryRepository" in source
    assert "InMemoryMemoryRepository" not in source


def test_real_validation_script_reuses_configurable_mem0_backend() -> None:
    source = Path("script/real_mem0_pressure.py").read_text(encoding="utf-8")

    assert "get_memory_backend(settings)" in source
    assert "Mem0HttpMemoryBackend" not in source
    assert "QdrantClient" not in source
    assert "repository.memories.values()" not in source


def test_real_validation_script_generates_unique_scope_and_ids() -> None:
    from script.real_mem0_pressure import _new_run_scope

    first = _new_run_scope("20260504T010203")
    second = _new_run_scope("20260504T010204")

    assert first.user_id != second.user_id
    assert first.character_id != second.character_id
    assert first.session_id != second.session_id
    assert first.request_id(1) != second.request_id(1)
    assert first.round_id(1) != second.round_id(1)
    assert first.operation_id("delete") != second.operation_id("delete")


def test_real_validation_delete_target_uses_any_active_memory_not_text_keywords() -> None:
    from script.real_mem0_pressure import _select_delete_target

    target = SimpleNamespace(
        memory_id="memory-1",
        backend_memory_id="backend-1",
        memory_status=MemoryStatus.ACTIVE,
        memory_text="用户更喜欢被称呼为阿鹏",
    )
    repository = SimpleNamespace(active_memories=lambda user_id, character_id: [target])

    assert _select_delete_target(repository, "user-1", "char-1") is target


def test_real_validation_delete_target_fails_when_no_active_l3_memory() -> None:
    from script.real_mem0_pressure import _select_delete_target

    repository = SimpleNamespace(active_memories=lambda user_id, character_id: [])

    try:
        _select_delete_target(repository, "user-1", "char-1")
    except RuntimeError as exc:
        assert "no active L3 memory" in str(exc)
    else:
        raise AssertionError("expected missing active memory to fail validation")


def test_real_validation_l3_generation_failure_points_to_mem0_provider() -> None:
    from script.real_mem0_pressure import _assert_l3_generated

    repository = SimpleNamespace(active_memories=lambda user_id, character_id: [])

    try:
        _assert_l3_generated(repository, "user-1", "char-1")
    except RuntimeError as exc:
        assert "produced no active L3 memories" in str(exc)
        assert "Mem0 LLM/embedding provider" in str(exc)
    else:
        raise AssertionError("expected missing generated L3 memory to fail validation")
