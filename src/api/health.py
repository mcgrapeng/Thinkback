"""Health and probe routes."""

from fastapi import APIRouter
from pydantic import BaseModel

from infra.config import settings

router = APIRouter(prefix="/health", tags=["health"])


class HealthResponse(BaseModel):
    status: str
    version: str
    environment: str


@router.get("", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    return HealthResponse(
        status="healthy",
        version=settings.app_version,
        environment=settings.environment,
    )


@router.get("/live")
async def liveness_check() -> dict[str, str]:
    return {"status": "alive"}
