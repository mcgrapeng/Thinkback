"""测试用纯内存 L3 后端。

对齐 ``Mem0LibraryMemoryBackend._call`` 的异常包装语义，
保证服务层锁外回调在测试环境行为一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4


@dataclass
class FakeMemoryBackend:
    memories: dict[str, dict[str, Any]] = field(default_factory=dict)

    def _call(self, operation: str, *args: Any, **kwargs: Any) -> Any:
        """N-6: 与 ``Mem0LibraryMemoryBackend._call`` 对齐的轻量包装。

        ``_BackendUpdate`` / ``_BackendDeleteSync`` 走 ``backend._call(...)``
        路径，测试用 ``FakeMemoryBackend`` 也必须暴露同名入口，否则会触发
        AttributeError。这里直接把调用转发到 ``self.<operation>``，并保留
        mem0 异常 → RuntimeError 包装语义。
        """
        method = getattr(self, operation, None)
        if method is None:
            raise AttributeError(f"FakeMemoryBackend has no operation {operation!r}")
        try:
            return method(*args, **kwargs)
        except Exception as exc:
            raise RuntimeError(f"fake backend {operation} failed: {exc}") from exc

    def add(
        self,
        messages: list[dict[str, str]],
        *,
        user_id: str,
        memory_scope_id: str,
        metadata: dict[str, Any] | None = None,
        infer: bool = True,
    ) -> list[dict[str, Any]]:
        _ = infer  # fake backend 不做 LLM 抽取，参数仅用于协议兼容
        text = " ".join(
            message["content"].strip() for message in messages if message.get("content", "").strip()
        )
        if not text:
            return []
        memory_id = f"fake-{uuid4()}"
        memory = {
            "id": memory_id,
            "memory": text,
            "event": "ADD",
            "user_id": user_id,
            "agent_id": memory_scope_id,
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
        memory_scope_id: str,
        limit: int,
        threshold: float | None = None,
    ) -> list[dict[str, Any]]:
        _ = threshold
        query_terms = {part for part in query.lower().split() if part}
        query_chunks = {query[index : index + 2] for index in range(max(0, len(query) - 1))}
        results: list[dict[str, Any]] = []
        for memory in self.memories.values():
            if memory["user_id"] != user_id or memory["agent_id"] != memory_scope_id:
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
        """更新一条 fake 记忆的内容。缺失 ID 视为 no-op，避免与生产后端语义差异。"""

        if memory_id in self.memories:
            self.memories[memory_id]["memory"] = data

    def delete(self, memory_id: str) -> None:
        """删除单条 fake 记忆。幂等：缺失 ID 不抛异常。"""

        self.memories.pop(memory_id, None)

    def delete_many(self, memory_ids: list[str]) -> int:
        """批量删除 fake 记忆；返回实际被删除的条数。"""

        deleted = 0
        for memory_id in memory_ids:
            if memory_id in self.memories:
                self.delete(memory_id)
                deleted += 1
        return deleted

    def delete_all(self, *, user_id: str, memory_scope_id: str) -> int:
        """删除指定 user+scope 范围内所有 fake 记忆，返回删除条数。"""

        matching = [
            memory_id
            for memory_id, memory in self.memories.items()
            if memory["user_id"] == user_id and memory["agent_id"] == memory_scope_id
        ]
        for memory_id in matching:
            self.memories.pop(memory_id, None)
        return len(matching)
