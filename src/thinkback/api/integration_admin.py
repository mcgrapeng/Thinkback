"""API Key 管理端点 — 供治理台 console 用。

- 列表:列出所有 key(隐藏 secret)
- 生成:返回明文 key(只显示一次)
- 撤销:删除 key(软删)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from thinkback.api.auth import (
    ApiKey,
    Scope,
    _API_KEYS,
    generate_api_key,
    revoke_api_key,
)


class GenerateKeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=64, description="租户 ID")
    scopes: list[Scope] = Field(description="授权的 scope 列表")
    plan: str = Field(default="free", description="计费计划: free | pro | enterprise")
    env: str = Field(default="live", description="环境: live | test")


class KeyInfo(BaseModel):
    key_id: str
    tenant_id: str
    scopes: list[str]
    plan: str
    rate_limit_per_minute: int
    rate_limit_burst: int
    is_active: bool
    created_at: str


class GenerateKeyResponse(BaseModel):
    key: KeyInfo
    plaintext: str = Field(description="API Key 明文,仅生成时返回一次,不再展示")


class RevokeKeyResponse(BaseModel):
    key_id: str
    revoked: bool


router = APIRouter(prefix="/admin/api/integration/keys", tags=["integration"])


def _key_to_info(record: ApiKey) -> KeyInfo:
    return KeyInfo(
        key_id=record.key_id,
        tenant_id=record.tenant_id,
        scopes=[s.value for s in record.scopes],
        plan=record.plan,
        rate_limit_per_minute=record.rate_limit_per_minute,
        rate_limit_burst=record.rate_limit_burst,
        is_active=record.is_active,
        created_at=datetime.fromtimestamp(record.created_at, tz=timezone.utc).isoformat(),
    )


@router.get("", response_model=list[KeyInfo], summary="列出所有 API Key")
async def list_keys() -> list[KeyInfo]:
    """列出进程内所有 API Key(隐藏 secret)。

    注意:生产环境应改为从 DB 读取,此端点为内部管控。
    """
    return [_key_to_info(record) for record in _API_KEYS.values()]


@router.post("", response_model=GenerateKeyResponse, summary="生成新 API Key")
async def create_key(request: GenerateKeyRequest) -> GenerateKeyResponse:
    """生成新 API Key,返回明文。明文只显示一次,丢失后需重新生成。"""
    if request.plan not in ("free", "pro", "enterprise"):
        raise HTTPException(
            status_code=400,
            detail={"error": {"code": "TB-1001", "message": f"Invalid plan: {request.plan}"}},
        )
    if request.env not in ("live", "test"):
        raise HTTPException(
            status_code=400,
            detail={"error": {"code": "TB-1001", "message": f"Invalid env: {request.env}"}},
        )

    plaintext, record = generate_api_key(
        tenant_id=request.tenant_id,
        scopes=set(request.scopes),
        plan=request.plan,
        env=request.env,
    )
    return GenerateKeyResponse(key=_key_to_info(record), plaintext=plaintext)


@router.delete("/{key_id}", response_model=RevokeKeyResponse, summary="撤销 API Key")
async def revoke_key(key_id: str) -> RevokeKeyResponse:
    """撤销 API Key(软删,记录审计后从进程内删除)。"""
    revoked = revoke_api_key(key_id)
    if not revoked:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "TB-1004", "message": f"API key {key_id} not found"}},
        )
    return RevokeKeyResponse(key_id=key_id, revoked=True)
