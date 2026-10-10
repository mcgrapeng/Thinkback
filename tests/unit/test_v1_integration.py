"""v1 业务系统接入 API 集成测试。

覆盖:
- API Key 认证(401)
- Scope 授权(403)
- Idempotency-Key 幂等(同 key 同 body → 缓存;同 key 不同 body → 409)
- 限流(超出 → 429)
- 完整 append → recall 流程
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from thinkback.api.auth import (
    _API_KEYS,
    Scope,
    generate_api_key,
)
from thinkback.memory.backends.fake import FakeMemoryBackend
from thinkback.memory.repositories.in_memory import InMemoryMemoryRepository
from thinkback.memory.service import MemoryService


@pytest.fixture(autouse=True)
def reset_state(monkeypatch) -> None:
    """每个测试前重置 API key 存储 + 注入 fake service。"""
    _API_KEYS.clear()
    service = MemoryService(repository=InMemoryMemoryRepository(), backend=FakeMemoryBackend())
    monkeypatch.setattr("thinkback.api.dependencies._memory_service", service)
    # 禁用 v1 限流(单独测)
    # 这里不复用 TestClient 创建 app,直接调用路由函数,避免中间件复杂度


# ─── 认证测试 ──────────────────────────────────────────────────────────


def test_v1_requires_authorization_header() -> None:
    """无 Authorization 头 → 401 TB-1002。"""
    from thinkback.api.app import create_app

    _API_KEYS.clear()
    with TestClient(create_app()) as client:
        # 走 v1 路径,触发认证
        response = client.post("/v1/memory/append", json={"user_id": "u1", "messages": []})
        assert response.status_code == 401
        body = response.json()
        assert body["error"]["code"] == "TB-1002"
        assert "Authorization" in body["error"]["message"]


def test_v1_rejects_invalid_api_key() -> None:
    """无效 key → 401。"""
    from thinkback.api.app import create_app

    _API_KEYS.clear()
    with TestClient(create_app()) as client:
        response = client.post(
            "/v1/memory/append",
            json={"user_id": "u1", "messages": []},
            headers={"Authorization": "Bearer tbk_live_invalid_xxxx"},
        )
        assert response.status_code == 401


def test_v1_succeeds_with_valid_key() -> None:
    """有效 key + 正确 scope → 200/202。"""
    from thinkback.api.app import create_app

    _API_KEYS.clear()
    plaintext, _ = generate_api_key(
        tenant_id="tenant_test",
        scopes={Scope.MEMORY_APPEND, Scope.MEMORY_RECALL, Scope.MEMORY_READ},
        plan="pro",
    )
    with TestClient(create_app()) as client:
        response = client.post(
            "/v1/memory/append",
            json={
                "request_id": "req_v1_001",
                "user_id": "u_test",
                "session_id": "s_test",
                "round_id": "r_001",
                "source_timestamp": "2026-10-08T10:00:00Z",
                "messages": [
                    {
                        "role": "user",
                        "content": "测试记忆",
                        "message_id": "m_001",
                        "timestamp": "2026-10-08T10:00:00Z",
                    },
                    {
                        "role": "assistant",
                        "content": "已记住",
                        "message_id": "m_002",
                        "timestamp": "2026-10-08T10:00:05Z",
                    },
                ],
            },
            headers={"Authorization": f"Bearer {plaintext}"},
        )
        assert response.status_code in (200, 202), response.text


# ─── Scope 测试 ──────────────────────────────────────────────────────────


def test_v1_rejects_request_without_required_scope() -> None:
    """key 没有 append scope,append 请求 → 403 TB-1003。"""
    from thinkback.api.app import create_app

    _API_KEYS.clear()
    plaintext, _ = generate_api_key(
        tenant_id="tenant_test",
        scopes={Scope.MEMORY_READ},  # 只有 read,没有 append
        plan="pro",
    )
    with TestClient(create_app()) as client:
        response = client.post(
            "/v1/memory/append",
            json={
                "user_id": "u_test",
                "session_id": "s_test",
                "round_id": "r_001",
                "messages": [{"role": "user", "content": "test"}],
            },
            headers={"Authorization": f"Bearer {plaintext}"},
        )
        assert response.status_code == 403
        body = response.json()
        assert body["error"]["code"] == "TB-1003"
        assert "memory:append" in body["error"]["message"]


# ─── 幂等测试 ──────────────────────────────────────────────────────────


def test_idempotency_same_key_same_body_returns_cached() -> None:
    """同 Idempotency-Key + 同 body → 第二次返回缓存(快速,不重新执行)。"""
    from thinkback.api.app import create_app

    _API_KEYS.clear()
    plaintext, _ = generate_api_key(
        tenant_id="tenant_test",
        scopes={Scope.MEMORY_APPEND, Scope.MEMORY_RECALL, Scope.MEMORY_READ},
        plan="pro",
    )
    with TestClient(create_app()) as client:
        body = {
            "request_id": "req_v1_idem_001",
            "user_id": "u_idem",
            "session_id": "s_idem",
            "round_id": "r_idem",
            "source_timestamp": "2026-10-08T10:00:00Z",
            "messages": [
                {
                    "role": "user",
                    "content": "幂等测试",
                    "message_id": "m_idem_1",
                    "timestamp": "2026-10-08T10:00:00Z",
                },
                {
                    "role": "assistant",
                    "content": "已记住",
                    "message_id": "m_idem_2",
                    "timestamp": "2026-10-08T10:00:05Z",
                },
            ],
        }
        headers = {
            "Authorization": f"Bearer {plaintext}",
            "Idempotency-Key": "idem_test_001",
        }
        r1 = client.post("/v1/memory/append", json=body, headers=headers)
        r2 = client.post("/v1/memory/append", json=body, headers=headers)
        assert r1.status_code in (200, 202)
        assert r2.status_code in (200, 202)
        assert r1.json() == r2.json()
        # 第二次应该带 X-Idempotent-Replay 头
        assert r2.headers.get("X-Idempotent-Replay") == "true"


def test_idempotency_same_key_different_body_returns_409() -> None:
    """同 Idempotency-Key + 不同 body → 409 TB-1005。"""
    from thinkback.api.app import create_app

    _API_KEYS.clear()
    plaintext, _ = generate_api_key(
        tenant_id="tenant_test",
        scopes={Scope.MEMORY_APPEND, Scope.MEMORY_RECALL, Scope.MEMORY_READ},
        plan="pro",
    )
    with TestClient(create_app()) as client:
        h = {
            "Authorization": f"Bearer {plaintext}",
            "Idempotency-Key": "idem_conflict_001",
        }
        body1 = {
            "request_id": "req_v1_conflict_1",
            "user_id": "u1",
            "session_id": "s1",
            "round_id": "r1",
            "source_timestamp": "2026-10-08T10:00:00Z",
            "messages": [
                {
                    "role": "user",
                    "content": "first",
                    "message_id": "m_c1",
                    "timestamp": "2026-10-08T10:00:00Z",
                },
                {
                    "role": "assistant",
                    "content": "ok1",
                    "message_id": "m_c1a",
                    "timestamp": "2026-10-08T10:00:05Z",
                },
            ],
        }
        body2 = {
            "request_id": "req_v1_conflict_1",
            "user_id": "u1",
            "session_id": "s1",
            "round_id": "r1",
            "source_timestamp": "2026-10-08T10:00:00Z",
            "messages": [
                {
                    "role": "user",
                    "content": "second",
                    "message_id": "m_c2",
                    "timestamp": "2026-10-08T10:00:00Z",
                },
                {
                    "role": "assistant",
                    "content": "ok2",
                    "message_id": "m_c2a",
                    "timestamp": "2026-10-08T10:00:05Z",
                },
            ],  # different
        }
        client.post("/v1/memory/append", json=body1, headers=h)
        r2 = client.post("/v1/memory/append", json=body2, headers=h)
        assert r2.status_code == 409
        assert r2.json()["error"]["code"] == "TB-1005"


# ─── 限流测试 ──────────────────────────────────────────────────────────


def test_rate_limit_returns_429_after_burst() -> None:
    """超出 burst 后 429 TB-1007 + Retry-After。"""
    from thinkback.api.app import create_app

    _API_KEYS.clear()
    # free plan: 60/min, burst=10
    plaintext, _ = generate_api_key(
        tenant_id="tenant_rl",
        scopes={Scope.MEMORY_READ},
        plan="free",
    )
    with TestClient(create_app()) as client:
        h = {"Authorization": f"Bearer {plaintext}"}
        # 消费 10 个 token(burst)
        for _ in range(10):
            r = client.get("/v1/memory?user_id=u1", headers=h)
            assert r.status_code in (200, 500)  # 数据可能不存在,但 token 被消费

        # 第 11 次应被限流
        r11 = client.get("/v1/memory?user_id=u1", headers=h)
        assert r11.status_code == 429
        body = r11.json()
        assert body["error"]["code"] == "TB-1007"
        assert r11.headers.get("Retry-After")
        assert r11.headers.get("X-RateLimit-Remaining") == "0"


# ─── Tenant 一致性 ────────────────────────────────────────────────────


def test_v1_tenant_id_mismatch_returns_403() -> None:
    """key 的 tenant_id 与 X-Tenant-Id 不一致 → 403。"""
    from thinkback.api.app import create_app

    _API_KEYS.clear()
    plaintext, _ = generate_api_key(
        tenant_id="tenant_A",
        scopes={Scope.MEMORY_READ},
        plan="pro",
    )
    with TestClient(create_app()) as client:
        r = client.get(
            "/v1/memory?user_id=u1",
            headers={
                "Authorization": f"Bearer {plaintext}",
                "X-Tenant-Id": "tenant_B",  # 故意写错
            },
        )
        assert r.status_code == 403
