"""Thinkback FastAPI application factory."""

from fastapi import FastAPI

from api.health import router as health_router


def create_app() -> FastAPI:
    app = FastAPI(
        title="Thinkback",
        version="0.1.0",
        description="Thinkback memory service",
        debug=False,
    )
    app.include_router(health_router)

    @app.get("/")
    async def root() -> dict[str, str]:
        return {
            "name": "Thinkback",
            "version": "0.1.0",
            "status": "running",
            "scope": "memory-service",
        }

    return app


app = create_app()

__all__ = ["app", "create_app"]
