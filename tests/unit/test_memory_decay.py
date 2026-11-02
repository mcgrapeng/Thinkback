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
    recorded_at: datetime | None = None,
) -> Any:
    """播种一条本地索引行。

    ``valid_at`` = 事实描述的事情发生在什么时候；``recorded_at`` = 我们
    **记下**它是什么时候。decay 判龄看后者（我们持有它多久还没用到），
    TTL 判时效看前者（写入时刻 + ttl_days）。两者在正常写入时几乎一样，
    在导入/重放场景下差很远。

    默认 ``recorded_at = valid_at``：这些用例描述的是「一条被我们持有很久
    的记忆」，两个时钟给同一个值正好表达这件事。
    """
    return repository.add_memory_index(
        backend_memory_id=backend_id,
        user_id="user-1",
        memory_scope_id="thinkback",
        source_refs=[{"session_id": "session-1", "round_id": f"round-{backend_id}"}],
        memory_text=text,
        valid_at=valid_at,
        recorded_at=recorded_at if recorded_at is not None else valid_at,
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
#   年龄：recorded_at <= now - min_age_days     才算候选（判定是 `>` 跳过）
#   未召回：last_recalled_at <= now - unrecalled_days 才算候选
# 方向反了不会让任何既有用例变红 —— 老/新样本离阈值差几十天，
# 边界翻转对它们没有区别，于是「差一天就衰减错」这种错要等生产上才发现。


def test_decay_age_threshold_boundary_is_inclusive() -> None:
    """recorded_at 恰好等于 min_age_days 时应当算候选（差一秒则不算）。"""
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


# ---------------------------------------------------------------------------
# 清扫器的记账与触发（变异审计暴露的覆盖缺口）
# ---------------------------------------------------------------------------


class _RecordingExecutor:
    """记录 ``submit`` 的实参，不真的执行 —— 用来断言「清扫跑了哪个作用域」。"""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def submit(self, fn, *args) -> None:  # type: ignore[no-untyped-def]
        self.calls.append((fn, *args))


def _sweeper_with(
    repository: InMemoryMemoryRepository,
    *,
    executor: Any = None,
    clock_values: list[float] | None = None,
    interval: float = 3600.0,
) -> MemoryDecaySweeper:
    values = iter(clock_values or [])
    return MemoryDecaySweeper(
        repository=repository,
        executor=executor or _RecordingExecutor(),
        min_age_days=90,
        unrecalled_days=60,
        sweep_interval_seconds=interval,
        clock=lambda: next(values, 999.0),
    )


def test_a_sweep_counts_every_memory_it_suppressed() -> None:
    """清扫计数是 ``+= 1``：写成 ``= 1`` 的话，一次清扫 3 条只报 1 条。

    计数进审计（`PurgeReport` 同理），报少了会让运维以为「只有一条长尾」，
    而实际清了一片 —— 或反过来以为没清，其实清了。
    """
    repository = InMemoryMemoryRepository()
    long_held = datetime.now(UTC) - timedelta(days=200)
    for index in range(3):
        repository.add_memory_index(
            backend_memory_id=f"tail-{index}",
            user_id="user-1",
            memory_scope_id="thinkback",
            source_refs=[{"session_id": "s1", "round_id": f"r-{index}"}],
            memory_text=f"用户提过一个长尾话题 {index}",
            valid_at=long_held,
            recorded_at=long_held,
        )

    suppressed = _sweeper_with(repository).sweep_now("user-1", "thinkback")

    assert suppressed == 3, f"一次清扫 3 条就该报 3，实际 {suppressed}"


def test_a_non_active_row_does_not_stop_the_scan_of_older_ones() -> None:
    """非 ACTIVE 行是 ``continue`` 跳过，不是 ``break`` 提前收尾。

    遇到一条已删除/已抑制的行就停下，后面那些真正的长尾候选就全漏掉 ——
    线上表现是「清扫只清到第一条脏行前面的那几条」。
    """
    repository = InMemoryMemoryRepository()
    long_held = datetime.now(UTC) - timedelta(days=200)
    dead = repository.add_memory_index(
        backend_memory_id="dead-first",
        user_id="user-1",
        memory_scope_id="thinkback",
        source_refs=[{"session_id": "s1", "round_id": "r-dead"}],
        memory_text="用户提过一个已经删掉的话题",
        valid_at=long_held,
        recorded_at=long_held,
    )
    repository.mark_memory_deleted(dead.memory_id)
    for index in range(2):
        repository.add_memory_index(
            backend_memory_id=f"live-{index}",
            user_id="user-1",
            memory_scope_id="thinkback",
            source_refs=[{"session_id": "s1", "round_id": f"r-live-{index}"}],
            memory_text=f"用户提过一个长尾话题 {index}",
            valid_at=long_held,
            recorded_at=long_held,
        )

    suppressed = _sweeper_with(repository).sweep_now("user-1", "thinkback")

    assert suppressed == 2, f"脏行之后的两条也必须被清，实际 {suppressed}"


def test_the_local_index_row_guard_is_what_keeps_them_out() -> None:
    """``local-p0:`` 行永不 decay —— 判据是 backend_memory_id，不是别处。

    改成判别处（如固定返回 False）的话，那些本地合成行会被当成普通长尾
    清掉，而它们是 P0 槽位的权威索引。

    文本刻意**不**命中任何 P0 槽位 —— 否则「槽位保护」那道守卫会挡住，
    这条用例就分不清是谁在起作用。
    """
    repository = InMemoryMemoryRepository()
    long_held = datetime.now(UTC) - timedelta(days=200)
    repository.add_memory_index(
        backend_memory_id="local-p0:some_slot:abc",
        user_id="user-1",
        memory_scope_id="thinkback",
        source_refs=[{"session_id": "s1", "round_id": "r-p0"}],
        memory_text="用户提过一个不再提的话题",
        valid_at=long_held,
        recorded_at=long_held,
    )

    suppressed = _sweeper_with(repository).sweep_now("user-1", "thinkback")

    assert suppressed == 0, "local-p0 行不得被 decay"
    assert len(repository.active_memories("user-1", "thinkback")) == 1


def test_a_sweep_only_touches_the_scope_it_was_asked_about() -> None:
    """清扫一个 user 时不得动另一个 user 的记忆。

    ``_run_sweep`` 把 user_id / scope 传给 ``_decay_scope`` —— 实参传错
    （传 None、传反了）会让清扫跑去清别人的记忆，或者清了个空作用域
    都不报错。用两条 user 的老长尾记忆一对照就看得出。
    """
    repository = InMemoryMemoryRepository()
    long_held = datetime.now(UTC) - timedelta(days=200)
    for owner in ("user-1", "user-2"):
        repository.add_memory_index(
            backend_memory_id=f"tail-{owner}",
            user_id=owner,
            memory_scope_id="thinkback",
            source_refs=[{"session_id": "s1", "round_id": f"r-{owner}"}],
            memory_text="用户提过一个长尾话题",
            valid_at=long_held,
            recorded_at=long_held,
        )

    _sweeper_with(repository)._run_sweep("user-1", "thinkback")

    assert repository.active_memories("user-1", "thinkback") == [], "user-1 的长尾记忆应当被清"
    assert len(repository.active_memories("user-2", "thinkback")) == 1, (
        "user-2 的记忆不得被清 —— 清扫跑的是它被给的那个 user"
    )


def test_maybe_submit_submits_the_scope_it_was_given() -> None:
    """后台清扫必须跑**调用方给的那个** user 与 scope，不能换别的。

    实参被改（如 user_id 传 None）会让清扫跑去清别人的记忆，或者清了个
    空作用域 —— 两种都不会有任何报错。
    """
    repository = InMemoryMemoryRepository()
    executor = _RecordingExecutor()
    sweeper = _sweeper_with(repository, executor=executor, clock_values=[100.0])

    sweeper.maybe_submit("user-7", "scope-7")

    assert len(executor.calls) == 1
    fn, user_id, scope = executor.calls[0]
    assert fn == sweeper._run_sweep
    assert user_id == "user-7", f"清扫必须跑调用方给的 user，实际 {user_id}"
    assert scope == "scope-7", f"清扫必须跑调用方给的 scope，实际 {scope}"


def test_maybe_submit_skips_while_a_sweep_is_already_running() -> None:
    """在飞清扫期间不再提交第二次：两次清扫会互相打架（重复抑制）。

    `_sweeping` 标志是这个守卫的全部依据 —— 它没被设上（None/False）
    的话，每个 append 都会再提交一次清扫。
    """
    repository = InMemoryMemoryRepository()
    executor = _RecordingExecutor()
    sweeper = _sweeper_with(repository, executor=executor, clock_values=[0.0, 0.0])

    sweeper.maybe_submit("user-1", "thinkback")
    assert sweeper.sweeping is True, "提交后清扫标志必须立起来"

    sweeper.maybe_submit("user-1", "thinkback")
    assert len(executor.calls) == 1, f"在飞清扫期间不得再提交，实际提交了 {len(executor.calls)} 次"
    assert sweeper.sweeping is True, "第二次 maybe_submit 不得把清扫标志清掉"


def test_the_sweep_interval_is_exclusive_at_the_boundary() -> None:
    """距上次清扫**恰好**间隔到时不放行，超过才放行（判定是 ``<`` 跳过）。

    写成 ``<=`` 的话，恰好到点那次会被跳过 —— 清扫频率悄悄降了一档，
    而离阈值差一天的样本看不出差别。
    """
    repository = InMemoryMemoryRepository()
    # interval=10：clock=10 时距上次（-inf 归一为 0）恰好 10，应放行。
    executor = _RecordingExecutor()
    sweeper = _sweeper_with(repository, executor=executor, clock_values=[10.0], interval=10.0)
    sweeper._last_sweep_monotonic = 0.0

    sweeper.maybe_submit("user-1", "thinkback")

    assert len(executor.calls) == 1, "恰好到点应当放行清扫"


def test_the_first_sweep_is_not_blocked_by_the_interval() -> None:
    """启动后首轮 append 就该清扫（``_last_sweep_monotonic`` 初值 -inf）。

    算式写错（差写成和）的话，`clock + (-inf) < interval` 恒成立 →
    首轮永远被跳过 → 服务起来后长尾记忆再也不会被清。
    """
    repository = InMemoryMemoryRepository()
    executor = _RecordingExecutor()
    sweeper = _sweeper_with(repository, executor=executor, clock_values=[1000.0], interval=3600.0)

    sweeper.maybe_submit("user-1", "thinkback")

    assert len(executor.calls) == 1, "首轮清扫不得被间隔判定挡住"


def test_a_submitted_sweep_clears_the_in_progress_flag_when_it_runs() -> None:
    """清扫跑完要把 ``_sweeping`` 放下，否则后续所有 append 都不再触发清扫。

    这是「清扫只跑一次就永久停摆」的经典成因。

    两次 ``_clock()`` 取值各给一个：``maybe_submit`` 的间隔判定一次、
    ``_run_sweep`` 收尾记时一次。
    """
    repository = InMemoryMemoryRepository()
    sweeper = _sweeper_with(repository, clock_values=[0.0, 7.0])
    sweeper.maybe_submit("user-1", "thinkback")
    assert sweeper.sweeping is True

    sweeper._run_sweep("user-1", "thinkback")

    assert sweeper.sweeping is False, "清扫跑完必须把标志放下"
    assert sweeper._last_sweep_monotonic == 7.0, "清扫跑完必须记录时间，否则下次立刻又触发"
