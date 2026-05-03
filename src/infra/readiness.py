"""Readiness aggregation for runtime dependencies."""

from infra.cache.redis_client import check_redis
from infra.database.engine import check_database
from infra.vectorstore.qdrant_client import check_qdrant


async def collect_readiness() -> dict[str, object]:
    dependencies = {
        "database": await check_database(),
        "redis": await check_redis(),
        "qdrant": await check_qdrant(),
    }
    status = (
        "ready"
        if all(item["status"] == "ready" for item in dependencies.values())
        else "not_ready"
    )
    return {"status": status, "dependencies": dependencies}
