"""mem0 configuration boundary.

This module prepares configuration for the future memory implementation. It does
not perform add, search, delete, or other memory workflows.
"""

from __future__ import annotations

from typing import Any

from infra.config import Settings
from infra.config import settings as default_settings


def build_mem0_config(*, settings: Settings = default_settings) -> dict[str, Any]:
    qdrant_config: dict[str, Any] = {
        "url": settings.qdrant_url,
        "collection_name": settings.memory_qdrant_collection,
        "embedding_model_dims": 1536,
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
                "openai_base_url": settings.memory_llm_base_url,
                "embedding_dims": 1536,
            },
        },
        "version": "v1.1",
    }
