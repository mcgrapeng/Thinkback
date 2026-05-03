"""mem0 configuration boundary.

This module prepares configuration for the future memory implementation. It does
not perform add, search, delete, or other memory workflows.
"""

from __future__ import annotations

from typing import Any

from infra.config import Settings, settings as default_settings


def build_mem0_config(*, settings: Settings = default_settings) -> dict[str, Any]:
    qdrant_config: dict[str, Any] = {
        "url": settings.qdrant_url,
        "collection_name": settings.memory_qdrant_collection,
    }
    if settings.qdrant_api_key:
        qdrant_config["api_key"] = settings.qdrant_api_key

    return {
        "llm": {
            "provider": "openai",
            "config": {
                "model": settings.memory_llm_model,
                "api_key": settings.memory_llm_api_key,
                "openai_base_url": settings.memory_llm_base_url,
            },
        },
        "vector_store": {
            "provider": "qdrant",
            "config": qdrant_config,
        },
        "embedder": {
            "provider": "openai",
            "config": {
                "model": settings.memory_embedding_model,
                "api_key": settings.memory_embedding_api_key,
            },
        },
        "version": "v1.1",
    }


class Mem0Factory:
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = config or build_mem0_config()

    @property
    def config(self) -> dict[str, Any]:
        return self._config

    def create_memory(self) -> Any:
        from mem0 import Memory

        return Memory.from_config(self._config)
