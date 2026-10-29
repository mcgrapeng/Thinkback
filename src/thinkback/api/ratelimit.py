"""限流中间件 — Token Bucket 算法。

内网部署（方案二）：业务面免鉴权，因此不再按 API key 分桶，而是按**调用方**
分桶（请求体里的 ``namespace`` + ``user_id``，取不到才退回对端 IP），
防止失控重试与脚本打爆记忆服务。超限返回 429 TB-1007 + Retry-After 头。

为什么不按 IP 分桶：服务化接入时所有调用方都从同一个出口 IP 出来
（k8s / ingress / sidecar），一个 IP 一个桶等于全服务共享一份额度 ——
一个多租户调用方（每个租户一次召回 + 一次写入）会被**别的租户**挤到 429。
分桶单位应该是「谁在用」，不是「从哪台机器出来的」。
"""

from __future__ import annotations

import time

from fastapi import Request, Response
from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from thinkback.api.idempotency import _caller_identity
from thinkback.infra.token_bucket import BucketRegistry

# 内网默认限流：单调用方 600 请求/分钟、突发 50。替代原先按 API key plan 的配额。
_RATE_LIMIT_PER_MINUTE = 600
_RATE_LIMIT_BURST = 50
# 按对端 IP 的天花板 = 每调用方限额的多少倍。分桶键（namespace + user_id）
# 是调用方给的，轮换 key 就换一把新桶 —— 天花板按 IP 分桶，轮换换不掉它。
# 20 倍：够一个网关后面几十个租户共用，又能把「换个 user_id 就重来」的
# 洪水按住。
_PEER_CEILING_FACTOR = 20


class RateLimitMiddleware(BaseHTTPMiddleware):
    """按调用方分桶的限流中间件。

    覆盖 /v2/memory/*（业务面）与 /admin/api/*（治理面）—— 治理面能删任意
    用户的全部记忆、列出全库，它的破坏力不比业务面小，不能因为「没有鉴权」
    就把速率一并放开。/health、/docs 等不走限流。
    """

    def __init__(
        self,
        app: ASGIApp,
        exclude_paths: set[str] | None = None,
        *,
        rate_limit_per_minute: int = _RATE_LIMIT_PER_MINUTE,
        burst: int = _RATE_LIMIT_BURST,
    ) -> None:
        super().__init__(app)
        self.exclude_paths = exclude_paths or {
            "/health",
            "/docs",
            "/redoc",
            "/openapi.json",
        }
        self.rate_limit_per_minute = rate_limit_per_minute
        self.burst = burst
        # 每调用方一把桶：租户之间互不挤兑。
        self.buckets = BucketRegistry(rate_limit_per_minute, burst)
        # 再压一层按对端 IP 的天花板：轮换分桶键绕不开它。
        self.peer_buckets = BucketRegistry(
            rate_limit_per_minute * _PEER_CEILING_FACTOR,
            burst * _PEER_CEILING_FACTOR,
        )

    @staticmethod
    async def _resolve_client_id(request: Request) -> str:
        """限流分桶的调用方标识。

        业务面免鉴权时，最接近「调用方」的标识是这次请求**为谁而写**：
        请求体里的 ``namespace`` + ``user_id`` —— 与幂等层用的是同一口径
        （``api/idempotency.py`` 的 ``_caller_identity``），两个中间件对
        「谁」的理解不能各说各话。

        取不到（没有这个字段的端点、GET 请求、非 JSON 请求体）才退回对端 IP。

        读请求体是安全的：Starlette 的 ``BaseHTTPMiddleware`` 用
        ``_CachedRequest`` 把 body 缓下来并向下游重放，下游中间件拿到的
        仍然是完整请求体。
        """

        try:
            body = await request.body()
        except Exception:  # noqa: BLE001 - 读不到 body 就按对端 IP 分桶
            body = b""
        identity = _caller_identity(body)
        if identity:
            return identity
        return RateLimitMiddleware._peer_ip(request)

    @staticmethod
    def _peer_ip(request: Request) -> str:
        """对端 IP：没有可信调用方标识时的兜底分桶键。"""

        return request.client.host if request.client and request.client.host else "unknown"

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        if path.startswith("/admin/api/"):
            # 管理面按**对端 IP**分桶：它没有 namespace/user_id 语义，
            # 按请求体分桶会跟业务面共用同一把桶 —— 管理面的调用会吃掉那个
            # 用户的业务额度，反过来也一样。「admin:」前缀是为了跟业务面
            # 退回 IP 时的桶键也不撞。
            client_id = f"admin:{self._peer_ip(request)}"
        elif path.startswith("/v2/memory/"):
            if path in self.exclude_paths:
                return await call_next(request)
            client_id = await self._resolve_client_id(request)
        else:
            # health / docs / openapi 走各自通道。
            return await call_next(request)

        # 先扣每调用方的桶：超了就是超了，不该再去动共享的天花板。
        ok, wait = self.buckets.consume(client_id)
        if ok:
            # 再扣按对端 IP 的天花板：轮换分桶键（换 user_id）绕不开这一层。
            ok, wait = self.peer_buckets.consume(self._peer_ip(request))

        if not ok:
            retry_after = max(1, int(wait) + 1)
            logger.warning(f"rate_limit.hit client={client_id} wait={wait:.1f}s")
            return JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "code": "TB-1007",
                        "message": f"Rate limit exceeded. Retry after {retry_after}s",
                        "request_id": getattr(request.state, "request_id", "unknown"),
                    }
                },
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(self.rate_limit_per_minute),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(int(time.time()) + retry_after),
                },
            )

        response: Response = await call_next(request)
        remaining = int(self.buckets.remaining(client_id))
        response.headers["X-RateLimit-Limit"] = str(self.rate_limit_per_minute)
        response.headers["X-RateLimit-Remaining"] = str(max(0, remaining))
        response.headers["X-RateLimit-Reset"] = str(int(time.time()) + 60)
        return response
