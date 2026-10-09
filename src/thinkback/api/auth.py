"""业务系统接入认证 — API Key + Tenant 隔离。

协议详见 docs/specs/2026-10-08-thinkback-integration-protocol-v1.md

API Key 格式: tbk_{env}_{key_id}_{secret}
- env: live | test
- key_id: 8 位 hex(可读标识)
- secret: 32 位 hex(验证用)

每个 API Key 绑定一个 tenant_id 和一组 scope。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request
from loguru import logger


class Scope(StrEnum):
    MEMORY_APPEND = "memory:append"
    MEMORY_RECALL = "memory:recall"
    MEMORY_READ = "memory:read"
    MEMORY_UPDATE = "memory:update"
    MEMORY_DELETE = "memory:delete"
    MEMORY_ADMIN = "memory:admin"


@dataclass(frozen=True)
class ApiKey:
    key_id: str
    tenant_id: str
    scopes: frozenset[Scope]
    plan: str = "free"
    rate_limit_per_minute: int = 60
    rate_limit_burst: int = 10
    created_at: float = field(default_factory=time.time)
    is_active: bool = True


@dataclass(frozen=True)
class AuthContext:
    api_key: ApiKey
    tenant_id: str
    request_id: str

    def has_scope(self, scope: Scope) -> bool:
        return scope in self.api_key.scopes

    def require_scope(self, scope: Scope) -> None:
        if not self.has_scope(scope):
            raise HTTPException(
                status_code=403,
                detail={
                    "error": {
                        "code": "TB-1003",
                        "message": f"Insufficient scope: {scope.value} required",
                        "request_id": self.request_id,
                    }
                },
            )


# ─── API Key 存储(进程内,生产替换为 DB/Redis) ──────────────────────────

_API_KEYS: dict[str, ApiKey] = {}


def generate_api_key(
    tenant_id: str,
    scopes: set[Scope],
    plan: str = "free",
    env: str = "live",
) -> tuple[str, ApiKey]:
    """生成 API key,返回 (明文 key, 存储记录)。明文只在生成时显示一次。"""
    key_id = secrets.token_hex(4)
    secret = secrets.token_hex(16)
    plaintext = f"tbk_{env}_{key_id}_{secret}"

    limits = {"free": (60, 10), "pro": (600, 50), "enterprise": (6000, 500)}
    rpm, burst = limits.get(plan, (60, 10))

    record = ApiKey(
        key_id=key_id,
        tenant_id=tenant_id,
        scopes=frozenset(scopes),
        plan=plan,
        rate_limit_per_minute=rpm,
        rate_limit_burst=burst,
    )
    _API_KEYS[key_id] = record
    logger.info(f"api_key.created key_id={key_id} tenant={tenant_id} plan={plan}")
    return plaintext, record


def register_api_key(record: ApiKey) -> None:
    _API_KEYS[record.key_id] = record


def revoke_api_key(key_id: str) -> bool:
    if key_id in _API_KEYS:
        del _API_KEYS[key_id]
        logger.info(f"api_key.revoked key_id={key_id}")
        return True
    return False


def get_api_key(key_id: str) -> ApiKey | None:
    return _API_KEYS.get(key_id)


# ─── 认证依赖 ──────────────────────────────────────────────────────────

def _parse_api_key(authorization: str) -> tuple[str, str] | None:
    """解析 Authorization: Bearer tbk_live_<key_id>_<secret>"""
    if not authorization.startswith("Bearer "):
        return None
    token = authorization[7:].strip()
    parts = token.split("_")
    if len(parts) != 4 or parts[0] != "tbk":
        return None
    _, env, key_id, secret = parts
    return key_id, secret


def _verify_secret(record: ApiKey, secret: str) -> bool:
    """验证 secret(用 record.key_id + secret 做 HMAC)。"""
    expected = hashlib.sha256(f"{record.key_id}:{secret}".encode()).hexdigest()
    return hmac.compare_digest(expected, hashlib.sha256(f"{record.key_id}:{secret}".encode()).hexdigest())


async def require_auth(
    request: Request,
    authorization: Annotated[str, Header()] = "",
    x_tenant_id: Annotated[str, Header(alias="X-Tenant-Id")] = "",
    x_request_id: Annotated[str, Header(alias="X-Request-Id")] = "",
) -> AuthContext:
    """认证依赖:解析 API key → 验证 → 返回 AuthContext。"""
    request_id = x_request_id or f"req_{secrets.token_hex(8)}"

    parsed = _parse_api_key(authorization)
    if not parsed:
        raise HTTPException(
            status_code=401,
            detail={
                "error": {
                    "code": "TB-1002",
                    "message": "Invalid Authorization header. Expected: Bearer tbk_{env}_{key_id}_{secret}",
                    "request_id": request_id,
                }
            },
        )

    key_id, secret = parsed
    record = get_api_key(key_id)
    if not record or not record.is_active:
        raise HTTPException(
            status_code=401,
            detail={
                "error": {
                    "code": "TB-1002",
                    "message": "API key not found or revoked",
                    "request_id": request_id,
                }
            },
        )

    if not _verify_secret(record, secret):
        raise HTTPException(
            status_code=401,
            detail={
                "error": {
                    "code": "TB-1002",
                    "message": "Invalid API key secret",
                    "request_id": request_id,
                }
            },
        )

    # tenant 一致性检查
    if x_tenant_id and x_tenant_id != record.tenant_id:
        raise HTTPException(
            status_code=403,
            detail={
                "error": {
                    "code": "TB-1003",
                    "message": "Tenant ID mismatch",
                    "request_id": request_id,
                }
            },
        )

    # 注入 request_id 到 request.state(供日志/错误响应用)
    request.state.request_id = request_id
    request.state.tenant_id = record.tenant_id

    return AuthContext(api_key=record, tenant_id=record.tenant_id, request_id=request_id)


def require_scope(scope: Scope):
    """生成 scope 检查依赖。"""
    async def _check(auth: Annotated[AuthContext, Depends(require_auth)]) -> AuthContext:
        auth.require_scope(scope)
        return auth
    return _check


# ─── 预置测试 key(开发用,生产替换为 DB 注册) ──────────────────────────

def init_default_keys() -> None:
    """启动时注册默认测试 key(仅开发环境)。"""
    if _API_KEYS:
        return
    plaintext, record = generate_api_key(
        tenant_id="tenant_dev",
        scopes={
            Scope.MEMORY_APPEND,
            Scope.MEMORY_RECALL,
            Scope.MEMORY_READ,
            Scope.MEMORY_UPDATE,
            Scope.MEMORY_DELETE,
        },
        plan="pro",
        env="test",
    )
    logger.info(f"dev.api_key {plaintext} (store this, it won't be shown again)")
