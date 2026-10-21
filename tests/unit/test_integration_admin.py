"""API Key 管理端点测试。"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from thinkback.api.auth import _API_KEYS
from thinkback.infra import config

ADMIN_TOKEN = "test-admin-token"
AUTH_HEADERS = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


@pytest.fixture(autouse=True)
def reset_keys(monkeypatch: Any) -> None:
    _API_KEYS.clear()
    # 这三个端点原先零鉴权（任何人都能列举全部 key、铸造任意 scope 的新 key、
    # 吊销他人 key）。现在统一挂 require_admin_token。
    monkeypatch.setattr(config.settings, "admin_token", ADMIN_TOKEN)


def test_list_keys_empty() -> None:
    from thinkback.api.app import create_app

    with TestClient(create_app(), headers=AUTH_HEADERS) as client:
        r = client.get("/admin/api/integration/keys")
        assert r.status_code == 200
        assert r.json() == []


def test_integration_admin_endpoints_require_admin_token() -> None:
    """无凭证不得触碰 API Key 管理面。

    回归缺陷：这三个端点原先**没有任何**鉴权 —— 无凭证就能列出全部 key 的
    key_id/tenant/scopes、为任意 tenant 铸造任意 scope（含 memory:admin）的新 key、
    吊销他人 key。原来这里还有一条测试断言「未认证返回 200」，把不安全行为锁死了。
    """
    from thinkback.api.app import create_app

    with TestClient(create_app()) as client:
        assert client.get("/admin/api/integration/keys").status_code == 401
        assert (
            client.post(
                "/admin/api/integration/keys",
                json={"tenant_id": "attacker", "scopes": ["memory:admin"]},
            ).status_code
            == 401
        )
        assert client.delete("/admin/api/integration/keys/any-key-id").status_code == 401


def test_integration_admin_rejects_wrong_admin_token() -> None:
    """错的 token 一律 401。"""
    from thinkback.api.app import create_app

    with TestClient(create_app(), headers={"Authorization": "Bearer wrong-token"}) as client:
        assert client.get("/admin/api/integration/keys").status_code == 401


def test_create_and_list_key() -> None:
    from thinkback.api.app import create_app

    with TestClient(create_app(), headers=AUTH_HEADERS) as client:
        # 创建
        r = client.post(
            "/admin/api/integration/keys",
            json={
                "tenant_id": "tenant_test",
                "scopes": ["memory:append", "memory:recall"],
                "plan": "pro",
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["key"]["tenant_id"] == "tenant_test"
        assert body["key"]["plan"] == "pro"
        assert body["key"]["is_active"] is True
        assert "memory:append" in body["key"]["scopes"]
        assert body["plaintext"].startswith("tbk_live_")

        # 列表
        r2 = client.get("/admin/api/integration/keys")
        assert r2.status_code == 200
        keys = r2.json()
        assert len(keys) == 1
        assert keys[0]["key_id"] == body["key"]["key_id"]
        assert "plaintext" not in keys[0]  # 列表不应返回明文


def test_revoke_key() -> None:
    from thinkback.api.app import create_app

    with TestClient(create_app(), headers=AUTH_HEADERS) as client:
        r = client.post(
            "/admin/api/integration/keys",
            json={"tenant_id": "t", "scopes": ["memory:read"]},
        )
        key_id = r.json()["key"]["key_id"]

        # 撤销
        r2 = client.delete(f"/admin/api/integration/keys/{key_id}")
        assert r2.status_code == 200
        assert r2.json() == {"key_id": key_id, "revoked": True}

        # 列表中标记为非 active
        r3 = client.get("/admin/api/integration/keys")
        assert r3.json()[0]["is_active"] is False


def test_revoke_nonexistent_key_returns_404() -> None:
    from thinkback.api.app import create_app

    with TestClient(create_app(), headers=AUTH_HEADERS) as client:
        r = client.delete("/admin/api/integration/keys/nonexistent")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "TB-1004"


def test_create_key_with_invalid_plan_returns_400() -> None:
    from thinkback.api.app import create_app

    with TestClient(create_app(), headers=AUTH_HEADERS) as client:
        r = client.post(
            "/admin/api/integration/keys",
            json={"tenant_id": "t", "scopes": ["memory:read"], "plan": "bogus"},
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "TB-1001"


def test_create_key_validates_required_fields() -> None:
    from thinkback.api.app import create_app

    with TestClient(create_app(), headers=AUTH_HEADERS) as client:
        # 缺 scopes
        r = client.post(
            "/admin/api/integration/keys",
            json={"tenant_id": "t"},
        )
        assert r.status_code == 422  # Pydantic validation

        # 缺 tenant_id
        r = client.post(
            "/admin/api/integration/keys",
            json={"scopes": ["memory:read"]},
        )
        assert r.status_code == 422
