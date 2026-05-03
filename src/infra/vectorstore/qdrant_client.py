"""Qdrant client and readiness helpers."""

from __future__ import annotations

import asyncio

from qdrant_client import QdrantClient

from infra.config import settings

_qdrant_client: QdrantClient | None = None


def get_qdrant_client() -> QdrantClient:
    global _qdrant_client
    if _qdrant_client is None:
        _qdrant_client = (
            QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
            if settings.qdrant_api_key
            else QdrantClient(url=settings.qdrant_url)
        )
    return _qdrant_client


async def check_qdrant() -> dict[str, str]:
    try:
        await asyncio.to_thread(get_qdrant_client().get_collections)
    except Exception as exc:
        return {"status": "not_ready", "detail": str(exc)}
    return {"status": "ready", "detail": "ok"}
