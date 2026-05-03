"""Thinkback FastAPI application factory."""

from collections.abc import Awaitable, Callable
from time import perf_counter

from fastapi import FastAPI, Request, Response
from loguru import logger

from api.health import router as health_router
from infra.config import settings
from infra.logging import clear_trace_id, set_trace_id


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
        title=settings.app_name,
        version=settings.app_version,
        description="Thinkback memory service",
        debug=settings.debug,
    )
    _register_request_middleware(app)
    app.include_router(health_router)

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
