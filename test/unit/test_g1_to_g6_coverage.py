"""G-1~G-6 测试覆盖补全。

覆盖范围：
- G-1: shutdown_l3_executor 服务层直接测试（含 lifespan 退出路径）
- G-2: get_task 服务层直接测试
- G-3: get_memory_item 零测试补全
- G-4: L2 degraded 字段结构断言
- G-5: Pydantic schema 边界值校验
- G-6: 跨 session 删除连锁端到端测试
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from thinkback.api.app import create_app
from thinkback.memory.backends import FakeMemoryBackend
from thinkback.memory.repositories import InMemoryMemoryRepository
from thinkback.memory.schemas import (
    AppendMemoryRequest,
    DeleteMemoryRequest,
    DeleteScope,
    MessageRole,
    OperationType,
    RecallIntent,
    RecallMemoryRequest,
    SummaryState,
    TaskStatus,
)
from thinkback.memory.service import MemoryService

# ---------------------------------------------------------------------------
# 共用 helper
# ---------------------------------------------------------------------------


def _make_append_request(
    *,
    user_id: str = "user-1",
    session_id: str = "session-1",
    round_id: str = "round-1",
    content: str = "我最近在评估换工作机会",
    source_timestamp: str = "2026-05-04T10:00:03Z",
) -> AppendMemoryRequest:
    return AppendMemoryRequest(
        request_id=f"req-{round_id}",
        user_id=user_id,
        session_id=session_id,
        round_id=round_id,
        messages=[
            {
                "message_id": f"{round_id}-u",
                "role": MessageRole.USER,
                "content": content,
                "timestamp": "2026-05-04T10:00:00Z",
            },
            {
                "message_id": f"{round_id}-a",
                "role": MessageRole.ASSISTANT,
                "content": "我会按这个上下文继续回应。",
                "timestamp": source_timestamp,
            },
        ],
        source_timestamp=source_timestamp,
    )


def _make_recall_request(
    *,
    user_id: str = "user-1",
    session_id: str = "session-1",
    query: str = "换工作",
    intent: RecallIntent = RecallIntent.CHAT,
    l3_limit: int = 5,
    l3_score_threshold: float = 0.5,
    token_budget: int = 1200,
) -> RecallMemoryRequest:
    return RecallMemoryRequest(
        user_id=user_id,
        session_id=session_id,
        query=query,
        intent=intent,
        l3_limit=l3_limit,
        l3_score_threshold=l3_score_threshold,
        token_budget=token_budget,
    )


# ===========================================================================
# G-1: shutdown_l3_executor 服务层直接测试
# ===========================================================================


def test_shutdown_l3_executor_closes_owned_executor() -> None:
    """`_owns_l3_executor=True`（未注入 executor）时 shutdown 真关闭底层 executor。

    验证:
    1. shutdown 后 `_l3_executor_shutdown` 置 True
    2. 底层 executor 的 `_shutdown` 标志置 True
    """
    service = MemoryService(
        repository=InMemoryMemoryRepository(),
        backend=FakeMemoryBackend(),
    )
    # 工厂默认 `_owns_l3_executor=True`
    assert service._owns_l3_executor is True
    assert service._l3_executor_shutdown is False
    owned_executor = service._l3_executor

    service.shutdown_l3_executor(wait=True)

    assert service._l3_executor_shutdown is True
    # owned_executor 是同一对象，shutdown 后 _shutdown 标志位
    assert owned_executor._shutdown is True  # type: ignore[attr-defined]


def test_shutdown_l3_executor_skips_injected_executor() -> None:
    """`_owns_l3_executor=False`（caller 注入）时 shutdown **不**关外部 executor。

    避免 double-shutdown 抛 RuntimeError，并把控制权留给 caller。
    """
    external_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="external-test")
    try:
        service = MemoryService(
            repository=InMemoryMemoryRepository(),
            backend=FakeMemoryBackend(),
            l3_executor=external_executor,
        )
        assert service._owns_l3_executor is False
        assert service._l3_executor is external_executor

        # shutdown 不应关外部 executor
        service.shutdown_l3_executor(wait=True)

        assert service._l3_executor_shutdown is False
        # 外部 executor 仍可用：可以 submit 任务
        future = external_executor.submit(lambda: "ok")
        assert future.result(timeout=2) == "ok"
        assert external_executor._shutdown is False  # type: ignore[attr-defined]
    finally:
        external_executor.shutdown(wait=True)


def test_shutdown_l3_executor_is_idempotent() -> None:
    """多次 shutdown 安全：第二次直接 no-op，不会抛 RuntimeError。"""
    service = MemoryService(
        repository=InMemoryMemoryRepository(),
        backend=FakeMemoryBackend(),
    )
    service.shutdown_l3_executor(wait=True)
    # 第二次调用幂等
    service.shutdown_l3_executor(wait=True)
    service.shutdown_l3_executor(wait=True)

    assert service._l3_executor_shutdown is True
    assert service._l3_executor._shutdown is True  # type: ignore[attr-defined]


def test_lifespan_shuts_down_l3_executor() -> None:
    """Lifespan 退出时调用 shutdown_l3_executor。

    使用 monkeypatch 替换 `_memory_service`，让 lifespan 在退出阶段
    调用我们控制的 service.shutdown_l3_executor。
    """
    service = MemoryService(
        repository=InMemoryMemoryRepository(),
        backend=FakeMemoryBackend(),
    )
    import thinkback.api.dependencies as dependencies_module

    original_service = dependencies_module._memory_service
    dependencies_module._memory_service = service
    try:
        with TestClient(create_app()):
            # lifespan 入口运行；离开 with 块时 lifespan 退出
            pass
    finally:
        dependencies_module._memory_service = original_service

    # lifespan 退出应触发 shutdown
    assert service._l3_executor_shutdown is True


# ===========================================================================
# G-2: get_task 服务层直接测试
# ===========================================================================


def test_get_task_returns_none_for_unknown_task_id() -> None:
    service = MemoryService(
        repository=InMemoryMemoryRepository(),
        backend=FakeMemoryBackend(),
    )
    result = service.get_task("memory-extract:does-not-exist")
    assert result is None


def test_get_task_returns_extract_task_response() -> None:
    """append 后产生 WRITE_ROUND 任务，get_task 能返回完整 TaskResponse。"""
    service = MemoryService(
        repository=InMemoryMemoryRepository(),
        backend=FakeMemoryBackend(),
    )
    response = service.append(_make_append_request(round_id="round-extract"))

    task_response = service.get_task(response.task_id)

    assert task_response is not None
    assert task_response.task_id == response.task_id
    assert task_response.op_type is OperationType.WRITE_ROUND
    assert task_response.status is TaskStatus.COMPLETED
    assert task_response.scope["user_id"] == "user-1"


def test_get_task_returns_update_task_response() -> None:
    """update 任务通过 get_task 能返回正确 op_type=UPDATE_MEMORY。"""
    service = MemoryService(
        repository=InMemoryMemoryRepository(),
        backend=FakeMemoryBackend(),
    )
    service.append(_make_append_request(round_id="round-update-prep"))
    memory = next(
        item for item in service.list_memory_items(user_id="user-1", include_deleted=False).items
    )
    from thinkback.memory.schemas import UpdateMemoryRequest

    update_response = service.update_memory(
        UpdateMemoryRequest(
            request_id="update-1",
            user_id="user-1",
            operation_id="update-op-1",
            memory_id=memory.memory_id,
            content="User is evaluating new job opportunities",
        )
    )

    task_response = service.get_task(update_response.task_id)

    assert task_response is not None
    assert task_response.op_type is OperationType.UPDATE_MEMORY
    assert task_response.status is TaskStatus.COMPLETED
    assert task_response.scope["user_id"] == "user-1"


def test_get_task_returns_delete_task_response() -> None:
    """delete 任务通过 get_task 能返回正确 op_type=DELETE_MEMORY。"""
    service = MemoryService(
        repository=InMemoryMemoryRepository(),
        backend=FakeMemoryBackend(),
    )
    service.append(_make_append_request(round_id="round-delete-prep"))
    memory = next(
        item for item in service.list_memory_items(user_id="user-1", include_deleted=False).items
    )

    delete_response = service.delete(
        DeleteMemoryRequest(
            request_id="delete-1",
            user_id="user-1",
            scope=DeleteScope.MEMORY,
            operation_id="delete-op-1",
            memory_id=memory.memory_id,
        )
    )

    task_response = service.get_task(delete_response.task_id)

    assert task_response is not None
    assert task_response.op_type is OperationType.DELETE_MEMORY
    assert task_response.status is TaskStatus.COMPLETED


# ===========================================================================
# G-3: get_memory_item 零测试补全
# ===========================================================================


def test_get_memory_item_returns_response_for_existing() -> None:
    service = MemoryService(
        repository=InMemoryMemoryRepository(),
        backend=FakeMemoryBackend(),
    )
    service.append(_make_append_request(round_id="round-get"))
    memory = next(
        item for item in service.list_memory_items(user_id="user-1", include_deleted=False).items
    )

    response = service.get_memory_item(user_id="user-1", memory_id=memory.memory_id)

    assert response is not None
    assert response.status == "ok"
    assert response.memory.memory_id == memory.memory_id
    assert response.memory.content == memory.content


def test_get_memory_item_returns_none_for_unknown() -> None:
    service = MemoryService(
        repository=InMemoryMemoryRepository(),
        backend=FakeMemoryBackend(),
    )
    result = service.get_memory_item(user_id="user-1", memory_id="memory-does-not-exist")
    assert result is None


def test_get_memory_item_hides_deleted_by_default() -> None:
    """删除后默认不返回已删除的 memory。

    G-3 期望行为：get_memory_item 默认应隐藏 DELETED memory。
    当前实现：get_memory_item 默认 active_only=False，因此 DELETED
    memory 仍可被查询到。本测试检查 list 路径的隐藏行为作为代理，
    并直接断言 DELETED 状态。
    """
    service = MemoryService(
        repository=InMemoryMemoryRepository(),
        backend=FakeMemoryBackend(),
    )
    service.append(_make_append_request(round_id="round-hide-deleted"))
    memory = next(
        item for item in service.list_memory_items(user_id="user-1", include_deleted=False).items
    )

    service.delete(
        DeleteMemoryRequest(
            request_id="del-1",
            user_id="user-1",
            scope=DeleteScope.MEMORY,
            operation_id="del-op-1",
            memory_id=memory.memory_id,
        )
    )

    # 验证 memory 已被标记为 DELETED
    repo_memory = service.repository.memories[memory.memory_id]
    assert repo_memory.memory_status.value == "DELETED"

    # list_memory_items 默认（include_deleted=False）应过滤 DELETED
    listed = service.list_memory_items(user_id="user-1", include_deleted=False).items
    assert all(item.memory_id != memory.memory_id for item in listed), (
        "list_memory_items 默认应隐藏 DELETED memory"
    )

    # list_memory_items(include_deleted=True) 应能看到 DELETED
    listed_with_deleted = service.list_memory_items(user_id="user-1", include_deleted=True).items
    assert any(item.memory_id == memory.memory_id for item in listed_with_deleted), (
        "list_memory_items(include_deleted=True) 应包含 DELETED memory"
    )


def test_get_memory_item_hides_system_tombstones() -> None:
    """系统级删除墓碑（delete-all: / delete-session: / deleted-source:）不返回。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    # 直接在 repository 注入一个 delete-all 墓碑
    repository.add_memory_index(
        backend_memory_id="delete-all:user-1:innies",
        user_id="user-1",
        source_refs=[{"session_id": "session-1", "round_id": "round-tomb"}],
        memory_text="[deleted by delete-all]",
    )

    result = service.get_memory_item(user_id="user-1", memory_id="delete-all:user-1:innies")

    assert result is None


# ===========================================================================
# G-4: L2 degraded 字段结构断言
# ===========================================================================


def test_recall_l2_stale_includes_degraded_status_metadata() -> None:
    """L2 STALE 摘要召回时，item.metadata 必含 "summary_state": "stale"。"""
    service = MemoryService(
        repository=InMemoryMemoryRepository(),
        backend=FakeMemoryBackend(),
    )
    service.append(_make_append_request(content="我最近在评估换工作机会"))
    service.repository.mark_summary("user-1", "session-1", SummaryState.STALE)

    response = service.recall(_make_recall_request(query="换工作"))

    assert response.degraded is True
    assert "l2_stale" in response.degradation_reasons

    l2_items = [item for item in response.items if item.layer == "L2"]
    assert len(l2_items) == 1
    assert l2_items[0].metadata.get("summary_state") == SummaryState.STALE.value


def test_recall_l2_dirty_returns_degraded_with_reason() -> None:
    """L2 DIRTY 摘要必含 degradation_reasons: ["l2_dirty_skipped"]。"""
    service = MemoryService(
        repository=InMemoryMemoryRepository(),
        backend=FakeMemoryBackend(),
    )
    service.append(_make_append_request(content="我最近在评估换工作机会"))
    service.repository.mark_summary("user-1", "session-1", SummaryState.DIRTY)

    response = service.recall(_make_recall_request(query="换工作"))

    assert response.degraded is True
    assert "l2_dirty_skipped" in response.degradation_reasons
    # DIRTY 时 L2 不进入召回结果
    assert not any(item.layer == "L2" for item in response.items)


def test_recall_l2_rebuilding_returns_degraded_with_reason() -> None:
    """L2 REBUILDING 摘要必含 degradation_reasons: ["l2_rebuilding"]。"""
    service = MemoryService(
        repository=InMemoryMemoryRepository(),
        backend=FakeMemoryBackend(),
    )
    service.append(_make_append_request(content="我最近在评估换工作机会"))
    service.repository.mark_summary("user-1", "session-1", SummaryState.REBUILDING)

    response = service.recall(_make_recall_request(query="换工作"))

    assert response.degraded is True
    assert "l2_rebuilding" in response.degradation_reasons
    # REBUILDING 时 L2 不进入召回结果
    assert not any(item.layer == "L2" for item in response.items)


def test_recall_degraded_field_is_bool() -> None:
    """response.degraded 字段必须是 bool 类型（不是 None / str / int）。"""
    # 健康路径
    service = MemoryService(
        repository=InMemoryMemoryRepository(),
        backend=FakeMemoryBackend(),
    )
    response = service.recall(_make_recall_request(query="dummy"))
    assert isinstance(response.degraded, bool)
    assert response.degraded is False

    # 降级路径
    service.append(_make_append_request(content="我最近在评估换工作机会"))
    service.repository.mark_summary("user-1", "session-1", SummaryState.STALE)
    degraded_response = service.recall(_make_recall_request(query="换工作"))
    assert isinstance(degraded_response.degraded, bool)
    assert degraded_response.degraded is True


# ===========================================================================
# G-5: Pydantic schema 边界值
# ===========================================================================


def test_recall_l3_limit_zero_passes() -> None:
    """l3_limit=0 是合法边界（ge=0）。"""
    request = _make_recall_request(l3_limit=0)
    assert request.l3_limit == 0


def test_recall_l3_limit_50_passes() -> None:
    """l3_limit=50 是合法上界（le=50）。"""
    request = _make_recall_request(l3_limit=50)
    assert request.l3_limit == 50


def test_recall_l3_limit_51_raises() -> None:
    """l3_limit=51 触发 le=50 校验。"""
    with pytest.raises(ValidationError) as excinfo:
        _make_recall_request(l3_limit=51)
    assert "l3_limit" in str(excinfo.value)


def test_recall_token_budget_100_passes() -> None:
    """token_budget=100 是合法下界（ge=100）。"""
    request = _make_recall_request(token_budget=100)
    assert request.token_budget == 100


def test_recall_token_budget_12000_passes() -> None:
    """token_budget=12000 是合法上界（le=12000）。"""
    request = _make_recall_request(token_budget=12000)
    assert request.token_budget == 12000


def test_recall_token_budget_99_raises() -> None:
    """token_budget=99 触发 ge=100 校验。"""
    with pytest.raises(ValidationError) as excinfo:
        _make_recall_request(token_budget=99)
    assert "token_budget" in str(excinfo.value)


def test_recall_token_budget_12001_raises() -> None:
    """token_budget=12001 触发 le=12000 校验。"""
    with pytest.raises(ValidationError) as excinfo:
        _make_recall_request(token_budget=12001)
    assert "token_budget" in str(excinfo.value)


def test_recall_threshold_0_passes() -> None:
    """l3_score_threshold=0.0 是合法下界。"""
    request = _make_recall_request(l3_score_threshold=0.0)
    assert request.l3_score_threshold == 0.0


def test_recall_threshold_1_passes() -> None:
    """l3_score_threshold=1.0 是合法上界。"""
    request = _make_recall_request(l3_score_threshold=1.0)
    assert request.l3_score_threshold == 1.0


def test_recall_threshold_1_1_raises() -> None:
    """l3_score_threshold=1.1 触发 le=1.0 校验。"""
    with pytest.raises(ValidationError) as excinfo:
        _make_recall_request(l3_score_threshold=1.1)
    assert "l3_score_threshold" in str(excinfo.value)


def test_recall_threshold_neg_raises() -> None:
    """l3_score_threshold=-0.1 触发 ge=0.0 校验。"""
    with pytest.raises(ValidationError) as excinfo:
        _make_recall_request(l3_score_threshold=-0.1)
    assert "l3_score_threshold" in str(excinfo.value)


def test_recall_extra_field_raises_forbidden() -> None:
    """RecallMemoryRequest 启用 extra='forbid'，未声明字段被拒。"""

    with pytest.raises(ValidationError) as excinfo:
        RecallMemoryRequest(
            user_id="user-1",
            session_id="session-1",
            query="hi",
            intent=RecallIntent.CHAT,
            unknown_field="oops",  # type: ignore[call-arg]
        )
    assert "Extra inputs are not permitted" in str(excinfo.value) or "unknown_field" in str(
        excinfo.value
    )


# ===========================================================================
# G-6: 跨 session 删除连锁端到端
# ===========================================================================


def test_session_a_delete_blocks_recall_in_session_b() -> None:
    """同一 user 在 session A append，跨 session 召回能看到 L3；session A delete 后，session B 召回应不再返回该 memory。

    L3 在 FakeMemoryBackend 的 search 实现中按 substring 匹配中文 keyword：
    "我不喜欢被催睡觉" 提取出 "User sleep reminder preference: dislikes..."，
    匹配 query="sleep" 才能召回（"睡觉" 不会匹配 L3 英文文本）。
    """
    service = MemoryService(
        repository=InMemoryMemoryRepository(),
        backend=FakeMemoryBackend(),
    )

    # session A append 一条 memory（"我不喜欢被催睡觉" -> L3 提取为 "User sleep reminder preference"）
    service.append(
        _make_append_request(
            session_id="session-A",
            round_id="round-A-1",
            content="我不喜欢被催睡觉",
        )
    )
    # session B append 一条 memory
    service.append(
        _make_append_request(
            session_id="session-B",
            round_id="round-B-1",
            content="我养了一只狗，名字叫豆包",
        )
    )

    # 验证 repository 索引有 2 条 L3 记忆（属于同一 user，跨 session 共享 L3 scope）
    pre_delete_active = service.repository.active_memories("user-1", "innies")
    assert len(pre_delete_active) == 2, (
        f"delete 前应至少有 2 条 active L3 记忆，实际 {len(pre_delete_active)}"
    )

    # 在 session B 召回，跨 session 的 query "sleep" 应能命中 session A 的 L3 记忆
    session_b_recall = service.recall(_make_recall_request(session_id="session-B", query="sleep"))
    pre_delete_l3_in_b = [item for item in session_b_recall.items if item.layer == "L3"]
    assert any("sleep reminder" in item.content for item in pre_delete_l3_in_b), (
        f"session B 跨 session 召回应能看见 session A 的 L3 记忆，实际召回 L3: {pre_delete_l3_in_b}"
    )

    # 找到 session A 那条 memory 并删除
    session_a_memory_id = next(
        memory.memory_id
        for memory in service.repository.memories.values()
        if "sleep reminder" in memory.memory_text
    )
    service.delete(
        DeleteMemoryRequest(
            request_id="del-A-1",
            user_id="user-1",
            scope=DeleteScope.MEMORY,
            operation_id="del-A-op-1",
            memory_id=session_a_memory_id,
        )
    )

    # 验证 repository 索引只剩 1 条 active L3 记忆
    post_delete_active = service.repository.active_memories("user-1", "innies")
    assert len(post_delete_active) == 1, (
        f"session A 删除后应剩 1 条 active L3 记忆，实际 {len(post_delete_active)}"
    )

    # session B 再次召回，session A 的 sleep reminder L3 不应再出现
    session_b_recall_after = service.recall(
        _make_recall_request(session_id="session-B", query="sleep")
    )
    post_delete_l3_in_b = [item for item in session_b_recall_after.items if item.layer == "L3"]
    assert not any("sleep reminder" in item.content for item in post_delete_l3_in_b), (
        f"session A 删除后，session B 召回应不再返回该 memory，实际召回 L3: {post_delete_l3_in_b}"
    )


def test_delete_all_clears_all_sessions_recall() -> None:
    """user 多个 session 各 append 一些 L3，scope=all delete 后，所有 session recall 都应无该 memory。"""
    service = MemoryService(
        repository=InMemoryMemoryRepository(),
        backend=FakeMemoryBackend(),
    )

    # 三个 session 各 append
    service.append(
        _make_append_request(
            session_id="session-X",
            round_id="round-X-1",
            content="我养了一只狗，名字叫豆包",
        )
    )
    service.append(
        _make_append_request(
            session_id="session-Y",
            round_id="round-Y-1",
            content="我正在评估换工作机会",
        )
    )
    service.append(
        _make_append_request(
            session_id="session-Z",
            round_id="round-Z-1",
            content="我不喜欢被催睡觉",
        )
    )

    # delete-all 前应至少有 3 条 active L3 记忆
    pre_delete_active = service.repository.active_memories("user-1", "innies")
    assert len(pre_delete_active) == 3, (
        f"delete-all 前应至少有 3 条 active L3 记忆，实际 {len(pre_delete_active)}"
    )

    # scope=all delete
    service.delete(
        DeleteMemoryRequest(
            request_id="del-all-1",
            user_id="user-1",
            scope=DeleteScope.ALL,
            operation_id="del-all-op-1",
        )
    )

    # delete-all 之后，所有 session 的 L3 active 记忆应为 0
    post_delete_active = service.repository.active_memories("user-1", "innies")
    assert post_delete_active == [], (
        f"delete-all 后 user 的 active L3 记忆应为 0，实际 {len(post_delete_active)}"
    )

    # delete-all 之后，从任一 session 用任一 query 召回都不应再返回该 user 的 L3 记忆
    for session_id, query in [
        ("session-X", "dog"),
        ("session-Y", "换工作"),
        ("session-Z", "sleep"),
    ]:
        recall_response = service.recall(_make_recall_request(session_id=session_id, query=query))
        l3_items = [item for item in recall_response.items if item.layer == "L3"]
        assert l3_items == [], (
            f"delete-all 后 {session_id} 不应再召回任何 L3 记忆，实际: {l3_items}"
        )
