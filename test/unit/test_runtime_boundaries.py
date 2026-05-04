from pathlib import Path

from infra.config import Settings


def test_celery_app_registers_diagnostic_memory_task() -> None:
    from infra.tasks.celery_app import celery_app

    assert celery_app.main == "thinkback"
    assert "memory.diagnostics.ping" in celery_app.tasks


def test_mem0_config_builder_targets_qdrant_memory_collection() -> None:
    from memory.mem0_client import build_mem0_config

    settings = Settings()
    config = build_mem0_config(settings=settings)

    assert config["vector_store"]["provider"] == "qdrant"
    assert config["vector_store"]["config"]["collection_name"] == "thinkback_memories"
    assert config["llm"]["provider"] == "openai"
    assert config["embedder"]["provider"] == "openai"


def test_mem0_boundary_does_not_instantiate_memory_engine() -> None:
    import memory.mem0_client as mem0_client

    assert not hasattr(mem0_client, "Mem0Factory")
    assert "Memory.from_config" not in mem0_client.__dict__.values()


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

    monkeypatch.setattr(dependencies, "_memory_service", None)
    monkeypatch.setattr(dependencies, "Mem0MemoryBackend", FakeMemoryBackend)
    service = dependencies.get_memory_service()

    assert isinstance(service.repository, SqlAlchemyMemoryRepository)


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
