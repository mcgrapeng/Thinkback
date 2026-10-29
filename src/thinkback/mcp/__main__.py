"""MCP 服务器启动入口。

用法::

    python -m thinkback.mcp                                   # stdio（助手本地拉起进程）
    python -m thinkback.mcp --transport http --port 8765      # streamable-http

namespace 由部署方决定（``--namespace`` 或 ``THINKBACK_MCP_NAMESPACE``），
助手侧无法切换：namespace 是租户/业务分组的边界。

http 传输下可配 ``--token`` / ``THINKBACK_MCP_TOKEN``，未配置时服务**不鉴权**
（只适合受信内网）。stdio 传输不需要 token：进程边界本身就是信任边界。
"""

from __future__ import annotations

import argparse
import os
import sys

from starlette.types import ASGIApp, Receive, Scope, Send

from thinkback.domain.keys import DEFAULT_NAMESPACE
from thinkback.mcp.server import create_mcp_server


class _BearerTokenMiddleware:
    """给 streamable-http 加一道 Bearer 校验。

    没配 token 时不启用（保持内网零配置可用）；配了就必须带对。
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            if headers.get("authorization") != f"Bearer {self.token}":
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [(b"content-type", b"text/plain; charset=utf-8")],
                    }
                )
                await send({"type": "http.response.body", "body": b"unauthorized"})
                return
        await self.app(scope, receive, send)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="thinkback-mcp", description="Thinkback MCP 服务器")
    parser.add_argument(
        "--namespace",
        default=os.environ.get("THINKBACK_MCP_NAMESPACE", DEFAULT_NAMESPACE),
        help="记忆作用域标签；一个服务器实例对应一个 namespace。",
    )
    parser.add_argument(
        "--transport",
        default=os.environ.get("THINKBACK_MCP_TRANSPORT", "stdio"),
        choices=["stdio", "http"],
        help="传输方式：stdio（本地进程）或 http（streamable-http）。",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("THINKBACK_MCP_HOST", "127.0.0.1"),
        help="http 传输的监听地址。",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("THINKBACK_MCP_PORT", "8765")),
        help="http 传输的监听端口。",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("THINKBACK_MCP_TOKEN", ""),
        help="http 传输的 Bearer token；留空表示不鉴权（仅限受信内网）。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    # 服务构造放在导入之后、运行之前：import 失败要立刻报错，不能等到
    # 客户端连上来才发现。
    from thinkback.api.dependencies import get_memory_service

    service = get_memory_service()
    server = create_mcp_server(service, namespace=args.namespace)

    if args.transport == "stdio":
        server.run(transport="stdio")
        return 0

    import uvicorn

    app = server.streamable_http_app()
    if args.token:
        app = _BearerTokenMiddleware(app, args.token)  # type: ignore[assignment]
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
