"""ALL 范围擦除：擦除期间用户新写的记忆不算残留。

回归的场景：擦除走完删除阶段后，用户又写了一条记忆。旧口径把作用域里的
任何记录都当成「没擦干净」，于是这次擦除必然失败；重试再删一次（连新写的
那条一起删），复核还是失败 —— 用户永远擦不掉，而且越重试丢得越多。

正确口径：ALL 复核的是**这次真删掉的那批 ID**，新写的记忆不属于这次擦除
的目标（用户还在用这个账号）。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from thinkback.memory.backends import FakeMemoryBackend
from thinkback.memory.repositories import InMemoryMemoryRepository
from thinkback.memory.schemas import (
    AppendMemoryRequest,
    DeleteMemoryRequest,
    DeleteScope,
    MemoryMessage,
    MessageRole,
)
from thinkback.memory.service import MemoryService


class _WriteDuringEraseBackend(FakeMemoryBackend):
    """删除阶段之后立刻冒出一条新记忆（模拟擦除期间用户又写了）。"""

    def __init__(self) -> None:
        super().__init__()
        self.new_written = False

    def delete_all(self, *, user_id: str, memory_scope_id: str) -> list[str]:
        deleted = super().delete_all(user_id=user_id, memory_scope_id=memory_scope_id)
        if not self.new_written:
            self.new_written = True
            self.memories["written-during-erase"] = {
                "id": "written-during-erase",
                "memory": "用户在擦除进行中又写的一条",
                "event": "ADD",
                "user_id": user_id,
                "agent_id": memory_scope_id,
                "metadata": {},
                "score": 1.0,
            }
        return deleted


def _append(round_id: str, content: str) -> AppendMemoryRequest:
    now = datetime(2026, 10, 29, 10, 0, 0, tzinfo=UTC)
    return AppendMemoryRequest(
        request_id=f"req-{round_id}",
        user_id="user-1",
        session_id="session-1",
        round_id=round_id,
        messages=[
            MemoryMessage(
                message_id=f"{round_id}-u",
                role=MessageRole.USER,
                content=content,
                timestamp=now,
            )
        ],
        source_timestamp=now,
    )


def test_erase_all_succeeds_when_user_writes_during_the_erase() -> None:
    """擦除期间新写的记忆不算残留：擦除要能完成，新记忆也不能被当成没擦干净。"""
    backend = _WriteDuringEraseBackend()
    service = MemoryService(
        repository=InMemoryMemoryRepository(),
        backend=backend,
        l3_write_mode="sync",
    )
    service.append(_append("r1", "要被擦除的事实"))

    response = service.erase(
        DeleteMemoryRequest(
            request_id="req-erase",
            user_id="user-1",
            scope=DeleteScope.ALL,
            operation_id="op-erase",
        )
    )

    assert response.status == "completed"
    # 目标擦掉了；擦除期间新写的那条还在（它不属于这次擦除的目标）。
    assert list(backend.memories) == ["written-during-erase"]


def test_erase_all_still_fails_when_a_target_survives() -> None:
    """目标没删掉时仍然要失败：不能因为放宽口径就把「没擦干净」放过去。"""

    class _StubbornBackend(_WriteDuringEraseBackend):
        def delete_all(self, *, user_id: str, memory_scope_id: str) -> list[str]:
            return []  # 什么都没删掉

    backend = _StubbornBackend()
    service = MemoryService(
        repository=InMemoryMemoryRepository(),
        backend=backend,
        l3_write_mode="sync",
    )
    service.append(_append("r1", "要被擦除的事实"))

    with pytest.raises(RuntimeError, match="still present in backend"):
        service.erase(
            DeleteMemoryRequest(
                request_id="req-erase",
                user_id="user-1",
                scope=DeleteScope.ALL,
                operation_id="op-erase",
            )
        )
