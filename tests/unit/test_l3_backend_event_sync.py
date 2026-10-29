"""L3 本地索引与引擎事件的对齐。

引擎（mem0 2.2.1）让 LLM 对每条事实给出 ADD/UPDATE/DELETE/NONE 判定
（``mem0/configs/prompts.py:176`` 的 DEFAULT_UPDATE_MEMORY_PROMPT）：

- ADD    —— 新事实，写入向量库
- UPDATE —— 覆盖既有事实，**沿用同一个 backend id**，文本被替换
- DELETE —— 既有事实不再成立，引擎已把它从向量库删掉，事件里带的是
  **被删记忆的既有 backend id**
- NONE   —— 无变化

前三种里，只有 ADD/UPDATE 会被 ``FakeMemoryBackend`` 造出来（它永远返回
``event: "ADD"``），所以引擎侧「事实被删除」这条路径在测试里从来没有被覆盖过。
本文件用会产出 UPDATE/DELETE 的后端替身把这条路径钉死。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from thinkback.domain.enums import MemoryStatus
from thinkback.memory.backends import FakeMemoryBackend
from thinkback.memory.repositories import InMemoryMemoryRepository
from thinkback.memory.schemas import AppendMemoryRequest, MemoryMessage, MessageRole
from thinkback.memory.service import MemoryService

USER = "user-1"
SCOPE = "thinkback"
_T0 = datetime(2026, 5, 4, 10, 0, 0, tzinfo=UTC)
_T1 = datetime(2026, 5, 4, 10, 0, 3, tzinfo=UTC)


def _make_append(round_id: str, content: str) -> AppendMemoryRequest:
    return AppendMemoryRequest(
        request_id=f"req-{round_id}",
        user_id=USER,
        session_id="session-1",
        round_id=round_id,
        messages=[
            MemoryMessage(
                message_id=f"{round_id}-u",
                role=MessageRole.USER,
                content=content,
                timestamp=_T0,
            ),
            MemoryMessage(
                message_id=f"{round_id}-a",
                role=MessageRole.ASSISTANT,
                content="好的。",
                timestamp=_T1,
            ),
        ],
        source_timestamp=_T1,
        namespace=SCOPE,
        metadata={},
    )


class _EventBackend(FakeMemoryBackend):
    """按脚本产出引擎事件，模拟 mem0 对每条事实的判定。

    ``events_script`` 里的每一项是一次 ``add`` 的返回值；用完后回落到
    FakeMemoryBackend 的默认行为。
    """

    def __init__(self, events_script: list[list[dict[str, Any]]]) -> None:
        super().__init__()
        self._events_script = list(events_script)
        self._removed: list[str] = []

    def add(  # type: ignore[no-untyped-def]
        self,
        messages,
        *,
        user_id,
        memory_scope_id,
        metadata=None,
        infer=True,
        expiration_date=None,
        prompt=None,
    ):
        if not self._events_script:
            return super().add(
                messages,
                user_id=user_id,
                memory_scope_id=memory_scope_id,
                metadata=metadata,
                infer=infer,
                expiration_date=expiration_date,
                prompt=prompt,
            )
        events = self._events_script.pop(0)
        for event in events:
            if event.get("event") == "DELETE":
                # 引擎真的把它从向量库删掉了
                self._removed.append(str(event.get("id", "")))
                self.memories.pop(str(event.get("id", "")), None)
            elif event.get("event") == "ADD":
                self.memories[str(event["id"])] = {
                    "id": event["id"],
                    "memory": event["memory"],
                    "event": "ADD",
                    "user_id": user_id,
                    "agent_id": memory_scope_id,
                    "metadata": metadata or {},
                    "score": 1.0,
                }
        return [dict(event) for event in events]


def test_backend_delete_event_deactivates_local_index_row() -> None:
    """引擎删掉事实 → 本地索引不能还把它当 ACTIVE。

    丢掉 DELETE 事件会破掉「要么进索引、要么不存在」这条口径：引擎已删、
    本地仍活跃，管理台显示一条引擎里没有的记忆，冲突仲裁还拿它当活跃事实。
    """
    repository = InMemoryMemoryRepository()
    backend = _EventBackend(
        [
            [{"id": "backend-1", "memory": "用户喜欢猫。", "event": "ADD"}],
            # 引擎判定第一条不再成立：事件带的是**被删记忆的既有 id**
            [{"id": "backend-1", "memory": "用户喜欢猫。", "event": "DELETE"}],
        ]
    )
    service = MemoryService(repository=repository, backend=backend)

    service.append(_make_append("round-1", "我喜欢猫"))
    service.append(_make_append("round-2", "我不喜欢猫了"))

    assert backend._removed == ["backend-1"], "引擎侧应已删除该事实"

    row = repository.get_active_memory_by_backend_id(USER, SCOPE, "backend-1")
    assert row is None, "引擎删除后本地索引不应再有 ACTIVE 行"

    entry = repository.memories.get(
        next(
            memory.memory_id
            for memory in repository.list_memories(USER, SCOPE)
            if memory.backend_memory_id == "backend-1"
        )
    )
    assert entry is not None
    assert entry.memory_status is MemoryStatus.DELETED, "应保留墓碑行（可审计）"


def test_backend_delete_event_not_listed_as_live_memory() -> None:
    """默认列表不返回墓碑；include_deleted 才返回。"""
    repository = InMemoryMemoryRepository()
    backend = _EventBackend(
        [
            [{"id": "backend-1", "memory": "用户喜欢猫。", "event": "ADD"}],
            [{"id": "backend-1", "memory": "用户喜欢猫。", "event": "DELETE"}],
        ]
    )
    service = MemoryService(repository=repository, backend=backend)

    service.append(_make_append("round-1", "我喜欢猫"))
    service.append(_make_append("round-2", "我不喜欢猫了"))

    live = service.list_memory_items(user_id=USER, namespace=SCOPE)
    assert live.items == [], "引擎已删的记忆不应出现在默认管理列表里"

    with_tombstones = service.list_memory_items(user_id=USER, include_deleted=True, namespace=SCOPE)
    assert len(with_tombstones.items) == 1, "墓碑应可查（审计口径）"


def test_backend_delete_event_for_unknown_backend_id_is_a_noop() -> None:
    """引擎删了一条本地没有索引行的记忆：不建行、不报错。"""
    repository = InMemoryMemoryRepository()
    backend = _EventBackend(
        [
            [{"id": "backend-unknown", "memory": "从未被索引过。", "event": "DELETE"}],
        ]
    )
    service = MemoryService(repository=repository, backend=backend)

    response = service.append(_make_append("round-1", "随便一句"))

    assert response.status == "completed"
    assert repository.list_memories(USER, SCOPE) == []


def test_backend_update_event_keeps_local_text_in_sync() -> None:
    """UPDATE 沿用同一个 backend id 并替换文本 → 本地索引必须跟着换文本。"""
    repository = InMemoryMemoryRepository()
    backend = _EventBackend(
        [
            [{"id": "backend-1", "memory": "用户喜欢猫。", "event": "ADD"}],
            [{"id": "backend-1", "memory": "用户喜欢狗。", "event": "UPDATE"}],
        ]
    )
    service = MemoryService(repository=repository, backend=backend)

    service.append(_make_append("round-1", "我喜欢猫"))
    service.append(_make_append("round-2", "我改成喜欢狗了"))

    row = repository.get_active_memory_by_backend_id(USER, SCOPE, "backend-1")
    assert row is not None, "UPDATE 后仍应是活跃记忆"
    assert "狗" in row.memory_text, f"本地文本应更新为新值，实际是 {row.memory_text!r}"
    assert "猫" not in row.memory_text, f"本地文本应替换旧值，实际是 {row.memory_text!r}"


# ---------------------------------------------------------------------------
# 事件过滤与「二次遮罩」（此前 0 覆盖）
# ---------------------------------------------------------------------------


class _RecordingBackend(FakeMemoryBackend):
    """记录 delete 调用，用来断言后端副本真的被删了。"""

    def __init__(self) -> None:
        super().__init__()
        self.deleted: list[str] = []

    def delete(self, memory_id: str) -> None:
        self.deleted.append(memory_id)
        super().delete(memory_id)


def test_an_l3_event_whose_source_round_was_deleted_is_not_indexed() -> None:
    """来源轮次已被删 → 这条 L3 事实不进本地索引，后端那份也要删掉。

    删掉一轮对话后，mem0 里抽出来的结论还在；把它索引进本地 = 用户删了
    原文，/items 里还留着结论。所以这里不但不索引，还要把后端副本删掉。

    这是「二次遮罩」：写入与索引之间可能刚好落进一次删除，第一次遮罩
    拦不住，得在真正建索引行之前再查一次。
    """
    repository = InMemoryMemoryRepository()
    backend = _RecordingBackend()
    service = MemoryService(repository=repository, backend=backend)

    deleted = repository.add_memory_index(
        backend_memory_id="deleted-1",
        user_id=USER,
        memory_scope_id=SCOPE,
        source_refs=[{"session_id": "session-1", "round_id": "round-deleted"}],
        memory_text="已删除的事实",
    )
    repository.mark_memory_deleted(deleted.memory_id)

    service._index_l3_event(
        {"id": "backend-l3-1", "memory": "用户喜欢猫", "event": "ADD"},
        user_id=USER,
        memory_scope_id=SCOPE,
        source_refs=[{"session_id": "session-1", "round_id": "round-deleted"}],
        l3_metadata={"source_text": "我喜欢猫"},
    )

    assert repository.get_active_memory_by_backend_id(USER, SCOPE, "backend-l3-1") is None, (
        "来源轮次已删，这条 L3 事实不该进本地索引"
    )
    assert backend.deleted == ["backend-l3-1"], "后端那份没被删掉：结论还留在引擎侧"


def test_an_unsupported_l3_event_is_skipped() -> None:
    """NONE 事件（引擎判定「无变化」）不得进本地索引。

    mem0 让 LLM 对每条事实给出 ADD/UPDATE/DELETE/NONE 判定。NONE 不该
    产生任何索引行 —— 否则一次「无变化」会在 /items 里凭空多出一条记录。
    """
    repository = InMemoryMemoryRepository()
    backend = _RecordingBackend()
    service = MemoryService(repository=repository, backend=backend)

    service._index_l3_event(
        {"id": "backend-none", "memory": "用户喜欢猫", "event": "NONE"},
        user_id=USER,
        memory_scope_id=SCOPE,
        source_refs=[{"session_id": "session-1", "round_id": "round-none"}],
        l3_metadata={"source_text": "我喜欢猫"},
    )

    assert repository.get_active_memory_by_backend_id(USER, SCOPE, "backend-none") is None


def test_an_l3_event_without_a_backend_id_is_skipped() -> None:
    """没有 backend id 的事件建不出索引行，只能跳过。"""
    repository = InMemoryMemoryRepository()
    backend = _RecordingBackend()
    service = MemoryService(repository=repository, backend=backend)

    service._index_l3_event(
        {"id": "", "memory": "用户喜欢猫", "event": "ADD"},
        user_id=USER,
        memory_scope_id=SCOPE,
        source_refs=[{"session_id": "session-1", "round_id": "round-no-id"}],
        l3_metadata={"source_text": "我喜欢猫"},
    )

    assert repository.list_memories(USER, SCOPE) == []
