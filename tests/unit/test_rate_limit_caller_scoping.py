"""限流分桶单位：调用方，不是网络位置。

服务化接入时所有调用方都从同一个出口 IP 出来（k8s / ingress / sidecar），
一个 IP 一个桶等于全服务共享一份额度 —— 一个多租户调用方（每个租户一次召回
+ 一次写入）会被**别的租户**挤到 429。

分桶单位必须是「谁在用」：请求体里的 ``namespace`` + ``user_id``，
与幂等层（``api/idempotency.py``）用的是同一口径。
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from thinkback.api.ratelimit import RateLimitMiddleware


def _app(rate_limit_per_minute: int, burst: int) -> FastAPI:
    app = FastAPI()
    app.add_middleware(
        RateLimitMiddleware,
        rate_limit_per_minute=rate_limit_per_minute,
        burst=burst,
    )

    @app.post("/v2/memory/append")
    async def append(payload: dict[str, str]) -> dict[str, str | bool]:
        return {"ok": True, "user_id": payload.get("user_id", "")}

    @app.get("/admin/api/memories")
    async def admin_memories() -> dict[str, bool]:
        return {"ok": True}

    return app


def _body(namespace: str, user_id: str) -> dict[str, str]:
    return {"namespace": namespace, "user_id": user_id}


def test_tenants_sharing_one_ip_do_not_share_a_rate_limit_bucket() -> None:
    """同一 IP 下的不同租户各用自己的额度。

    限流取 1/分钟、突发 2：单租户连打 3 次会被拒，但换租户后应当照常放行。
    """
    with TestClient(_app(rate_limit_per_minute=1, burst=2)) as client:
        # 租户 A 用掉自己的突发额度
        assert client.post("/v2/memory/append", json=_body("t1", "u1")).status_code == 200
        assert client.post("/v2/memory/append", json=_body("t1", "u1")).status_code == 200
        rejected = client.post("/v2/memory/append", json=_body("t1", "u1"))
        assert rejected.status_code == 429
        assert rejected.json()["error"]["code"] == "TB-1007"

        # 租户 B 在同一个 IP 下：额度独立，不该被 A 挤掉
        assert client.post("/v2/memory/append", json=_body("t2", "u1")).status_code == 200
        assert client.post("/v2/memory/append", json=_body("t2", "u1")).status_code == 200
        assert client.post("/v2/memory/append", json=_body("t2", "u1")).status_code == 429


def test_same_tenant_different_users_have_separate_buckets() -> None:
    """同租户不同用户也是不同调用方：不能互相挤掉。"""
    with TestClient(_app(rate_limit_per_minute=1, burst=2)) as client:
        for _ in range(2):
            assert client.post("/v2/memory/append", json=_body("t1", "u1")).status_code == 200
        assert client.post("/v2/memory/append", json=_body("t1", "u1")).status_code == 429
        assert client.post("/v2/memory/append", json=_body("t1", "u2")).status_code == 200


def test_requests_without_identity_still_share_the_peer_ip_bucket() -> None:
    """取不到调用方标识（无请求体 / 非业务字段）时退回对端 IP，行为不变。"""
    with TestClient(_app(rate_limit_per_minute=1, burst=2)) as client:
        for _ in range(2):
            assert client.post("/v2/memory/append", json={"note": "无身份"}).status_code == 200
        assert client.post("/v2/memory/append", json={"note": "无身份"}).status_code == 429


def test_body_is_still_available_downstream_after_rate_limit_reads_it() -> None:
    """限流读过的请求体，下游必须还能拿到完整内容。

    读 body 换分桶口径的前提是「读body 不吃掉请求体」——
    Starlette 的 ``_CachedRequest`` 会把 body 缓存并向下游重放。
    """
    with TestClient(_app(rate_limit_per_minute=600, burst=50)) as client:
        response = client.post("/v2/memory/append", json=_body("t1", "u42"))
        assert response.status_code == 200
        assert response.json()["user_id"] == "u42"


def test_the_admin_plane_is_rate_limited() -> None:
    """管理面能删任意用户的全部记忆、还能列出全库 —— 免鉴权也得有速率上限。

    免鉴权（D10）把「谁能调」交给了网络边界；速率上限管的是「一次能调多少」。
    两者管的不是一回事，不能因为没有鉴权就把速率也一并放开。
    """
    with TestClient(_app(rate_limit_per_minute=1, burst=2)) as client:
        assert client.get("/admin/api/memories").status_code == 200
        assert client.get("/admin/api/memories").status_code == 200
        rejected = client.get("/admin/api/memories")
        assert rejected.status_code == 429
        assert rejected.json()["error"]["code"] == "TB-1007"


def test_admin_traffic_does_not_consume_the_peer_ip_fallback_bucket() -> None:
    """管理面有自己的桶，不能跟业务面「取不到身份时按对端 IP」的桶共用一把。

    共用的后果：治理台在批量操作时会把某个用户的业务写入挤到 429 ——
    治理动作与业务流量互相伤害。
    """
    with TestClient(_app(rate_limit_per_minute=1, burst=2)) as client:
        # 业务面取不到调用方标识 → 退回对端 IP，把那把桶用光
        for _ in range(2):
            assert client.post("/v2/memory/append", json={"note": "无身份"}).status_code == 200
        assert client.post("/v2/memory/append", json={"note": "无身份"}).status_code == 429

        # 管理面分桶独立，不受业务面影响
        assert client.get("/admin/api/memories").status_code == 200
        assert client.get("/admin/api/memories").status_code == 200
        assert client.get("/admin/api/memories").status_code == 429


def test_rotating_the_caller_key_cannot_escape_the_peer_ceiling() -> None:
    """轮换 user_id 换的是「每调用方」那把桶 —— 按对端 IP 的天花板换不掉。

    分桶键是调用方给的，不压一层按 IP 的总闸，限额就只是个摆设：换个
    user_id 就是一把满额的新桶。
    """
    with TestClient(_app(rate_limit_per_minute=1, burst=1)) as client:
        # 天花板是每调用方限额的 20 倍 → 这里能过 20 次
        for index in range(20):
            response = client.post("/v2/memory/append", json=_body("t1", f"u{index}"))
            assert response.status_code == 200

        rejected = client.post("/v2/memory/append", json=_body("t1", "u999"))
        assert rejected.status_code == 429
