"""Long-term memory backend adapters."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from inspect import signature
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse
from uuid import uuid4


class MemoryBackend(Protocol):
    def add(
        self,
        messages: list[dict[str, str]],
        *,
        user_id: str,
        character_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        ...

    def search(
        self,
        query: str,
        *,
        user_id: str,
        character_id: str,
        limit: int,
        threshold: float | None = None,
    ) -> list[dict[str, Any]]:
        ...

    def update(self, memory_id: str, data: str) -> None:
        ...

    def delete(self, memory_id: str) -> None:
        ...

    def delete_many(self, memory_ids: list[str]) -> int:
        ...

    def delete_all(self, *, user_id: str, character_id: str) -> int:
        ...


@dataclass
class FakeMemoryBackend:
    memories: dict[str, dict[str, Any]] = field(default_factory=dict)

    def add(
        self,
        messages: list[dict[str, str]],
        *,
        user_id: str,
        character_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        text = " ".join(message["content"].strip() for message in messages if message.get("content", "").strip())
        if not text:
            return []
        memory_id = f"fake-{uuid4()}"
        memory = {
            "id": memory_id,
            "memory": text,
            "event": "ADD",
            "user_id": user_id,
            "agent_id": character_id,
            "metadata": metadata or {},
            "score": 1.0,
        }
        self.memories[memory_id] = memory
        return [{"id": memory_id, "memory": text, "event": "ADD"}]

    def search(
        self,
        query: str,
        *,
        user_id: str,
        character_id: str,
        limit: int,
        threshold: float | None = None,
    ) -> list[dict[str, Any]]:
        _ = threshold
        query_terms = {part for part in query.lower().split() if part}
        query_chunks = {query[index : index + 2] for index in range(max(0, len(query) - 1))}
        results: list[dict[str, Any]] = []
        for memory in self.memories.values():
            if memory["user_id"] != user_id or memory["agent_id"] != character_id:
                continue
            content = memory["memory"].lower()
            if (
                query_terms
                and not any(term in content for term in query_terms)
                and query not in memory["memory"]
                and not any(chunk and chunk in memory["memory"] for chunk in query_chunks)
            ):
                continue
            results.append(memory)
        return results[:limit]

    def update(self, memory_id: str, data: str) -> None:
        if memory_id in self.memories:
            self.memories[memory_id]["memory"] = data

    def delete(self, memory_id: str) -> None:
        self.memories.pop(memory_id, None)

    def delete_many(self, memory_ids: list[str]) -> int:
        deleted = 0
        for memory_id in memory_ids:
            if memory_id in self.memories:
                self.delete(memory_id)
                deleted += 1
        return deleted

    def delete_all(self, *, user_id: str, character_id: str) -> int:
        matching = [
            memory_id
            for memory_id, memory in self.memories.items()
            if memory["user_id"] == user_id and memory["agent_id"] == character_id
        ]
        for memory_id in matching:
            self.memories.pop(memory_id, None)
        return len(matching)


def build_mem0_library_config(
    *,
    openai_api_key: str,
    openai_base_url: str,
    qdrant_url: str,
    qdrant_api_key: str,
    collection_name: str,
    llm_model: str,
    embedding_model: str,
    history_db_path: str,
    embedding_model_dims: int = 1536,
) -> dict[str, Any]:
    """Build the Mem0 Library config used by Thinkback's L3 backend."""

    qdrant_config: dict[str, Any] = {
        "collection_name": collection_name,
        "embedding_model_dims": embedding_model_dims,
    }
    if qdrant_api_key:
        qdrant_config["url"] = qdrant_url
        qdrant_config["api_key"] = qdrant_api_key
    else:
        parsed_url = urlparse(qdrant_url)
        if parsed_url.scheme == "https":
            raise ValueError("QDRANT_API_KEY is required when QDRANT_URL uses https")
        qdrant_config["host"] = parsed_url.hostname or qdrant_url
        qdrant_config["port"] = parsed_url.port or 6333
    llm_config = {"model": llm_model, "api_key": openai_api_key}
    embedder_config: dict[str, Any] = {
        "model": embedding_model,
        "api_key": openai_api_key,
        "embedding_dims": embedding_model_dims,
    }
    if openai_base_url:
        llm_config["openai_base_url"] = openai_base_url
        embedder_config["openai_base_url"] = openai_base_url
    return {
        "vector_store": {
            "provider": "qdrant",
            "config": qdrant_config,
        },
        "llm": {
            "provider": "openai",
            "config": llm_config,
        },
        "embedder": {
            "provider": "openai",
            "config": embedder_config,
        },
        "history_db_path": history_db_path,
    }


class Mem0LibraryMemoryBackend:
    def __init__(
        self,
        *,
        config: dict[str, Any],
        memory_client: Any | None = None,
        memory_factory: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        self.config = deepcopy(config)
        self._memory_client = memory_client
        self._memory_factory = memory_factory

    def add(
        self,
        messages: list[dict[str, str]],
        *,
        user_id: str,
        character_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        scoped_metadata = dict(metadata or {})
        scoped_metadata["character_id"] = character_id
        result = self._call(
            "add",
            messages,
            user_id=user_id,
            agent_id=character_id,
            metadata=scoped_metadata,
        )
        return self._results(result)

    def search(
        self,
        query: str,
        *,
        user_id: str,
        character_id: str,
        limit: int,
        threshold: float | None = None,
    ) -> list[dict[str, Any]]:
        result = self._call(
            "search",
            query,
            **self._scope_kwargs("search", user_id=user_id, character_id=character_id),
            **self._limit_kwargs("search", limit),
            threshold=threshold,
        )
        return self._results(result)

    def update(self, memory_id: str, data: str) -> None:
        self._call("update", memory_id, data)

    def delete(self, memory_id: str) -> None:
        self._call("delete", memory_id)

    def delete_many(self, memory_ids: list[str]) -> int:
        deleted = 0
        for memory_id in memory_ids:
            if not memory_id:
                continue
            self.delete(memory_id)
            deleted += 1
        return deleted

    def delete_all(self, *, user_id: str, character_id: str) -> int:
        result = self._call(
            "get_all",
            **self._scope_kwargs("get_all", user_id=user_id, character_id=character_id),
            **self._limit_kwargs("get_all", 10000),
        )
        memory_ids = [str(memory.get("id", "")) for memory in self._results(result)]
        return self.delete_many(memory_ids)

    def health_check(self) -> dict[str, str]:
        try:
            self.search(
                "thinkback library health check",
                user_id="thinkback-health",
                character_id="thinkback-health",
                limit=1,
                threshold=None,
            )
        except Exception as exc:
            return {"status": "not_ready", "detail": str(exc)}
        return {"status": "ready", "detail": "ok"}

    @property
    def memory_client(self) -> Any:
        if self._memory_client is None:
            self._memory_client = self._build_memory_client()
        return self._memory_client

    def _build_memory_client(self) -> Any:
        history_db_path = self.config.get("history_db_path")
        if isinstance(history_db_path, str) and history_db_path:
            Path(history_db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        if self._memory_factory is not None:
            return self._memory_factory(deepcopy(self.config))
        try:
            from mem0 import Memory
        except ImportError as exc:
            raise RuntimeError("mem0 library is not installed; install mem0ai") from exc
        return Memory.from_config(deepcopy(self.config))

    def _call(self, operation: str, *args: Any, **kwargs: Any) -> Any:
        try:
            method = getattr(self.memory_client, operation)
            return method(*args, **kwargs)
        except Exception as exc:
            raise RuntimeError(f"mem0 library {operation} failed: {exc}") from exc

    def _limit_kwargs(self, operation: str, limit: int) -> dict[str, int]:
        method = getattr(self.memory_client, operation)
        try:
            parameters = signature(method).parameters
        except (TypeError, ValueError):
            return {"limit": limit}
        if "limit" in parameters:
            return {"limit": limit}
        if "top_k" in parameters:
            return {"top_k": limit}
        return {"limit": limit}

    def _scope_kwargs(self, operation: str, *, user_id: str, character_id: str) -> dict[str, Any]:
        method = getattr(self.memory_client, operation)
        try:
            parameters = signature(method).parameters
        except (TypeError, ValueError):
            return {"user_id": user_id, "agent_id": character_id}
        supports_top_level_scope = "user_id" in parameters and "agent_id" in parameters
        if supports_top_level_scope:
            return {"user_id": user_id, "agent_id": character_id, "filters": None}
        return {"filters": {"user_id": user_id, "agent_id": character_id}}

    @staticmethod
    def _results(result: object) -> list[dict[str, Any]]:
        if isinstance(result, dict):
            raw_results = result.get("results", result.get("memories", []))
            if isinstance(raw_results, list):
                return [dict(item) for item in raw_results if isinstance(item, dict)]
            if {"id", "memory"} <= result.keys():
                return [dict(result)]
        if isinstance(result, list):
            return [dict(item) for item in result if isinstance(item, dict)]
        return []
