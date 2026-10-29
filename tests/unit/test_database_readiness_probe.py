"""数据库 readiness 探针的契约测试。

``infra/database/engine.py`` 的 ``check_database`` 是 k8s readinessProbe 的
底座。它有两个必须钉死的行为：

1. **出错不抛异常**，返回 ``not_ready`` + 异常串。抛异常会让 readiness 端点
   500 而不是 503 —— k8s 看到的都是「探测失败」，但 500 会掩盖真实原因，
   排障时拿到的是栈而不是数据库连接失败的原因。
2. **每次探测用独立连接**（``NullPool``），不把连接放回业务池。这是 R-4 修复
   的核心：连接绑定创建它的事件循环，readiness 在主循环上 checkout 再归还会
   让业务循环拿到错绑连接 → 首个业务请求随机 500。

探针本身此前没有任何直接单测（只被 readiness 端点间接调用过一次成功路径）。
"""

from __future__ import annotations

from typing import Any

import pytest

from thinkback.infra.database import engine as engine_module
from thinkback.infra.database.engine import check_database


class _FakeConnection:
    def __init__(self, *, fail_with: Exception | None = None) -> None:
        self.fail_with = fail_with
        self.executed: list[str] = []
        self.closed = False

    async def execute(self, statement: Any) -> None:
        self.executed.append(str(statement))
        if self.fail_with is not None:
            raise self.fail_with

    async def __aenter__(self) -> _FakeConnection:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self.closed = True


class _FakeEngine:
    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection
        self.connect_calls = 0

    def connect(self) -> _FakeConnection:
        self.connect_calls += 1
        return self.connection


@pytest.fixture()
def fake_engine(monkeypatch: pytest.MonkeyPatch) -> _FakeEngine:
    """把模块里的 readiness 引擎换成一个可观测的替身。"""
    fake = _FakeEngine(_FakeConnection())
    monkeypatch.setattr(engine_module, "readiness_engine", fake)
    return fake


def test_healthy_database_reports_ready(fake_engine: _FakeEngine) -> None:
    result = asyncio_run(check_database())
    assert result == {"status": "ready", "detail": "ok"}
    assert fake_engine.connection.executed == ["SELECT 1"]


def test_probe_closes_the_connection_it_opened(fake_engine: _FakeEngine) -> None:
    """连接用完必须关闭 —— NullPool 也要显式关，否则探测会漏连接。"""
    asyncio_run(check_database())
    assert fake_engine.connection.closed is True


def test_failing_probe_reports_not_ready_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """探测失败返回 not_ready + 原因，**不抛异常**。

    抛异常会让 readiness 端点 500 而不是 503：k8s 看到的都是「探测失败」，
    但 500 掩盖了真实原因 —— 排障拿到的是栈，不是「数据库连不上」。
    """
    failure = ConnectionError("connection refused")
    fake = _FakeEngine(_FakeConnection(fail_with=failure))
    monkeypatch.setattr(engine_module, "readiness_engine", fake)

    result = asyncio_run(check_database())

    assert result["status"] == "not_ready"
    assert "connection refused" in result["detail"]


def test_each_probe_opens_its_own_connection(fake_engine: _FakeEngine) -> None:
    """两次探测各开各的连接 —— 不复用、更不归还业务池。"""
    asyncio_run(check_database())
    asyncio_run(check_database())
    assert fake_engine.connect_calls == 2


def asyncio_run(coro: Any) -> Any:
    """跑一次协程（模块内不引 asyncio.run 以免与事件循环策略纠缠）。"""
    import asyncio

    return asyncio.run(coro)
