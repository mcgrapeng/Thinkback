"""MCP 服务器启动入口。

用法::

    python -m thinkback.mcp                                   # stdio（助手本地拉起进程）
    python -m thinkback.mcp --transport http --port 8765      # streamable-http

namespace 由部署方决定（``--namespace`` 或 ``THINKBACK_MCP_NAMESPACE``），
助手侧无法切换：namespace 是租户/业务分组的边界。

服务只部署在内网，信任边界是网络边界（决策 D10）：不做调用方鉴权。
stdio 传输的进程边界本身就是信任边界；http 传输请只在内网可达地址上监听。

「信任边界是网络边界」这句是部署方的承诺，代码本身验不了。所以绑定到非回环
地址时必须显式加 ``--allow-unauthenticated``：确认外侧已经用 NetworkPolicy
或带鉴权的网关把这个端口收口了，再把服务暴露出去。没有这一步就直接退出，
免得一个忘了收口的部署静默变成「谁都能读写任意记忆」。
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import sys

from thinkback.domain.keys import DEFAULT_NAMESPACE
from thinkback.mcp.server import create_mcp_server


def _is_loopback(host: str) -> bool:
    """这个监听地址是不是只有本机够得着。

    回环地址 = 碰不到别人，不需要额外收口。其余一律当外露处理：主机名
    可能解析到哪去代码不知道，不能拿「大概是回环」当放行理由。
    """
    if host.strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


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
        "--allow-unauthenticated",
        action="store_true",
        default=os.environ.get("THINKBACK_MCP_ALLOW_UNAUTHENTICATED", "").lower()
        in {"1", "true", "yes", "on"},
        help="允许在非回环地址上启动不鉴权的 http 传输。决策 D10 下信任边界是网络边界，"
        "确认外侧已用 NetworkPolicy / 带鉴权的网关收口后再加。",
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

    if not _is_loopback(args.host) and not args.allow_unauthenticated:
        print(
            f"thinkback-mcp: 拒绝在 {args.host} 上启动不鉴权的 MCP 服务。\n"
            "决策 D10：MCP 不做调用方鉴权，信任边界是网络边界。\n"
            "绑定非回环地址前，先用 NetworkPolicy 或带鉴权的网关把这个端口收口，\n"
            "再显式加 --allow-unauthenticated 启动。",
            file=sys.stderr,
        )
        return 2

    import uvicorn

    app = server.streamable_http_app()
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
