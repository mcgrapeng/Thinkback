"""幂等保证 — Idempotency-Key 支持。

协议详见 docs/specs/2026-10-08-thinkback-integration-protocol-v1.md

写操作(POST/PUT/DELETE)携带 Idempotency-Key 头时:
- 同 key + 同请求体 → 返回缓存响应(不重复执行)
- 同 key + 不同请求体 → 409 TB-1005
- 缓存 TTL 24h
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from dataclasses import dataclass

from fastapi import Request, Response
from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

CACHE_TTL = 24 * 3600  # 24h
MAX_CACHE_SIZE = 10000


@dataclass
class CachedResponse:
    body: bytes
    status_code: int
    content_type: str
    timestamp: float


class IdempotencyCache:
    """LRU 缓存,按 (key, body_hash) 存储响应。"""

    def __init__(self):
        self._store: OrderedDict[str, CachedResponse] = OrderedDict()

    def _make_key(self, idem_key: str, body_hash: str) -> str:
        return f"{idem_key}:{body_hash}"

    def get(self, idem_key: str, body_hash: str) -> CachedResponse | None:
        key = self._make_key(idem_key, body_hash)
        entry = self._store.get(key)
        if entry is None:
            return None
        if time.time() - entry.timestamp > CACHE_TTL:
            del self._store[key]
            return None
        self._store.move_to_end(key)
        return entry

    def get_conflict(self, idem_key: str, body_hash: str) -> bool:
        """检查同 key 但不同 body 是否存在(冲突)。"""
        for existing_key in self._store:
            if existing_key.startswith(f"{idem_key}:") and not existing_key.endswith(body_hash):
                return True
        return False

    def put(self, idem_key: str, body_hash: str, response: CachedResponse) -> None:
        key = self._make_key(idem_key, body_hash)
        self._store[key] = response
        self._store.move_to_end(key)
        while len(self._store) > MAX_CACHE_SIZE:
            self._store.popitem(last=False)

    def clear_expired(self) -> int:
        now = time.time()
        expired = [k for k, v in self._store.items() if now - v.timestamp > CACHE_TTL]
        for k in expired:
            del self._store[k]
        return len(expired)


# 全局缓存实例(生产替换为持久化层)
_idempotency_cache = IdempotencyCache()


def get_idempotency_cache() -> IdempotencyCache:
    return _idempotency_cache


class IdempotencyMiddleware(BaseHTTPMiddleware):
    """只对 /v1/* 的写操作(POST/PUT/DELETE/PATCH)做幂等保护。"""

    def __init__(self, app):
        super().__init__(app)
        self.cache = _idempotency_cache

    async def dispatch(self, request: Request, call_next):
        # 只处理 /v1/* 路径
        if not request.url.path.startswith("/v1/"):
            return await call_next(request)

        # 只处理写操作
        if request.method not in ("POST", "PUT", "DELETE", "PATCH"):
            return await call_next(request)

        idem_key = request.headers.get("idempotency-key", "").strip()
        if not idem_key:
            return await call_next(request)

        # 读 body(用于 hash)
        body = await request.body()
        body_hash = hashlib.sha256(body).hexdigest()[:16]

        # 检查冲突
        if self.cache.get_conflict(idem_key, body_hash):
            logger.warning(f"idempotency.conflict key={idem_key}")
            return JSONResponse(
                status_code=409,
                content={
                    "error": {
                        "code": "TB-1005",
                        "message": "Idempotency key reused with different request body",
                        "request_id": request.state.request_id if hasattr(request.state, "request_id") else "unknown",
                    }
                },
            )

        # 检查缓存
        cached = self.cache.get(idem_key, body_hash)
        if cached:
            logger.info(f"idempotency.cache_hit key={idem_key}")
            return Response(
                content=cached.body,
                status_code=cached.status_code,
                media_type=cached.content_type,
                headers={"X-Idempotent-Replay": "true"},
            )

        # 执行请求
        # 重建 request._body(因为已经读过了)
        request._body = body
        response = await call_next(request)

        # 缓存成功响应(2xx)
        if 200 <= response.status_code < 300:
            resp_body = b""
            # Starlette Response 不同版本 body 访问方式不同
            body_iter = getattr(response, "body_iterator", None)
            if body_iter is not None:
                async for chunk in body_iter:
                    resp_body += chunk if isinstance(chunk, bytes) else str(chunk).encode()
            else:
                resp_body = bytes(getattr(response, "body", b"") or b"")

            self.cache.put(
                idem_key,
                body_hash,
                CachedResponse(
                    body=resp_body,
                    status_code=response.status_code,
                    content_type=response.headers.get("content-type", "application/json"),
                    timestamp=time.time(),
                ),
            )
            return Response(
                content=resp_body,
                status_code=response.status_code,
                media_type=response.headers.get("content-type", "application/json"),
            )

        return response
