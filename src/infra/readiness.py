"""Readiness aggregation for runtime dependencies."""

import asyncio
from collections.abc import Awaitable, Callable

from infra.cache.redis_client import check_redis
from infra.config import settings
from infra.database.engine import check_database
from infra.vectorstore.qdrant_client import check_qdrant
from memory.backends import Mem0HttpMemoryBackend

DependencyCheck = Callable[[], Awaitable[dict[str, str]]]


async def _run_dependency_check(
    name: str, check: DependencyCheck, timeout_seconds: float
) -> tuple[str, dict[str, str]]:
    try:
        return name, await asyncio.wait_for(check(), timeout=timeout_seconds)
    except TimeoutError:
        return (
            name,
            {
                "status": "not_ready",
                "detail": f"timed out after {timeout_seconds:g}s",
            },
        )
    except Exception as exc:
        return name, {"status": "not_ready", "detail": str(exc)}


async def collect_readiness(timeout_seconds: float = 2.0) -> dict[str, object]:
    checks: dict[str, DependencyCheck] = {
        "database": check_database,
        "redis": check_redis,
    }
    if settings.mem0_backend_mode == "http_api":
        checks["mem0"] = check_mem0_api
    else:
        checks["qdrant"] = check_qdrant
    dependencies = dict(
        await asyncio.gather(
            *(_run_dependency_check(name, check, timeout_seconds) for name, check in checks.items())
        )
    )
    status = (
        "ready" if all(item["status"] == "ready" for item in dependencies.values()) else "not_ready"
    )
    return {"status": status, "dependencies": dependencies}


async def check_mem0_api() -> dict[str, str]:
    backend = Mem0HttpMemoryBackend(
        api_url=settings.mem0_api_url,
        api_key=settings.mem0_api_key,
        timeout_seconds=2.0,
    )
    return await asyncio.to_thread(backend.health_check)
