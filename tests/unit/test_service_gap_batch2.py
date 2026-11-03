"""service.py 第二批行覆盖缺口的逐行收口。

第一批（``test_service_core_decisions.py`` / ``test_service_error_mapping.py``）
钉的是「决定结果」的主干分支；这一批补的是主干旁边的边角：**幂等短路、
墓碑去重、失败回滚、竞态兜底**。

这些行的共同点是**改坏了没人会发现**：

- 墓碑重复写 / 更新任务重复执行 → 同一 operation_id 产生两条墓碑，擦除屏障
  语义漂移；
- 更新失败不回滚本地索引 → 用户看到「编辑失败」，库里却已经是新内容；
- 回滚不把被 supersede 的记忆标回 active → 事实凭空消失；
- rebuild 抢不到任务时自己跑一遍 → 两个副本同时重建，重复抽取。

行覆盖率只证明「这行跑过」，证明不了「结论对不对」。这里每个用例钉一个
具体结论：把分支反过来（跳过写成执行、跳过写成跳过、回滚写成不回滚）时
必须红。

所有依赖都是内存实现：``InMemoryMemoryRepository`` + ``FakeMemoryBackend``，
不碰网络、真实 LLM / Milvus / PG。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

import pytest

from thinkback.domain.entities import TaskEntry
from thinkback.domain.enums import OperationType, TaskStatus
from thinkback.memory.backends import FakeMemoryBackend
from thinkback.memory.repositories import InMemoryMemoryRepository
from thinkback.memory.schemas import (
    AppendMemoryRequest,
    DeleteMemoryRequest,
    DeleteScope,
    MemoryMessage,
    MemoryType,
    MessageRole,
    RebuildMemoryRequest,
    UpdateMemoryRequest,
)
from thinkback.memory.service import MemoryService

LONG_TERM_SCOPE = "thinkback"


def _service(**kwargs: Any) -> MemoryService:
    return MemoryService(
        repository=InMemoryMemoryRepository(), backend=FakeMemoryBackend(), **kwargs
    )


def _round(
    *,
    round_id: str,
    session_id: str = "s1",
    content: str = "我养了一只猫，名字叫阿福。",
) -> AppendMemoryRequest:
    return AppendMemoryRequest(
        request_id=f"req-{round_id}",
        user_id="user-1",
        session_id=session_id,
        round_id=round_id,
        messages=[
            MemoryMessage(
                message_id=f"{round_id}-u",
                role=MessageRole.USER,
                content=content,
                timestamp=datetime(2026, 1, 1, tzinfo=UTC),
            )
        ],
        source_timestamp=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _update_task(
    repository: InMemoryMemoryRepository,
    *,
    operation_id: str,
    content: str,
    status: TaskStatus,
    memory_id: str = "memory-missing",
    result: dict[str, Any] | None = None,
) -> TaskEntry:
    """落一条与 update 请求 scope 完全一致的任务行（幂等分支的前置条件）。"""
    task = TaskEntry(
        task_id=f"memory-update:{operation_id}",
        request_id="req-1",
        op_type=OperationType.UPDATE_MEMORY,
        scope={
            "user_id": "user-1",
            "long_term_scope_id": LONG_TERM_SCOPE,
            "memory_id": memory_id,
            "content_fingerprint": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "memory_type": None,
        },
        status=status,
        operation_id=operation_id,
        result=result or {},
    )
    repository.save_task(task)
    return task


def _update_request(
    *, operation_id: str, memory_id: str, content: str, memory_type: MemoryType | None = None
) -> UpdateMemoryRequest:
    return UpdateMemoryRequest(
        request_id="req-1",
        user_id="user-1",
        operation_id=operation_id,
        memory_id=memory_id,
        content=content,
        memory_type=memory_type,
    )


# ---------------------------------------------------------------------------
# 擦除范围扫描 / 墓碑去重
# ---------------------------------------------------------------------------


def test_deleted_round_refs_skips_a_row_with_corrupted_metadata() -> None:
    """metadata 不是 dict 的行必须被跳过，不能把扫描整个打断掉。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    entry = repository.add_memory_index(
        backend_memory_id="backend-1",
        user_id="user-1",
        memory_scope_id=LONG_TERM_SCOPE,
        source_refs=[{"session_id": "s1", "round_id": "r1"}],
        memory_text="我养了一只猫",
        metadata={"delete_all_tombstone": True},
    )
    assert service._deleted_round_refs("user-1", LONG_TERM_SCOPE, DeleteScope.ALL) == [
        {"session_id": "s1", "round_id": "r1"}
    ]

    entry.metadata = ["corrupted"]  # type: ignore[assignment]

    assert service._deleted_round_refs("user-1", LONG_TERM_SCOPE, DeleteScope.ALL) == []


def test_erase_tombstone_is_written_once_per_operation_id() -> None:
    """同一 operation_id 重复擦除只能留一条墓碑：屏障语义靠墓碑唯一性。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())

    for _ in range(2):
        service._add_erase_tombstone(
            user_id="user-1",
            memory_scope_id=LONG_TERM_SCOPE,
            source_refs=[{"session_id": "s1", "round_id": "r1"}],
            operation_id="op-erase",
        )

    tombstones = [
        memory
        for memory in repository.list_memories("user-1", LONG_TERM_SCOPE)
        if (memory.metadata or {}).get("erase_tombstone") is True
    ]
    assert len(tombstones) == 1


def test_erased_message_texts_skips_refs_without_round_id() -> None:
    """缺 round_id 的来源引用取不到原文，必须跳过而不是按空串去匹配。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    repository.save_round(_round(round_id="r1", content="原始正文"))

    texts = service._erased_message_texts(
        [{"session_id": "s1"}, {"session_id": "s1", "round_id": "r1"}],
        LONG_TERM_SCOPE,
    )

    assert texts == ["原始正文"]


def test_replayed_erase_returns_the_completed_response_of_the_existing_task() -> None:
    """同一 operation_id 重复擦除：回放既有任务的结果，不重跑擦除阶段。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    entry = repository.add_memory_index(
        backend_memory_id="backend-1",
        user_id="user-1",
        memory_scope_id=LONG_TERM_SCOPE,
        source_refs=[{"session_id": "s1", "round_id": "r1"}],
        memory_text="我养了一只猫",
    )
    request = DeleteMemoryRequest(
        request_id="erase-1",
        user_id="user-1",
        scope=DeleteScope.MEMORY,
        operation_id="op-erase",
        memory_id=entry.memory_id,
    )

    first = service.erase(request)
    second = service.erase(request)

    assert first.status == "completed"
    assert second.status == "completed"
    assert second.task_id == first.task_id
    assert second.affected_memories == first.affected_memories


def test_erase_targets_one_requires_a_memory_id() -> None:
    """单条擦除缺 memory_id 必须显式拒绝，不能退化成「删空」。"""
    service = _service()
    request = DeleteMemoryRequest(
        request_id="erase-1",
        user_id="user-1",
        scope=DeleteScope.ALL,
        operation_id="op-erase",
    )

    with pytest.raises(ValueError, match="memory_id is required"):
        service._erase_targets_one(request, LONG_TERM_SCOPE)


def test_erase_targets_session_requires_a_session_id() -> None:
    """会话擦除缺 session_id 必须显式拒绝，不能退化成「删全量」。"""
    service = _service()
    request = DeleteMemoryRequest(
        request_id="erase-1",
        user_id="user-1",
        scope=DeleteScope.ALL,
        operation_id="op-erase",
    )

    with pytest.raises(ValueError, match="session_id is required"):
        service._erase_targets_session(request, LONG_TERM_SCOPE)


# ---------------------------------------------------------------------------
# 更新幂等：任务已存在但记忆已经不在
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [TaskStatus.DEAD_LETTER, TaskStatus.RUNNING, TaskStatus.PENDING])
def test_replayed_update_reports_memory_not_found_when_the_memory_is_gone(
    status: TaskStatus,
) -> None:
    """任务在、记忆没了：必须报「记忆不存在」，不能回一个空记忆的成功态。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    _update_task(
        repository,
        operation_id=f"op-{status.value}",
        content="新内容",
        status=status,
    )

    with pytest.raises(ValueError, match="memory not found"):
        service.update_memory(
            _update_request(
                operation_id=f"op-{status.value}",
                memory_id="memory-missing",
                content="新内容",
            )
        )


def test_replayed_completed_update_reports_conflict_when_the_memory_is_gone() -> None:
    """已完成任务重放时记忆已被删：报冲突（409），不能谎报「早就成功」。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    _update_task(
        repository,
        operation_id="op-completed",
        content="新内容",
        status=TaskStatus.COMPLETED,
        result={"memory_id": "memory-missing", "content_fingerprint": "x"},
    )

    with pytest.raises(ValueError, match="operation_id conflict"):
        service.update_memory(
            _update_request(
                operation_id="op-completed",
                memory_id="memory-missing",
                content="新内容",
            )
        )


def test_replayed_completed_update_falls_back_to_the_scope_memory_type() -> None:
    """任务 result 里没有 memory_type 时，期望类型从 task.scope 取。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    entry = repository.add_memory_index(
        backend_memory_id="backend-1",
        user_id="user-1",
        memory_scope_id=LONG_TERM_SCOPE,
        source_refs=[{"session_id": "s1", "round_id": "r1"}],
        memory_text="新内容",
        memory_type=MemoryType.PROFILE.value,
    )
    fingerprint = hashlib.sha256("新内容".encode()).hexdigest()
    repository.save_task(
        TaskEntry(
            task_id="memory-update:op-scope",
            request_id="req-1",
            op_type=OperationType.UPDATE_MEMORY,
            scope={
                "user_id": "user-1",
                "long_term_scope_id": LONG_TERM_SCOPE,
                "memory_id": entry.memory_id,
                "content_fingerprint": fingerprint,
                "memory_type": MemoryType.PROFILE.value,
            },
            status=TaskStatus.COMPLETED,
            operation_id="op-scope",
            # result 里故意没有 memory_type：只能回落 scope。
            result={"memory_id": entry.memory_id, "content_fingerprint": fingerprint},
        )
    )

    response = service.update_memory(
        _update_request(
            operation_id="op-scope",
            memory_id=entry.memory_id,
            content="新内容",
            memory_type=MemoryType.PROFILE,
        )
    )

    assert response.status == "already_done"
    assert response.task_id == "memory-update:op-scope"


# ---------------------------------------------------------------------------
# rebuild：抢不到任务 / dead_letter / 受限轮次
# ---------------------------------------------------------------------------


def test_rebuild_returns_the_existing_response_when_the_task_claim_is_lost() -> None:
    """claim 抢不到（另一个副本先抢到）：回放对方状态，不并行重建。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())

    def fake_claim(task: TaskEntry) -> tuple[TaskEntry, bool]:
        return (
            TaskEntry(
                task_id=task.task_id,
                request_id=task.request_id,
                op_type=task.op_type,
                scope=dict(task.scope),
                status=TaskStatus.RUNNING,
                operation_id=task.operation_id,
            ),
            False,
        )

    repository.claim_task = fake_claim  # type: ignore[method-assign]

    response = service.rebuild(
        RebuildMemoryRequest(
            request_id="rebuild-1",
            user_id="user-1",
            operation_id="op-rebuild",
            rebuild_l2=False,
            rebuild_l3=True,
        )
    )

    assert response.status == "running"
    assert response.task_id == "memory-rebuild:op-rebuild"


def test_rebuild_short_circuits_a_dead_letter_task() -> None:
    """dead_letter 是终态：直接回报 failed，不复活任务重跑重建。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    repository.save_task(
        TaskEntry(
            task_id="memory-rebuild:op-dead",
            request_id="req-1",
            op_type=OperationType.REBUILD_L3,
            scope={
                "user_id": "user-1",
                "session_id": None,
                "session_scope_id": "__all_sessions__",
                "long_term_scope_id": LONG_TERM_SCOPE,
                "rebuild_l2": False,
                "rebuild_l3": True,
                "history_version": None,
            },
            status=TaskStatus.DEAD_LETTER,
            operation_id="op-dead",
        )
    )

    response = service.rebuild(
        RebuildMemoryRequest(
            request_id="rebuild-1",
            user_id="user-1",
            operation_id="op-dead",
            rebuild_l2=False,
            rebuild_l3=True,
        )
    )

    assert response.status == "failed"
    assert response.rebuilt_l2 is False
    assert response.rebuilt_l3 is False


def test_rebuild_replay_skips_rounds_with_restricted_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回放时命中 restricted 的轮次必须跳过，安全轮次照常回放。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    repository.save_round(_round(round_id="r-safe"))
    repository.save_round(_round(round_id="r-unsafe", content="api_key: abc123"))

    replayed: list[str] = []

    def fake_run_l3_write(**kwargs: Any) -> list[dict[str, Any]]:
        replayed.append(str(kwargs["source_refs"][0]["round_id"]))
        return []

    monkeypatch.setattr(service, "_run_l3_write", fake_run_l3_write)

    response = service.rebuild(
        RebuildMemoryRequest(
            request_id="rebuild-1",
            user_id="user-1",
            session_id="s1",
            operation_id="op-rebuild",
            rebuild_l2=False,
            rebuild_l3=True,
        )
    )

    assert response.status == "completed"
    assert replayed == ["r-safe"]


# ---------------------------------------------------------------------------
# 读路径自愈
# ---------------------------------------------------------------------------


def test_get_task_returns_none_when_the_reclaimed_task_disappeared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """幽灵 RUNNING 被回收后任务行已消失：如实返回 None，不返回过期快照。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    ghost = TaskEntry(
        task_id="t1",
        request_id="req-1",
        op_type=OperationType.REBUILD_L3,
        scope={},
        status=TaskStatus.RUNNING,
    )
    reads = {"count": 0}

    def fake_get_task(task_id: str) -> TaskEntry | None:
        reads["count"] += 1
        return ghost if reads["count"] == 1 else None

    monkeypatch.setattr(repository, "get_task", fake_get_task)
    monkeypatch.setattr(
        repository, "reclaim_stale_running_task", lambda task_id, *, max_age_seconds: True
    )

    assert service.get_task("t1") is None


# ---------------------------------------------------------------------------
# 记忆可管理性 / 枚举回落
# ---------------------------------------------------------------------------


def test_a_row_with_corrupted_metadata_is_still_manageable() -> None:
    """metadata 不是 dict 时按「可管理」处理：不能因为脏数据把记忆藏起来。"""
    memory = type("_Memory", (), {"backend_memory_id": "backend-1", "metadata": ["corrupted"]})()

    assert MemoryService._is_manageable_memory(memory) is True


def test_enum_or_none_returns_the_value_of_a_str_enum_member() -> None:
    """传入枚举成员时取 value 的字符串，而不是成员名。"""
    assert MemoryService._enum_or_none(MemoryType, MemoryType.PROFILE) == "profile"


# ---------------------------------------------------------------------------
# 更新失败：索引写失败 / 回滚
# ---------------------------------------------------------------------------


def test_update_fails_when_the_local_index_update_returns_nothing() -> None:
    """索引写没落库（返回 None）必须报错，不能当作更新成功继续。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    entry = repository.add_memory_index(
        backend_memory_id="backend-1",
        user_id="user-1",
        memory_scope_id=LONG_TERM_SCOPE,
        source_refs=[{"session_id": "s1", "round_id": "r1"}],
        memory_text="旧内容",
    )
    repository.update_memory_index = lambda *args, **kwargs: None  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="memory index update failed"):
        service.update_memory(
            _update_request(operation_id="op-1", memory_id=entry.memory_id, content="新内容")
        )


def test_failed_update_restores_the_previous_local_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """更新中途失败：本地索引必须回滚成旧内容，不能停在半写状态。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    entry = repository.add_memory_index(
        backend_memory_id="backend-1",
        user_id="user-1",
        memory_scope_id=LONG_TERM_SCOPE,
        source_refs=[{"session_id": "s1", "round_id": "r1"}],
        memory_text="旧内容",
    )

    def boom(**kwargs: Any) -> tuple[list[str], list[Any]]:
        raise RuntimeError("supersede failed")

    monkeypatch.setattr(service, "_supersede_conflicting_memories", boom)

    with pytest.raises(RuntimeError, match="supersede failed"):
        service.update_memory(
            _update_request(operation_id="op-1", memory_id=entry.memory_id, content="新内容")
        )

    assert repository.memories[entry.memory_id].memory_text == "旧内容"


def test_failed_update_marks_superseded_memories_active_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回滚时被 supersede 的记忆必须标回 active：事实不能凭空消失。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    target = repository.add_memory_index(
        backend_memory_id="backend-1",
        user_id="user-1",
        memory_scope_id=LONG_TERM_SCOPE,
        source_refs=[{"session_id": "s1", "round_id": "r1"}],
        memory_text="旧内容",
    )
    other = repository.add_memory_index(
        backend_memory_id="backend-2",
        user_id="user-1",
        memory_scope_id=LONG_TERM_SCOPE,
        source_refs=[{"session_id": "s1", "round_id": "r2"}],
        memory_text="另一条事实",
    )
    repository.mark_memory_superseded(other.memory_id)

    def fake_supersede(**kwargs: Any) -> tuple[list[str], list[Any]]:
        # 返回不可迭代的 pending 写：extend 在这里炸掉，触发回滚路径。
        return ([other.memory_id], None)

    monkeypatch.setattr(service, "_supersede_conflicting_memories", fake_supersede)

    with pytest.raises(TypeError):
        service.update_memory(
            _update_request(operation_id="op-1", memory_id=target.memory_id, content="新内容")
        )

    assert repository.memories[other.memory_id].memory_status.value == "ACTIVE"
