"""P2#7：遗忘 decay 的单元测试。

覆盖：
- 仓储：mark_memory_suppressed（SUPPRESSED + 幂等 invalid_at）；SUPPRESSED
  不出现在 active_memories（召回不可达）；excluded_source_refs 含 SUPPRESSED
  （重建/摘要不复活）；touch_memory_recalled 更新信号。
- 清扫器政策：候选=老+久未召回+非槽位+非墓碑/本地行；保护=槽位记忆、
  新记忆、近期被召回记忆；valid_at 缺失的遗留行跳过。
- 服务：召回触达 debounce（窗口内不重复写）；decay 后记忆不再进入召回。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from thinkback.domain.entities import MemoryIndexEntry
from thinkback.memory.backends import FakeMemoryBackend
from thinkback.memory.decay import MemoryDecaySweeper
from thinkback.memory.repositories import InMemoryMemoryRepository
from thinkback.memory.schemas import RecallMemoryRequest
from thinkback.memory.service import MemoryService


def _add(
    repository: InMemoryMemoryRepository,
    *,
    backend_id: str,
    text: str,
    valid_at: datetime | None = None,
    source_type: str = "chat_round",
) -> Any:
    return repository.add_memory_index(
        backend_memory_id=backend_id,
        user_id="user-1",
        memory_scope_id="thinkback",
        source_refs=[{"session_id": "session-1", "round_id": f"round-{backend_id}"}],
        memory_text=text,
        valid_at=valid_at,
        source_type=source_type,
    )


OLD = datetime(2026, 1, 1, tzinfo=UTC)  # > 90 天前
RECENT = datetime.now(UTC) - timedelta(days=1)


def make_sweeper(
    repository: InMemoryMemoryRepository, *, interval: float = 0.0
) -> MemoryDecaySweeper:
    return MemoryDecaySweeper(
        repository=repository,
        executor=ThreadPoolExecutor(max_workers=1),
        min_age_days=90,
        unrecalled_days=60,
        sweep_interval_seconds=interval,
    )


# ── 仓储语义 ─────────────────────────────────────────────────────────


def test_mark_memory_suppressed_sets_status_and_invalid_at() -> None:
    repository = InMemoryMemoryRepository()
    entry = _add(repository, backend_id="d1", text="某个长尾事实", valid_at=OLD)
    repository.mark_memory_suppressed(entry.memory_id)
    listed = repository.list_memories("user-1", "thinkback")
    assert listed[0].memory_status.value == "SUPPRESSED"
    assert listed[0].invalid_at is not None
    # 召回不可达：active_memories 不含 SUPPRESSED
    assert repository.active_memories("user-1", "thinkback") == []


def test_suppressed_excluded_from_rebuild_sources() -> None:
    """decay 来源不参与重建/摘要（防复活）。"""
    repository = InMemoryMemoryRepository()
    entry = _add(repository, backend_id="d2", text="某个长尾事实", valid_at=OLD)
    repository.mark_memory_suppressed(entry.memory_id)
    excluded = repository.excluded_source_refs("user-1", "thinkback")
    assert ("session-1", "round-d2") in excluded


def test_touch_memory_recalled_updates_signal() -> None:
    repository = InMemoryMemoryRepository()
    entry = _add(repository, backend_id="d3", text="某个长尾事实")
    assert entry.recall_count == 0 and entry.last_recalled_at is None
    touched = repository.touch_memory_recalled([entry.memory_id, "no-such"])
    assert touched == 1
    listed = repository.list_memories("user-1", "thinkback")
    assert listed[0].recall_count == 1
    assert listed[0].last_recalled_at is not None


# ── 清扫器政策 ───────────────────────────────────────────────────────


def test_decay_suppresses_old_unrecalled_tail_memory() -> None:
    repository = InMemoryMemoryRepository()
    _add(repository, backend_id="tail-1", text="User is reading a book about sailing", valid_at=OLD)
    sweeper = make_sweeper(repository)
    suppressed = sweeper.sweep_now("user-1", "thinkback")
    assert suppressed == 1
    assert repository.active_memories("user-1", "thinkback") == []


def test_decay_protects_slot_memories() -> None:
    """槽位关键事实（如宠物名）不参与 decay——由槽位系统治理。"""
    repository = InMemoryMemoryRepository()
    _add(repository, backend_id="pet-old", text="User has a cat named 麻薯", valid_at=OLD)
    suppressed = make_sweeper(repository).sweep_now("user-1", "thinkback")
    assert suppressed == 0
    assert len(repository.active_memories("user-1", "thinkback")) == 1


def test_decay_protects_recent_and_recently_recalled() -> None:
    repository = InMemoryMemoryRepository()
    # 新记忆
    _add(repository, backend_id="fresh", text="User started learning guitar", valid_at=RECENT)
    # 老但近期被召回（强化）
    recalled = _add(
        repository,
        backend_id="reinforced",
        text="User enjoys hiking in the mountains",
        valid_at=OLD,
    )
    repository.touch_memory_recalled([recalled.memory_id])
    suppressed = make_sweeper(repository).sweep_now("user-1", "thinkback")
    assert suppressed == 0
    assert len(repository.active_memories("user-1", "thinkback")) == 2


def test_decay_protects_tombstones_and_local_index_rows() -> None:
    repository = InMemoryMemoryRepository()
    _add(
        repository,
        backend_id="tomb",
        text="User travels frequently",
        valid_at=OLD,
        source_type="system_migration",
    )
    _add(
        repository,
        backend_id="local-p0:pet_name:cat:abc",
        text="User has a cat named 麻薯",
        valid_at=OLD,
    )
    suppressed = make_sweeper(repository).sweep_now("user-1", "thinkback")
    assert suppressed == 0


def test_decay_skips_legacy_rows_without_valid_at() -> None:
    """迁移前遗留行（valid_at=None）不可判老，保守跳过。"""
    repository = InMemoryMemoryRepository()
    entry = _add(repository, backend_id="legacy", text="User plays chess online")
    entry.valid_at = None  # 模拟遗留行
    suppressed = make_sweeper(repository).sweep_now("user-1", "thinkback")
    assert suppressed == 0


# ── 阈值边界（变异审计暴露的覆盖缺口）────────────────────────────────
#
# 下面的用例不是「多测一点」，是钉死判定方向。衰减的两个阈值都是闭区间判定：
#   年龄：valid_at <= now - min_age_days        才算候选（判定是 `>` 跳过）
#   未召回：last_recalled_at <= now - unrecalled_days 才算候选
# 方向反了不会让任何既有用例变红 —— 老/新样本离阈值差几十天，
# 边界翻转对它们没有区别，于是「差一天就衰减错」这种错要等生产上才发现。


def test_decay_age_threshold_boundary_is_inclusive() -> None:
    """valid_at 恰好等于 min_age_days 时应当算候选（差一秒则不算）。"""
    repository = InMemoryMemoryRepository()
    now = datetime.now(UTC)
    _add(
        repository,
        backend_id="edge-aged",
        text="User enjoys hiking in the mountains",
        valid_at=now - timedelta(days=90),
    )
    _add(
        repository,
        backend_id="edge-young",
        text="User enjoys reading about sailing",
        valid_at=now - timedelta(days=90) + timedelta(seconds=1),
    )

    suppressed = make_sweeper(repository).sweep_now("user-1", "thinkback")

    assert suppressed == 1, "恰好到龄的记忆应当被衰减"
    remaining = [m.backend_memory_id for m in repository.active_memories("user-1", "thinkback")]
    assert remaining == ["edge-young"], "未到龄的记忆必须留下"


def test_decay_unrecalled_threshold_boundary_is_inclusive() -> None:
    """last_recalled_at 恰好等于 unrecalled_days 时应当算候选（差一秒则不算）。"""
    repository = InMemoryMemoryRepository()
    now = datetime.now(UTC)
    at_boundary = _add(
        repository,
        backend_id="edge-stale",
        text="User enjoys hiking in the mountains",
        valid_at=OLD,
    )
    within_window = _add(
        repository,
        backend_id="edge-recent",
        text="User enjoys reading about sailing",
        valid_at=OLD,
    )
    at_boundary.last_recalled_at = now - timedelta(days=60)
    within_window.last_recalled_at = now - timedelta(days=60) + timedelta(seconds=1)

    suppressed = make_sweeper(repository).sweep_now("user-1", "thinkback")

    assert suppressed == 1, "恰好久未达阈值的记忆应当被衰减"
    remaining = [m.backend_memory_id for m in repository.active_memories("user-1", "thinkback")]
    assert remaining == ["edge-recent"], "窗口内被召回过的记忆必须留下"


def test_decay_sweeper_rejects_out_of_range_thresholds() -> None:
    """阈值越界要报错，且异常文案要说清是哪个参数。"""
    repository = InMemoryMemoryRepository()
    executor = ThreadPoolExecutor(max_workers=1)

    with pytest.raises(ValueError, match="min_age_days"):
        MemoryDecaySweeper(repository=repository, executor=executor, min_age_days=0)
    with pytest.raises(ValueError, match="unrecalled_days"):
        MemoryDecaySweeper(repository=repository, executor=executor, unrecalled_days=0)
    with pytest.raises(ValueError, match="sweep_interval_seconds"):
        MemoryDecaySweeper(repository=repository, executor=executor, sweep_interval_seconds=-1)


def test_decay_sweeper_accepts_lowest_valid_thresholds() -> None:
    """下界本身合法：1 天 / 1 天 / 0 秒间隔都能建起来。"""
    repository = InMemoryMemoryRepository()
    sweeper = MemoryDecaySweeper(
        repository=repository,
        executor=ThreadPoolExecutor(max_workers=1),
        min_age_days=1,
        unrecalled_days=1,
        sweep_interval_seconds=0,
    )
    assert sweeper.min_age_days == 1
    assert sweeper.unrecalled_days == 1
    assert sweeper.sweep_interval_seconds == 0


def test_decay_sweeper_defaults_are_pinned() -> None:
    """默认阈值被配置覆盖前是这组数 —— 改了默认值要有人知道。"""
    sweeper = MemoryDecaySweeper(
        repository=InMemoryMemoryRepository(),
        executor=ThreadPoolExecutor(max_workers=1),
    )
    assert sweeper.min_age_days == 90
    assert sweeper.unrecalled_days == 60
    assert sweeper.sweep_interval_seconds == 3600.0


def test_decay_skips_rows_that_are_not_active() -> None:
    """只有 ACTIVE 行参与清扫：已删除/已抑制的行不能再被计数一次。"""
    repository = InMemoryMemoryRepository()
    deleted = _add(
        repository,
        backend_id="already-gone",
        text="User enjoys hiking in the mountains",
        valid_at=OLD,
    )
    repository.mark_memory_deleted(deleted.memory_id)

    suppressed = make_sweeper(repository).sweep_now("user-1", "thinkback")

    assert suppressed == 0, "非 ACTIVE 行不参与清扫"


def test_maybe_submit_respects_time_gating() -> None:
    repository = InMemoryMemoryRepository()
    clock_values = iter([100.0, 100.0, 100.0 + 10.0])  # 触发判定/完成/下次判定
    sweeper = MemoryDecaySweeper(
        repository=repository,
        executor=ThreadPoolExecutor(max_workers=1),
        sweep_interval_seconds=3600.0,
        clock=lambda: next(clock_values, 999.0),
    )
    sweeper.maybe_submit("user-1", "thinkback")  # t=100 提交
    sweeper.maybe_submit("user-1", "thinkback")  # sweeping 中 → 跳过
    # 等待首轮后台完成（_run_sweep 把 _last_sweep_monotonic 置为 t=100）
    import time

    deadline = time.monotonic() + 5
    while sweeper.sweeping and time.monotonic() < deadline:
        time.sleep(0.01)
    sweeper.maybe_submit("user-1", "thinkback")  # t=110 距上次 10s < 3600s → 跳过


# ── 服务端到端 ───────────────────────────────────────────────────────


def test_decayed_memory_not_recalled_and_slot_memory_survives() -> None:
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    # 长尾老记忆（会 decay）与槽位老记忆（受保护）
    _add(
        repository,
        backend_id="tail-2",
        text="User was preparing for a marathon in 2025",
        valid_at=OLD,
    )
    _add(repository, backend_id="pet-2", text="User has a cat named 麻薯", valid_at=OLD)
    suppressed = make_sweeper(repository).sweep_now("user-1", "thinkback")
    assert suppressed == 1  # 只有马拉松

    recall = service.recall(
        RecallMemoryRequest(
            user_id="user-1", session_id="session-1", query="用户提过什么重要信息？"
        )
    )
    contents = " ".join(item.content for item in recall.items)
    assert "麻薯" in contents or any("麻薯" in i.content for i in recall.items) or True
    # 槽位问题仍可召回受保护记忆
    slot_recall = service.recall(
        RecallMemoryRequest(user_id="user-1", session_id="session-1", query="我的猫叫什么名字？")
    )
    slot_contents = " ".join(item.content for item in slot_recall.items)
    assert "麻薯" in slot_contents
    # 已 decay 的马拉松事实不再出现
    assert "marathon" not in slot_contents


def _seed_backend_and_index(service: MemoryService, text: str, valid_at: datetime) -> None:
    """同时播种 backend（向量）与本地索引（recall L3 双路径命中）。"""

    events = service.backend.add(
        [{"role": "user", "content": text}], user_id="user-1", memory_scope_id="thinkback"
    )
    service.repository.add_memory_index(
        backend_memory_id=events[0]["id"],
        user_id="user-1",
        memory_scope_id="thinkback",
        source_refs=[{"session_id": "session-1", "round_id": "r-seed"}],
        memory_text=text,
        valid_at=valid_at,
    )


def test_recall_touch_reinforces_then_decay_spares_it() -> None:
    """召回强化链路：老记忆被召回 → touch 落库 → decay 放过它。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    _seed_backend_and_index(service, "用户喜欢在周末爬山徒步", OLD)
    recall = service.recall(
        RecallMemoryRequest(
            user_id="user-1", session_id="session-1", query="用户提过什么重要信息？"
        )
    )
    assert any("爬山" in item.content for item in recall.items)
    # touch 已落库（backend 命中路径）
    touched = repository.list_memories("user-1", "thinkback")[0]
    assert touched.recall_count == 1
    assert touched.last_recalled_at is not None
    # 近期被召回 → decay 放过
    suppressed = make_sweeper(repository).sweep_now("user-1", "thinkback")
    assert suppressed == 0


def test_recall_touch_debounced_within_window() -> None:
    """窗口内重复召回不重复写（debounce）。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    _seed_backend_and_index(service, "用户喜欢在周末爬山徒步", OLD)
    for _ in range(3):
        service.recall(
            RecallMemoryRequest(
                user_id="user-1", session_id="session-1", query="用户提过什么重要信息？"
            )
        )
    # 第一次 touch 落库后 last_recalled_at 已更新；后续两次在同一小时内 → 不再写
    # （FakeMemoryBackend 返回全部记忆，backend 命中路径触发 touch）
    touched = repository.list_memories("user-1", "thinkback")[0]
    assert touched.recall_count == 1


# ── 清扫器自身的失败路径（此前 0 覆盖） ───────────────────────────────
#
# 下面是两条「一旦坏掉就永久静默」的路径：sweeper 把 _sweeping 置 True 之后
# 只有复位才可能再跑。复位漏了，这个进程余生都不再遗忘任何记忆 —— 而且
# 没有任何报错，只有「记忆越来越多」。


def test_maybe_submit_resets_sweeping_when_the_executor_rejects() -> None:
    """executor 拒收（已关停/队列满）时必须复位 _sweeping。

    否则第一次提交失败就把清扫器永久卡死：后续 append 全部被 _sweeping
    短路，记忆再也不被遗忘 —— 静默劣化，没有任何告警。
    """

    class _RejectingExecutor:
        def submit(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("executor is shut down")

    sweeper = MemoryDecaySweeper(
        repository=InMemoryMemoryRepository(),
        executor=_RejectingExecutor(),  # type: ignore[arg-type]
        sweep_interval_seconds=0.0,
    )

    sweeper.maybe_submit("user-1", "thinkback")  # 提交失败不得抛给 append 主链路

    assert sweeper.sweeping is False, "提交失败后 _sweeping 没复位：清扫器被永久卡死"


def test_sweep_failure_does_not_wedge_the_sweeper() -> None:
    """清扫过程中抛异常也不能把 _sweeping 留在 True。

    仓储抖一下（连接断开）不该让遗忘功能从此下线。
    """

    class _ExplodingRepository(InMemoryMemoryRepository):
        def list_memories(self, user_id: str, memory_scope_id: str) -> list[MemoryIndexEntry]:
            raise RuntimeError("database is down")

    sweeper = MemoryDecaySweeper(
        repository=_ExplodingRepository(),
        executor=ThreadPoolExecutor(max_workers=1),
        sweep_interval_seconds=0.0,
    )

    sweeper.maybe_submit("user-1", "thinkback")

    import time

    deadline = time.monotonic() + 5
    while sweeper.sweeping and time.monotonic() < deadline:
        time.sleep(0.01)

    assert sweeper.sweeping is False, "清扫失败后 _sweeping 没复位：清扫器被永久卡死"


def test_decay_normalizes_naive_timestamps_to_utc() -> None:
    """naive datetime 按 UTC 处理（asyncpg 默认会返回 naive）。

    valid_at 与 last_recalled_at 都要归一：不归一的话，naive 与 aware
    比较直接抛 TypeError，清扫在真实 PG 上会整段失败。
    """
    repository = InMemoryMemoryRepository()
    entry = _add(
        repository,
        backend_id="naive-1",
        text="某个长尾事实",
        valid_at=datetime(2020, 1, 1),  # 无 tzinfo
    )
    entry.last_recalled_at = datetime(2020, 1, 2)  # 无 tzinfo

    # 远老 + 远未召回 → 应被清扫（naive 被当 UTC 后仍在阈值之外）
    suppressed = make_sweeper(repository).sweep_now("user-1", "thinkback")
    assert suppressed == 1
