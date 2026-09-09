"""Request trace id helpers.

负责两件事：
1. 集中配置 loguru 输出格式（trace_id + level + 位置 + 消息 + extra）；
2. 提供 trace_id 的 ContextVar：每个请求在中间件中设置，请求结束后清空，
   让每条日志都自动携带 trace_id，方便分布式链路追踪。

注意：``configure_logging`` 使用 ``@lru_cache(maxsize=1)`` 防止重复初始化覆盖
先前注入的 sink；典型调用入口是 ``create_app``。
"""

from __future__ import annotations

import sys
from contextvars import ContextVar
from functools import lru_cache
from typing import Any
from uuid import uuid4

from loguru import logger

from innies_memory.infra.config import settings

_trace_id: ContextVar[str | None] = ContextVar("trace_id", default=None)


@lru_cache(maxsize=1)
def configure_logging() -> None:
    """配置 loguru：默认 sink 输出到 stderr，并 patch 注入 trace_id。

    重复调用会被 ``lru_cache`` 短路，避免覆盖运行时动态添加的 sink。
    """

    logger.remove()
    logger.configure(patcher=patch_log_record)
    logger.add(
        sys.stderr,
        level=settings.log_level,
        format=(
            "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | trace_id={trace_id} | "
            "{name}:{function}:{line} - {message} | {extra}"
        ),
        enqueue=True,
        backtrace=False,
        diagnose=False,
    )
    logger.bind(
        log_level=settings.log_level,
        sink="stderr",
    ).info("loguru configured")


def patch_log_record(record: Any) -> None:
    """把 ContextVar 中的 trace_id 注入到日志记录的格式化字段。"""

    record["trace_id"] = get_trace_id() or "-"
    record["extra"].pop("trace_id", None)


def set_trace_id(incoming_trace_id: str | None = None) -> str:
    """设置当前请求的 trace_id；返回最终生效的值（可能是新生成的）。"""

    trace_id = incoming_trace_id or uuid4().hex
    _trace_id.set(trace_id)
    return trace_id


def get_trace_id() -> str | None:
    """读取当前 ContextVar 中的 trace_id。"""

    return _trace_id.get()


def clear_trace_id() -> None:
    """清空当前 ContextVar 中的 trace_id，避免污染下一个请求。"""

    _trace_id.set(None)
