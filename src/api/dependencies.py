"""API dependency accessors."""

from infra.config import Settings, settings
from memory.backends import Mem0LibraryMemoryBackend, MemoryBackend, build_mem0_library_config
from memory.repositories import SqlAlchemyMemoryRepository
from memory.service import MemoryService

_memory_service: MemoryService | None = None


def get_settings() -> Settings:
    return settings


def get_memory_backend(config: Settings | None = None) -> MemoryBackend:
    current_settings = config or settings
    mem0_config = build_mem0_library_config(
        openai_api_key=current_settings.openai_api_key,
        openai_base_url=current_settings.memory_openai_base_url,
        qdrant_url=current_settings.qdrant_url,
        qdrant_api_key=current_settings.qdrant_api_key,
        collection_name=current_settings.memory_qdrant_collection,
        llm_model=current_settings.memory_llm_model,
        embedding_model=current_settings.memory_embedding_model,
        embedding_model_dims=current_settings.memory_embedding_dims,
        history_db_path=current_settings.mem0_history_db_path,
    )
    return Mem0LibraryMemoryBackend(config=mem0_config)


def get_memory_service() -> MemoryService:
    global _memory_service
    if _memory_service is None:
        _memory_service = MemoryService(
            repository=SqlAlchemyMemoryRepository(),
            backend=get_memory_backend(settings),
            l3_write_mode=settings.memory_l3_write_mode,
            l3_executor_workers=settings.memory_l3_executor_workers,
            l3_max_pending_tasks=settings.memory_l3_max_pending_tasks,
            l3_queue_wait_seconds=settings.memory_l3_queue_wait_seconds,
        )
    return _memory_service
