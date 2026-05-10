"""Health and probe routes."""

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from infra.config import settings
from infra.readiness import collect_readiness

router = APIRouter(prefix="/health", tags=["health"])


class HealthResponse(BaseModel):
    status: str
    version: str
    environment: str


@router.get(
    "",
    response_model=HealthResponse,
    summary="基础健康检查",
    description="返回服务健康状态、版本和运行环境。",
)
async def health_check() -> HealthResponse:
    return HealthResponse(
        status="healthy",
        version=settings.app_version,
        environment=settings.environment,
    )


@router.get(
    "/live",
    summary="存活检查",
    description="用于容器或网关判断 API 进程是否存活。",
)
async def liveness_check() -> dict[str, str]:
    return {"status": "alive"}


@router.get(
    "/ready",
    summary="依赖就绪检查",
    description="检查数据库、Redis、Qdrant 和 Mem0 Library 等依赖是否就绪。",
)
async def readiness_check() -> JSONResponse:
    payload = await collect_readiness()
    status_code = 200 if payload["status"] == "ready" else 503
    return JSONResponse(status_code=status_code, content=payload)
