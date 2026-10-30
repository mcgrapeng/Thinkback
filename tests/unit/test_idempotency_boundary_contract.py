"""幂等缓存的边界契约（api/idempotency.py 进变异范围后的补齐）。

幂等缓存决定「同一把 Idempotency-Key 的第二次写入是重放、冲突，还是真执行」。
三个边界一旦写歪，后果各不相同：

- 容量边界写歪 → 缓存提前腾空，重放失效，写入重复执行；
- TTL 边界写歪 → 条目提前过期（该重放的变成了真执行）或过期太晚（该真执行的
  变成 409）；
- 成功码边界写歪 → 302/304 也被当成成功缓存，重放出一个不该重放的响应。

这些用例此前只走过「正常值」，边界值一个都没钉。
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.testclient import TestClient

from thinkback.api import idempotency as mod
from thinkback.api.idempotency import IdempotencyCache, IdempotencyMiddleware


def _cached(cache: IdempotencyCache, key: str = "k", body_hash: str = "h") -> mod.CachedResponse:
    return mod.CachedResponse(
        body=b"{}",
        status_code=200,
        content_type="application/json",
        headers={},
        timestamp=time.time(),
    )


def test_the_cache_keeps_exactly_max_size_entries() -> None:
    """容量边界：正好装满时**一条都不该被腾**。

    写成了 ``>=`` 的话，装到上限的那一刻就会腾掉一条 —— 明明还在容量内，
    重放却已经失效。
    """
    cache = IdempotencyCache()
    for index in range(mod.MAX_CACHE_SIZE):
        cache.put("scope", f"key-{index}", "POST", "/v2/memory/x", "hash", _cached(cache))

    assert len(cache._store) == mod.MAX_CACHE_SIZE
    assert cache.get("scope", "key-0", "POST", "/v2/memory/x", "hash") is not None


def test_the_cache_evicts_the_oldest_entry_when_it_grows_past_the_limit() -> None:
    """超出一条时腾掉的是**最旧**那条（LRU 语义）。"""
    cache = IdempotencyCache()
    for index in range(mod.MAX_CACHE_SIZE + 1):
        cache.put("scope", f"key-{index}", "POST", "/v2/memory/x", "hash", _cached(cache))

    assert len(cache._store) == mod.MAX_CACHE_SIZE
    assert cache.get("scope", "key-0", "POST", "/v2/memory/x", "hash") is None
    assert cache.get("scope", "key-1", "POST", "/v2/memory/x", "hash") is not None


def test_a_fresh_cache_reports_no_conflict() -> None:
    """空缓存没有冲突 —— 第一次用这把 key 不该撞上 409。"""
    cache = IdempotencyCache()

    assert cache.get_conflict("scope", "key", "POST", "/v2/memory/x", "hash") is False


def test_an_entry_exactly_at_the_ttl_is_still_valid() -> None:
    """TTL 边界：正好等于 TTL 的条目**还没过期**。

    写成了 ``>=`` 的话，条目会在到点那一刻就被清掉 —— 客户端刚要重放，
    服务端已经把它当新请求执行了。
    """
    fixed = 1_700_000_000.0
    cache = IdempotencyCache()
    entry = mod.CachedResponse(
        body=b"{}",
        status_code=200,
        content_type="application/json",
        headers={},
        timestamp=fixed - mod.CACHE_TTL,
    )
    cache.put("scope", "key", "POST", "/v2/memory/x", "hash", entry)

    original_time = time.time
    time.time = lambda: fixed  # noqa: SIM115
    try:
        assert cache.get("scope", "key", "POST", "/v2/memory/x", "hash") is not None
    finally:
        time.time = original_time


def test_an_injected_cache_is_used_instead_of_the_global_one() -> None:
    """注入的缓存必须真的被用上。

    注入被忽略的话，用例看着都绿（全局缓存一样能跑），但测试之间会互相
    污染 —— 这正是全局缓存让「同一把 key 跑第二遍」不确定的根源。
    """
    cache = IdempotencyCache()
    app = FastAPI()
    app.add_middleware(IdempotencyMiddleware, cache=cache)
    calls = {"n": 0}

    @app.post("/v2/memory/x")
    async def route() -> dict[str, int]:
        calls["n"] += 1
        return {"n": calls["n"]}

    headers = {"idempotency-key": "injected"}
    with TestClient(app) as client:
        first = client.post("/v2/memory/x", headers=headers)
        second = client.post("/v2/memory/x", headers=headers)

    assert first.json() == second.json()
    assert calls["n"] == 1
    assert len(cache._store) == 1


def test_a_non_2xx_response_is_not_cached() -> None:
    """只有 2xx 才算成功、才缓存。

    300/304 被缓存的话，重放会返回一个「看起来成功、其实没写进去」的响应。
    """
    cache = IdempotencyCache()
    app = FastAPI()
    app.add_middleware(IdempotencyMiddleware, cache=cache)
    calls = {"n": 0}

    @app.post("/v2/memory/x")
    async def route() -> JSONResponse:
        calls["n"] += 1
        return JSONResponse(status_code=300, content={"n": calls["n"]})

    headers = {"idempotency-key": "redirect"}
    with TestClient(app) as client:
        client.post("/v2/memory/x", headers=headers)
        client.post("/v2/memory/x", headers=headers)

    assert calls["n"] == 2
    assert len(cache._store) == 0


def test_a_streamed_body_is_captured_whole() -> None:
    """流式响应的每一段都要进缓存 —— 少一段，重放就少了内容。"""
    cache = IdempotencyCache()
    app = FastAPI()
    app.add_middleware(IdempotencyMiddleware, cache=cache)

    @app.post("/v2/memory/x")
    async def route() -> StreamingResponse:
        async def chunks():
            yield b"first-"
            yield "second"  # 字符串段也要能被编码后缓存

        return StreamingResponse(chunks(), media_type="text/plain")

    headers = {"idempotency-key": "stream"}
    with TestClient(app) as client:
        first = client.post("/v2/memory/x", headers=headers)
        second = client.post("/v2/memory/x", headers=headers)

    assert first.content == b"first-second"
    assert second.content == b"first-second"


def test_the_request_body_reaches_the_handler() -> None:
    """中间件读过 body 之后要把它还回去 —— 否则下游处理器拿不到请求体。

    写请求的正文被吞掉的话，服务端会按「空请求体」处理这次写入。
    """
    cache = IdempotencyCache()
    app = FastAPI()
    app.add_middleware(IdempotencyMiddleware, cache=cache)
    seen: list[bytes] = []

    @app.post("/v2/memory/x")
    async def route(request: Request) -> dict[str, int]:
        seen.append(await request.body())
        return {"len": len(seen[-1])}

    payload = {"user_id": "u1", "content": "hi"}
    with TestClient(app) as client:
        response = client.post("/v2/memory/x", json=payload)

    expected = json.dumps(payload, separators=(",", ":")).encode()
    assert response.json() == {"len": len(expected)}
    assert seen == [expected]


def test_a_blank_namespace_falls_back_to_the_default_one() -> None:
    """空白 namespace 归一到默认作用域 —— 空白与「省略」是同一个请求。

    归一漏了的话，省略 namespace 与显式传空白 namespace 会落进两个分域：
    同一把 key 既不重放也不报冲突，写入会真的执行两次。
    """
    assert mod._caller_identity(b'{"user_id": "u1", "namespace": "   "}') == "thinkback:u1"


@pytest.mark.parametrize("status", [200, 201, 204])
def test_a_2xx_response_is_cached(status: int) -> None:
    """2xx 各档都要缓存（重放的前提）。"""
    cache = IdempotencyCache()
    app = FastAPI()
    app.add_middleware(IdempotencyMiddleware, cache=cache)
    calls = {"n": 0}

    @app.post("/v2/memory/x")
    async def route() -> JSONResponse:
        calls["n"] += 1
        return JSONResponse(status_code=status, content={"n": calls["n"]})

    headers = {"idempotency-key": f"ok-{status}"}
    with TestClient(app) as client:
        first = client.post("/v2/memory/x", headers=headers)
        second = client.post("/v2/memory/x", headers=headers)

    assert calls["n"] == 1
    assert len(cache._store) == 1
    # 重放出来的正文必须和第一次一模一样：缓存里存的是响应体，存空了
    # 重放就变成了空响应 —— 客户端看到「成功但没有内容」。
    assert second.content == first.content
    assert second.content != b""
    assert second.status_code == first.status_code == status


def test_an_expired_entry_leaves_no_conflict_index_behind() -> None:
    """过期条目要把冲突索引一起清掉 —— 留着空壳就是索引泄漏。

    冲突索引只用来判「同 key 不同 body」。过期条目的哈希还留在索引里，
    过期后换 body 就会被判成冲突 —— 那正是「过期后永远 409」的老毛病。
    """
    fixed = 1_700_000_000.0
    cache = IdempotencyCache()
    entry = mod.CachedResponse(
        body=b"{}",
        status_code=200,
        content_type="application/json",
        headers={},
        timestamp=fixed,
    )

    original_time = time.time
    time.time = lambda: fixed  # noqa: SIM115
    try:
        cache.put("scope", "key", "POST", "/v2/memory/x", "hash", entry)
        assert len(cache._index) == 1

        time.time = lambda: fixed + mod.CACHE_TTL + 1  # noqa: SIM115
        assert cache.clear_expired() == 1
    finally:
        time.time = original_time

    assert len(cache._store) == 0
    assert len(cache._index) == 0


def test_a_response_without_a_content_type_is_still_cached() -> None:
    """没有 content-type 的响应也要能缓存。

    取 content-type 时不能假设它一定在 —— Starlette 的裸 Response 默认不带
    这个头。取不到就应当退回默认类型，而不是抛 KeyError 把整个写请求打成 500。
    """
    cache = IdempotencyCache()
    app = FastAPI()
    app.add_middleware(IdempotencyMiddleware, cache=cache)

    @app.post("/v2/memory/x")
    async def route() -> Response:
        return Response(content=b"plain", status_code=200)

    headers = {"idempotency-key": "no-content-type"}
    with TestClient(app) as client:
        first = client.post("/v2/memory/x", headers=headers)
        second = client.post("/v2/memory/x", headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.content == first.content == b"plain"


def test_the_conflict_body_carries_the_documented_message() -> None:
    """冲突响应的正文也是协议的一部分。

    错误码对了、文案被改写，排查时看到的是「一句无关的话」；兜底的
    request_id 被改掉，客户端就认不出是哪一次请求撞的。
    """
    cache = IdempotencyCache()
    app = FastAPI()
    app.add_middleware(IdempotencyMiddleware, cache=cache)

    @app.post("/v2/memory/x")
    async def route() -> dict[str, int]:
        return {"n": 1}

    headers = {"idempotency-key": "conflict-message"}
    with TestClient(app) as client:
        client.post("/v2/memory/x", json={"user_id": "u1", "x": 1}, headers=headers)
        conflict = client.post("/v2/memory/x", json={"user_id": "u1", "x": 2}, headers=headers)

    assert conflict.status_code == 409
    assert conflict.json() == {
        "error": {
            "code": "TB-1005",
            "message": "Idempotency key reused with different request body",
            "request_id": "unknown",
        }
    }


def test_a_path_outside_the_memory_namespace_is_left_alone() -> None:
    """幂等只管 /v2/memory/* —— 别的路径原样放行。

    早退分支里的 call_next 传错东西，非业务路径的请求就会变成 500。
    """
    cache = IdempotencyCache()
    app = FastAPI()
    app.add_middleware(IdempotencyMiddleware, cache=cache)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert len(cache._store) == 0
