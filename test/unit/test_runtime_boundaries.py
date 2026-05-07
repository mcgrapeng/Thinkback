from pathlib import Path
from types import SimpleNamespace

from memory.schemas import MemoryStatus


def test_celery_app_registers_diagnostic_memory_task() -> None:
    from infra.tasks.celery_app import celery_app

    assert celery_app.main == "thinkback"
    assert "memory.diagnostics.ping" in celery_app.tasks


def test_mem0_config_builder_targets_qdrant_memory_collection() -> None:
    assert not Path("src/memory/mem0_client.py").exists()


def test_mem0_boundary_does_not_instantiate_memory_engine() -> None:
    source = Path("src/memory/backends.py").read_text(encoding="utf-8")

    assert "Memory.from_config" not in source
    assert "build_mem0_config" not in source
    assert "QdrantClient" not in source


def test_memory_service_exposes_p0_public_workflows() -> None:
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
    monkeypatch.setattr(dependencies, "Mem0HttpMemoryBackend", FakeHttpBackend)
    service = dependencies.get_memory_service()

    assert isinstance(service.repository, SqlAlchemyMemoryRepository)


def test_memory_service_can_use_mem0_http_backend_from_settings(monkeypatch) -> None:
    import api.dependencies as dependencies
    from infra.config import Settings
    from memory.backends import Mem0HttpMemoryBackend

    monkeypatch.setattr(dependencies, "_memory_service", None)
    monkeypatch.setattr(
        dependencies,
        "settings",
        Settings(
            mem0_api_url="http://localhost:8889",
            mem0_api_key="test-key",
        ),
    )
    service = dependencies.get_memory_service()

    assert isinstance(service.backend, Mem0HttpMemoryBackend)
    assert service.backend.api_url == "http://localhost:8889"
    assert service.backend.api_key == "test-key"
    assert service.backend.timeout_seconds == 120.0


def test_memory_routes_call_sync_service_in_threadpool() -> None:
    source = Path("src/api/memory.py").read_text(encoding="utf-8")

    assert "return await run_in_threadpool(method, *args, **kwargs)" in source
    assert "_run_memory_call(service.append, request)" in source
    assert "_run_memory_call(service.recall, request)" in source
    assert "_run_memory_call(service.delete, request)" in source
    assert "_run_memory_call(service.rebuild, request)" in source


def test_real_pressure_script_uses_sql_repository_not_in_memory() -> None:
    source = Path("script/real_mem0_pressure.py").read_text(encoding="utf-8")

    assert "SqlAlchemyMemoryRepository" in source
    assert "InMemoryMemoryRepository" not in source


def test_real_pressure_script_reuses_configurable_mem0_backend() -> None:
    source = Path("script/real_mem0_pressure.py").read_text(encoding="utf-8")

    assert "get_memory_backend(settings)" in source
    assert "Mem0MemoryBackend()" not in source
    assert "QdrantClient" not in source
    assert "repository.memories.values()" not in source


def test_real_pressure_script_generates_unique_scope_and_ids() -> None:
    from script.real_mem0_pressure import _new_run_scope

    first = _new_run_scope("20260504T010203")
    second = _new_run_scope("20260504T010204")

    assert first.user_id != second.user_id
    assert first.character_id != second.character_id
    assert first.session_id != second.session_id
    assert first.request_id(1) != second.request_id(1)
    assert first.round_id(1) != second.round_id(1)
    assert first.operation_id("delete") != second.operation_id("delete")


def test_real_pressure_delete_target_uses_any_active_memory_not_text_keywords() -> None:
    from script.real_mem0_pressure import _select_delete_target

    target = SimpleNamespace(
        memory_id="memory-1",
        backend_memory_id="backend-1",
        memory_status=MemoryStatus.ACTIVE,
        memory_text="用户更喜欢被称呼为阿鹏",
    )
    repository = SimpleNamespace(active_memories=lambda user_id, character_id: [target])

    assert _select_delete_target(repository, "user-1", "char-1") is target


def test_real_pressure_delete_target_fails_when_no_active_l3_memory() -> None:
    from script.real_mem0_pressure import _select_delete_target

    repository = SimpleNamespace(active_memories=lambda user_id, character_id: [])

    try:
        _select_delete_target(repository, "user-1", "char-1")
    except RuntimeError as exc:
        assert "no active L3 memory" in str(exc)
    else:
        raise AssertionError("expected missing active memory to fail pressure test")


def test_real_pressure_l3_generation_failure_points_to_mem0_provider() -> None:
    from script.real_mem0_pressure import _assert_l3_generated

    repository = SimpleNamespace(active_memories=lambda user_id, character_id: [])

    try:
        _assert_l3_generated(repository, "user-1", "char-1")
    except RuntimeError as exc:
        assert "produced no active L3 memories" in str(exc)
        assert "Mem0 LLM/embedding provider" in str(exc)
    else:
        raise AssertionError("expected missing generated L3 memory to fail pressure test")
