"""API 接入面此前无测试触达的行。

覆盖目标：
- ``api.app``：关停时 L3 drain 抛错、DB engine dispose 抛错都不能挡住关停；
  管理台 CORS 中间件按配置注册。
- ``api.dependencies``：P0 槽位白名单提示与 namespace 边界提示要真的说给运维听。
- ``api.errors_standard``：错误体带 details；路由已标准化的错误体透传；
  响应头透传；未捕获异常统一 500。
- ``api.idempotency``：请求没有 query_params 时作用域为空；响应没有 body_iter
  时回落到 response.body。
- ``api.metrics``：快照文本钩子的默认时间。
- ``api.trusted_user``：读不到 body 时按「未自报身份」处理。

不碰网络、不连真实 DB：lifespan 用替身服务与替身引擎，HTTP 层用 TestClient。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from loguru import logger

from thinkback.api import app as app_module
from thinkback.api import dependencies as dependencies_module
from thinkback.api.app import create_app
from thinkback.api.errors_standard import error_response, register_exception_handlers
from thinkback.api.metrics import MetricsRegistry

# ── 关停路径：异常不得挡住关停 ───────────────────────────────────────


class _FakeRepository:
    def __init__(self) -> None:
        self.closed = False

    def close(self, timeout: float | None = None) -> None:
        _ = timeout
        self.closed = True


class _FakeService:
    """drain 会抛错；其余调用只记录。"""

    def __init__(self) -> None:
        self.repository = _FakeRepository()
        self.calls: list[str] = []

    def drain_l3_background_tasks(self, timeout: float | None = None) -> None:
        _ = timeout
        self.calls.append("drain")
        raise RuntimeError("drain blew up")

    def shutdown_l3_executor(self, wait: bool = True) -> None:
        _ = wait
        self.calls.append("shutdown_executor")


class _FailingEngine:
    async def dispose(self) -> None:
        raise RuntimeError("dispose failed")


def test_shutdown_survives_a_failing_l3_drain(monkeypatch: pytest.MonkeyPatch) -> None:
    """drain 把在途任务的失败抛出来，但不能挡住 executor 关闭（否则 SIGTERM 挂死）。"""
    service = _FakeService()
    monkeypatch.setattr(dependencies_module, "_memory_service", service)

    with TestClient(create_app(run_startup_tasks=False)):
        pass

    assert "shutdown_executor" in service.calls
    assert service.repository.closed is True


def test_shutdown_survives_a_failing_engine_dispose(monkeypatch: pytest.MonkeyPatch) -> None:
    """DB 连接池 dispose 失败只记警告：关停流程要走到最后（重置单例）。"""
    from thinkback.infra.database import engine as engine_module

    monkeypatch.setattr(engine_module, "engine", _FailingEngine())
    monkeypatch.setattr(engine_module, "readiness_engine", _FailingEngine())

    with TestClient(create_app(run_startup_tasks=False)):
        pass

    assert dependencies_module._memory_service is None


def test_admin_cors_middleware_is_registered_when_origins_are_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """配置了管理台来源就装 CORS 中间件：浏览器端管理台要能跨域调。"""
    monkeypatch.setattr(app_module.settings, "admin_cors_allow_origins", "https://admin.example")

    app = create_app(run_startup_tasks=False)

    with TestClient(app) as client:
        response = client.get("/health/live", headers={"Origin": "https://admin.example"})

    assert response.headers.get("access-control-allow-origin") == "https://admin.example"


# ── 服务初始化：运维提示要真的说出口 ─────────────────────────────────


@pytest.fixture()
def warnings() -> Iterator[list[str]]:
    sink: list[str] = []
    handler_id = logger.add(sink.append, level="WARNING", format="{message} {extra}")
    try:
        yield sink
    finally:
        logger.remove(handler_id)


def test_p0_slots_and_namespace_warnings_are_logged_at_service_startup(
    monkeypatch: pytest.MonkeyPatch,
    warnings: list[str],
) -> None:
    """白名单与 namespace 边界的告警必须真的出现在日志里（否则运维看不到）。"""
    monkeypatch.setattr(dependencies_module, "_memory_service", None)
    # 两条提示都是 Settings 的派生属性（property 没有 setter），
    # 所以改它们背后的字段：白名单非空 → 有 P0 提示；生产环境且没设
    # namespace 白名单 → 有 namespace 边界提示。
    monkeypatch.setattr(dependencies_module.settings, "memory_p0_slots", "current_location,food")
    monkeypatch.setattr(dependencies_module.settings, "environment", "production")
    monkeypatch.setattr(dependencies_module.settings, "memory_allowed_namespaces", "")
    monkeypatch.setattr(
        dependencies_module, "SqlAlchemyMemoryRepository", lambda **kwargs: object()
    )
    monkeypatch.setattr(dependencies_module, "get_memory_backend", lambda settings: object())

    try:
        dependencies_module.get_memory_service()
    finally:
        dependencies_module._memory_service = None

    assert any("MEMORY_P0_SLOTS is set to" in line for line in warnings)
    assert any("MEMORY_ALLOWED_NAMESPACES is not set" in line for line in warnings)


# ── 标准化错误体 ──────────────────────────────────────────────────────


def test_error_response_carries_the_details_block() -> None:
    """带 details 的错误要把 details 放进 error 体里（调用方要能定位到具体字段）。"""
    response = error_response(
        code="semantic",
        message="bad request",
        request_id="req-1",
        status_code=400,
        details={"field": "user_id"},
    )

    body = json.loads(response.body)
    assert body["error"]["details"] == {"field": "user_id"}
    assert body["detail"] == "bad request"


def _app_with_handlers() -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/already-standard")
    async def _already_standard() -> None:
        raise HTTPException(
            status_code=409,
            detail={"error": {"code": "conflict", "message": "已标准化"}},
        )

    @app.get("/with-headers")
    async def _with_headers() -> None:
        raise HTTPException(status_code=405, detail="method not allowed", headers={"Allow": "GET"})

    @app.get("/boom")
    async def _boom() -> None:
        raise RuntimeError("unexpected")

    return app


def test_an_already_standardized_error_body_is_passed_through() -> None:
    """路由自己给过标准错误体时不再包一层（否则 code/message 被吃掉）。"""
    with TestClient(_app_with_handlers(), raise_server_exceptions=False) as client:
        response = client.get("/already-standard")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"


def test_the_original_response_headers_are_preserved() -> None:
    """405 的 Allow 头这类合规头必须透传，否则标准化错误体会把合规响应变成违约响应。"""
    with TestClient(_app_with_handlers(), raise_server_exceptions=False) as client:
        response = client.get("/with-headers")

    assert response.status_code == 405
    assert response.headers.get("allow") == "GET"


def test_an_unhandled_exception_becomes_a_500_without_leaking_details() -> None:
    """未捕获异常统一 500：响应里不能出现异常原文（可能带用户数据）。"""
    with TestClient(_app_with_handlers(), raise_server_exceptions=False) as client:
        response = client.get("/boom")

    assert response.status_code == 500
    assert "unexpected" not in response.text
    assert response.json()["error"]["code"]


# ── 幂等中间件 ────────────────────────────────────────────────────────


def test_a_request_without_query_params_has_an_empty_caller_identity() -> None:
    """没有 query_params 的请求拿不到调用方身份：不能拿别的维度兜底。"""
    from thinkback.api.idempotency import _caller_identity_from_query

    class _Bare:
        pass

    assert _caller_identity_from_query(_Bare()) == ""


async def test_an_idempotent_response_without_a_body_stream_uses_the_response_body() -> None:
    """响应没有 body_iter 时回落到 response.body：缓存键要算得出来。

    BaseHTTPMiddleware 正常会给 body_iter；这条分支是给「框架直接返回普通
    Response」的形态兜底（Starlette 版本差异），不能因此缓存出一个空响应。
    """
    from starlette.requests import Request
    from starlette.responses import Response

    from thinkback.api.idempotency import IdempotencyCache, IdempotencyMiddleware

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v2/memory/a",
        "raw_path": b"/v2/memory/a",
        "query_string": b"",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 1234),
        "headers": [(b"idempotency-key", b"key-1"), (b"content-type", b"application/json")],
    }
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": b"{}", "more_body": False}

    request = Request(scope, receive)
    middleware = IdempotencyMiddleware(app=None, cache=IdempotencyCache())  # type: ignore[arg-type]

    async def call_next(_request: Request) -> Response:
        return Response(content=b'{"route": "a"}', media_type="application/json")

    response = await middleware.dispatch(request, call_next)

    assert response.body == b'{"route": "a"}'


# ── 指标快照钩子 ──────────────────────────────────────────────────────


def test_the_metrics_snapshot_hook_uses_a_default_timestamp() -> None:
    """不传时间时用当前时刻渲染快照（测试钩子本身也要能自洽）。"""
    registry = MetricsRegistry()

    text = registry._snapshot_text()  # noqa: SLF001

    assert text.endswith("\n")
    assert "thinkback_request_total" in text


# ── 受信用户绑定 ──────────────────────────────────────────────────────


async def test_a_body_that_cannot_be_read_counts_as_no_claimed_identity() -> None:
    """读不到 body 时按「未自报身份」处理：不能因此把请求当成可信用户放行。"""
    from thinkback.api.trusted_user import TrustedUserHeaderMiddleware

    class _Request:
        method = "POST"
        query_params: Any = {}

        async def body(self) -> bytes:
            raise RuntimeError("body stream broken")

    middleware = TrustedUserHeaderMiddleware(app=None, header_name="X-User-Id")  # type: ignore[arg-type]

    assert await middleware._claimed_user_id(_Request()) is None  # noqa: SLF001
