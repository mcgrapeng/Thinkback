"""Thinkback FastAPI application factory."""

from collections.abc import Awaitable, Callable
from time import perf_counter

from fastapi import FastAPI, Request, Response
from loguru import logger

from api.health import router as health_router
from api.memory import router as memory_router
from infra.config import settings
from infra.logging import clear_trace_id, set_trace_id

OPENAPI_DESCRIPTION = """
Thinkback 首版主链路记忆服务 API。

图形化 API 文档：

- Swagger UI: `/docs`
- ReDoc: `/redoc`
- OpenAPI JSON: `/openapi.json`

本 API 覆盖记忆写入、召回、删除、重建、后台任务查询和健康检查。
质量评测方案、性能与稳定性测试方案、评测报告模板分别维护在 `docs/` 目录。
"""

OPENAPI_TAGS = [
    {
        "name": "health",
        "description": "服务健康、存活和依赖就绪检查。",
    },
    {
        "name": "memory",
        "description": "记忆写入、召回、删除、重建和后台任务查询。",
    },
]


def _register_request_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def trace_and_access_log(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming_trace_id = request.headers.get("X-Trace-Id") or request.headers.get("X-Request-Id")
        trace_id = set_trace_id(incoming_trace_id.strip() if incoming_trace_id else None)
        started_at = perf_counter()
        response: Response | None = None
        try:
            response = await call_next(request)
            response.headers["X-Trace-Id"] = trace_id
            return response
        finally:
            duration_ms = (perf_counter() - started_at) * 1000
            logger.info(
                "HTTP request completed",
                method=request.method,
                path=request.url.path,
                status_code=response.status_code if response else 500,
                duration_ms=round(duration_ms, 2),
            )
            clear_trace_id()


def create_app() -> FastAPI:
    app = FastAPI(
        title=f"{settings.app_name} Memory Service API",
        version=settings.app_version,
        description=OPENAPI_DESCRIPTION,
        openapi_tags=OPENAPI_TAGS,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        debug=settings.debug,
    )
    _register_request_middleware(app)
    app.include_router(health_router)
    app.include_router(memory_router)

    @app.get("/")
    async def root() -> dict[str, str]:
        return {
            "name": settings.app_name,
            "version": settings.app_version,
            "status": "running",
            "scope": "memory-service",
        }

    return app


app = create_app()

__all__ = ["app", "create_app"]
