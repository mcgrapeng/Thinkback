"""遗忘 decay 清扫器（P2#7：四杠杆中最轻的 decay 起步）。

政策（保守、确定性、无 LLM）：
- **不可达而非删除**（Oblivion 2604.00131 / 本仓 SUPERSEDED 同语义）：
  decay 记忆置 ``SUPPRESSED`` + ``invalid_at``，数据保留可审计可恢复，
  不做物理删除（合规底线）。
- **艾宾浩斯式信号**：只有"足够老 且 久未被召回"的长尾记忆才衰减；
  被召回即强化（recall 路径 ``touch_memory_recalled`` 维护信号）。
- **槽位保护**：``memory_conflict_slot`` 命中的关键事实记忆不参与 decay
  ——它们由槽位 supersede / 双时态排序治理（职责分离），decay 只管长尾。
- **墓碑/本地合成行保护**：source_type=system_migration 与 local-p0:
  等本地索引 ID 永不触碰。

触发（sleep-time 式，无独立线程）：append 后 ``maybe_submit``，进程级
时间门控（默认 1 小时最多一次全局清扫）；关闭开关后完全不启用。
清扫失败只打 warning，不影响主链路。

效果度量：P2#8 评测集（script/eval/zh_replay_eval.py）可对比开关前后
通过率与注入量。
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import TYPE_CHECKING, Any

from loguru import logger

from thinkback.domain.slots import is_local_index_memory_id, memory_conflict_slot

if TYPE_CHECKING:
    from concurrent.futures import ThreadPoolExecutor

    from thinkback.domain.ports import MemoryRepository


class MemoryDecaySweeper:
    """每个 ``MemoryService`` 持有零或一个实例（未启用时不构造）。"""

    def __init__(
        self,
        *,
        repository: MemoryRepository,
        executor: ThreadPoolExecutor,
        min_age_days: int = 90,
        unrecalled_days: int = 60,
        sweep_interval_seconds: float = 3600.0,
        clock: Any = None,
    ) -> None:
        if min_age_days < 1:
            raise ValueError("min_age_days must be >= 1")
        if unrecalled_days < 1:
            raise ValueError("unrecalled_days must be >= 1")
        if sweep_interval_seconds < 0:
            raise ValueError("sweep_interval_seconds must be >= 0")
        self._repository = repository
        self._executor = executor
        self.min_age_days = min_age_days
        self.unrecalled_days = unrecalled_days
        self.sweep_interval_seconds = sweep_interval_seconds
        self._clock = clock or time.monotonic
        self._state_lock = Lock()
        self._last_sweep_monotonic: float = float("-inf")  # 启动后首轮 append 即可清扫
        self._sweeping = False

    # ── 对外接口 ────────────────────────────────────────────────────

    def maybe_submit(self, user_id: str, memory_scope_id: str) -> None:
        """append 后调用：距上次清扫超过间隔时提交一次后台清扫。"""

        with self._state_lock:
            if self._sweeping:
                return
            if self._clock() - self._last_sweep_monotonic < self.sweep_interval_seconds:
                return
            self._sweeping = True

        try:
            self._executor.submit(self._run_sweep, user_id, memory_scope_id)
        except Exception:
            with self._state_lock:
                self._sweeping = False
            logger.warning("memory decay sweep submit failed")

    def sweep_now(self, user_id: str, memory_scope_id: str) -> int:
        """同步清扫（测试/运维入口）；返回被置 SUPPRESSED 的条数。"""

        return self._decay_scope(user_id, memory_scope_id)

    @property
    def sweeping(self) -> bool:
        with self._state_lock:
            return self._sweeping

    # ── 内部 ────────────────────────────────────────────────────────

    def _run_sweep(self, user_id: str, memory_scope_id: str) -> None:
        sweep_log = logger.bind(user_id=user_id, memory_scope_id=memory_scope_id)
        try:
            suppressed = self._decay_scope(user_id, memory_scope_id)
            if suppressed:
                sweep_log.bind(suppressed_count=suppressed).info("memory decay sweep completed")
        except Exception as exc:
            sweep_log.bind(error_type=type(exc).__name__).warning("memory decay sweep failed")
        finally:
            with self._state_lock:
                self._sweeping = False
                self._last_sweep_monotonic = self._clock()

    def _decay_scope(self, user_id: str, memory_scope_id: str) -> int:
        now = datetime.now(UTC)
        min_valid_at = now - timedelta(days=self.min_age_days)
        max_last_recalled = now - timedelta(days=self.unrecalled_days)

        suppressed = 0
        for memory in self._repository.list_memories(user_id, memory_scope_id):
            if memory.memory_status.value != "ACTIVE":
                continue
            if self._is_decay_candidate(memory, min_valid_at, max_last_recalled):
                result = self._repository.mark_memory_suppressed(memory.memory_id)
                if result is not None:
                    suppressed += 1
        return suppressed

    def _is_decay_candidate(
        self, memory: Any, min_valid_at: datetime, max_last_recalled: datetime
    ) -> bool:
        # 墓碑/本地合成行（local-p0: 等）永不 decay
        if is_local_index_memory_id(str(memory.backend_memory_id)):
            return False
        if memory.source_type == "system_migration":
            return False
        # 槽位保护：关键事实记忆由槽位系统治理
        if memory_conflict_slot(memory.memory_text) is not None:
            return False
        # 足够老（valid_at 缺失的遗留行按 created 语义处理：视为不可判老，跳过）。
        # S3-P3: asyncpg 默认可能返回 naive datetime；归一化 UTC 后比较。
        valid_at = getattr(memory, "valid_at", None)
        if valid_at is None:
            return False
        if valid_at.tzinfo is None:
            valid_at = valid_at.replace(tzinfo=UTC)
        if valid_at > min_valid_at:
            return False
        # 久未被召回（从未召回过 → last_recalled_at 为 None 视为"久未"）。
        # S3-P3: asyncpg 默认可能返回 naive datetime；归一化 UTC 后比较。
        last_recalled = getattr(memory, "last_recalled_at", None)
        if last_recalled is None:
            return True
        if last_recalled.tzinfo is None:
            last_recalled = last_recalled.replace(tzinfo=UTC)
        result: bool = last_recalled <= max_last_recalled
        return result
