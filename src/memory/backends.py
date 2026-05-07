"""Long-term memory backend adapters."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from http.client import RemoteDisconnected
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
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


class Mem0HttpMemoryBackend:
    def __init__(
        self,
        *,
        api_url: str,
        api_key: str,
        transport: Any | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.transport = transport or self._request

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
        result = self.transport(
            "POST",
            "/memories",
            json_body={
                "messages": messages,
                "user_id": user_id,
                "agent_id": character_id,
                "metadata": scoped_metadata,
            },
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
        body: dict[str, Any] = {
            "query": query,
            "filters": {"user_id": user_id, "agent_id": character_id},
            "top_k": limit,
        }
        if threshold is not None:
            body["threshold"] = threshold
        result = self.transport("POST", "/search", json_body=body)
        return self._results(result)

    def update(self, memory_id: str, data: str) -> None:
        self.transport("PUT", f"/memories/{memory_id}", json_body={"text": data})

    def delete(self, memory_id: str) -> None:
        self.transport("DELETE", f"/memories/{memory_id}", json_body=None)

    def delete_many(self, memory_ids: list[str]) -> int:
        deleted = 0
        for memory_id in memory_ids:
            if not memory_id:
                continue
            self.delete(memory_id)
            deleted += 1
        return deleted

    def delete_all(self, *, user_id: str, character_id: str) -> int:
        query = urlencode({"user_id": user_id, "agent_id": character_id})
        result = self.transport("GET", f"/memories?{query}", json_body=None)
        memory_ids = [str(memory.get("id", "")) for memory in self._results(result)]
        return self.delete_many(memory_ids)

    def health_check(self) -> dict[str, str]:
        try:
            self.transport("GET", "/configure", json_body=None)
        except Exception as exc:
            return {"status": "not_ready", "detail": str(exc)}
        return {"status": "ready", "detail": "ok"}

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

    def _request(
        self, method: str, path: str, *, json_body: dict[str, Any] | None
    ) -> dict[str, Any] | list[Any]:
        data = None
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self.api_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                payload = response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"mem0 http {method} {path} failed: {exc.code} {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"mem0 http {method} {path} failed: {exc.reason}") from exc
        except RemoteDisconnected as exc:
            raise RuntimeError(f"mem0 http {method} {path} failed: {exc}") from exc
        except TimeoutError as exc:
            raise RuntimeError(f"mem0 http {method} {path} failed: {exc}") from exc
        if not payload:
            return {}
        decoded = json.loads(payload.decode("utf-8"))
        if isinstance(decoded, dict | list):
            return decoded
        return {}
