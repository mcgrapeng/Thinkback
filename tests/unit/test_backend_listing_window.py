"""后端列举的单页上限与擦除复核的失败可见性。

真实栈实测（Milvus + Mem0 + PG）暴露的缺陷：``get_all(limit=10000)`` 在
mem0 里会按 ``top_k * 4`` 取数，而 Milvus 单次 query 的结果窗口上限是
16384 —— 超出直接报 ``invalid max query result window``。擦除复核把这个
异常当成「查不到残留」，于是每次真实擦除都失败。

这两条用例把这个坑钉住：单页上限不越界；复核在无法证明已删时如实失败。
"""

from __future__ import annotations

from typing import Any

import pytest

from thinkback.domain.ports import MEMORY_LIST_PAGE_LIMIT
from thinkback.memory.backends import Mem0LibraryMemoryBackend
from thinkback.memory.repositories import InMemoryMemoryRepository
from thinkback.memory.schemas import DeleteMemoryRequest, DeleteScope
from thinkback.memory.service import MemoryService


class _RecordingClient:
    """只记录 get_all 参数的假 mem0 客户端。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def get_all(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"results": []}


def _requested_limit(call: dict[str, Any]) -> int:
    """从 get_all 的 kwargs 里取出 limit/top_k。"""
    for key in ("limit", "top_k"):
        if key in call:
            return int(call[key])
    raise AssertionError(f"get_all 调用里没有 limit/top_k: {call}")


def test_get_all_limit_is_capped_to_milvus_window() -> None:
    """越过 Milvus 查询窗口的 limit 必须被截断，否则整条擦除链路在真实栈上炸。"""
    client = _RecordingClient()
    backend = Mem0LibraryMemoryBackend(
        config={"history_db_path": ".mem0/history.db"}, memory_client=client
    )

    backend.get_all(user_id="user-1", memory_scope_id="thinkback", limit=10000)

    assert client.calls, "get_all 应被调用"
    requested = _requested_limit(client.calls[0])
    assert requested <= MEMORY_LIST_PAGE_LIMIT
    # mem0 内部按 top_k * 4 取数：乘 4 之后仍须落在 Milvus 的 16384 窗口内。
    assert requested * 4 <= 16384


def test_delete_all_pages_within_window() -> None:
    """delete_all 的单页同样不能越过 Milvus 窗口。"""
    client = _RecordingClient()
    backend = Mem0LibraryMemoryBackend(
        config={"history_db_path": ".mem0/history.db"}, memory_client=client
    )

    backend.delete_all(user_id="user-1", memory_scope_id="thinkback")

    assert client.calls
    for call in client.calls:
        assert _requested_limit(call) <= MEMORY_LIST_PAGE_LIMIT


class _PagedBackend:
    """总是返回满页的后端：模拟「还有更多记录没列出来」。"""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    def get_all(self, *, user_id: str, memory_scope_id: str, limit: int = 200) -> list[dict]:
        _ = (user_id, memory_scope_id)
        return [{"id": f"m-{index}", "memory": "x"} for index in range(limit)]

    def delete_many(self, memory_ids: list[str]) -> int:
        self.deleted.extend(memory_ids)
        return len(memory_ids)

    def delete(self, memory_id: str) -> None:
        self.deleted.append(memory_id)

    def delete_all(self, *, user_id: str, memory_scope_id: str) -> int:
        _ = (user_id, memory_scope_id)
        return 0

    def purge_history(
        self,
        *,
        user_id: str,
        memory_ids: list[str],
        namespace: str,
        message_texts: list[str],
        purge_all_messages: bool,
    ) -> dict[str, int]:
        _ = (user_id, memory_ids, namespace, message_texts, purge_all_messages)
        return {}


def test_erase_fails_closed_when_backend_page_is_full() -> None:
    """满页 = 无法证明已删：擦除任务必须失败，不能谎报 completed。"""
    from thinkback.memory.backends import FakeMemoryBackend

    repository = InMemoryMemoryRepository()
    service = MemoryService(
        repository=repository,
        backend=FakeMemoryBackend(),
        l3_write_mode="sync",
    )
    service.append(
        _append_request("round-erase-full-page"),
    )
    memory_id = repository.active_memories("user-1", "thinkback")[0].memory_id

    # 换成「永远返回满页」的后端：模拟还有大量记录没列出来。
    service.backend = _PagedBackend()  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="erase backend stage incomplete"):
        service.erase(
            DeleteMemoryRequest(
                request_id="req-erase-full-page",
                user_id="user-1",
                scope=DeleteScope.MEMORY,
                operation_id="op-erase-full-page",
                memory_id=memory_id,
            )
        )


def _append_request(round_id: str):  # type: ignore[no-untyped-def]
    from datetime import UTC, datetime

    from thinkback.memory.schemas import AppendMemoryRequest, MemoryMessage, MessageRole

    return AppendMemoryRequest(
        request_id=f"req-{round_id}",
        user_id="user-1",
        session_id="session-1",
        round_id=round_id,
        source_timestamp=datetime(2026, 10, 28, tzinfo=UTC),
        messages=[
            MemoryMessage(
                message_id="m-1",
                role=MessageRole.USER,
                content="我负责 3nm 工艺良率提升",
                timestamp="2026-10-28T00:00:00Z",
            ),
            MemoryMessage(
                message_id="m-2",
                role=MessageRole.ASSISTANT,
                content="好的，已记录。",
                timestamp="2026-10-28T00:00:01Z",
            ),
        ],
    )
