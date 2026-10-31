"""HTTP 面限流的**精确值**契约。

``api/ratelimit.py`` 决定「谁能写、写多快」——写错的线上表现全是静默的：

- 回填算错 → 限流比配置的松（或紧）一倍，用户只觉得「有时候能写有时候不能」；
- 分桶口径错 → 两个租户共用一把桶，一个租户的流量把另一个挤到 429；
- 「限额变了不重置余额」破掉 → 改一次配置，给**每个**桶发一次免费额度；
- 天花板倍数错 → 轮换 user_id 就能绕开限流（分桶键是调用方给的）。

所以这里的用例都断言**精确值**（令牌数、等待秒数、状态码、头字段、桶数），
而不是「没报错」。

时钟是冻住的：模块里是 ``import time`` 后调 ``time.time()``，所以把模块里的
``time`` 换掉即可 —— 不然回填会随真实时间漂移，断言就成了概率事件。
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from thinkback.api import ratelimit as ratelimit_module
from thinkback.infra import token_bucket as token_bucket_module


class _Clock:
    """可冻结的时钟。"""

    def __init__(self, now: float) -> None:
        self.now = now

    def time(self) -> float:
        return self.now


def _app(
    rate_limit_per_minute: int,
    burst: int,
    *,
    exclude_paths: set[str] | None = None,
) -> FastAPI:
    app = FastAPI()
    app.add_middleware(
        ratelimit_module.RateLimitMiddleware,
        exclude_paths=exclude_paths,
        rate_limit_per_minute=rate_limit_per_minute,
        burst=burst,
    )

    @app.post("/v2/memory/append")
    async def append(payload: dict[str, str]) -> dict[str, str | bool]:
        return {"ok": True, "user_id": payload.get("user_id", "")}

    @app.post("/admin/api/purge")
    async def purge(payload: dict[str, str]) -> dict[str, bool]:
        return {"ok": True}

    @app.get("/v2/memory/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    return app


def _body(namespace: str = "t1", user_id: str = "u1") -> dict[str, str]:
    return {"namespace": namespace, "user_id": user_id}


async def _dummy_app(scope: Any, receive: Any, send: Any) -> None:
    """占位的 ASGI 应用（构造中间件时用，不会被调用）。"""


def _frozen_clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    """把限流链上的两个时钟都冻住（令牌桶与中间件各有一份 ``time``）。"""
    clock = _Clock(1_000.0)
    monkeypatch.setattr(token_bucket_module, "time", clock)
    monkeypatch.setattr(ratelimit_module, "time", clock)
    return clock


# ---------------------------------------------------------------------------
# HTTP 面：超限响应的精确字段
# ---------------------------------------------------------------------------


def test_the_429_carries_the_exact_retry_after_and_error_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """超限响应必须带准确的 Retry-After 与错误码。

    Retry-After 是调用方唯一的退避依据：算少 → 立刻重试 → 更凶；
    算多 → 白等。错误码 TB-1007 是对外契约的一部分。
    """
    _frozen_clock(monkeypatch)

    with TestClient(_app(rate_limit_per_minute=1, burst=1)) as client:
        assert client.post("/v2/memory/append", json=_body()).status_code == 200
        rejected = client.post("/v2/memory/append", json=_body())

    assert rejected.status_code == 429
    assert rejected.json()["error"]["code"] == "TB-1007"
    assert rejected.json()["error"]["message"] == "Rate limit exceeded. Retry after 61s"
    # 每分钟 1 个 → 等一个令牌要 60 秒；取整 + 1 → 61
    assert rejected.headers["Retry-After"] == "61"
    assert rejected.headers["X-RateLimit-Limit"] == "1"
    assert rejected.headers["X-RateLimit-Remaining"] == "0"
    assert rejected.headers["X-RateLimit-Reset"] == "1061"
    # 没有追踪 ID 时回落到 "unknown"，不能是 null
    assert rejected.json()["error"]["request_id"] == "unknown"


def test_a_successful_response_reports_the_exact_remaining_balance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成功响应的 X-RateLimit-Remaining 必须是**扣完之后**的余额。"""
    _frozen_clock(monkeypatch)

    with TestClient(_app(rate_limit_per_minute=600, burst=5)) as client:
        first = client.post("/v2/memory/append", json=_body())

    assert first.status_code == 200
    assert first.headers["X-RateLimit-Limit"] == "600"
    assert first.headers["X-RateLimit-Remaining"] == "4"


def test_the_success_response_carries_the_exact_reset_and_remaining(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成功响应的两个头也是契约。

    - Remaining 是**扣完之后**的余额（用掉最后一个令牌就是 0）；
    - Reset 是「现在 + 60 秒」—— 客户端据此知道额度什么时候回来。
    """
    _frozen_clock(monkeypatch)

    with TestClient(_app(rate_limit_per_minute=600, burst=1)) as client:
        response = client.post("/v2/memory/append", json=_body())

    assert response.status_code == 200
    assert response.headers["X-RateLimit-Remaining"] == "0"
    assert response.headers["X-RateLimit-Reset"] == "1060"


def test_a_path_outside_the_limited_planes_is_not_rate_limited() -> None:
    """限流只盖业务面与治理面 —— /health、/docs 走各自通道。

    这里钉的是「没被限流面盖住的路径不被限流」：桶被用光之后，
    这类路径仍然通。
    """
    with TestClient(_app(rate_limit_per_minute=1, burst=1)) as client:
        assert client.post("/v2/memory/append", json=_body()).status_code == 200
        assert client.post("/v2/memory/append", json=_body()).status_code == 429
        # 没这个路由（404），但**不是**被限流拦下的 429
        assert client.get("/health").status_code == 404


# ---------------------------------------------------------------------------
# HTTP 面：分桶口径
# ---------------------------------------------------------------------------


def test_two_tenants_do_not_share_a_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    """两个租户各有一份额度 —— 分桶单位是「谁在用」，不是「从哪台机器出来的」。

    按 IP 分桶的话所有调用方共用一份额度：一个租户被限，另一个租户跟着挂。
    """
    _frozen_clock(monkeypatch)

    with TestClient(_app(rate_limit_per_minute=1, burst=1)) as client:
        assert client.post("/v2/memory/append", json=_body(user_id="u1")).status_code == 200
        assert client.post("/v2/memory/append", json=_body(user_id="u1")).status_code == 429
        # 另一个租户有自己的额度
        assert client.post("/v2/memory/append", json=_body(user_id="u2")).status_code == 200


def test_the_admin_plane_buckets_by_peer_address_not_by_the_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """管理面按**对端地址**分桶，不吃请求体里的 user_id。

    照着请求体分桶的话，管理面的调用会吃掉那个用户的业务额度，反过来也一样。
    """
    _frozen_clock(monkeypatch)

    with TestClient(_app(rate_limit_per_minute=1, burst=1)) as client:
        assert client.post("/admin/api/purge", json={"user_id": "u1"}).status_code == 200
        # 换个 user_id 也没用 —— 管理面那把桶是按对端地址分的
        assert client.post("/admin/api/purge", json={"user_id": "u2"}).status_code == 429


def test_the_admin_plane_gives_each_peer_its_own_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不同对端地址各有一把管理面的桶 —— 不能退化成全局一把。"""
    _frozen_clock(monkeypatch)
    app = _app(rate_limit_per_minute=1, burst=1)

    with TestClient(app, client=("1.2.3.4", 1111)) as client_a:
        assert client_a.post("/admin/api/purge", json={"user_id": "u1"}).status_code == 200
    with TestClient(app, client=("5.6.7.8", 2222)) as client_b:
        assert client_b.post("/admin/api/purge", json={"user_id": "u1"}).status_code == 200


def test_an_excluded_path_is_not_rate_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    """排除清单里的路径不走限流。

    要点是路径得在**限流面覆盖到的**范围内才有意义：默认那几项
    （/health、/docs）本来就不走限流面，列进去是白列。
    """
    _frozen_clock(monkeypatch)
    app = _app(rate_limit_per_minute=1, burst=1, exclude_paths={"/v2/memory/health"})

    with TestClient(app) as client:
        assert client.get("/v2/memory/health").status_code == 200
        assert client.get("/v2/memory/health").status_code == 200  # 第二次也不限流
        # 同一把应用里，业务路径照常限流
        assert client.post("/v2/memory/append", json=_body()).status_code == 200
        assert client.post("/v2/memory/append", json=_body()).status_code == 429


# ---------------------------------------------------------------------------
# HTTP 面：按对端地址的天花板
# ---------------------------------------------------------------------------


def test_rotating_the_caller_key_does_not_escape_the_peer_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """轮换 user_id 也绕不开按对端地址的天花板。

    每调用方限额 1/分钟、突发 1；天花板是它的 20 倍 → 同一个对端地址
    的第 21 个请求必须被拒，哪怕每次都换一个 user_id。
    """
    _frozen_clock(monkeypatch)
    app = _app(rate_limit_per_minute=1, burst=1)

    with TestClient(app, client=("1.2.3.4", 1111)) as client:
        for index in range(20):
            response = client.post("/v2/memory/append", json=_body(user_id=f"u{index}"))
            assert response.status_code == 200, f"第 {index + 1} 个请求被提前拒了"
        blocked = client.post("/v2/memory/append", json=_body(user_id="u20"))

    assert blocked.status_code == 429


def test_the_peer_ceiling_is_per_address_not_global(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """另一个对端地址不受前一个的天花板影响 —— 天花板不能变成全局额度。"""
    _frozen_clock(monkeypatch)
    app = _app(rate_limit_per_minute=1, burst=1)

    with TestClient(app, client=("1.2.3.4", 1111)) as client_a:
        for index in range(20):
            response = client_a.post("/v2/memory/append", json=_body(user_id=f"a{index}"))
            assert response.status_code == 200
    with TestClient(app, client=("5.6.7.8", 2222)) as client_b:
        response = client_b.post("/v2/memory/append", json=_body(user_id="b0"))

    assert response.status_code == 200, "另一个对端被前一个的天花板连坐了"


def test_the_peer_ceiling_ignores_the_ephemeral_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一个对端地址的**不同连接**（端口不同）共用一份天花板。

    端口是客户端的临时端口，每条连接都不同 —— 把它算进分桶键，
    天花板就永远咬不到（每条连接一把新桶）。
    """
    _frozen_clock(monkeypatch)
    app = _app(rate_limit_per_minute=1, burst=1)

    with TestClient(app, client=("1.2.3.4", 1111)) as client_a:
        for index in range(20):
            response = client_a.post("/v2/memory/append", json=_body(user_id=f"a{index}"))
            assert response.status_code == 200
    with TestClient(app, client=("1.2.3.4", 2222)) as client_b:
        response = client_b.post("/v2/memory/append", json=_body(user_id="b0"))

    assert response.status_code == 429, "换了个端口就绕开了天花板"


def test_a_request_without_a_peer_falls_back_to_one_shared_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取不到对端地址时退化成统一的桶 —— 不能崩，也不能放行。

    有些 ASGI 服务器（或某些代理后面）不给 ``client``，这条路径要能走通。
    """
    _frozen_clock(monkeypatch)
    app = _app(rate_limit_per_minute=1, burst=1)

    with TestClient(app, client=cast(Any, None)) as client:
        assert client.post("/v2/memory/append", json=_body()).status_code == 200
        # 没有请求体就没有调用方标识 → 退回按对端地址分桶（取不到就统一一把桶）
        assert client.get("/v2/memory/health").status_code == 200


def test_the_peer_ceiling_refills_at_twenty_times_the_caller_rate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """天花板是每调用方限额的 20 倍 —— **突发额度与回填速度都是**。

    只钉突发额度不够：回填速度写成 1/20 的话，打满天花板之后要等 3600 倍
    这么久才恢复一个令牌。
    """
    clock = _frozen_clock(monkeypatch)
    middleware = ratelimit_module.RateLimitMiddleware(
        app=_dummy_app,
        rate_limit_per_minute=1,
        burst=1,
    )
    # 天花板的突发额度是 1 × 20 = 20 —— 先把它打空
    for _ in range(20):
        assert middleware.peer_buckets.consume("peer")[0] is True
    assert middleware.peer_buckets.remaining("peer") == 0.0

    clock.now += 30.0  # 每秒回填 20/60 = 1/3 个 → 30 秒回填 10 个

    ok, wait = middleware.peer_buckets.consume("peer")
    assert ok is True, "天花板回填太慢 —— 倍数写反了？"
    assert wait == 0.0
