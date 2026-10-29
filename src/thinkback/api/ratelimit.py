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
from collections import defaultdict
from dataclasses import dataclass

from fastapi import Request, Response
from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from thinkback.api.idempotency import _caller_identity

# 内网默认限流：单调用方 600 请求/分钟、突发 50。替代原先按 API key plan 的配额。
_RATE_LIMIT_PER_MINUTE = 600
_RATE_LIMIT_BURST = 50


@dataclass
class TokenBucket:
    tokens: float
    last_refill: float
    rate_per_second: float
    burst: int

    def consume(self, n: int = 1) -> tuple[bool, float]:
        """尝试消费 n 个 token,返回 (是否成功, 需等待秒数)。"""
        now = time.time()
        elapsed = now - self.last_refill
        self.tokens = min(self.burst, self.tokens + elapsed * self.rate_per_second)
        self.last_refill = now

        if self.tokens >= n:
            self.tokens -= n
            return True, 0.0
        wait = (n - self.tokens) / self.rate_per_second
        return False, wait


class RateLimitMiddleware(BaseHTTPMiddleware):
    """只对 /v1/* 路径做 API key 级限流。

    /v2/memory/*、/admin/api/*、/health 等内部路径不走限流。
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
        self.buckets: dict[str, TokenBucket] = defaultdict(
            lambda: TokenBucket(tokens=0, last_refill=0, rate_per_second=0, burst=0)
        )

    def _get_bucket(self, key_id: str, rate_per_minute: int, burst: int) -> TokenBucket:
        bucket = self.buckets[key_id]
        if bucket.burst == 0:
            # 新桶：按 burst 额度初始化
            bucket.burst = burst
            bucket.rate_per_second = rate_per_minute / 60.0
            bucket.tokens = float(burst)
            bucket.last_refill = time.time()
        elif bucket.burst != burst:
            # 限额变了：只改速率与上限，**不重置余额**。
            # 原实现在这里把 tokens 重置成 burst —— 每桶被「重置」一次，
            # 改一次配置就等于发一次免费额度。
            bucket.burst = burst
            bucket.rate_per_second = rate_per_minute / 60.0
            bucket.tokens = min(bucket.tokens, float(burst))
        return bucket

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

        bucket = self._get_bucket(client_id, self.rate_limit_per_minute, self.burst)
        ok, wait = bucket.consume(1)

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
        remaining = int(bucket.tokens)
        response.headers["X-RateLimit-Limit"] = str(self.rate_limit_per_minute)
        response.headers["X-RateLimit-Remaining"] = str(max(0, remaining))
        response.headers["X-RateLimit-Reset"] = str(int(time.time()) + 60)
        return response
