"""幂等中间件的行为契约（内网免鉴权，/v2/memory/* 为唯一业务面）。"""

from __future__ import annotations

import json
import time

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from thinkback.api import idempotency as idempotency_module
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


# ---------------------------------------------------------------------------
# 取不到调用方标识时的退化路径（此前 0 覆盖）
# ---------------------------------------------------------------------------


def test_a_body_that_is_not_json_falls_back_to_the_peer_domain() -> None:
    """body 不是 JSON → 取不到调用方标识 → 退回对端 IP 分域。

    解析失败不是错误（这种写请求本来就不走幂等语义），但**不能**因此
    把不同调用方压进同一个域 —— 那正是跨调用方数据泄漏的成因。
    """
    assert _caller_identity(b"not json at all") == ""


def test_a_non_object_json_body_falls_back_to_the_peer_domain() -> None:
    """JSON 但不是对象（数组/数字/字符串）→ 同样退回对端 IP 分域。"""
    assert _caller_identity(b"[1, 2, 3]") == ""
    assert _caller_identity(b'"just a string"') == ""


def test_an_empty_body_falls_back_to_the_peer_domain() -> None:
    assert _caller_identity(b"") == ""


def test_a_body_without_a_usable_user_id_falls_back_to_the_peer_domain() -> None:
    """缺 user_id / user_id 不是字符串 / user_id 只有空白 → 都取不到标识。"""
    assert _caller_identity(json.dumps({"namespace": "thinkback"}).encode()) == ""
    assert _caller_identity(json.dumps({"user_id": 42}).encode()) == ""
    assert _caller_identity(json.dumps({"user_id": "   "}).encode()) == ""


def test_a_non_string_namespace_falls_back_to_the_default_namespace() -> None:
    """namespace 不是字符串 → 归一到默认域，而不是留空串。

    留空串会让「省略 namespace」与「显式传默认值」落进两个分域：同一把
    Idempotency-Key 既不重放也不报冲突，写入会真的执行两次。
    """
    identity = _caller_identity(json.dumps({"user_id": "tenant-A", "namespace": 7}).encode())

    assert identity == "thinkback:tenant-A"


# ---------------------------------------------------------------------------
# 缓存容量淘汰与失败响应（此前 0 覆盖）
# ---------------------------------------------------------------------------


def _cached_response(body: bytes = b"{}") -> CachedResponse:
    return CachedResponse(
        body=body,
        status_code=200,
        content_type="application/json",
        headers={},
        timestamp=time.time(),
    )


def test_evicting_the_oldest_entry_also_clears_its_conflict_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LRU 淘汰必须连带把冲突索引里的那条清掉。

    ``_store`` 有上限（MAX_CACHE_SIZE），``_index`` 没有 —— 索引不跟着清理
    的话，它会随着淘汰无限增长，而键对应的响应早就没了。这条路径以前
    从没被走过：缓存一直比上限小，淘汰分支根本进不去。
    """
    monkeypatch.setattr(idempotency_module, "MAX_CACHE_SIZE", 2)
    cache = IdempotencyCache()

    cache.put("scope", "k1", "POST", "/v2/memory/a", "hash-1", _cached_response())
    cache.put("scope", "k2", "POST", "/v2/memory/a", "hash-2", _cached_response())
    cache.put("scope", "k3", "POST", "/v2/memory/a", "hash-3", _cached_response())

    # 最早那条被淘汰了
    assert cache.get("scope", "k1", "POST", "/v2/memory/a", "hash-1") is None
    # 它的冲突索引也必须一起消失，否则 _index 无限增长
    assert ("scope", "k1", "POST", "/v2/memory/a") not in cache._index
    assert not cache.get_conflict("scope", "k1", "POST", "/v2/memory/a", "hash-1")


def test_clear_expired_prunes_entries_past_the_ttl() -> None:
    """过期条目必须能被清掉：TTL 到了之后同一把 key 可以重新用。"""
    cache = IdempotencyCache()
    stale = CachedResponse(
        body=b"{}",
        status_code=200,
        content_type="application/json",
        headers={},
        timestamp=time.time() - CACHE_TTL - 1,
    )
    cache.put("scope", "k1", "POST", "/v2/memory/a", "hash-1", stale)

    assert cache.clear_expired() == 1, "过期条目没被清掉"
    assert cache.get("scope", "k1", "POST", "/v2/memory/a", "hash-1") is None


def test_the_middleware_uses_the_shared_cache_instance() -> None:
    """中间件必须用那个共享的缓存实例。

    另起一个实例 = 每个 worker 各写各的幂等记录，重放与冲突检测在多副本
    下全部失效 —— 而这正是幂等存在的理由。
    """
    from thinkback.api.idempotency import IdempotencyMiddleware, get_idempotency_cache

    middleware = IdempotencyMiddleware(FastAPI())

    assert middleware.cache is get_idempotency_cache()


def test_a_failed_response_is_not_cached_and_can_be_retried() -> None:
    """失败响应不得进缓存：重试必须真的再执行一次。

    把 5xx 缓存起来 = 一次抖动把这次写入永久判成失败，客户端重试多少次
    拿到的都是同一个错误，这次写入再也成功不了。
    """
    from thinkback.api.idempotency import IdempotencyMiddleware

    app = FastAPI()
    app.add_middleware(IdempotencyMiddleware)
    calls = {"count": 0}

    @app.post("/v2/memory/flaky")
    async def flaky() -> JSONResponse:
        calls["count"] += 1
        if calls["count"] == 1:
            return JSONResponse(status_code=503, content={"error": "boom"})
        return JSONResponse(status_code=200, content={"ok": True})

    client = TestClient(app)
    headers = {"idempotency-key": "key-flaky"}

    first = client.post("/v2/memory/flaky", headers=headers)
    second = client.post("/v2/memory/flaky", headers=headers)

    assert first.status_code == 503
    assert second.status_code == 200, "失败响应被缓存了：重试拿到的是旧的失败"
    assert calls["count"] == 2, "第二次请求没有真的执行"
