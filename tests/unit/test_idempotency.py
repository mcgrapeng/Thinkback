"""幂等中间件的行为契约（内网免鉴权，/memory/* 为唯一业务面）。"""

from __future__ import annotations

import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from thinkback.api.idempotency import CACHE_TTL, CachedResponse, IdempotencyCache


def _app() -> FastAPI:
    from thinkback.api.idempotency import IdempotencyMiddleware

    app = FastAPI()
    app.add_middleware(IdempotencyMiddleware)

    @app.post("/memory/a")
    async def route_a() -> dict[str, str]:
        return {"route": "a"}

    @app.post("/memory/b")
    async def route_b() -> dict[str, str]:
        return {"route": "b"}

    return app


def test_idempotency_key_does_not_cross_endpoints() -> None:
    """同一把 key 用在不同端点：recall 不得领走 append 的缓存响应。"""
    with TestClient(_app()) as client:
        headers = {"Idempotency-Key": "shared-endpoint-key"}
        r_a = client.post("/memory/a", json={"x": 1}, headers=headers)
        r_b = client.post("/memory/b", json={"x": 1}, headers=headers)
        assert r_a.json() == {"route": "a"}
        assert r_b.json() == {"route": "b"}


def test_idempotent_replay_on_same_route() -> None:
    """同 key + 同 body + 同端点：第二次直接回放，不重复执行。"""
    with TestClient(_app()) as client:
        headers = {"Idempotency-Key": "same-route-key"}
        r1 = client.post("/memory/a", json={"x": 1}, headers=headers)
        r2 = client.post("/memory/a", json={"x": 1}, headers=headers)
        assert r1.json() == r2.json()
        assert r2.headers.get("X-Idempotent-Replay") == "true"


def test_read_path_is_not_cached_or_replayed() -> None:
    """recall 是读操作：响应不得进幂等缓存。

    缓存它意味着擦除后，同一把 Idempotency-Key 仍能回放含旧内容的响应。
    """
    from thinkback.api.idempotency import IdempotencyMiddleware

    app = FastAPI()
    app.add_middleware(IdempotencyMiddleware)
    calls = {"n": 0}

    @app.post("/memory/recall")
    async def route_recall() -> dict[str, str]:
        calls["n"] += 1
        return {"call": str(calls["n"])}

    with TestClient(app) as client:
        headers = {"Idempotency-Key": "recall-read-key"}
        first = client.post("/memory/recall", json={"x": 1}, headers=headers)
        second = client.post("/memory/recall", json={"x": 1}, headers=headers)
        assert first.json() == {"call": "1"}
        assert second.json() == {"call": "2"}
        assert "X-Idempotent-Replay" not in second.headers


def test_expired_idempotency_entry_does_not_conflict() -> None:
    """过期条目必须连冲突索引一起清理，否则新 body 永远 409。"""
    cache = IdempotencyCache()
    expired = CachedResponse(
        body=b"old",
        status_code=200,
        content_type="application/json",
        headers={},
        timestamp=time.time() - CACHE_TTL - 1,
    )
    cache.put("client", "k", "POST", "/memory/a", "hash-old", expired)

    assert cache.get_conflict("client", "k", "POST", "/memory/a", "hash-new") is False
    assert cache.get("client", "k", "POST", "/memory/a", "hash-old") is None
