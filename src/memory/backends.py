"""Long-term memory backend adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import uuid4

from mem0 import Memory

from memory.mem0_client import build_mem0_config


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


class Mem0MemoryBackend:
    def __init__(self, memory: Any | None = None) -> None:
        self.memory = memory or Memory.from_config(build_mem0_config())

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
        result = self.memory.add(
            messages,
            user_id=user_id,
            agent_id=character_id,
            metadata=scoped_metadata,
        )
        return list(result.get("results", []))

    def search(
        self,
        query: str,
        *,
        user_id: str,
        character_id: str,
        limit: int,
        threshold: float | None = None,
    ) -> list[dict[str, Any]]:
        result = self.memory.search(
            query,
            user_id=user_id,
            agent_id=character_id,
            limit=limit,
            threshold=threshold,
        )
        return list(result.get("results", []))

    def update(self, memory_id: str, data: str) -> None:
        self.memory.update(memory_id, data)

    def delete(self, memory_id: str) -> None:
        self.memory.delete(memory_id)

    def delete_many(self, memory_ids: list[str]) -> int:
        deleted = 0
        for memory_id in memory_ids:
            if not memory_id:
                continue
            self.memory.delete(memory_id)
            deleted += 1
        return deleted

    def delete_all(self, *, user_id: str, character_id: str) -> int:
        result = self.memory.get_all(user_id=user_id, agent_id=character_id, limit=1000)
        memories = result.get("results", []) if isinstance(result, dict) else result
        memory_ids = [str(memory.get("id", "")) for memory in memories if isinstance(memory, dict)]
        return self.delete_many(memory_ids)
