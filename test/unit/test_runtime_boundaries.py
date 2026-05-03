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


def test_memory_service_boundary_has_no_public_workflows() -> None:
    from memory.service import MemoryService

    service = MemoryService()

    assert service.name == "thinkback-memory"
