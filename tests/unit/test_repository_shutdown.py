"""仓储/服务生命周期关闭后的行为契约。

回归背景（2026-09 生产排查）：lifespan shutdown 关闭 repository 后，
``_memory_service`` 单例不重置 + ``_run`` 拒绝路径不关闭协程，导致：
1. 同进程第二次 lifespan 启动时 reclaim 打到已关闭仓储，协程被丢弃
   （RuntimeWarning: never awaited）；
2. 生产关机竞态中 L3 straggler 的 save_task 同样泄漏协程且任务停留 RUNNING。
"""

from __future__ import annotations

import gc
import warnings

import pytest

from thinkback.api import dependencies
from thinkback.api.app import create_app
from thinkback.memory.repositories import SqlAlchemyMemoryRepository


def _never_awaited_warnings(record: list[warnings.WarningMessage]) -> list[str]:
    return [str(w.message) for w in record if "never awaited" in str(w.message)]


def test_run_rejects_closed_repository_without_leaking_coroutine() -> None:
    """关闭后的仓储调用应 fail-fast（RuntimeError），且不泄漏未关闭协程。"""

    repository = SqlAlchemyMemoryRepository()
    repository.close()
    with (
        pytest.raises(RuntimeError, match="closed"),
        warnings.catch_warnings(record=True) as record,
    ):
        warnings.simplefilter("always")
        repository.reclaim_stale_running_tasks(max_age_seconds=60.0)
    gc.collect()
    assert _never_awaited_warnings(record) == []


def test_lifespan_shutdown_resets_service_singleton() -> None:
    """lifespan 退出后单例必须重置：下一次生命周期构造全新服务，而非复用已关闭仓储。"""

    from fastapi.testclient import TestClient

    with TestClient(create_app()):
        first = dependencies._memory_service
        assert first is not None
    assert dependencies._memory_service is None

    with TestClient(create_app()):
        second = dependencies._memory_service
        assert second is not None
        assert second is not first
        assert not second.repository._loop.is_closed()  # noqa: SLF001
