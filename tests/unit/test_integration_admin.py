"""API Key 管理端点测试。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from thinkback.api.auth import Scope, _API_KEYS


@pytest.fixture(autouse=True)
def reset_keys() -> None:
    _API_KEYS.clear()


def test_list_keys_empty() -> None:
    from thinkback.api.app import create_app

    with TestClient(create_app()) as client:
        r = client.get("/admin/api/integration/keys")
        assert r.status_code == 200
        assert r.json() == []


def test_create_and_list_key() -> None:
    from thinkback.api.app import create_app

    with TestClient(create_app()) as client:
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

    with TestClient(create_app()) as client:
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

    with TestClient(create_app()) as client:
        r = client.delete("/admin/api/integration/keys/nonexistent")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "TB-1004"


def test_create_key_with_invalid_plan_returns_400() -> None:
    from thinkback.api.app import create_app

    with TestClient(create_app()) as client:
        r = client.post(
            "/admin/api/integration/keys",
            json={"tenant_id": "t", "scopes": ["memory:read"], "plan": "bogus"},
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "TB-1001"


def test_create_key_validates_required_fields() -> None:
    from thinkback.api.app import create_app

    with TestClient(create_app()) as client:
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
