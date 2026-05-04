"""API dependency accessors."""

from infra.config import Settings, settings
from memory.backends import Mem0HttpMemoryBackend, Mem0MemoryBackend, MemoryBackend
from memory.repositories import SqlAlchemyMemoryRepository
from memory.service import MemoryService

_memory_service: MemoryService | None = None


def get_settings() -> Settings:
    return settings


def get_memory_backend(config: Settings | None = None) -> MemoryBackend:
    current_settings = config or settings
    if current_settings.mem0_backend_mode == "http_api":
        return Mem0HttpMemoryBackend(
            api_url=current_settings.mem0_api_url,
            api_key=current_settings.mem0_api_key,
            timeout_seconds=current_settings.mem0_http_timeout_seconds,
        )
    return Mem0MemoryBackend()


def get_memory_service() -> MemoryService:
    global _memory_service
    if _memory_service is None:
        _memory_service = MemoryService(
            repository=SqlAlchemyMemoryRepository(),
            backend=get_memory_backend(settings),
        )
    return _memory_service
