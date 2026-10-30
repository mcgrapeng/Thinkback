"""幂等中间件的行为契约（内网免鉴权，/v2/memory/* 为唯一业务面）。"""

from __future__ import annotations

import json
import time

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.requests import Request

from thinkback.api import idempotency as idempotency_module
from thinkback.api.idempotency import (
    CACHE_TTL,
    CachedResponse,
    IdempotencyCache,
    IdempotencyMiddleware,
    _caller_identity,
)


def _app() -> FastAPI:
    from thinkback.api.idempotency import IdempotencyMiddleware

    app = FastAPI()
    app.add_middleware(IdempotencyMiddleware, cache=IdempotencyCache())

    @app.post("/v2/memory/a")
    async def route_a() -> dict[str, str]:
        return {"route": "a"}

    @app.post("/v2/memory/b")
    async def route_b() -> dict[str, str]:
        return {"route": "b"}

    @app.put("/v2/memory/a")
    async def route_a_put() -> dict[str, str]:
        return {"route": "a-put"}

    @app.delete("/v2/memory/a")
    async def route_a_delete() -> dict[str, str]:
        return {"route": "a-delete"}

    @app.patch("/v2/memory/a")
    async def route_a_patch() -> dict[str, str]:
        return {"route": "a-patch"}

    return app


def _app_with_request_id() -> FastAPI:
    """带 trace 中间件的 app：``request.state.request_id`` 有值。"""

    from thinkback.api.idempotency import IdempotencyMiddleware

    app = FastAPI()
    app.add_middleware(IdempotencyMiddleware, cache=IdempotencyCache())

    @app.middleware("http")
    async def _trace(request, call_next):  # type: ignore[no-untyped-def]
        request.state.request_id = "req-trace-1"
        return await call_next(request)

    @app.post("/v2/memory/a")
    async def route_a() -> dict[str, str]:
        return {"route": "a"}

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
    app.add_middleware(IdempotencyMiddleware, cache=IdempotencyCache())
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


def _request(body: bytes = b"", *, host: str = "10.0.0.7") -> Request:
    """构造一个带对端地址的 Request（取不到调用方标识时的分域依据）。"""

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v2/memory/events",
        "headers": [],
        "query_string": b"",
        "client": (host, 12345),
        "server": ("testserver", 80),
        "scheme": "http",
    }
    return Request(scope, receive=_make_receive(body))


def _make_receive(body: bytes):
    """构造 ASGI 的 receive 回调（Request 构造时要一个可调用的 receive）。"""

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": body, "more_body": False}

    return receive


def test_the_scope_falls_back_to_the_peer_address() -> None:
    """取不到调用方标识时退回对端 IP —— 不能退化成空串（那就是全局一个域）。"""

    request = _request(b"not json")

    scope = IdempotencyMiddleware._resolve_scope(request, b"not json")

    assert scope == "10.0.0.7"


def test_the_scope_prefers_the_caller_identity_over_the_peer_address() -> None:
    """能从请求体取到调用方标识时，就用它分域。"""

    body = json.dumps({"user_id": "tenant-A"}).encode()
    request = _request(body)

    scope = IdempotencyMiddleware._resolve_scope(request, body)

    assert scope == "thinkback:tenant-A"


def test_a_request_without_a_client_still_gets_a_scope() -> None:
    """没有对端地址时退回空串，但**不能**抛异常 —— 那会让整条写路径 500。"""

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v2/memory/events",
        "headers": [],
        "query_string": b"",
        "client": None,
        "server": ("testserver", 80),
        "scheme": "http",
    }
    request = Request(scope, receive=_make_receive(b""))

    assert IdempotencyMiddleware._resolve_scope(request, b"") == ""


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


def test_a_conflict_answers_with_the_documented_error_code() -> None:
    """冲突响应的契约：错误码 TB-1005 + 409。

    客户端按错误码分支处理；码变了就是静默的协议变更。
    """
    with TestClient(_app()) as client:
        headers = {"Idempotency-Key": "conflict-code-key"}
        client.post("/v2/memory/a", json={"user_id": "u"}, headers=headers)
        conflict = client.post("/v2/memory/a", json={"user_id": "u", "x": 2}, headers=headers)

        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "TB-1005"
        assert "request_id" in conflict.json()["error"]


def test_the_cache_key_separates_scope_key_method_path_and_body() -> None:
    """缓存键的每一维都要参与：少一维就是一次跨端点 / 跨调用方的串味。"""

    cache = IdempotencyCache()
    cache.put("scope-a", "k", "POST", "/v2/memory/a", "hash-1", _cached_response())

    assert cache.get("scope-b", "k", "POST", "/v2/memory/a", "hash-1") is None
    assert cache.get("scope-a", "other", "POST", "/v2/memory/a", "hash-1") is None
    assert cache.get("scope-a", "k", "PUT", "/v2/memory/a", "hash-1") is None
    assert cache.get("scope-a", "k", "POST", "/v2/memory/b", "hash-1") is None
    assert cache.get("scope-a", "k", "POST", "/v2/memory/a", "hash-2") is None
    assert cache.get("scope-a", "k", "POST", "/v2/memory/a", "hash-1") is not None


def test_an_expired_entry_stops_conflicting_with_a_new_body() -> None:
    """过期后换 body 必须放行：否则「过期后永远 409」，这把 key 就废了。"""

    cache = IdempotencyCache()
    stale = CachedResponse(
        body=b"{}",
        status_code=200,
        content_type="application/json",
        headers={},
        timestamp=time.time() - CACHE_TTL - 1,
    )
    cache.put("scope", "k", "POST", "/v2/memory/a", "hash-old", stale)

    assert cache.get_conflict("scope", "k", "POST", "/v2/memory/a", "hash-new") is False


def test_a_different_body_under_the_same_key_is_a_conflict() -> None:
    cache = IdempotencyCache()
    cache.put("scope", "k", "POST", "/v2/memory/a", "hash-1", _cached_response())

    assert cache.get_conflict("scope", "k", "POST", "/v2/memory/a", "hash-2") is True
    assert cache.get_conflict("scope", "k", "POST", "/v2/memory/a", "hash-1") is False


def test_a_key_prefix_is_not_a_conflict() -> None:
    """key ``a`` 与 key ``a:b`` 是两把 key —— 前缀匹配会把它们误判成冲突。"""

    cache = IdempotencyCache()
    cache.put("scope", "a", "POST", "/v2/memory/a", "hash-1", _cached_response())

    assert cache.get_conflict("scope", "a:b", "POST", "/v2/memory/a", "hash-2") is False


def test_a_redirect_response_is_not_cached() -> None:
    """只有 2xx 进幂等缓存 —— 3xx 是重定向，不是「这次写入的结果」。"""

    from fastapi.responses import RedirectResponse

    from thinkback.api.idempotency import IdempotencyMiddleware

    app = FastAPI()
    app.add_middleware(IdempotencyMiddleware, cache=IdempotencyCache())

    @app.post("/v2/memory/redirect")
    async def route_redirect():
        return RedirectResponse("/v2/memory/a", status_code=300)

    with TestClient(app) as client:
        headers = {"Idempotency-Key": "redirect-key"}
        first = client.post("/v2/memory/redirect", json={"x": 1}, headers=headers)
        second = client.post("/v2/memory/redirect", json={"x": 1}, headers=headers)

        assert first.status_code == 300
        assert second.headers.get("X-Idempotent-Replay") is None


def test_an_entry_exactly_at_the_ttl_is_still_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    """TTL 边界：恰好等于 TTL 的条目**还没过期**（判定是 `>` 不是 `>=`）。

    差这一秒的边界写反了，条目会在到期前一刻被清掉 —— 幂等重放随即失效，
    同一把 key 会被执行两次。
    """

    now = 1_000_000.0
    monkeypatch.setattr(idempotency_module.time, "time", lambda: now)
    cache = IdempotencyCache()
    cache.put(
        "scope",
        "k",
        "POST",
        "/v2/memory/a",
        "hash",
        CachedResponse(
            body=b"{}",
            status_code=200,
            content_type="application/json",
            headers={},
            timestamp=now - CACHE_TTL,
        ),
    )

    assert cache.clear_expired() == 0
    assert cache.get("scope", "k", "POST", "/v2/memory/a", "hash") is not None


def test_an_entry_past_the_ttl_is_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    """对照组：过了 TTL 就该被清掉（边界用例的另一侧）。"""

    now = 1_000_000.0
    monkeypatch.setattr(idempotency_module.time, "time", lambda: now)
    cache = IdempotencyCache()
    cache.put(
        "scope",
        "k",
        "POST",
        "/v2/memory/a",
        "hash",
        CachedResponse(
            body=b"{}",
            status_code=200,
            content_type="application/json",
            headers={},
            timestamp=now - CACHE_TTL - 1,
        ),
    )

    assert cache.clear_expired() == 1


def test_a_hit_refreshes_the_entry_position() -> None:
    """命中的条目要移到队尾 —— 否则「最近用过」的会被先淘汰。"""

    cache = IdempotencyCache()
    cache.put("scope", "k1", "POST", "/v2/memory/a", "hash-1", _cached_response())
    cache.put("scope", "k2", "POST", "/v2/memory/a", "hash-2", _cached_response())

    assert cache.get("scope", "k1", "POST", "/v2/memory/a", "hash-1") is not None
    assert list(cache._store)[0][1] == "k2"


# ---------------------------------------------------------------------------
# 分域：namespace / user_id 是「谁在写」，写错就是跨调用方串味
# ---------------------------------------------------------------------------


def test_an_explicit_namespace_becomes_part_of_the_scope() -> None:
    """显式 namespace 必须进域 —— 归一成默认域 = 两个 namespace 共用一个域。"""

    identity = _caller_identity(json.dumps({"user_id": "u", "namespace": "acme"}).encode())

    assert identity == "acme:u"


def test_a_blank_namespace_falls_back_to_the_default() -> None:
    """空白 namespace 与「省略」是同一个请求 —— 域必须一样。"""

    identity = _caller_identity(json.dumps({"user_id": "u", "namespace": "   "}).encode())

    assert identity == "thinkback:u"


def test_the_default_namespace_is_used_when_it_is_omitted() -> None:
    assert _caller_identity(json.dumps({"user_id": "u"}).encode()) == "thinkback:u"


# ---------------------------------------------------------------------------
# 写方法都要受保护
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "route", "expected"),
    [
        ("post", "/v2/memory/a", {"route": "a"}),
        ("put", "/v2/memory/a", {"route": "a-put"}),
        ("delete", "/v2/memory/a", {"route": "a-delete"}),
        ("patch", "/v2/memory/a", {"route": "a-patch"}),
    ],
)
def test_every_write_method_is_protected_by_the_idempotency_key(
    method: str, route: str, expected: dict[str, str]
) -> None:
    """POST / PUT / DELETE / PATCH 都要受幂等保护 —— 漏一个就少一道闸。"""

    with TestClient(_app()) as client:
        headers = {"Idempotency-Key": f"method-{method}"}
        first = client.request(method, route, json={"x": 1}, headers=headers)
        second = client.request(method, route, json={"x": 1}, headers=headers)

        assert first.status_code == 200
        assert first.json() == expected
        assert second.json() == expected
        assert second.headers.get("X-Idempotent-Replay") == "true"


def test_a_request_without_the_key_is_not_cached() -> None:
    """没有 key 的请求不进幂等缓存 —— 否则所有无 key 的写共用一个域。"""

    with TestClient(_app()) as client:
        first = client.post("/v2/memory/a", json={"x": 1})
        second = client.post("/v2/memory/a", json={"x": 1})

        assert first.status_code == 200
        assert second.status_code == 200
        assert second.headers.get("X-Idempotent-Replay") is None


# ---------------------------------------------------------------------------
# 重放必须原样返回：状态码 / 内容类型 / 响应头
# ---------------------------------------------------------------------------


def test_the_replay_preserves_the_status_code_and_content_type() -> None:
    """重放要把原响应的状态码与内容类型带回来 —— 否则调用方按状态码分支会走错。"""

    with TestClient(_app()) as client:
        headers = {"Idempotency-Key": "preserve-meta"}
        first = client.post("/v2/memory/a", json={"x": 1}, headers=headers)
        second = client.post("/v2/memory/a", json={"x": 1}, headers=headers)

        assert second.status_code == first.status_code == 200
        assert second.headers["content-type"] == first.headers["content-type"]


def test_the_replay_preserves_the_original_response_headers() -> None:
    """原响应的头必须透传 —— 丢头会把 trace 中间件设的 X-Trace-Id 抹掉。"""

    with TestClient(_app()) as client:
        headers = {"Idempotency-Key": "preserve-headers"}
        first = client.post("/v2/memory/a", json={"x": 1}, headers=headers)
        second = client.post("/v2/memory/a", json={"x": 1}, headers=headers)

        assert second.headers.get("content-length") == first.headers.get("content-length")


def test_the_conflict_error_carries_the_request_id() -> None:
    """冲突响应要带上 request_id —— 排查时能对上是哪一次请求。"""

    with TestClient(_app_with_request_id()) as client:
        headers = {"Idempotency-Key": "conflict-request-id"}
        client.post("/v2/memory/a", json={"user_id": "u"}, headers=headers)
        conflict = client.post("/v2/memory/a", json={"user_id": "u", "x": 2}, headers=headers)

        assert conflict.status_code == 409
        assert conflict.json()["error"]["request_id"] == "req-trace-1"


# ---------------------------------------------------------------------------
# 缓存容量：淘汰顺序与触发时机
# ---------------------------------------------------------------------------


def test_the_eviction_keeps_the_newest_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    """淘汰必须从最旧的开始 —— 淘汰最新的等于把刚写的记忆扔了。"""

    monkeypatch.setattr(idempotency_module, "MAX_CACHE_SIZE", 2)
    cache = IdempotencyCache()
    cache.put("scope", "k1", "POST", "/v2/memory/a", "hash-1", _cached_response())
    cache.put("scope", "k2", "POST", "/v2/memory/a", "hash-2", _cached_response())
    cache.put("scope", "k3", "POST", "/v2/memory/a", "hash-3", _cached_response())

    assert cache.get("scope", "k1", "POST", "/v2/memory/a", "hash-1") is None
    assert cache.get("scope", "k3", "POST", "/v2/memory/a", "hash-3") is not None


def test_the_cache_holds_exactly_the_limit_before_evicting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """装到上限不该开始淘汰 —— 刚好等于上限时两条都得在。"""

    monkeypatch.setattr(idempotency_module, "MAX_CACHE_SIZE", 2)
    cache = IdempotencyCache()
    cache.put("scope", "k1", "POST", "/v2/memory/a", "hash-1", _cached_response())
    cache.put("scope", "k2", "POST", "/v2/memory/a", "hash-2", _cached_response())

    assert cache.get("scope", "k1", "POST", "/v2/memory/a", "hash-1") is not None
    assert cache.get("scope", "k2", "POST", "/v2/memory/a", "hash-2") is not None


def test_the_conflict_index_is_cleaned_when_an_entry_expires() -> None:
    """过期条目被清掉后，它的冲突索引也必须一起清 —— 否则索引无限增长。"""

    cache = IdempotencyCache()
    stale = CachedResponse(
        body=b"{}",
        status_code=200,
        content_type="application/json",
        headers={},
        timestamp=time.time() - CACHE_TTL - 1,
    )
    cache.put("scope", "k", "POST", "/v2/memory/a", "hash-old", stale)

    cache.clear_expired()

    assert ("scope", "k", "POST", "/v2/memory/a") not in cache._index


def test_the_injected_cache_is_the_one_that_gets_used() -> None:
    """注入的缓存必须真的被用 —— 否则「每条用例自己的缓存」是假的。"""

    from thinkback.api.idempotency import IdempotencyMiddleware

    cache = IdempotencyCache()
    middleware = IdempotencyMiddleware(FastAPI(), cache=cache)

    assert middleware.cache is cache


def test_the_default_cache_is_still_the_shared_instance() -> None:
    """不注入时必须用全局共享实例 —— 多副本下幂等的前提。"""

    from thinkback.api.idempotency import IdempotencyMiddleware, get_idempotency_cache

    assert IdempotencyMiddleware(FastAPI()).cache is get_idempotency_cache()


def test_a_failed_response_is_not_cached_and_can_be_retried() -> None:
    """失败响应不得进缓存：重试必须真的再执行一次。

    把 5xx 缓存起来 = 一次抖动把这次写入永久判成失败，客户端重试多少次
    拿到的都是同一个错误，这次写入再也成功不了。
    """
    from thinkback.api.idempotency import IdempotencyMiddleware

    app = FastAPI()
    app.add_middleware(IdempotencyMiddleware, cache=IdempotencyCache())
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
