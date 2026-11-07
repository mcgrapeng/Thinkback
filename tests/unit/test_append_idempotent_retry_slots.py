"""``append`` 的**幂等重试**路径：L3 写槽位必须归还。

``append`` 在 async 模式下先占一个 L3 写槽位（``reserve_write_slot``），
然后才去抢任务执行权。抢不到 / 上一轮还在跑 / 上一轮已完成时，
它都要**把槽位还回去**再返回 —— 否则槽位只进不出。

生产参数 ``memory_l3_max_pending_tasks=256``：槽位泄漏一次，
该 Pod 的写入能力就少一分；泄漏 256 次，**写入能力永久归零**，
直到重启。仓库里那段注释（``service.py`` 的 append 开头）就是在说这件事。

**这些分支此前从未被走到**，原因很具体 —— 两条早退路径把它们都挡住了：

- ``append`` 开头：journal 行存在 + 任务是 ``PENDING/RUNNING/COMPLETED``
  → 直接回放（早退，压根没占槽位）；
- journal 行存在 + 任务是 ``FAILED`` → 走到重试逻辑（``else`` 分支），
  但那不是本文件要覆盖的三条。

所以这三条分支的精确前置条件是：**任务行存在、journal 行不存在** ——
即「任务已建但回合还没落库」的那段窗口（进程在两步之间被硬杀）。
真实场景：服务在 claim 之后、save_round 之前崩了；调用方重试同一轮。

写槽位泄漏的代价在这个窗口里最高：占着槽位的那个进程已经死了，
没人会替它归还 —— 除非重试的那次调用把它还掉。
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest

from thinkback.domain.entities import TaskEntry
from thinkback.domain.enums import OperationType, TaskStatus
from thinkback.memory.backends import FakeMemoryBackend
from thinkback.memory.repositories import InMemoryMemoryRepository
from thinkback.memory.schemas import AppendMemoryRequest, MessageRole
from thinkback.memory.service import MemoryService

_CONTENT = "我叫麻薯，记住我"
_TS = datetime(2026, 11, 5, tzinfo=UTC)


def _service() -> MemoryService:
    return MemoryService(
        repository=InMemoryMemoryRepository(),
        backend=FakeMemoryBackend(),
        l3_write_mode="async",
    )


def _append(service: MemoryService, round_id: str) -> object:
    return service.append(
        AppendMemoryRequest(
            request_id=f"req-{round_id}",
            user_id="u1",
            session_id="s1",
            round_id=round_id,
            messages=[
                {
                    "message_id": f"m-{round_id}",
                    "role": MessageRole.USER,
                    "content": _CONTENT,
                    "timestamp": _TS,
                }
            ],
            source_timestamp=_TS,
            metadata={},
        )
    )


def _seed_orphan_task(service: MemoryService, round_id: str, status: TaskStatus) -> None:
    """造出「任务行在、journal 行不在」的状态。

    这是「进程在 claim 之后、save_round 之前被硬杀」留下的现场：
    任务标着 RUNNING/PENDING/COMPLETED，回合却没落库。调用方重试同一轮时
    就会走进那三条分支。

    任务行已存在时**改它的状态**（而不是新建）—— 仓储有乐观锁
    （``row_version``），拿旧快照再存一次会被挡下。
    """
    task_id = f"memory-extract:{round_id}"
    assert service.repository.get_round(round_id) is None, "前置不干净：journal 行不该存在"
    existing = service.repository.get_task(task_id)
    if existing is not None:
        existing.status = status
        service.repository.save_task(existing)
        return
    service.repository.save_task(
        TaskEntry(
            task_id=task_id,
            request_id=f"req-{round_id}",
            op_type=OperationType.WRITE_ROUND,
            scope={
                "user_id": "u1",
                "session_id": "s1",
                "session_scope_id": "s1",
                "long_term_scope_id": "s1",
                "round_id": round_id,
            },
            status=status,
        )
    )


def test_a_pending_orphan_task_is_short_circuited_and_the_slot_is_returned() -> None:
    """上一轮的孤儿任务是 PENDING → 回放它的状态，槽位归还。

    走 ``if task.status in {PENDING, RUNNING}`` 分支。
    """
    service = _service()
    _seed_orphan_task(service, "r-pending", TaskStatus.PENDING)

    response = _append(service, "r-pending")
    time.sleep(0.2)

    assert getattr(response, "status", None) == "running"
    assert service._l3._pending_write_slots == 0, "回放路径漏了写槽位"


def test_a_running_orphan_task_is_short_circuited_and_the_slot_is_returned() -> None:
    """上一轮的孤儿任务是 RUNNING → 回放，槽位归还。"""
    service = _service()
    _seed_orphan_task(service, "r-running", TaskStatus.RUNNING)

    response = _append(service, "r-running")
    time.sleep(0.2)

    assert getattr(response, "status", None) == "running"
    assert service._l3._pending_write_slots == 0, "回放路径漏了写槽位"


def test_a_completed_orphan_task_is_short_circuited_and_the_slot_is_returned() -> None:
    """上一轮的孤儿任务是 COMPLETED → 回放已完成状态，槽位归还。

    走 ``if task.status is TaskStatus.COMPLETED`` 分支。
    """
    service = _service()
    _seed_orphan_task(service, "r-completed", TaskStatus.COMPLETED)

    response = _append(service, "r-completed")
    time.sleep(0.2)

    assert getattr(response, "status", None) == "already_done"
    assert service._l3._pending_write_slots == 0, "回放路径漏了写槽位"


def test_the_write_slot_never_accumulates_across_orphan_replays() -> None:
    """反复重试孤儿任务，槽位数不得增长。

    「槽位只进不出」那个故障的直接观测：生产上限 256，
    每次重试漏一个 → 256 次重试后该 Pod 再也写不进任何记忆。

    每轮重试前要**重新播种**：上一轮重试会把任务行更新掉
    （``row_version`` +1），直接再存一次会被乐观锁挡下
    （``TaskStaleWriteError`` —— 那道锁本身是对的）。
    """
    service = _service()
    for _ in range(5):
        _seed_orphan_task(service, "r-repeat", TaskStatus.PENDING)
        _append(service, "r-repeat")
        time.sleep(0.15)
        # 下一轮重试前把任务改回 PENDING（模拟又崩了一次）
        _seed_orphan_task(service, "r-repeat", TaskStatus.PENDING)

    assert service._l3._pending_write_slots == 0, (
        f"5 次重试后仍占着 {service._l3._pending_write_slots} 个写槽位 —— 槽位在泄漏"
    )


def test_the_slot_is_released_when_the_task_claim_is_lost() -> None:
    """抢任务执行权失败（别人抢到了）→ 槽位归还。

    走 ``if not task_claimed`` 分支：同一轮并发两个写入者，
    claim 是原子的，抢不到的一方直接回放对方的状态。
    """
    service = _service()

    from thinkback.domain.entities import TaskEntry as _TE

    def _claim(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        task = kwargs.get("task") or (args[0] if args else None)
        claimed = _TE(
            task_id=getattr(task, "task_id", "unknown"),
            request_id="other-writer",
            op_type=OperationType.WRITE_ROUND,
            scope={},
            status=TaskStatus.RUNNING,
        )
        return claimed, False

    service.repository.claim_task = _claim  # type: ignore[method-assign]
    _append(service, "r-claim")
    time.sleep(0.2)

    assert service._l3._pending_write_slots == 0, "抢不到执行权时漏了写槽位"


@pytest.mark.parametrize(
    "status",
    [TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.COMPLETED],
)
def test_no_orphan_replay_leaves_a_slot_behind(status: TaskStatus) -> None:
    """三种孤儿任务状态逐一验证：回放后槽位数必须是 0。"""
    service = _service()
    _seed_orphan_task(service, f"r-{status.name}", status)
    _append(service, f"r-{status.name}")
    time.sleep(0.2)

    assert service._l3._pending_write_slots == 0


# ── 另一半：压根没占槽位时，不能去「归还」一个没占过的槽位 ──────────────
#
# ``l3_slot_reserved`` 是「这次真的占了一个槽位」的标记。命中 write-gate
# （正文受限/不安全）时不占槽位 —— 那条路压根不写 L3。
# 这时若走到「归还槽位」那几条分支，``release_write_slot()`` 会把
# **别人的**槽位也放掉 —— 槽位计数被抬高，实际并发上限比配置的高。
#
# 这几条分支的另一侧（``l3_slot_reserved`` 为 False）就是「没占过槽位，
# 不能归还」。此前从未被走到，因为受限内容的轮次基本不会撞上
# 「任务已存在且状态是 PENDING/RUNNING/COMPLETED」。


def _append_restricted(service: MemoryService, round_id: str) -> object:
    """正文命中受限词表 → 命中 write-gate → 不占 L3 槽位。"""
    return service.append(
        AppendMemoryRequest(
            request_id=f"req-{round_id}",
            user_id="u1",
            session_id="s1",
            round_id=round_id,
            messages=[
                {
                    "message_id": f"m-{round_id}",
                    "role": MessageRole.USER,
                    "content": "我的密钥是 sk-abcdef1234567890",
                    "timestamp": _TS,
                }
            ],
            source_timestamp=_TS,
            metadata={},
        )
    )


def test_a_restricted_orphan_replay_does_not_release_a_slot_it_never_took() -> None:
    """受限内容 + 孤儿任务 → 走短-circuit，但**不能**归还槽位。

    归还一个没占过的槽位 = 把别人的槽位也放掉 = 实际并发上限高于配置。
    """
    service = _service()
    _seed_orphan_task(service, "r-restricted", TaskStatus.PENDING)

    # 先占一个槽位，模拟「另一个写入者正在写」
    service._l3.reserve_write_slot()
    assert service._l3._pending_write_slots == 1

    _append_restricted(service, "r-restricted")
    time.sleep(0.2)

    assert service._l3._pending_write_slots == 1, (
        f"受限内容的重试归还了它没占过的槽位（剩 {service._l3._pending_write_slots}）—— "
        "实际并发上限被抬高"
    )


def test_a_restricted_orphan_replay_with_a_completed_task_keeps_the_slot() -> None:
    """同一场景，任务状态是 COMPLETED —— 走的分支不同，结论相同。"""
    service = _service()
    _seed_orphan_task(service, "r-restricted2", TaskStatus.COMPLETED)

    service._l3.reserve_write_slot()
    assert service._l3._pending_write_slots == 1

    _append_restricted(service, "r-restricted2")
    time.sleep(0.2)

    assert service._l3._pending_write_slots == 1, "受限内容的重试归还了它没占过的槽位"


def test_a_restricted_write_that_loses_the_claim_keeps_the_slot() -> None:
    """受限内容 + **抢不到执行权** → 也不能归还它没占过的槽位。

    这是 ``if not task_claimed`` 分支下「压根没占槽位」的那一侧：
    写 gate 命中 → 不占槽位；claim 又输给别人 → 直接回放。
    两条路都不该动槽位计数 —— 动了就是把别人的槽位放掉。
    """
    service = _service()

    from thinkback.domain.entities import TaskEntry as _TE

    def _claim(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        task = kwargs.get("task") or (args[0] if args else None)
        claimed = _TE(
            task_id=getattr(task, "task_id", "unknown"),
            request_id="other-writer",
            op_type=OperationType.WRITE_ROUND,
            scope={},
            status=TaskStatus.RUNNING,
        )
        return claimed, False

    service.repository.claim_task = _claim  # type: ignore[method-assign]

    # 别人的槽位
    service._l3.reserve_write_slot()
    assert service._l3._pending_write_slots == 1

    _append_restricted(service, "r-claim-restricted")
    time.sleep(0.2)

    assert service._l3._pending_write_slots == 1, (
        "受限内容抢不到执行权时归还了它没占过的槽位 —— 实际并发上限被抬高"
    )
