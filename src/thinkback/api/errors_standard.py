"""标准化错误响应模型 + FastAPI 异常处理器。

协议详见 docs/specs/2026-10-08-thinkback-integration-protocol-v1.md

错误码表:
TB-1001 400 参数校验失败
TB-1002 401 认证失败
TB-1003 403 权限不足
TB-1004 404 资源不存在
TB-1005 409 冲突(死信/幂等键重复)
TB-1006 422 语义校验失败
TB-1007 429 限流
TB-2001 500 内部错误
TB-2002 502 上游依赖故障
TB-2003 503 服务过载
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from loguru import logger
from starlette.exceptions import HTTPException as StarletteHTTPException


class ErrorCode:
    VALIDATION = "TB-1001"
    AUTH_FAILED = "TB-1002"
    FORBIDDEN = "TB-1003"
    NOT_FOUND = "TB-1004"
    CONFLICT = "TB-1005"
    SEMANTIC = "TB-1006"
    RATE_LIMIT = "TB-1007"
    INTERNAL = "TB-2001"
    UPSTREAM = "TB-2002"
    OVERLOAD = "TB-2003"


def _get_request_id(request: Request) -> str:
    return getattr(request.state, "request_id", f"req_{uuid.uuid4().hex[:12]}")


def error_response(
    code: str,
    message: str,
    request_id: str,
    status_code: int,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "error": {
            "code": code,
            "message": message,
            "request_id": request_id,
        },
        # 向后兼容:保留 FastAPI 默认的 detail 字段
        "detail": message,
    }
    if details:
        body["error"]["details"] = details
    return JSONResponse(status_code=status_code, content=body)


def register_exception_handlers(app: FastAPI) -> None:
    """注册标准化异常处理器。"""

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        rid = _get_request_id(request)
        logger.warning(f"validation_error rid={rid} errors={exc.errors()}")
        # 用 jsonable_encoder 安全序列化(Pydantic 错误可能含 ValueError 等不可 JSON 化对象)
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": ErrorCode.SEMANTIC,
                    "message": "Request validation failed",
                    "request_id": rid,
                },
                "detail": jsonable_encoder(exc.errors()),
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        rid = _get_request_id(request)
        # 路由器给出的响应头（如 405 必带的 Allow，RFC 9110）必须透传，
        # 否则标准化错误体会把合规响应变成违约响应。
        if isinstance(exc.detail, dict) and "error" in exc.detail:
            return JSONResponse(
                status_code=exc.status_code, content=exc.detail, headers=exc.headers
            )

        code_map = {
            400: ErrorCode.VALIDATION,
            401: ErrorCode.AUTH_FAILED,
            403: ErrorCode.FORBIDDEN,
            404: ErrorCode.NOT_FOUND,
            409: ErrorCode.CONFLICT,
            422: ErrorCode.SEMANTIC,
            429: ErrorCode.RATE_LIMIT,
            500: ErrorCode.INTERNAL,
            502: ErrorCode.UPSTREAM,
            503: ErrorCode.OVERLOAD,
        }
        code = code_map.get(exc.status_code, ErrorCode.INTERNAL)
        # 安全序列化 detail(可能是 dict/list/任意对象)
        detail = jsonable_encoder(exc.detail) if exc.detail is not None else ""
        response = error_response(
            code=code,
            message=str(detail),
            request_id=rid,
            status_code=exc.status_code,
        )
        if exc.headers:
            response.headers.update(exc.headers)
        return response

    @app.exception_handler(Exception)
    async def global_handler(request: Request, exc: Exception) -> JSONResponse:
        rid = _get_request_id(request)
        logger.error(f"unhandled_error rid={rid} type={type(exc).__name__} msg={exc}")
        return error_response(
            code=ErrorCode.INTERNAL,
            message="Internal server error",
            request_id=rid,
            status_code=500,
        )
