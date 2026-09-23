"""service 异常 → HTTP 状态码的统一映射（业务路由与治理台共享）。

契约（与 /memory 路由既有语义逐字一致）：
- ValueError 含 ``fail-closed`` → 403；含 ``conflict`` → 409；含 ``not found`` → 404；其它 → 400
- RuntimeError 含 ``dead_letter`` / ``stale write`` → 409；``l3 background queue full`` /
  ``repository operation timed out`` → 503；``mem0 library`` / ``memory backend`` → 502
"""

from __future__ import annotations

from fastapi import HTTPException


def service_error_to_http(exc: Exception) -> HTTPException | None:
    """映射 ValueError/RuntimeError；其余类型返回 None（由调用方记日志后原样上抛）。"""

    if isinstance(exc, ValueError):
        detail = str(exc)
        if "fail-closed" in detail:
            status_code = 403
        elif "conflict" in detail:
            status_code = 409
        elif "not found" in detail:
            status_code = 404
        else:
            status_code = 400
        return HTTPException(status_code=status_code, detail=detail)
    if isinstance(exc, RuntimeError):
        detail = str(exc)
        if "dead_letter" in detail or "stale write" in detail:
            return HTTPException(status_code=409, detail=detail)
        if "l3 background queue full" in detail:
            return HTTPException(status_code=503, detail=detail)
        if "repository operation timed out" in detail:
            return HTTPException(status_code=503, detail=detail)
        if "mem0 library" in detail or "memory backend" in detail:
            return HTTPException(status_code=502, detail=detail)
        return None
    return None
