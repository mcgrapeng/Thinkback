"""限流中间件 — Token Bucket 算法。

内网部署（方案二）：业务面免鉴权，因此不再按 API key 分桶，而是按客户端 IP
分桶，防止失控重试与脚本打爆记忆服务。超限返回 429 TB-1007 + Retry-After 头。
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

# 内网默认限流：单客户端 600 请求/分钟、突发 50。替代原先按 API key plan 的配额。
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

    /memory/*、/admin/api/*、/health 等内部路径不走限流。
    """

    def __init__(self, app: ASGIApp, exclude_paths: set[str] | None = None) -> None:
        super().__init__(app)
        self.exclude_paths = exclude_paths or {
            "/health",
            "/docs",
            "/redoc",
            "/openapi.json",
        }
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

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        # 只限流业务记忆路径；admin/health/docs 走各自通道。
        if not path.startswith("/memory/"):
            return await call_next(request)
        if path in self.exclude_paths:
            return await call_next(request)

        client_id = request.client.host if request.client and request.client.host else "unknown"
        bucket = self._get_bucket(client_id, _RATE_LIMIT_PER_MINUTE, _RATE_LIMIT_BURST)
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
                    "X-RateLimit-Limit": str(_RATE_LIMIT_PER_MINUTE),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(int(time.time()) + retry_after),
                },
            )

        response: Response = await call_next(request)
        remaining = int(bucket.tokens)
        response.headers["X-RateLimit-Limit"] = str(_RATE_LIMIT_PER_MINUTE)
        response.headers["X-RateLimit-Remaining"] = str(max(0, remaining))
        response.headers["X-RateLimit-Reset"] = str(int(time.time()) + 60)
        return response
