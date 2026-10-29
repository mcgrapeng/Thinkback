"""MCP 启动入口：参数解析、传输方式选择与暴露面闸门。

服务只部署在内网，信任边界是网络边界（决策 D10），**不做调用方鉴权**。
stdio 传输的进程边界本身就是信任边界。

http 传输因此多了一道闸门：绑到非回环地址（= 外露）必须显式加
``--allow-unauthenticated``，确认外侧已用 NetworkPolicy 或带鉴权的网关收口。
这条闸门是「免鉴权」能成立的前提 —— 没有它，一个忘了收口的部署会静默变成
「谁都能读写任意记忆」。
"""

from __future__ import annotations

from typing import Any

import pytest

from thinkback.mcp import __main__ as mcp_main


def _patch_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 MCP server 换成假的：只关心 main() 的编排，不碰协议实现。"""

    class _FakeServer:
        def run(self, **kwargs: Any) -> None:
            return None

        def streamable_http_app(self) -> Any:
            return "http-app"

    monkeypatch.setattr(mcp_main, "create_mcp_server", lambda service, namespace: _FakeServer())
    monkeypatch.setattr("thinkback.api.dependencies.get_memory_service", lambda: object())


# ---------------------------------------------------------------------------
# 监听地址判定
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.5", "::1", "localhost", "LOCALHOST"])
def test_loopback_hosts_need_no_extra_opt_in(host: str) -> None:
    assert mcp_main._is_loopback(host) is True


@pytest.mark.parametrize("host", ["0.0.0.0", "10.0.0.5", "example.internal", "192.168.1.10"])
def test_non_loopback_hosts_count_as_exposed(host: str) -> None:
    """主机名可能解析到哪去代码不知道 —— 不能拿「大概是回环」当放行理由。"""
    assert mcp_main._is_loopback(host) is False


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------


def test_parser_defaults_to_stdio_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认 stdio：助手本地拉起进程，进程边界就是信任边界。"""
    monkeypatch.delenv("THINKBACK_MCP_TRANSPORT", raising=False)

    args = mcp_main._build_parser().parse_args([])

    assert args.transport == "stdio"


def test_parser_reads_transport_and_port_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """环境变量是部署方配的入口：命令行没给时用它。"""
    monkeypatch.setenv("THINKBACK_MCP_TRANSPORT", "http")
    monkeypatch.setenv("THINKBACK_MCP_PORT", "9999")

    args = mcp_main._build_parser().parse_args([])

    assert args.transport == "http"
    assert args.port == 9999


def test_parser_reads_the_exposure_opt_in_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """部署方用环境变量声明「外侧已收口」—— 与命令行参数等价。"""
    monkeypatch.setenv("THINKBACK_MCP_ALLOW_UNAUTHENTICATED", "true")

    args = mcp_main._build_parser().parse_args([])

    assert args.allow_unauthenticated is True


# ---------------------------------------------------------------------------
# 启动入口
# ---------------------------------------------------------------------------


def test_main_runs_the_server_over_stdio(monkeypatch: pytest.MonkeyPatch) -> None:
    """stdio 直接跑 server，不起 http，也不过暴露面闸门。"""
    runs: list[dict[str, Any]] = []

    class _FakeServer:
        def run(self, **kwargs: Any) -> None:
            runs.append(kwargs)

    monkeypatch.setattr(mcp_main, "create_mcp_server", lambda service, namespace: _FakeServer())
    monkeypatch.setattr("thinkback.api.dependencies.get_memory_service", lambda: object())

    exit_code = mcp_main.main(["--transport", "stdio"])

    assert exit_code == 0
    assert runs == [{"transport": "stdio"}]


def test_main_serves_the_http_app_on_loopback_without_an_auth_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回环地址直接起 ASGI app：内网部署，不加鉴权包装。"""
    served: list[Any] = []
    _patch_server(monkeypatch)
    monkeypatch.setattr(
        "uvicorn.run",
        lambda app, host, port: served.append((app, host, port)),
    )

    exit_code = mcp_main.main(["--transport", "http", "--host", "127.0.0.1", "--port", "1234"])

    assert exit_code == 0
    app, host, port = served[0]
    assert app == "http-app"
    assert (host, port) == ("127.0.0.1", 1234)


def test_main_refuses_to_expose_an_unauthenticated_http_transport(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """绑到非回环地址又不显式声明 → 直接退出，不起服务。

    这是「免鉴权」能成立的前提：忘了收口的部署不会静默变成谁都能读写记忆。
    """
    served: list[Any] = []
    _patch_server(monkeypatch)
    monkeypatch.setattr(
        "uvicorn.run",
        lambda app, host, port: served.append(app),
    )

    exit_code = mcp_main.main(["--transport", "http", "--host", "0.0.0.0", "--port", "1234"])

    assert exit_code == 2
    assert served == []
    assert "--allow-unauthenticated" in capsys.readouterr().err


def test_main_serves_an_exposed_http_transport_after_the_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """显式声明「外侧已收口」之后，外露地址可以起服务。"""
    served: list[Any] = []
    _patch_server(monkeypatch)
    monkeypatch.setattr(
        "uvicorn.run",
        lambda app, host, port: served.append((app, host, port)),
    )

    exit_code = mcp_main.main(
        [
            "--transport",
            "http",
            "--host",
            "0.0.0.0",
            "--port",
            "1234",
            "--allow-unauthenticated",
        ]
    )

    assert exit_code == 0
    app, host, port = served[0]
    assert app == "http-app"
    assert (host, port) == ("0.0.0.0", 1234)
