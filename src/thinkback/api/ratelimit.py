"""限流中间件 — Token Bucket 算法。

协议详见 docs/specs/2026-10-08-thinkback-integration-protocol-v1.md

每个 API key 独立 bucket,按 plan 配置 rpm/burst。
超限返回 429 TB-1007 + Retry-After 头。
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field

from fastapi import Request, Response
from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from thinkback.api.auth import get_api_key
from thinkback.api.auth import _parse_api_key


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

    def __init__(self, app, exclude_paths: set[str] | None = None):
        super().__init__(app)
        self.exclude_paths = exclude_paths or {
            "/health", "/docs", "/redoc", "/openapi.json",
        }
        self.buckets: dict[str, TokenBucket] = defaultdict(
            lambda: TokenBucket(tokens=0, last_refill=0, rate_per_second=0, burst=0)
        )

    def _get_bucket(self, key_id: str, rate_per_minute: int, burst: int) -> TokenBucket:
        bucket = self.buckets[key_id]
        if bucket.burst != burst:
            bucket.burst = burst
            bucket.rate_per_second = rate_per_minute / 60.0
            bucket.tokens = float(burst)
            bucket.last_refill = time.time()
        return bucket

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # 只限流 /v1/* 业务系统接入路径
        if not path.startswith("/v1/"):
            return await call_next(request)
        if path in self.exclude_paths:
            return await call_next(request)

        # 解析 API key
        auth_header = request.headers.get("authorization", "")
        parsed = _parse_api_key(auth_header)
        if not parsed:
            return await call_next(request)

        key_id, _ = parsed
        record = get_api_key(key_id)
        if not record:
            return await call_next(request)

        bucket = self._get_bucket(key_id, record.rate_limit_per_minute, record.rate_limit_burst)
        ok, wait = bucket.consume(1)

        if not ok:
            retry_after = max(1, int(wait) + 1)
            logger.warning(f"rate_limit.hit key_id={key_id} wait={wait:.1f}s")
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
                    "X-RateLimit-Limit": str(record.rate_limit_per_minute),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(int(time.time()) + retry_after),
                },
            )

        response: Response = await call_next(request)
        remaining = int(bucket.tokens)
        response.headers["X-RateLimit-Limit"] = str(record.rate_limit_per_minute)
        response.headers["X-RateLimit-Remaining"] = str(max(0, remaining))
        response.headers["X-RateLimit-Reset"] = str(int(time.time()) + 60)
        return response
