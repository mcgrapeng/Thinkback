"""MCP 启动入口：参数解析与 http 传输的 Bearer 校验。

MCP server 是助手侧直接连的服务。http 传输下的 Bearer 校验是唯一的对外
防线，它此前一次都没被执行过 —— 校验写错了就是「看起来配了 token，实际
谁都能连」。

另两条契约：
- stdio 传输不需要 token：进程边界本身就是信任边界；
- 没配 token 的 http 传输**不鉴权**（只在受信内网用）—— 这是刻意的，
  但必须显式，不许悄悄变成「有 token 也不校验」。
"""

from __future__ import annotations

from typing import Any

import pytest

from thinkback.mcp import __main__ as mcp_main
from thinkback.mcp.__main__ import _BearerTokenMiddleware


async def _drive(middleware: Any, scope: dict[str, Any]) -> list[dict[str, Any]]:
    """跑一次 ASGI 调用，返回发出去的消息。"""
    sent: list[dict[str, Any]] = []

    async def _receive() -> dict[str, Any]:
        return {"type": "http.request"}

    async def _send(message: dict[str, Any]) -> None:
        sent.append(message)

    await middleware(scope, _receive, _send)
    return sent


def _scope(authorization: str | None) -> dict[str, Any]:
    headers = [] if authorization is None else [(b"authorization", authorization.encode())]
    return {"type": "http", "headers": headers}


# ---------------------------------------------------------------------------
# Bearer 校验
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bearer_middleware_rejects_a_request_without_authorization() -> None:
    """没带 token → 401，不得落到应用里。"""
    reached: list[str] = []

    async def _app(scope: Any, receive: Any, send: Any) -> None:
        _ = (scope, receive, send)
        reached.append("app")

    sent = await _drive(_BearerTokenMiddleware(_app, "secret-token"), _scope(None))

    assert reached == []
    assert sent[0]["status"] == 401


@pytest.mark.asyncio
async def test_bearer_middleware_rejects_a_wrong_token() -> None:
    """token 不对 → 401。"""
    reached: list[str] = []

    async def _app(scope: Any, receive: Any, send: Any) -> None:
        _ = (scope, receive, send)
        reached.append("app")

    sent = await _drive(_BearerTokenMiddleware(_app, "secret-token"), _scope("Bearer wrong-token"))

    assert reached == []
    assert sent[0]["status"] == 401


@pytest.mark.asyncio
async def test_bearer_middleware_rejects_a_non_bearer_authorization_header() -> None:
    """Authorization 头格式不对（Basic / 裸 token）→ 401。"""
    reached: list[str] = []

    async def _app(scope: Any, receive: Any, send: Any) -> None:
        _ = (scope, receive, send)
        reached.append("app")

    sent = await _drive(_BearerTokenMiddleware(_app, "secret-token"), _scope("Basic secret-token"))

    assert reached == []
    assert sent[0]["status"] == 401


@pytest.mark.asyncio
async def test_bearer_middleware_lets_the_correct_token_through() -> None:
    """token 对 → 放行到应用。"""
    reached: list[str] = []

    async def _app(scope: Any, receive: Any, send: Any) -> None:
        _ = (scope, receive, send)
        reached.append("app")

    sent = await _drive(_BearerTokenMiddleware(_app, "secret-token"), _scope("Bearer secret-token"))

    assert reached == ["app"]
    assert sent == []


@pytest.mark.asyncio
async def test_bearer_middleware_ignores_non_http_scopes() -> None:
    """非 http 的 scope（lifespan / websocket）不校验：它们不是数据面请求。"""
    reached: list[str] = []

    async def _app(scope: Any, receive: Any, send: Any) -> None:
        _ = (scope, receive, send)
        reached.append("app")

    await _drive(_BearerTokenMiddleware(_app, "secret-token"), {"type": "lifespan", "headers": []})

    assert reached == ["app"]


# ---------------------------------------------------------------------------
# 启动入口
# ---------------------------------------------------------------------------


def test_parser_defaults_to_stdio_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认 stdio：助手本地拉起进程，进程边界就是信任边界。"""
    monkeypatch.delenv("THINKBACK_MCP_TRANSPORT", raising=False)

    args = mcp_main._build_parser().parse_args([])

    assert args.transport == "stdio"
    assert args.token == ""


def test_parser_reads_transport_and_token_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """环境变量是部署方配的入口：命令行没给时用它。"""
    monkeypatch.setenv("THINKBACK_MCP_TRANSPORT", "http")
    monkeypatch.setenv("THINKBACK_MCP_TOKEN", "env-token")
    monkeypatch.setenv("THINKBACK_MCP_PORT", "9999")

    args = mcp_main._build_parser().parse_args([])

    assert args.transport == "http"
    assert args.token == "env-token"
    assert args.port == 9999


def test_main_runs_the_server_over_stdio_without_a_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stdio 不需要 token，也不该走 http 那套鉴权包装。"""
    runs: list[dict[str, Any]] = []

    class _FakeServer:
        def run(self, **kwargs: Any) -> None:
            runs.append(kwargs)

    monkeypatch.setattr(mcp_main, "create_mcp_server", lambda service, namespace: _FakeServer())
    monkeypatch.setattr("thinkback.api.dependencies.get_memory_service", lambda: object())

    exit_code = mcp_main.main(["--transport", "stdio"])

    assert exit_code == 0
    assert runs == [{"transport": "stdio"}]


def test_main_wraps_the_http_app_with_the_bearer_middleware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """配了 token 就必须真的包上鉴权：不然 http 传输等于裸奔。"""
    served: list[Any] = []

    class _FakeServer:
        def streamable_http_app(self) -> Any:
            return "http-app"

    monkeypatch.setattr(mcp_main, "create_mcp_server", lambda service, namespace: _FakeServer())
    monkeypatch.setattr("thinkback.api.dependencies.get_memory_service", lambda: object())
    monkeypatch.setattr(
        "uvicorn.run",
        lambda app, host, port: served.append((app, host, port)),
    )

    exit_code = mcp_main.main(
        ["--transport", "http", "--token", "cli-token", "--host", "0.0.0.0", "--port", "1234"]
    )

    assert exit_code == 0
    app, host, port = served[0]
    assert isinstance(app, mcp_main._BearerTokenMiddleware)
    assert app.token == "cli-token"
    assert (host, port) == ("0.0.0.0", 1234)


def test_main_does_not_add_auth_when_no_token_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没配 token 就不鉴权（受信内网）：但也不能加一个空 token 的包装。"""
    served: list[Any] = []

    class _FakeServer:
        def streamable_http_app(self) -> Any:
            return "http-app"

    monkeypatch.setattr(mcp_main, "create_mcp_server", lambda service, namespace: _FakeServer())
    monkeypatch.setattr("thinkback.api.dependencies.get_memory_service", lambda: object())
    monkeypatch.setattr(
        "uvicorn.run",
        lambda app, host, port: served.append(app),
    )

    exit_code = mcp_main.main(["--transport", "http", "--token", ""])

    assert exit_code == 0
    assert served[0] == "http-app"


# ---------------------------------------------------------------------------
# 测试辅助
# ---------------------------------------------------------------------------
