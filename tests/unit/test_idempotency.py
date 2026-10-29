"""幂等中间件的行为契约（内网免鉴权，/v2/memory/* 为唯一业务面）。"""

from __future__ import annotations

import json
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from thinkback.api.idempotency import (
    CACHE_TTL,
    CachedResponse,
    IdempotencyCache,
    _caller_identity,
)


def _app() -> FastAPI:
    from thinkback.api.idempotency import IdempotencyMiddleware

    app = FastAPI()
    app.add_middleware(IdempotencyMiddleware)

    @app.post("/v2/memory/a")
    async def route_a() -> dict[str, str]:
        return {"route": "a"}

    @app.post("/v2/memory/b")
    async def route_b() -> dict[str, str]:
        return {"route": "b"}

    return app


def test_idempotency_key_does_not_cross_endpoints() -> None:
    """同一把 key 用在不同端点：recall 不得领走 append 的缓存响应。"""
    with TestClient(_app()) as client:
        headers = {"Idempotency-Key": "shared-endpoint-key"}
        r_a = client.post("/v2/memory/a", json={"x": 1}, headers=headers)
        r_b = client.post("/v2/memory/b", json={"x": 1}, headers=headers)
        assert r_a.json() == {"route": "a"}
        assert r_b.json() == {"route": "b"}


def test_idempotent_replay_on_same_route() -> None:
    """同 key + 同 body + 同端点：第二次直接回放，不重复执行。"""
    with TestClient(_app()) as client:
        headers = {"Idempotency-Key": "same-route-key"}
        r1 = client.post("/v2/memory/a", json={"x": 1}, headers=headers)
        r2 = client.post("/v2/memory/a", json={"x": 1}, headers=headers)
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

    @app.post("/v2/memory/recall")
    async def route_recall() -> dict[str, str]:
        calls["n"] += 1
        return {"call": str(calls["n"])}

    with TestClient(app) as client:
        headers = {"Idempotency-Key": "recall-read-key"}
        first = client.post("/v2/memory/recall", json={"x": 1}, headers=headers)
        second = client.post("/v2/memory/recall", json={"x": 1}, headers=headers)
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
    cache.put("client", "k", "POST", "/v2/memory/a", "hash-old", expired)

    assert cache.get_conflict("client", "k", "POST", "/v2/memory/a", "hash-new") is False
    assert cache.get("client", "k", "POST", "/v2/memory/a", "hash-old") is None


def test_same_idempotency_key_from_different_callers_does_not_conflict() -> None:
    """两个不同调用方用同一把 key：不能被判成冲突拒写。

    内网免鉴权后，域一度退化成「直连对端 IP」——k8s / ingress / sidecar 部署下
    所有外部调用方的 IP 都是网关地址，域被压平成一个全局：一个客户端用过 key
    ``k`` 之后，别的客户端再用 ``k``（body 不同）直接 409，一把 key 就能堵死
    别人的写路径。域必须是调用方：请求体里的 namespace + user_id。
    """
    with TestClient(_app()) as client:
        headers = {"Idempotency-Key": "shared-key"}
        r_a = client.post(
            "/v2/memory/a", json={"user_id": "tenant-A", "namespace": "acme"}, headers=headers
        )
        r_b = client.post(
            "/v2/memory/a", json={"user_id": "tenant-B", "namespace": "acme"}, headers=headers
        )
        assert r_a.status_code == 200
        assert r_b.status_code == 200, "不同调用方用同一把 key 不该被判冲突"


def test_same_caller_reusing_key_with_different_body_still_conflicts() -> None:
    """同一个调用方换 body 重用 key：仍然 409（幂等键的语义没有被放松）。"""
    with TestClient(_app()) as client:
        headers = {"Idempotency-Key": "same-caller-key"}
        r1 = client.post(
            "/v2/memory/a", json={"user_id": "tenant-A", "namespace": "acme"}, headers=headers
        )
        r2 = client.post(
            "/v2/memory/a",
            json={"user_id": "tenant-A", "namespace": "acme", "x": 2},
            headers=headers,
        )
        assert r1.status_code == 200
        assert r2.status_code == 409


def test_caller_identity_normalizes_an_omitted_namespace_to_the_default() -> None:
    """省略 namespace 与显式传默认值是**同一个请求**：域必须一样。

    请求模型会把缺省的 namespace 填成默认值，所以两种写法等价。域若因此
    分成两个，同一把 Idempotency-Key 既不重放也不报冲突，写入会执行两次。
    """
    omitted = _caller_identity(json.dumps({"user_id": "tenant-A"}).encode())
    explicit = _caller_identity(
        json.dumps({"user_id": "tenant-A", "namespace": "thinkback"}).encode()
    )

    assert omitted == explicit == "thinkback:tenant-A"


def test_omitted_namespace_shares_the_idempotency_scope_with_the_explicit_default() -> None:
    """同一把 key + 省略/显式 namespace：必须判成冲突，不能静默执行第二次。"""
    with TestClient(_app()) as client:
        headers = {"Idempotency-Key": "default-namespace-key"}
        r1 = client.post("/v2/memory/a", json={"user_id": "tenant-A"}, headers=headers)
        r2 = client.post(
            "/v2/memory/a",
            json={"user_id": "tenant-A", "namespace": "thinkback"},
            headers=headers,
        )
        assert r1.status_code == 200
        assert r2.status_code == 409, "同一请求的两种写法落进两个域，写入会执行两次"


def test_a_caller_with_and_without_explicit_namespace_shares_one_rate_limit_bucket() -> None:
    """同一个调用方不得因为写法不同而拿到两份限流额度。

    幂等层与限流层用的是同一个 ``_caller_identity``：域口径不一致时，
    一个调用方能绕开限流，也绕开幂等。
    """
    from thinkback.api.ratelimit import _caller_identity as rate_limit_identity

    omitted = rate_limit_identity(json.dumps({"user_id": "tenant-A"}).encode())
    explicit = rate_limit_identity(
        json.dumps({"user_id": "tenant-A", "namespace": "thinkback"}).encode()
    )

    assert omitted == explicit
