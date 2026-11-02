"""受信调用方身份头（D1 接入层身份绑定）的 HTTP 面。

为什么需要它：D10（内网免鉴权）下 user_id 由调用方自报，接到服务上的
**任何一个**调用方都能读写任意用户的记忆 —— 这在内网单租户时成立，在
「被任何企业 AI 助手接入」的插件形态下是唯一的越权面。

启用方式：``TRUSTED_USER_HEADER=X-User-Id``（网关注入该头）。启用后：

- 请求体 / 查询串里的 ``user_id`` 必须与受信头**完全一致**，不一致 403；
- 头缺失也 403（配置承诺了绑定，缺失说明网关没配对，fail-closed）。

为什么是「校验」而不是「覆盖」：静默用头里的值覆盖请求值，会掩盖
「助手以为在写 A、实际写了 B」的配置错误 —— 响亮拒绝让错配当场暴露。
MCP 面的同能力是启动参数 ``--user-id``（``mcp/__main__.py``），gRPC 面是
``rpc/servicer.py`` 的 ``_check_trusted_user``：三面同一语义。
"""

from __future__ import annotations

import json
from typing import Any

from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp


class TrustedUserHeaderMiddleware(BaseHTTPMiddleware):
    """校验 ``/v2/memory/*`` 的 user_id 与受信头一致；未启用时零开销直通。"""

    def __init__(self, app: ASGIApp, header_name: str) -> None:
        super().__init__(app)
        self.header_name = header_name

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        trusted = (request.headers.get(self.header_name) or "").strip()
        if not trusted:
            return self._forbidden(request, f"trusted user header {self.header_name!r} is required")
        claimed = await self._claimed_user_id(request)
        if claimed is not None and claimed != trusted:
            return self._forbidden(
                request,
                f"user_id does not match the trusted {self.header_name!r} header",
            )
        return await call_next(request)

    async def _claimed_user_id(self, request: Request) -> str | None:
        """调用方自报的 user_id：JSON 请求体优先，GET 退回查询串。"""

        query_user = (request.query_params.get("user_id") or "").strip()
        if request.method == "GET":
            return query_user or None
        try:
            body = await request.body()
        except Exception:  # noqa: BLE001 - 读不到 body 时按未自报处理
            return query_user or None
        # BaseHTTPMiddleware 会把 body 缓下来向下游重放，这里读一次是安全的。
        claimed: Any = None
        if body:
            try:
                payload = json.loads(body)
                claimed = payload.get("user_id") if isinstance(payload, dict) else None
            except (json.JSONDecodeError, UnicodeDecodeError):
                claimed = None
        if claimed is None:
            return query_user or None
        return str(claimed).strip() or None

    @staticmethod
    def _forbidden(request: Request, message: str) -> JSONResponse:
        logger.warning(f"trusted_user.rejected path={request.url.path} reason={message}")
        return JSONResponse(
            status_code=403,
            content={
                "error": {
                    "code": "TB-1003",
                    "message": message,
                    "request_id": getattr(request.state, "request_id", "unknown"),
                }
            },
        )
