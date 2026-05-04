"""API dependency accessors."""

from infra.config import Settings, settings
from memory.backends import Mem0MemoryBackend
from memory.repositories import SqlAlchemyMemoryRepository
from memory.service import MemoryService

_memory_service: MemoryService | None = None


def get_settings() -> Settings:
    return settings


def get_memory_service() -> MemoryService:
    global _memory_service
    if _memory_service is None:
        _memory_service = MemoryService(
            repository=SqlAlchemyMemoryRepository(),
            backend=Mem0MemoryBackend(),
        )
    return _memory_service
