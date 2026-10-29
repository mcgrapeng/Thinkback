"""生产启动路径（lifespan ``run_startup_tasks=True``）的行为测试。

单测历史上全部用 ``run_startup_tasks=False``，于是**生产启动路径从未被执行过**：
迁移、Milvus 建库、多副本前置检查、启动恢复、gRPC 启动、关停全无覆盖。
启动路径坏掉 = Pod 起不来 = 全量故障，而且 CI 不会红。

本文件钉的是「lifespan 调了什么、失败时怎么办」，不是某个桩自身的行为：

- 外部依赖在**调用点**替换。lifespan 内部是函数内 import
  （``from thinkback.infra.database.migration import ...``），所以替换
  源模块属性即可生效 —— 这也顺带证明了 lifespan 用的确实是那几条路径。
- 多副本前置检查**不用桩**：直接开 ``MEMORY_MULTI_REPLICA=true`` 让真实的
  ``assert_multi_replica_ready`` 抛错，证明硬失败是真的会挡住启动。
- 启动恢复跑在后台线程里，用捕获到的 Thread 对象 join，结果确定、不靠 sleep。
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from thinkback.api import app as app_module
from thinkback.api import dependencies as dependencies_module
from thinkback.api.app import create_app
from thinkback.infra.replica_guard import assert_multi_replica_ready


class _FakeBackend:
    """只提供 lifespan 预热用到的那一个方法。"""

    def __init__(self, *, warmup_error: Exception | None = None) -> None:
        self.warmup_error = warmup_error
        self.warmed = False

    def warmup(self) -> None:
        self.warmed = True
        if self.warmup_error is not None:
            raise self.warmup_error


class _FakeRepository:
    def __init__(self) -> None:
        self.closed = False

    def close(self, timeout: float | None = None) -> None:
        _ = timeout
        self.closed = True


class _FakeService:
    """记录 lifespan 对服务的全部调用。"""

    def __init__(
        self,
        *,
        recover_error: Exception | None = None,
        reclaim_error: Exception | None = None,
        warmup_error: Exception | None = None,
    ) -> None:
        self.backend = _FakeBackend(warmup_error=warmup_error)
        self.repository = _FakeRepository()
        self.calls: list[str] = []
        self.recover_error = recover_error
        self.reclaim_error = reclaim_error

    def recover_pending_writes(self) -> int:
        self.calls.append("recover")
        if self.recover_error is not None:
            raise self.recover_error
        return 2

    def reclaim_orphan_running_tasks(self) -> list[str]:
        self.calls.append("reclaim")
        if self.reclaim_error is not None:
            raise self.reclaim_error
        return ["orphan-1"]

    def drain_l3_background_tasks(self, timeout: float | None = None) -> None:
        _ = timeout
        self.calls.append("drain")

    def shutdown_l3_executor(self, wait: bool = True) -> None:
        _ = wait
        self.calls.append("shutdown_executor")


class _FakeGrpcServer:
    def __init__(self) -> None:
        self.started = False
        self.stopped_with_grace: float | None = None

    def start(self) -> None:
        self.started = True

    def stop(self, grace: float | None = None) -> None:
        self.stopped_with_grace = grace


class _Harness:
    """一次启动测试的全部可观测状态。"""

    def __init__(self) -> None:
        self.service = _FakeService()
        self.migration_calls = 0
        self.migration_error: Exception | None = None
        self.milvus_calls: list[dict[str, Any]] = []
        self.milvus_error: Exception | None = None
        self.grpc_created = False
        self.grpc_kwargs: dict[str, Any] = {}
        self.grpc_server = _FakeGrpcServer()
        self.threads: list[threading.Thread] = []


@pytest.fixture()
def harness(monkeypatch: pytest.MonkeyPatch) -> _Harness:
    state = _Harness()

    async def _run_migrations() -> None:
        state.migration_calls += 1
        if state.migration_error is not None:
            raise state.migration_error

    def _ensure_milvus_database(**kwargs: Any) -> None:
        state.milvus_calls.append(kwargs)
        if state.milvus_error is not None:
            raise state.milvus_error

    def _create_server(*args: Any, **kwargs: Any) -> tuple[_FakeGrpcServer, str]:
        _ = args
        state.grpc_created = True
        state.grpc_kwargs = kwargs
        return state.grpc_server, "127.0.0.1:50051"

    # lifespan 内部是函数内 import：替换源模块属性即可接管调用点。
    monkeypatch.setattr(
        "thinkback.infra.database.migration.run_migrations_serialized_async", _run_migrations
    )
    monkeypatch.setattr("thinkback.infra.milvus.ensure_milvus_database", _ensure_milvus_database)
    monkeypatch.setattr("thinkback.rpc.server.create_server", _create_server)
    monkeypatch.setattr(dependencies_module, "get_memory_service", lambda: state.service)
    monkeypatch.setattr(dependencies_module, "_memory_service", state.service)

    # 捕获启动恢复线程：拿到 Thread 对象才能 join，结果不靠 sleep 猜。
    class _CapturingThread(threading.Thread):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            state.threads.append(self)

    monkeypatch.setattr(app_module, "Thread", _CapturingThread)
    return state


def _join_recovery_threads(state: _Harness) -> None:
    """等待启动恢复线程结束并断言它真的结束了。"""
    for thread in state.threads:
        thread.join(timeout=10)
        assert not thread.is_alive(), "启动恢复线程未在超时内结束"


# ---------------------------------------------------------------------------
# 启动阶段
# ---------------------------------------------------------------------------


def test_startup_runs_migrations_and_ensures_milvus_database(harness: _Harness) -> None:
    """生产启动必须先跑迁移再建 Milvus 库：mem0 不会自己建库。"""
    with TestClient(create_app(run_startup_tasks=True)):
        pass

    assert harness.migration_calls == 1
    assert len(harness.milvus_calls) == 1
    assert harness.milvus_calls[0]["milvus_database"] == app_module.settings.milvus_database


def test_startup_fails_when_migration_fails(harness: _Harness) -> None:
    """迁移失败必须让启动失败：结构没对齐就服务流量 = 写出一半的脏数据。"""
    harness.migration_error = RuntimeError("alembic upgrade failed")

    with (
        pytest.raises(RuntimeError, match="alembic upgrade failed"),
        TestClient(create_app(run_startup_tasks=True)),
    ):
        pass


@pytest.mark.usefixtures("harness")
def test_startup_hard_fails_when_multi_replica_prerequisites_are_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """宣称多副本但前置没齐 → 启动期硬失败（用真实 guard，不打桩）。

    静默跑起来的后果：重试被当成新写入、限流额度按副本数放大、
    别的副本追加的轮次本副本看不见。
    """
    monkeypatch.setattr(app_module.settings, "memory_multi_replica", True)
    monkeypatch.setattr(app_module.settings, "memory_l1_cache_enabled", True)
    monkeypatch.setattr(app_module.settings, "memory_history_db_shared", False)

    with (
        pytest.raises(RuntimeError, match="多副本"),
        TestClient(create_app(run_startup_tasks=True)),
    ):
        pass


def test_multi_replica_guard_is_a_noop_for_single_replica() -> None:
    """单副本是当前发布边界：多副本检查直接放行，不报错。"""
    assert_multi_replica_ready(SimpleNamespace(memory_multi_replica=False))


def test_multi_replica_guard_rejects_startup_and_names_what_is_missing() -> None:
    """开了多副本但前置没齐 → 报错要说清缺哪几项、每项不满足会出什么事。

    报错文案是运维唯一的抓手：只说「拒绝启动」等于让人自己猜。
    """
    with pytest.raises(RuntimeError) as excinfo:
        assert_multi_replica_ready(
            SimpleNamespace(
                memory_multi_replica=True,
                memory_l1_cache_enabled=True,
                memory_history_db_shared=False,
            )
        )

    message = str(excinfo.value)
    assert "MEMORY_MULTI_REPLICA" in message
    assert "L1" in message
    assert "history.db" in message


def test_startup_survives_backend_warmup_failure(harness: _Harness) -> None:
    """预热失败不挡启动：预热只是省掉首个请求的 4 秒延迟。"""
    harness.service.backend.warmup_error = RuntimeError("mem0 client broken")

    with TestClient(create_app(run_startup_tasks=True)):
        pass

    assert harness.service.backend.warmed is True
    assert harness.migration_calls == 1


# ---------------------------------------------------------------------------
# 启动恢复（后台线程）
# ---------------------------------------------------------------------------


def test_startup_recovers_pending_writes_then_reclaims_orphans(harness: _Harness) -> None:
    """顺序必须是「先重放待写、再回收孤儿」。

    反了的话：回收会把超龄 RUNNING 标成 failed，之后就认不出「待重放」的工作，
    那条长期记忆永久缺失。
    """
    with TestClient(create_app(run_startup_tasks=True)):
        _join_recovery_threads(harness)

    assert harness.service.calls[:2] == ["recover", "reclaim"]


def test_startup_recovery_failure_does_not_block_boot(harness: _Harness) -> None:
    """恢复失败不挡启动，但**不得**继续回收孤儿。"""
    harness.service.recover_error = RuntimeError("pg unreachable")

    with TestClient(create_app(run_startup_tasks=True)):
        _join_recovery_threads(harness)

    assert "recover" in harness.service.calls
    assert "reclaim" not in harness.service.calls


def test_startup_orphan_reclaim_failure_does_not_block_boot(harness: _Harness) -> None:
    """回收失败不挡启动：恢复已经跑完，回收只是清扫。"""
    harness.service.reclaim_error = RuntimeError("pg unreachable")

    with TestClient(create_app(run_startup_tasks=True)):
        _join_recovery_threads(harness)

    assert harness.service.calls[:2] == ["recover", "reclaim"]


def test_startup_does_not_run_recovery_when_startup_tasks_disabled(
    harness: _Harness,
) -> None:
    """单测模式（run_startup_tasks=False）不碰外部依赖，也不起恢复线程。"""
    with TestClient(create_app(run_startup_tasks=False)):
        pass

    assert harness.migration_calls == 0
    assert harness.milvus_calls == []
    assert harness.threads == []


# ---------------------------------------------------------------------------
# gRPC 与关停
# ---------------------------------------------------------------------------


def test_startup_starts_grpc_server_when_enabled(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """开了 gRPC 就在启动期起 server，参数取自配置。"""
    monkeypatch.setattr(app_module.settings, "grpc_enabled", True)

    with TestClient(create_app(run_startup_tasks=True)):
        assert harness.grpc_server.started is True

    assert harness.grpc_created is True
    assert harness.grpc_kwargs["port"] == app_module.settings.grpc_port


def test_startup_does_not_start_grpc_server_when_disabled(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module.settings, "grpc_enabled", False)

    with TestClient(create_app(run_startup_tasks=True)):
        pass

    assert harness.grpc_created is False


def test_shutdown_drains_l3_closes_repository_and_stops_grpc(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """关停顺序：排空 L3 → 关 executor → 关仓储 → 停 gRPC。

    不排空就关 executor，在途任务永远停在 RUNNING，客户端轮询到天荒地老。
    """
    monkeypatch.setattr(app_module.settings, "grpc_enabled", True)

    with TestClient(create_app(run_startup_tasks=True)):
        pass

    assert harness.service.calls[-2:] == ["drain", "shutdown_executor"]
    assert harness.service.repository.closed is True
    assert harness.grpc_server.stopped_with_grace == (
        app_module.settings.grpc_shutdown_grace_seconds
    )
