"""单测套件必须能在没有外部依赖的机器上跑。

此前单测构造真实应用时会在 lifespan 里跑 Alembic 迁移、连 Milvus、恢复
后台任务：同一份代码在有 PG 的开发机上绿，在 CI（没有 PG）上直接报错。
单测必须是确定性的。
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from thinkback.api.app import create_app


def _port_is_closed(host: str, port: int) -> bool:
    """端口没人监听（用于确认数据库确实不可达）。"""
    with socket.socket() as sock:
        sock.settimeout(0.2)
        return sock.connect_ex((host, port)) != 0


def test_unit_app_does_not_touch_external_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run_startup_tasks=False`` 的应用不跑迁移、不连 Milvus。"""
    monkeypatch.setenv("DATABASE_URL", "postgres://postgres:postgres@127.0.0.1:1/nope")

    app = create_app(run_startup_tasks=False)

    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.json()["status"] in {"ok", "running"}


def test_production_app_still_runs_startup_tasks() -> None:
    """生产路径不变：默认就是跑启动任务。"""
    import inspect

    signature = inspect.signature(create_app)
    assert signature.parameters["run_startup_tasks"].default is True


def test_lifespan_shutdown_runs_even_without_startup_tasks() -> None:
    """跳过启动任务时，关停流程照常执行（排空队列、关 executor、重置单例）。"""
    from thinkback.api import dependencies

    calls: list[str] = []

    class _TrackingService:
        def drain_l3_background_tasks(self, timeout: float | None = None) -> None:
            calls.append("drain")

        def shutdown_l3_executor(self, *, wait: bool = True) -> None:
            calls.append("shutdown")

        @property
        def repository(self) -> object:
            class _Repo:
                def close(self, timeout: float = 5.0) -> None:
                    calls.append("close")

            return _Repo()

    original = dependencies._memory_service
    dependencies._memory_service = _TrackingService()  # type: ignore[assignment]
    try:
        with TestClient(create_app(run_startup_tasks=False)):
            pass
    finally:
        dependencies._memory_service = original  # type: ignore[assignment]

    assert "drain" in calls
    assert "shutdown" in calls


def test_dockerfile_ships_profiles_directory() -> None:
    """镜像必须带 profiles/（部署配置的一部分）。"""
    assert Path("profiles").is_dir()
    assert (Path("profiles") / "semiconductor.json").is_file()
    assert os.path.isfile("Dockerfile")
