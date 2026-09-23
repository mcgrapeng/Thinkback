"""
Structured logging utilities for memory service.

统一的"操作级"日志帮助类，所有方法都遵循 ``[PREFIX] {action} {verb}`` 格式，
方便在生产日志中按 PREFIX 聚合。

Predefined loggers:
  LOGGER_MEMORY: Memory operations（记忆写入/召回/删除）
  LOGGER_EMBED: Embedding generation（embedding 抽取/调用）
  LOGGER_STORE: Storage operations（仓库层读写）
  LOGGER_QUERY: Query execution（召回查询执行）

设计动机：
- 比裸 ``logger.info("started")`` 更结构化，可读性更强；
- 自动绑定 ``operation_id``，跨函数追踪同一操作；
- ``timed_block`` 把耗时统计和结果状态合并到一行。
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from loguru import logger


class StructuredLogger:
    """统一的结构化日志帮助类。

    每个方法对应一个语义动词（started/completed/skipped/failed/degraded/
    decision/metric），调用方按业务阶段选择合适动词，让日志自然呈现操作轨迹。
    """

    def __init__(self, prefix: str, operation_id: str | None = None) -> None:
        self.prefix = f"[{prefix}]"
        self.operation_id = operation_id

    def _bind_context(self, **kwargs: Any) -> dict[str, Any]:
        """合并 operation_id 与调用方上下文，便于日志聚合。"""

        ctx = dict(kwargs)
        if self.operation_id:
            ctx["operation_id"] = self.operation_id
        return ctx

    def started(self, action: str, **context: Any) -> None:
        """记录操作开始。"""

        logger.bind(**self._bind_context(**context)).info(f"{self.prefix} {action} started")

    def completed(self, action: str, status: str = "ok", **context: Any) -> None:
        """记录操作结束及状态。"""

        logger.bind(**self._bind_context(**context)).info(
            f"{self.prefix} {action} completed ({status})"
        )

    def skipped(self, action: str, reason: str, **context: Any) -> None:
        """记录被跳过的操作（含原因）。"""

        logger.bind(**self._bind_context(**context)).warning(
            f"{self.prefix} {action} skipped ({reason})"
        )

    def failed(self, action: str, error_type: str, detail: str = "", **context: Any) -> None:
        """记录失败操作；级别为 error。"""

        detail_str = f": {detail}" if detail else ""
        logger.bind(**self._bind_context(**context)).error(
            f"{self.prefix} {action} failed ({error_type}{detail_str})"
        )

    def degraded(self, action: str, reason: str, **context: Any) -> None:
        """记录降级操作（如 L3 不可用时只返回 L1/L2）。"""

        logger.bind(**self._bind_context(**context)).warning(
            f"{self.prefix} {action} degraded ({reason})"
        )

    def decision(self, decision: str, allowed: bool, reason: str = "", **context: Any) -> None:
        """记录显式 allow/deny 决策；allowed 用 info，denied 用 warning。"""

        status = "allowed" if allowed else "denied"
        detail = f" ({reason})" if reason else ""
        log_fn = (
            logger.bind(**self._bind_context(**context)).info
            if allowed
            else logger.bind(**self._bind_context(**context)).warning
        )
        log_fn(f"{self.prefix} {decision} {status}{detail}")

    def metric(self, name: str, value: float | int, unit: str = "", **context: Any) -> None:
        """记录指标值；默认 debug 级别，避免生产环境淹没 info 流。"""

        unit_str = unit if unit else ""
        logger.bind(**self._bind_context(**context)).debug(
            f"{self.prefix} metric {name}={value}{unit_str}"
        )

    @contextmanager
    def timed_block(
        self, action: str, warn_threshold_ms: float = 1000, **context: Any
    ) -> Iterator[None]:
        """带耗时的代码块上下文管理器。

        退出时计算 ``duration_ms``，超阈值会把 ``status`` 标为 ``slow``，
        便于后续日志聚合统计慢调用。
        """

        start_ms = time.time_ns() / 1_000_000
        try:
            yield
        finally:
            duration_ms = time.time_ns() / 1_000_000 - start_ms
            status = "slow" if duration_ms > warn_threshold_ms else "ok"
            self.completed(action, status, duration_ms=round(duration_ms, 1), **context)


# Predefined loggers
LOGGER_MEMORY = StructuredLogger("MEMORY")
LOGGER_EMBED = StructuredLogger("EMBED")
LOGGER_STORE = StructuredLogger("STORE")
LOGGER_QUERY = StructuredLogger("QUERY")
