"""Readiness aggregation for runtime dependencies."""

import asyncio
from collections.abc import Awaitable, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from api.dependencies import get_memory_backend
from infra.cache.redis_client import check_redis
from infra.config import settings
from infra.database.engine import check_database

DependencyCheck = Callable[[], Awaitable[dict[str, str]]]


class QdrantClient:
    """Small diagnostics-only Qdrant REST client."""

    def __init__(self, *, url: str, api_key: str | None = None, timeout_seconds: float = 2.0) -> None:
        self.url = url.rstrip("/")
        self.api_key = api_key or ""
        self.timeout_seconds = timeout_seconds

    def get_collections(self) -> None:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["api-key"] = self.api_key
        request = Request(
            urljoin(f"{self.url}/", "collections"),
            headers=headers,
            method="GET",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                response.read()
        except HTTPError as exc:
            raise RuntimeError(f"qdrant http GET /collections failed: {exc.code}") from exc
        except URLError as exc:
            raise RuntimeError(f"qdrant http GET /collections failed: {exc.reason}") from exc


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


async def collect_readiness(timeout_seconds: float | None = None) -> dict[str, object]:
    effective_timeout_seconds = (
        settings.readiness_timeout_seconds if timeout_seconds is None else timeout_seconds
    )
    checks: dict[str, DependencyCheck] = {
        "database": check_database,
        "redis": check_redis,
        "qdrant": check_qdrant,
        "mem0": check_mem0_library,
    }
    dependencies = dict(
        await asyncio.gather(
            *(
                _run_dependency_check(name, check, effective_timeout_seconds)
                for name, check in checks.items()
            )
        )
    )
    status = (
        "ready" if all(item["status"] == "ready" for item in dependencies.values()) else "not_ready"
    )
    return {"status": status, "dependencies": dependencies}


async def check_mem0_library() -> dict[str, str]:
    backend = get_memory_backend(settings)
    return await asyncio.to_thread(backend.health_check)


async def check_qdrant() -> dict[str, str]:
    client = QdrantClient(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key,
        timeout_seconds=2.0,
    )
    try:
        await asyncio.to_thread(client.get_collections)
    except Exception as exc:
        return {"status": "not_ready", "detail": str(exc)}
    return {"status": "ready", "detail": "ok"}
