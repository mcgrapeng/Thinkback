"""P1a：任务表乐观锁（row_version CAS）的单元与双副本模拟测试。

覆盖：
- 仓储语义：stale 快照写入抛 TaskStaleWriteError；成功写 +1；claim 原子。
- 服务重试助手：冲突时重读重放收敛；重试耗尽放弃不抛。
- 双副本模拟（两个 MemoryService 共享一个快照语义仓库，各自独立
  进程锁）：B-1 场景（A 删除中途失败 + A 后台清理迟到 + B 重试）
  收敛 COMPLETED 且 pending=0；并发计数增减不丢失更新。
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event, Thread
from typing import Any

import pytest

from thinkback.domain.entities import TaskEntry
from thinkback.domain.enums import OperationType, TaskStatus
from thinkback.domain.errors import TaskStaleWriteError
from thinkback.memory.backends import FakeMemoryBackend
from thinkback.memory.repositories import InMemoryMemoryRepository
from thinkback.memory.schemas import DeleteMemoryRequest, DeleteScope
from thinkback.memory.service import MemoryService


def make_task(task_id: str = "t1", *, status: TaskStatus = TaskStatus.RUNNING) -> TaskEntry:
    return TaskEntry(
        task_id=task_id,
        request_id="req-1",
        op_type=OperationType.DELETE_MEMORY,
        scope={},
        status=status,
        operation_id="op-1",
        result={},
    )


# ── 仓储语义 ─────────────────────────────────────────────────────────


def test_save_task_bumps_row_version_each_write() -> None:
    repository = InMemoryMemoryRepository()
    task = repository.save_task(make_task())
    assert task.row_version == 1
    task.status = TaskStatus.COMPLETED
    task = repository.save_task(task)
    assert task.row_version == 2
    assert repository.get_task("t1").row_version == 2


def test_save_task_stale_snapshot_raises() -> None:
    repository = InMemoryMemoryRepository()
    repository.save_task(make_task())  # v1

    stale = make_task()  # 独立构造，row_version=0（未见过 v1）
    stale.task_id = "t1"
    with pytest.raises(TaskStaleWriteError) as exc_info:
        repository.save_task(stale)
    assert exc_info.value.task_id == "t1"

    # 库中仍是最新写者的状态
    assert repository.get_task("t1").status is TaskStatus.RUNNING


def test_claim_task_returns_existing_with_version() -> None:
    repository = InMemoryMemoryRepository()
    saved = repository.save_task(make_task())
    again, claimed = repository.claim_task(make_task())
    assert claimed is False
    assert again.row_version == saved.row_version == 1


# ── 服务重试助手 ─────────────────────────────────────────────────────


def test_mutate_task_with_retry_replays_on_contention() -> None:
    """首次 save 注入一次 CAS 冲突 → 助手重读快照重放，结果恰为一次增量。"""

    class OnceContendedRepository(InMemoryMemoryRepository):
        def __init__(self) -> None:
            super().__init__()
            self.contend_once = False

        def save_task(self, task: TaskEntry) -> TaskEntry:
            if self.contend_once:
                self.contend_once = False
                raise TaskStaleWriteError(task.task_id, task.row_version)
            return super().save_task(task)

    repository = OnceContendedRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    task = make_task("t2", status=TaskStatus.RUNNING)
    task.result = {"pending_cleanup_tasks": 5}
    repository.save_task(task)
    repository.contend_once = True  # setup 之后武装：下一次 save（助手的首次尝试）冲突

    def increment(entry: TaskEntry) -> None:
        entry.result = {
            **entry.result,
            "pending_cleanup_tasks": int(entry.result.get("pending_cleanup_tasks", 0)) + 1,
        }

    result = service._mutate_task_with_retry("t2", increment)
    assert result is not None
    # 重放语义：从最新快照重读后应用一次变更 → 6（不是冲突前快照上的叠加）
    assert result.result["pending_cleanup_tasks"] == 6


def test_mutate_task_with_retry_gives_up_after_attempts() -> None:
    class AlwaysContendedRepository(InMemoryMemoryRepository):
        def save_task(self, task: TaskEntry) -> TaskEntry:
            raise TaskStaleWriteError(task.task_id, task.row_version)

    contended = AlwaysContendedRepository()
    contended.tasks["t3"] = make_task("t3")
    service = MemoryService(repository=contended, backend=FakeMemoryBackend())

    def increment(task: TaskEntry) -> None:
        task.result = {**task.result, "n": int(task.result.get("n", 0)) + 1}

    assert service._mutate_task_with_retry("t3", increment, attempts=2) is None  # 不抛


def test_mutate_task_missing_task_returns_none() -> None:
    service = MemoryService(repository=InMemoryMemoryRepository(), backend=FakeMemoryBackend())
    assert service._mutate_task_with_retry("no-such", lambda task: None) is None


# ── 双副本模拟（两个 service 共享一个快照仓库 = 两套独立进程锁）──────


class _GatedDeleteBackend(FakeMemoryBackend):
    """delete 阻塞在门控上，让清理 future 完成时序可控。"""

    def __init__(self) -> None:
        super().__init__()
        self.gate = Event()

    def delete(self, memory_id: str) -> None:
        self.gate.wait(timeout=5)
        super().delete(memory_id)


class _FlakyMarkDeletedRepository(InMemoryMemoryRepository):
    def __init__(self) -> None:
        super().__init__()
        self.fail_next_mark_memory_deleted = True

    def mark_memory_deleted(self, memory_id: str) -> Any:
        if self.fail_next_mark_memory_deleted:
            self.fail_next_mark_memory_deleted = False
            raise RuntimeError("simulated replica-a db failure")
        return super().mark_memory_deleted(memory_id)


def _delete_request(op: str, memory_id: str) -> DeleteMemoryRequest:
    return DeleteMemoryRequest(
        request_id=f"req-{op}",
        user_id="user-1",
        scope=DeleteScope.MEMORY,
        operation_id=op,
        memory_id=memory_id,
    )


def test_dual_replica_delete_failure_and_retry_converges() -> None:
    """B-1×多副本：副本A删除中途失败、其后台清理迟到完成、副本B用同
    operation_id 重试——乐观锁 + 重试重放下任务必须收敛 COMPLETED/pending=0
    （修复前：别名语义掩盖竞争，跨副本盲写会丢计数卡 RUNNING）。"""
    repository = _FlakyMarkDeletedRepository()
    backend = _GatedDeleteBackend()
    memory = repository.add_memory_index(
        backend_memory_id="backend-multi",
        user_id="user-1",
        memory_scope_id="thinkback",
        source_refs=[{"session_id": "session-1", "round_id": "round-multi"}],
        memory_text="User has a cat named Mochi",
    )
    replica_a = MemoryService(repository=repository, backend=backend, l3_write_mode="async")
    replica_b = MemoryService(repository=repository, backend=backend, l3_write_mode="async")

    # 副本A：清理已提交（pending=1，后台阻塞中），随后主体失败 → FAILED
    with pytest.raises(RuntimeError, match="simulated replica-a db failure"):
        replica_a.delete(_delete_request("op-multi", memory.memory_id))
    failed = repository.get_task("memory-delete:op-multi")
    assert failed is not None and failed.status is TaskStatus.FAILED

    # A 的后台清理现在才完成（task 已 FAILED → 计数仍要递减归零）
    backend.gate.set()
    replica_a.drain_l3_background_tasks(timeout=5)
    after_cleanup = repository.get_task("memory-delete:op-multi")
    assert after_cleanup is not None
    assert after_cleanup.result.get("pending_cleanup_tasks") == 0

    # 副本B：同 operation_id 重试（另一套进程锁，快照可能滞后）→ 必须收敛
    response = replica_b.delete(_delete_request("op-multi", memory.memory_id))
    replica_b.drain_l3_background_tasks(timeout=5)
    task = repository.get_task(response.task_id)
    assert task is not None
    assert task.status is TaskStatus.COMPLETED
    assert task.result.get("pending_cleanup_tasks") == 0


def test_dual_replica_concurrent_register_unregister_keeps_count_consistent() -> None:
    """两副本并发对同一任务做计数增减：最终值必须等于净增量（无丢失更新）。"""
    repository = InMemoryMemoryRepository()
    repository.save_task(make_task("memory-delete:op-count", status=TaskStatus.RUNNING))
    executor = ThreadPoolExecutor(max_workers=4)
    replica_a = MemoryService(repository=repository, backend=FakeMemoryBackend())
    replica_b = MemoryService(repository=repository, backend=FakeMemoryBackend())

    futures = []
    for index in range(8):
        service = replica_a if index % 2 == 0 else replica_b
        if index < 5:
            futures.append(
                executor.submit(service._register_pending_l3_cleanup, "memory-delete:op-count")
            )
        else:
            futures.append(
                executor.submit(service._unregister_pending_l3_cleanup, "memory-delete:op-count")
            )
    for future in futures:
        future.result(timeout=10)

    task = repository.get_task("memory-delete:op-count")
    assert task is not None
    assert task.result.get("pending_cleanup_tasks") == 5 - 3  # 净增量 2，无丢失


def test_finish_l3_cleanup_concurrent_completions_converge_with_cas() -> None:
    """8 个清理完成并发抵达（跨线程，模拟多副本回调）：pending 收敛到 0。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(
        repository=repository, backend=FakeMemoryBackend(), l3_write_mode="async"
    )
    task = make_task("memory-delete:op-concurrent", status=TaskStatus.RUNNING)
    task.result = {"pending_cleanup_tasks": 8}
    repository.save_task(task)

    def finish_one() -> None:
        future: Future[None] = Future()
        future.set_result(None)
        service._finish_l3_cleanup(future, "memory-delete:op-concurrent")

    threads = [Thread(target=finish_one) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    final = repository.get_task("memory-delete:op-concurrent")
    assert final is not None
    assert final.result.get("pending_cleanup_tasks") == 0
    assert final.status is TaskStatus.COMPLETED


# ── 孤儿 running 任务启动回收 ─────────────────────────────────────────


def test_reclaim_stale_running_tasks_recycles_only_aged_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """超龄 running 被回收为 failed；新近 running / completed 不受影响。"""

    import time as time_module

    repository = InMemoryMemoryRepository()
    orphan = make_task("orphan-task", status=TaskStatus.RUNNING)
    fresh = make_task("fresh-task", status=TaskStatus.RUNNING)
    done = make_task("done-task", status=TaskStatus.COMPLETED)
    repository.claim_task(orphan)
    repository.claim_task(fresh)
    repository.claim_task(done)

    # 把 orphan-task 的最后更新时间拨回 2 小时前
    repository.task_updated_at["orphan-task"] = time_module.time() - 7200

    reclaimed = repository.reclaim_stale_running_tasks(max_age_seconds=1800.0)

    assert reclaimed == ["orphan-task"]
    orphan_after = repository.get_task("orphan-task")
    fresh_after = repository.get_task("fresh-task")
    done_after = repository.get_task("done-task")
    assert orphan_after is not None and orphan_after.status is TaskStatus.FAILED
    assert orphan_after.last_error is not None and "reclaimed" in orphan_after.last_error
    assert orphan_after.row_version == 2  # claim=1 + 回收 +1
    assert fresh_after is not None and fresh_after.status is TaskStatus.RUNNING
    assert done_after is not None and done_after.status is TaskStatus.COMPLETED


def test_reclaim_stale_running_tasks_is_idempotent() -> None:
    """二次回收不命中（status 已非 running），多副本并发执行语义安全。"""

    repository = InMemoryMemoryRepository()
    repository.claim_task(make_task("orphan-task", status=TaskStatus.RUNNING))
    repository.task_updated_at["orphan-task"] = 0.0  # 远古时间戳

    assert repository.reclaim_stale_running_tasks(max_age_seconds=60.0) == ["orphan-task"]
    assert repository.reclaim_stale_running_tasks(max_age_seconds=60.0) == []


def test_service_reclaim_orphan_running_tasks_swallows_repository_failure() -> None:
    """仓储回收抛错时 service 层吞掉并返回空列表，不阻塞启动。"""

    class ExplodingRepository(InMemoryMemoryRepository):
        def reclaim_stale_running_tasks(self, *, max_age_seconds: float) -> list[str]:
            raise RuntimeError("db unavailable")

    service = MemoryService(
        repository=ExplodingRepository(), backend=FakeMemoryBackend(), l3_write_mode="async"
    )

    assert service.reclaim_orphan_running_tasks() == []


# ── 幽灵 running 任务读路径自愈（2026-09 关机竞态残留堵死） ───────────


def test_reclaim_stale_running_task_single_task_semantics(monkeypatch: pytest.MonkeyPatch) -> None:
    """单任务惰性回收：超龄 running 命中；新近 / 非 running / 不存在 / 二次调用 no-op。"""

    import time as time_module

    repository = InMemoryMemoryRepository()
    repository.claim_task(make_task("orphan-task", status=TaskStatus.RUNNING))
    repository.claim_task(make_task("fresh-task", status=TaskStatus.RUNNING))
    repository.claim_task(make_task("done-task", status=TaskStatus.COMPLETED))
    repository.task_updated_at["orphan-task"] = time_module.time() - 7200

    assert repository.reclaim_stale_running_task("orphan-task", max_age_seconds=1800.0) is True
    # 幂等：status 已非 running，二次回收（多副本并发）不命中
    assert repository.reclaim_stale_running_task("orphan-task", max_age_seconds=1800.0) is False
    assert repository.reclaim_stale_running_task("fresh-task", max_age_seconds=1800.0) is False
    assert repository.reclaim_stale_running_task("done-task", max_age_seconds=1800.0) is False
    assert repository.reclaim_stale_running_task("missing-task", max_age_seconds=1800.0) is False

    orphan_after = repository.get_task("orphan-task")
    assert orphan_after is not None and orphan_after.status is TaskStatus.FAILED
    assert orphan_after.last_error is not None and "reclaimed" in orphan_after.last_error


def test_get_task_heals_stale_running_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_task 读到超龄幽灵 running 就地回收返回 failed；健康 running 原样返回。"""

    import time as time_module

    from thinkback.infra import config

    monkeypatch.setattr(config.settings, "task_orphan_running_seconds", 1800.0)
    repository = InMemoryMemoryRepository()
    repository.claim_task(make_task("orphan-task", status=TaskStatus.RUNNING))
    repository.claim_task(make_task("fresh-task", status=TaskStatus.RUNNING))
    repository.task_updated_at["orphan-task"] = time_module.time() - 7200

    service = MemoryService(repository=repository, backend=FakeMemoryBackend())

    healed = service.get_task("orphan-task")
    assert healed is not None and healed.status is TaskStatus.FAILED
    assert healed.last_error is not None and "reclaimed" in healed.last_error

    fresh = service.get_task("fresh-task")
    assert fresh is not None and fresh.status is TaskStatus.RUNNING


def test_get_task_heal_failure_returns_original_task() -> None:
    """读路径回收抛错不阻塞读：按原状态返回，不向上传播。"""

    class ExplodingRepository(InMemoryMemoryRepository):
        def reclaim_stale_running_task(self, task_id: str, *, max_age_seconds: float) -> bool:
            raise RuntimeError("db unavailable")

    repository = ExplodingRepository()
    repository.claim_task(make_task("task-1", status=TaskStatus.RUNNING))
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())

    result = service.get_task("task-1")
    assert result is not None and result.status is TaskStatus.RUNNING


def test_complete_async_l3_write_survives_repository_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """关机竞态：终态写落库失败时后台回调不抛异常（损失显式记录，由读路径自愈兜底）。"""

    repository = InMemoryMemoryRepository()
    repository.claim_task(make_task("task-straggler", status=TaskStatus.RUNNING))
    service = MemoryService(
        repository=repository, backend=FakeMemoryBackend(), l3_write_mode="async"
    )

    def explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("memory repository event loop is closed")

    monkeypatch.setattr(repository, "get_task", explode)

    # 契约：不抛异常（终态丢失被记录成日志；任务保持 RUNNING，由 get_task
    # 自愈 / 启动回收收敛为 FAILED）
    service._complete_async_l3_write("task-straggler", [], "user-1", "thinkback", [], {})
