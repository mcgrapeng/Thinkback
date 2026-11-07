"""编辑长期记忆的**失败语义**：本地索引先提交、后端写失败 → 可重试。

编辑一条记忆要同时改两处：后端（mem0/Milvus）与本地索引。
顺序是**先本地、后后端**（后端写放在锁外，避免 mem0 网络 I/O 串行化同 user 的写入）：

```
锁内：算好本地索引 → 更新本地索引 → 收集待写的后端操作
锁外：执行后端写
      失败 → 任务标 FAILED → 返回 failed（**不**回滚本地索引）
```

**为什么不回滚本地索引**：本地索引是这次编辑的「已提交版本」。
后端写失败只说明**后端那一侧**没同步上，本地这一侧的内容是对的
（用户改的内容本身没问题）。回滚本地索引会把用户的编辑整个丢掉 ——
用户白改一次，还得再改一遍。

代价是：失败后到重试成功之间，两侧内容不一致。
**这个不一致由「可重试」兜住**：用**同一个 operation_id** 重试，
本地索引幂等重放、���端写补上，两侧收敛。

所以本文件钉的是三件事：

1. 后端写失败 → 本地索引**保留**新内容（不回滚），且如实报 ``failed``；
2. 同一个 ``operation_id`` 重试 → 后端补写成功，两侧**收敛一致**；
3. 本地 P0 索引行（``local-p0:`` 前缀）与后端管理的记忆是两种东西 ——
   前者只在本地库里，改它**不需要**通知后端。

**为什么要专门写这份**：``update_memory`` 的这条失败路径此前只有
「本地索引不回滚」这一半被断言（``test_backend_update_failure_does_not_corrupt_index``），
**「重试能收敛」那一半没人验** —— 而它正是这个设计成立的前提。
少了它，「失败后重试」这件事在代码里是**没有证据**的。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from thinkback.domain.enums import TaskStatus
from thinkback.memory.backends import FakeMemoryBackend
from thinkback.memory.repositories import InMemoryMemoryRepository
from thinkback.memory.schemas import (
    AppendMemoryRequest,
    MessageRole,
    UpdateMemoryRequest,
)
from thinkback.memory.service import MemoryService

_TS = datetime(2026, 11, 5, tzinfo=UTC)


class _FlakyUpdateBackend(FakeMemoryBackend):
    """``update`` 可以被打开/关掉的假后端。"""

    def __init__(self) -> None:
        super().__init__()
        self.fail = True
        self.update_calls: list[tuple[str, str]] = []

    def update(self, memory_id: str, data: str, **kwargs: Any) -> dict[str, Any]:
        self.update_calls.append((memory_id, data))
        if self.fail:
            raise RuntimeError("backend update failed")
        self.memories[memory_id]["memory"] = data
        return {"message": "ok"}


def _service(backend: Any = None) -> tuple[MemoryService, InMemoryMemoryRepository]:
    repository = InMemoryMemoryRepository()
    return (
        MemoryService(
            repository=repository,
            backend=backend or FakeMemoryBackend(),
            l3_write_mode="sync",
        ),
        repository,
    )


def _append(service: MemoryService, content: str = "我叫麻薯") -> str:
    service.append(
        AppendMemoryRequest(
            request_id="req-r1",
            user_id="u1",
            session_id="s1",
            round_id="r1",
            messages=[
                {
                    "message_id": "m1",
                    "role": MessageRole.USER,
                    "content": content,
                    "timestamp": _TS,
                }
            ],
            source_timestamp=_TS,
            metadata={},
        )
    )
    items = service.list_memory_items(user_id="u1", namespace="thinkback")
    assert items.items
    return items.items[0].memory_id


def _update_request(memory_id: str, content: str = "新名字") -> UpdateMemoryRequest:
    return UpdateMemoryRequest(
        request_id="req-upd",
        user_id="u1",
        memory_id=memory_id,
        operation_id="op-1",
        content=content,
        namespace="thinkback",
    )


def _texts(service: MemoryService) -> list[str]:
    items = service.list_memory_items(user_id="u1", namespace="thinkback")
    return [item.content for item in items.items]


def test_a_backend_update_failure_keeps_the_new_content_in_the_local_index() -> None:
    """后端写失败 → 本地索引**保留**新内容（不回滚）。

    本地索引是这次编辑的已提交版本；回滚等于把用户的编辑整个丢掉。
    """
    backend = _FlakyUpdateBackend()
    service, _repository = _service(backend)
    memory_id = _append(service)

    service.update_memory(_update_request(memory_id))

    assert _texts(service) == ["新名字"], f"后端失败后本地索引不该回滚：{_texts(service)}"


def test_a_backend_update_failure_reports_failed_to_the_caller() -> None:
    """后端写失败 → 返回 ``failed``，不是谎报成功。

    后端没写成却返回 completed，等于告诉治理方「已改」而实际 L3 还是旧内容。
    """
    backend = _FlakyUpdateBackend()
    service, _repository = _service(backend)
    memory_id = _append(service)

    response = service.update_memory(_update_request(memory_id))

    assert response.status == "failed", f"后端失败却报了成功：{response.status}"


def test_a_backend_update_failure_marks_the_task_failed_with_the_reason() -> None:
    """后端写失败 → 任务标 FAILED 并记失败原因（客户端查得到）。"""
    backend = _FlakyUpdateBackend()
    service, repository = _service(backend)
    memory_id = _append(service)

    response = service.update_memory(_update_request(memory_id))

    task = repository.get_task(f"memory-update:{response.task_id.split(':')[-1]}")
    assert task is not None
    assert task.status is TaskStatus.FAILED, f"任务状态是 {task.status}，不是 FAILED"
    assert "backend update failed" in (task.last_error or ""), (
        f"任务没记失败原因：{task.last_error!r}"
    )


def test_retrying_the_same_operation_id_converges_both_sides() -> None:
    """同一个 ``operation_id`` 重试 → 后端补写成功，两侧**收敛一致**。

    这是「本地索引不回滚」这个设计成立的前提：
    失败 → 用同一把 operation_id 重试 → 本地幂等重放、后端补上。
    没有这条，重试就只是「再失败一次」，不一致永远收敛不了。
    """
    backend = _FlakyUpdateBackend()
    service, _repository = _service(backend)
    memory_id = _append(service)

    first = service.update_memory(_update_request(memory_id))
    assert first.status == "failed"
    assert _texts(service) == ["新名字"]

    # 后端恢复，用同一把 operation_id 重试
    backend.fail = False
    second = service.update_memory(_update_request(memory_id))

    assert second.status == "completed", f"重试没有成功：{second.status}"
    assert _texts(service) == ["新名字"]
    backend_texts = [memory["memory"] for memory in backend.memories.values()]
    assert backend_texts == ["新名字"], f"重试后后端还是旧内容：{backend_texts}"


def test_the_retry_writes_the_backend_again() -> None:
    """重试会**再写一次**后端（第一次失败的那次不算数）。

    幂等重放要能把上次没写成的内容补上，所以后端会被写两次：
    第一次抛错、第二次成功。写同一个内容、幂等，不产生重复事实。
    """
    backend = _FlakyUpdateBackend()
    service, _repository = _service(backend)
    memory_id = _append(service)

    service.update_memory(_update_request(memory_id))
    backend.fail = False
    service.update_memory(_update_request(memory_id))

    attempts = [call for call in backend.update_calls if call[1] == "新名字"]
    assert len(attempts) == 2, f"后端应被写两次（一次失败一次成功），实际 {len(attempts)} 次"
    # 两次写的是同一个 memory_id 与同一份内容 → 幂等
    assert len(set(attempts)) == 1, f"重试写的内容/对象与上次不同：{set(attempts)}"


def test_a_successful_update_changes_the_content() -> None:
    """正常路径：编辑成功后本地索引变成新内容。"""
    service, _repository = _service()
    memory_id = _append(service)

    response = service.update_memory(_update_request(memory_id))

    assert response.status == "completed"
    assert "新名字" in _texts(service), _texts(service)


def test_a_local_p0_memory_update_does_not_touch_the_backend() -> None:
    """本地 P0 索引行（``local-p0:`` 前缀）改**不需要**通知后端。

    它只存在于本地库里；去后端改它 = 改一条不存在的记录。
    """
    backend = FakeMemoryBackend()
    service, repository = _service(backend)
    _append(service)

    # 造一条本地 P0 索引行（backend_memory_id 带 local-p0: 前缀 = 本地合成）
    row = repository.add_memory_index(
        backend_memory_id="local-p0:1",
        user_id="u1",
        memory_scope_id="thinkback",
        source_refs=[{"session_id": "s1", "round_id": "r1"}],
        memory_text="居住城市：上海",
    )
    before = len(backend.memories)

    service.update_memory(
        UpdateMemoryRequest(
            request_id="req-upd2",
            user_id="u1",
            memory_id=row.memory_id,
            operation_id="op-2",
            content="居住城市：杭州",
            namespace="thinkback",
        )
    )

    assert len(backend.memories) == before, "本地 P0 行的编辑不应该动后端"


def test_editing_to_a_different_recall_slot_is_rejected() -> None:
    """编辑不能把一条事实挪到**另一个召回槽位**。

    槽位变了 = 这条事实从「猫名」变成了「住址」，语义已经不是同一条；
    旧槽位的其它事实会因此变得不可达。
    """
    service, repository = _service()
    _append(service)

    row = repository.add_memory_index(
        backend_memory_id="local-p0:2",
        user_id="u1",
        memory_scope_id="thinkback",
        source_refs=[{"session_id": "s1", "round_id": "r1"}],
        memory_text="居住城市：上海",
    )

    with pytest.raises(ValueError, match="slot"):
        service.update_memory(
            UpdateMemoryRequest(
                request_id="req-upd3",
                user_id="u1",
                memory_id=row.memory_id,
                operation_id="op-3",
                content="User has a cat named 麻薯",
                namespace="thinkback",
            )
        )
