"""Health and probe routes.

三层健康探针：
- ``/health``：进程级基础信息（永远返回 healthy，常用于部署回滚时的瞬时探测）。
- ``/health/live``：存活探针（Liveness Probe），仅判断进程是否在跑。
- ``/health/ready``：就绪探针（Readiness Probe），探测数据库、Milvus、Mem0 Library
  是否可用，任一不可用则返回 503，让上游网关摘流。

S2 (P0-3 落地): ``/metrics`` 暴露 Prometheus 文本 exposition 格式，运维可在
Prometheus / VictoriaMetrics 端 scrape。
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse, PlainTextResponse
from loguru import logger
from pydantic import BaseModel

from thinkback.api.metrics import render_prometheus
from thinkback.infra.config import settings
from thinkback.infra.readiness import collect_readiness

router = APIRouter(prefix="/health", tags=["health"])


class HealthResponse(BaseModel):
    """基础健康检查响应"""

    status: str
    version: str
    environment: str


@router.get(
    "",
    response_model=HealthResponse,
    summary="基础健康检查",
    operation_id="health_check",
    description="返回服务健康状态、版本和运行环境。",
)
async def health_check() -> HealthResponse:
    """进程级健康检查：不依赖任何外部系统，仅返回版本与环境。"""

    logger.bind(version=settings.app_version, environment=settings.environment).info(
        "health check requested"
    )
    return HealthResponse(
        status="healthy",
        version=settings.app_version,
        environment=settings.environment,
    )


@router.get(
    "/live",
    summary="存活检查",
    operation_id="health_live",
    description="用于容器或网关判断 API 进程是否存活。",
)
async def liveness_check() -> dict[str, str]:
    """存活探针：始终返回 alive，用于 k8s livenessProbe。"""

    return {"status": "alive"}


@router.get(
    "/ready",
    summary="依赖就绪检查",
    operation_id="health_ready",
    description="检查数据库、Milvus 和 Mem0 Library 等依赖是否就绪。",
)
async def readiness_check() -> JSONResponse:
    """就绪探针：探测外部依赖，任一不可用则返回 503 让流量摘除。"""
    logger.debug("readiness check requested")
    payload = await collect_readiness()
    status_code = 200 if payload["status"] == "ready" else 503
    readiness_log = logger.bind(status=payload["status"], status_code=status_code)
    if status_code == 200:
        readiness_log.debug("readiness check completed")
    else:
        readiness_log.warning("readiness check completed")
    return JSONResponse(status_code=status_code, content=payload)


@router.get(
    "/metrics",
    summary="Prometheus metrics endpoint",
    operation_id="health_metrics",
    description=(
        "Prometheus 文本 exposition 格式输出当前滚动窗口的请求计数与速率。"
        "运维可在 Prometheus / VictoriaMetrics 端配置 scrape 抓取。"
    ),
    response_class=PlainTextResponse,
)
async def metrics_endpoint() -> PlainTextResponse:
    """S2: /metrics 暴露 thinkback_request_total / thinkback_request_per_minute。"""
    return PlainTextResponse(
        content=render_prometheus(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
