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
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

CACHE_TTL = 24 * 3600  # 24h
MAX_CACHE_SIZE = 10000

# 读路径不进幂等缓存。recall 返回的是记忆正文：缓存它意味着内容被擦除后，
# 同一把 Idempotency-Key 仍能在 24h 内回放含旧内容的响应。
READ_ONLY_PATHS = frozenset({"/v2/memory/recall"})


def _caller_identity(body: bytes) -> str:
    """从请求体里取调用方标识（namespace + user_id）。

    取不到就返回空串：调用方退回到对端 IP 分域。解析失败不是错误 ——
    body 不是 JSON 的写请求本来就不走幂等缓存的语义。
    """
    if not body:
        return ""
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    user_id = payload.get("user_id")
    if not isinstance(user_id, str) or not user_id.strip():
        return ""
    namespace = payload.get("namespace")
    if not isinstance(namespace, str) or not namespace.strip():
        namespace = ""
    return f"{namespace}:{user_id}"


@dataclass
class CachedResponse:
    body: bytes
    status_code: int
    content_type: str
    headers: dict[str, str]
    timestamp: float


class IdempotencyCache:
    """LRU 缓存,按 (租户, key, body_hash) 存储响应。

    租户这一维是**必需**的：原来键只有 (key, body_hash)，缓存是进程内全局的，
    于是租户 B 用和租户 A 相同的 Idempotency-Key + 相同 body，就能拿到 A 的缓存响应
    （同一个 backend_memory_id、同一个 task_id）—— 明确的跨租户数据泄漏。

    冲突检测也从「扫全表 startswith」改成二级索引
    （(租户, key) -> 已见过的 body_hash 集合）：
    - 不再是 O(缓存大小)
    - 不会再把 key `a` 和 key `a:b` 误判成冲突（startswith 前缀匹配的经典坑）
    """

    def __init__(self) -> None:
        self._store: OrderedDict[tuple[str, str, str, str, str], CachedResponse] = OrderedDict()
        self._index: dict[tuple[str, str, str, str], set[str]] = {}

    def _make_key(
        self, scope: str, idem_key: str, method: str, path: str, body_hash: str
    ) -> tuple[str, str, str, str, str]:
        return (scope, idem_key, method, path, body_hash)

    def _prune_expired(self) -> int:
        """清掉过期条目及其冲突索引，避免「过期后换 body 永远 409」。"""
        now = time.time()
        expired = [k for k, v in self._store.items() if now - v.timestamp > CACHE_TTL]
        for key in expired:
            del self._store[key]
            scope, idem_key, method, path, body_hash = key
            hashes = self._index.get((scope, idem_key, method, path))
            if hashes is not None:
                hashes.discard(body_hash)
                if not hashes:
                    self._index.pop((scope, idem_key, method, path), None)
        return len(expired)

    def get(
        self, scope: str, idem_key: str, method: str, path: str, body_hash: str
    ) -> CachedResponse | None:
        self._prune_expired()
        key = self._make_key(scope, idem_key, method, path, body_hash)
        entry = self._store.get(key)
        if entry is None:
            return None
        self._store.move_to_end(key)
        return entry

    def get_conflict(
        self, scope: str, idem_key: str, method: str, path: str, body_hash: str
    ) -> bool:
        """检查同 key 但不同 body 是否存在(冲突)。"""
        self._prune_expired()
        hashes = self._index.get((scope, idem_key, method, path))
        if not hashes:
            return False
        return body_hash not in hashes

    def put(
        self,
        scope: str,
        idem_key: str,
        method: str,
        path: str,
        body_hash: str,
        response: CachedResponse,
    ) -> None:
        self._prune_expired()
        key = self._make_key(scope, idem_key, method, path, body_hash)
        self._store[key] = response
        self._store.move_to_end(key)
        self._index.setdefault((scope, idem_key, method, path), set()).add(body_hash)
        while len(self._store) > MAX_CACHE_SIZE:
            evicted_key, _ = self._store.popitem(last=False)
            evicted_scope, evicted_idem, evicted_method, evicted_path, evicted_hash = evicted_key
            hashes = self._index.get((evicted_scope, evicted_idem, evicted_method, evicted_path))
            if hashes is not None:
                hashes.discard(evicted_hash)
                if not hashes:
                    self._index.pop(
                        (evicted_scope, evicted_idem, evicted_method, evicted_path), None
                    )

    def clear_expired(self) -> int:
        return self._prune_expired()


# 全局缓存实例(生产替换为持久化层)
_idempotency_cache = IdempotencyCache()


def get_idempotency_cache() -> IdempotencyCache:
    return _idempotency_cache


class IdempotencyMiddleware(BaseHTTPMiddleware):
    """只对 /v1/* 的写操作(POST/PUT/DELETE/PATCH)做幂等保护。"""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)
        self.cache = _idempotency_cache

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # 只处理业务记忆路径的写操作（内网免鉴权，/memory/* 为唯一业务面）
        if not request.url.path.startswith("/v2/memory/"):
            return await call_next(request)

        # 读操作不需要写入幂等：缓存响应只会让过期内容被回放。
        if request.url.path in READ_ONLY_PATHS:
            return await call_next(request)

        # 只处理写操作
        if request.method not in ("POST", "PUT", "DELETE", "PATCH"):
            return await call_next(request)

        idem_key = request.headers.get("idempotency-key", "").strip()
        if not idem_key:
            return await call_next(request)

        # 读 body(用于 hash + 分域)
        body = await request.body()
        body_hash = hashlib.sha256(body).hexdigest()[:16]

        # 缓存按调用方分域：不同调用方用同一个 Idempotency-Key 是两件事。
        scope = self._resolve_scope(request, body)

        # 检查冲突：同 key 不同 body（按 method+path 区分端点）
        if self.cache.get_conflict(scope, idem_key, request.method, request.url.path, body_hash):
            logger.warning(f"idempotency.conflict key={idem_key}")
            return JSONResponse(
                status_code=409,
                content={
                    "error": {
                        "code": "TB-1005",
                        "message": "Idempotency key reused with different request body",
                        "request_id": request.state.request_id
                        if hasattr(request.state, "request_id")
                        else "unknown",
                    }
                },
            )

        # 检查缓存
        cached = self.cache.get(scope, idem_key, request.method, request.url.path, body_hash)
        if cached:
            logger.info(f"idempotency.cache_hit key={idem_key}")
            return Response(
                content=cached.body,
                status_code=cached.status_code,
                media_type=cached.content_type,
                # 透传原响应头：丢头会把 trace 中间件设的 X-Trace-Id 抹掉
                # —— 恰好在需要排查的写链路上失去关联。
                headers={**cached.headers, "X-Idempotent-Replay": "true"},
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
                scope,
                idem_key,
                request.method,
                request.url.path,
                body_hash,
                CachedResponse(
                    body=resp_body,
                    status_code=response.status_code,
                    content_type=response.headers.get("content-type", "application/json"),
                    headers=dict(response.headers),
                    timestamp=time.time(),
                ),
            )
            rebuilt = Response(
                content=resp_body,
                status_code=response.status_code,
                media_type=response.headers.get("content-type", "application/json"),
                headers=dict(response.headers),
            )
            return rebuilt

        return response

    @staticmethod
    def _resolve_scope(request: Request, body: bytes = b"") -> str:
        """幂等缓存的调用方域。

        域必须是**调用方**而不是传输层：内网免鉴权后如果拿「直连对端 IP」当域，
        k8s / ingress / sidecar 部署下所有外部调用方的 IP 都一样（网关地址），
        域被压平成一个全局 —— 一个客户端用过 key ``k`` 之后，别的客户端再用
        ``k``（body 不同）直接被判成冲突 409，一把 key 就能堵死别人的写路径。

        没有鉴权时最接近「调用方」的标识是这次写入**为谁而写**：请求体里的
        ``namespace`` + ``user_id``。取不到（没有这个字段的端点）才退回对端 IP。
        """
        identity = _caller_identity(body)
        if identity:
            return identity
        return request.client.host if request.client and request.client.host else ""
