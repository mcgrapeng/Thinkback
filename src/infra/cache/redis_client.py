"""Redis client and readiness helpers."""

from collections.abc import Awaitable

from redis.asyncio import Redis

from infra.config import settings

_redis_client: Redis | None = None


def get_redis_client() -> Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = Redis.from_url(settings.redis_url, decode_responses=True)
    return _redis_client


async def check_redis() -> dict[str, str]:
    try:
        ping_result = get_redis_client().ping()
        if isinstance(ping_result, Awaitable):
            await ping_result
    except Exception as exc:
        return {"status": "not_ready", "detail": str(exc)}
    return {"status": "ready", "detail": "ok"}
