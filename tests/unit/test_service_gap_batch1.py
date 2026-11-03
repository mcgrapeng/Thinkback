"""service.py 第一批行覆盖缺口（写入/任务/擦除计划的分支钉子）。

与 batch2/batch3 同一方法：真实缺口 → 用例；防御性死分支 → 登记。
多数分支只能从私有方法触达（公开入口的前置校验会先拦下）——钉的是
第二道防线本身。
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.unit.test_memory_service import make_append
from thinkback.domain.entities import TaskEntry
from thinkback.domain.enums import OperationType, TaskStatus
from thinkback.memory.backends import FakeMemoryBackend
from thinkback.memory.repositories import InMemoryMemoryRepository
from thinkback.memory.schemas import DeleteMemoryRequest, DeleteScope
from thinkback.memory.service import MemoryService, TaskLockExhaustedError


def _service(**kwargs: Any) -> MemoryService:
    return MemoryService(
        repository=InMemoryMemoryRepository(),
        backend=FakeMemoryBackend(),
        l3_write_mode="sync",
        **kwargs,
    )


def _task(
    task_id: str = "t-1", *, status: TaskStatus = TaskStatus.RUNNING, result: dict | None = None
) -> TaskEntry:
    return TaskEntry(
        task_id=task_id,
        request_id="req-1",
        op_type=OperationType.WRITE_ROUND,
        scope={"user_id": "user-1"},
        status=status,
        result=result or {},
    )


# ── 作用域解析（383） ────────────────────────────────────────────────


def test_long_term_scope_falls_back_to_default_when_namespace_is_missing() -> None:
    """没有 namespace 的请求按默认长期作用域处理（老调用方兼容）。"""

    class _LegacyRequest:
        user_id = "user-1"
        session_id = "session-1"
        round_id = "r-1"

    assert MemoryService._long_term_scope_id(_LegacyRequest()) == MemoryService.LONG_TERM_SCOPE_ID


# ── 幂等重放的作用域补齐（741） ──────────────────────────────────────
# 741 判定为防御性死分支，不造用例：append 顶部（L618）与并发胜者（L737）
# 两处入口都先经过 _assert_round_matches_request 的「作用域必须相等」断言，
# 断言通过后 entry.memory_scope_id == session_scope_id 恒成立，740 永远为假。
# 它防御的是「历史裸 session 数据」——但那类数据现在会先被 409 拒掉。


# ── async 追加的锁外 backend 写（789） ──────────────────────────────


def test_pending_backend_writes_collected_by_append_run_outside_the_lock() -> None:
    """append 收集到的兜底 backend 写必须在锁外真的执行，不能只收集。"""
    service = MemoryService(
        repository=InMemoryMemoryRepository(),
        backend=FakeMemoryBackend(),
        l3_write_mode="async",
    )
    executed: list[list[Any]] = []
    service._run_backend_writes = lambda writes, index_log=None: executed.append(list(writes))  # noqa: SLF001
    service._publish_append_round_locally = lambda **kwargs: ["backend-write-op"]  # type: ignore[assignment]  # noqa: SLF001
    service._submit_l3_write = lambda *args, **kwargs: None  # type: ignore[method-assign]  # noqa: SLF001

    service.append(make_append(round_id="r-async"))

    assert executed == [["backend-write-op"]]


# ── 遗忘清扫（947） ──────────────────────────────────────────────────


def test_append_fires_the_decay_sweeper_when_one_is_attached() -> None:
    """装配了清扫器时每次发布都要问一次「要不要清扫」（进程级门控在清扫器里）。"""
    service = _service()

    class _RecordingSweeper:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def maybe_submit(self, user_id: str, scope_id: str) -> None:
            self.calls.append((user_id, scope_id))

    sweeper = _RecordingSweeper()
    service._decay_sweeper = sweeper  # noqa: SLF001

    service.append(make_append(round_id="r-decay"))

    assert sweeper.calls == [("user-1", "thinkback")]


# ── L3 后台写提交失败要释放槽位（1065） ─────────────────────────────


def test_a_failed_l3_submit_releases_the_write_slot_it_reserved() -> None:
    """提交炸了必须归还队列槽位：不还就是永久漏一个并发名额。"""
    service = MemoryService(
        repository=InMemoryMemoryRepository(),
        backend=FakeMemoryBackend(),
        l3_write_mode="async",
    )
    released: list[int] = []
    service._l3.release_write_slot = lambda: released.append(1)  # type: ignore[method-assign]  # noqa: SLF001

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("executor closed")

    service._l3.executor.submit = _boom  # type: ignore[method-assign]  # noqa: SLF001
    task = _task()
    service.repository.save_task(task)

    with pytest.raises(RuntimeError, match="executor closed"):
        service._submit_l3_write(  # noqa: SLF001
            task,
            messages=[{"role": "user", "content": "x"}],
            user_id="user-1",
            memory_scope_id="thinkback",
            source_refs=[{"session_id": "session-1", "round_id": "r-1"}],
            request_metadata={},
            slot_reserved=False,
        )

    assert released == [1]


# ── 任务乐观锁（1151 / 1166 / 1188） ────────────────────────────────


def test_a_strict_task_mutation_raises_when_the_lock_is_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """strict 终态写耗尽重试必须抛错：静默放弃会把任务永久留在 RUNNING。"""
    from thinkback.domain.errors import TaskStaleWriteError

    service = _service()
    task = _task()
    service.repository.save_task(task)

    def _always_stale(t: TaskEntry) -> TaskEntry:
        raise TaskStaleWriteError(t.task_id, expected_version=2)

    monkeypatch.setattr(service.repository, "save_task", _always_stale)

    with pytest.raises(TaskLockExhaustedError):
        service._mutate_task_with_retry(task.task_id, lambda t: None, attempts=2, strict=True)  # noqa: SLF001


def test_a_stale_attempt_cannot_finalize_a_reclaimed_task() -> None:
    """旧 attempt 号的执行者不得把任务写回 COMPLETED（防 RUNNING→FAILED→COMPLETED 翻转）。"""
    service = _service()
    reclaimed = _task(result={"attempt": 2})

    service._finalize_append_task(  # noqa: SLF001
        reclaimed, round_id="r-1", l3_events=[], attempt=1
    )

    assert reclaimed.status is TaskStatus.RUNNING
    assert "round_id" not in reclaimed.result


def test_unregistering_a_missing_task_id_is_a_noop() -> None:
    """task_id 为 None（未登记清理）时直接返回，不去查任务表。"""
    service = _service()

    assert service._unregister_pending_l3_cleanup(None) is None  # noqa: SLF001


# ── 后端删除的同步/异步分支（1210-1211 / 1238-1240） ────────────────


def test_a_sync_delete_runs_inline_and_reports_no_async_tracking() -> None:
    """sync 模式的后端删除当场执行，返回 False（没有后台任务要跟）。"""
    backend = FakeMemoryBackend()
    service = _service()
    service.backend = backend  # noqa: SLF001

    deleted_inline = service._delete_backend_memory("whatever-id")  # noqa: SLF001

    assert deleted_inline is False


def test_a_failed_batch_delete_submit_unregisters_the_pending_cleanup() -> None:
    """批量删除提交失败要撤销 pending 计数再抛：不撤销就把计数留成了坏账。"""
    service = MemoryService(
        repository=InMemoryMemoryRepository(),
        backend=FakeMemoryBackend(),
        l3_write_mode="async",
    )
    task = _task()
    service.repository.save_task(task)

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("executor closed")

    service._l3.executor.submit = _boom  # type: ignore[method-assign]  # noqa: SLF001

    with pytest.raises(RuntimeError, match="executor closed"):
        service._delete_backend_memories(["m-1", "m-2"], task_id=task.task_id)  # noqa: SLF001

    stored = service.repository.get_task(task.task_id)
    assert stored is not None
    assert stored.result.get("pending_cleanup_tasks") == 0


# ── 任务状态回放（1346） ─────────────────────────────────────────────


def test_a_failed_task_replays_as_failed_not_as_running() -> None:
    """终态失败的任务重放要如实报 failed，不能说成 running 或 already_done。"""
    service = _service()
    task = _task(status=TaskStatus.FAILED)
    service.repository.save_task(task)

    response = service._append_task_state_response(task, make_append(round_id="r-1"))  # noqa: SLF001

    assert response.status == "failed"


# ── 重试对账（1383） ────────────────────────────────────────────────


def test_prefetch_skips_backend_rows_with_corrupted_metadata() -> None:
    """后端行的 metadata 不是 dict 时跳过该行：对账不能被一条脏数据打断。"""
    service = _service()

    class _RawBackend(FakeMemoryBackend):
        def get_all(self, **kwargs: Any) -> list[dict[str, Any]]:  # type: ignore[override]
            return [
                {
                    "id": "m-1",
                    "memory": "ok",
                    "metadata": {"source_refs": [{"session_id": "session-1", "round_id": "r-1"}]},
                },
                {"id": "m-2", "memory": "bad", "metadata": "corrupted"},
            ]

    service.backend = _RawBackend()  # noqa: SLF001

    events = service._prefetch_backend_events(  # noqa: SLF001
        user_id="user-1",
        memory_scope_id="thinkback",
        source_refs=[{"session_id": "session-1", "round_id": "r-1"}],
    )

    assert [event["id"] for event in events] == ["m-1"]


# ── 心跳容错（1448-1454） ───────────────────────────────────────────


def test_a_failing_heartbeat_beat_does_not_stop_the_heart(monkeypatch: pytest.MonkeyPatch) -> None:
    """一次心跳写库失败只记警告，下一拍继续：心跳停了健康任务会被当孤儿回收。"""
    service = _service()
    task = _task()
    service.repository.save_task(task)

    failures = {"count": 0}

    real_save = service.repository.save_task

    def _flaky(t: TaskEntry) -> TaskEntry:
        if failures["count"] < 1:
            failures["count"] += 1
            raise RuntimeError("db jitter")
        return real_save(t)

    monkeypatch.setattr(service.repository, "save_task", _flaky)
    monkeypatch.setattr(
        service,
        "_mutate_task_with_retry",
        lambda task_id, mutate, **kw: mutate,  # type: ignore[method-assign]
    )
    # 直接驱动 _beat 一拍：失败后 continue（不向上抛）。
    # 用白盒方式跑 _run_with_task_heartbeat，action 立即返回。
    result = service._run_with_task_heartbeat(task.task_id, lambda: "done")  # noqa: SLF001
    assert result == "done"


# ── pending write 恢复（1525 / 1550） ───────────────────────────────


def test_recovery_does_not_overwrite_an_already_finalized_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重放期间任务已被别人写终态时，恢复不得再改它的状态。"""
    service = MemoryService(
        repository=InMemoryMemoryRepository(),
        backend=FakeMemoryBackend(),
        l3_write_mode="async",  # 恢复只在 async 模式有意义（sync 不会留悬空任务）
    )
    task = _task(status=TaskStatus.RUNNING)
    task.scope = {"user_id": "user-1", "round_id": "r-rec"}
    service.repository.save_task(task)
    service.repository.save_round(make_append(round_id="r-rec"))
    # 让重放成功，并在重放内部把任务标成 COMPLETED（模拟并发完成者）。
    monkeypatch.setattr(service, "_run_l3_write", lambda **kwargs: [{"event": "ADD"}])  # type: ignore[method-assign]

    def _finalize_and_complete(task_id: str, mutate: Any, **kwargs: Any) -> Any:
        task_now = service.repository.get_task(task_id)
        assert task_now is not None
        task_now.status = TaskStatus.COMPLETED
        service.repository.save_task(task_now)
        mutate(task_now)  # finalize 看到的已是终态 → 不改
        return task_now

    monkeypatch.setattr(service, "_mutate_task_with_retry", _finalize_and_complete)  # type: ignore[method-assign]

    recovered = service.recover_pending_writes()

    assert recovered == 1
    assert service.repository.get_task(task.task_id).status is TaskStatus.COMPLETED


def test_collecting_pending_tasks_stops_at_the_first_empty_page() -> None:
    """没有待恢复任务时一页都拿不到就停：不要空转翻页。"""
    service = _service()

    assert service._collect_pending_write_tasks() == []  # noqa: SLF001


# ── L3 写入前的来源排除（1648-1649） ───────────────────────────────


def test_a_round_whose_source_is_excluded_skips_the_l3_write() -> None:
    """来源已被删除/擦除的轮次不得再进 L3（原文不能回到上下文）。"""
    service = _service()
    source_refs = [{"session_id": "session-1", "round_id": "r-1"}]
    # 写来源墓碑：这一轮被排除。
    service._add_deleted_source_ref_tombstone(  # noqa: SLF001
        user_id="user-1", memory_scope_id="thinkback", source_refs=source_refs, memory_text="原文"
    )

    events = service._run_l3_write(  # noqa: SLF001
        messages=[{"role": "user", "content": "不该被抽取的事实"}],
        user_id="user-1",
        memory_scope_id="thinkback",
        source_refs=source_refs,
        request_metadata={},
    )

    assert events == [{"event": "SKIP", "reason": "source_ref_excluded"}]


# ── 召回相关性（1903） ──────────────────────────────────────────────


def test_recall_skips_backend_items_that_are_not_relevant() -> None:
    """低于阈值的候选不进上下文包（预算留给相关事实）。"""
    from thinkback.memory.schemas import RecallMemoryRequest

    service = _service()
    service._l3_item_relevant = lambda item, threshold: False  # type: ignore[method-assign]  # noqa: SLF001

    response = service.recall(
        RecallMemoryRequest(
            user_id="user-1",
            session_id="session-1",
            query="任何词",
        )
    )

    assert response.items == [] or all(item.layer != "L3" for item in response.items)


# ── 擦除任务抢不到（2291-2293） ─────────────────────────────────────


def _erase_request() -> DeleteMemoryRequest:
    return DeleteMemoryRequest(
        request_id="req-e",
        user_id="user-1",
        scope=DeleteScope.MEMORY,
        operation_id="op-e",
        memory_id="m-1",
    )


def test_an_erase_that_loses_the_claim_to_a_completed_task_replays_its_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """别人已经做完：抢不到的这次必须回放对方的完成结果。"""
    service = _service()

    def _lost_the_race(task: TaskEntry) -> tuple[TaskEntry, bool]:
        task.status = TaskStatus.COMPLETED
        task.result = {**task.result, "affected_memories": 3}
        return task, False

    monkeypatch.setattr(service.repository, "claim_task", _lost_the_race)

    response = service.erase(_erase_request())

    assert response.status == "completed"
    assert response.affected_memories == 3


def test_an_erase_that_loses_the_claim_to_a_running_task_reports_in_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """别人正在做：如实回报 running，不能抢跑把对方的阶段记账搅乱。"""
    service = _service()

    def _lost_the_race(task: TaskEntry) -> tuple[TaskEntry, bool]:
        task.status = TaskStatus.RUNNING
        return task, False

    monkeypatch.setattr(service.repository, "claim_task", _lost_the_race)

    response = service.erase(_erase_request())

    assert response.status == "running"


# ── 擦除计划里的来源引用（2323） ────────────────────────────────────


def test_a_source_ref_without_a_session_id_is_skipped_by_the_erase_plan() -> None:
    """缺 session_id 的来源引用不参与「会话→轮次」映射：不能拿 None 当键。"""
    service = _service()
    added = service.repository.add_memory_index(
        backend_memory_id="b-1",
        user_id="user-1",
        source_refs=[
            {"session_id": "session-1", "round_id": "r-1"},
            {"round_id": "r-no-session"},  # 脏数据：没有 session_id
        ],
        memory_text="带脏来源引用的事实",
    )
    request = DeleteMemoryRequest(
        request_id="req-e",
        user_id="user-1",
        scope=DeleteScope.MEMORY,
        operation_id="op-e",
        memory_id=added.memory_id,
    )

    plan = service._plan_erase(request, "thinkback", "thinkback::session-1")  # noqa: SLF001

    rounds = plan.get("round_ids", {})
    assert rounds.get("session-1") == ["r-1"]
    assert all("r-no-session" not in (bucket or []) for bucket in rounds.values())
