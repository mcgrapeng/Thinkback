"""mem0 配置管理端点 — 供治理台 console 编辑 mem0 抽取提示词等。

支持:
- GET: 列出所有 mem0 配置项(含默认值)
- PUT: 更新单个配置项
- POST /reset: 恢复默认值
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from thinkback.api.admin import require_admin_token
from thinkback.memory.mem0_config import (
    DEFAULT_CONFIGS,
    get_config_value,
    list_all_configs,
    set_config_value,
)

router = APIRouter(prefix="/admin/api/mem0", tags=["mem0-config"])


class Mem0ConfigItem(BaseModel):
    key: str
    value: str
    description: str
    is_active: bool = True
    updated_at: str | None = None


class UpdateConfigRequest(BaseModel):
    value: str = Field(min_length=1, description="新的配置值(不能为空)")


@router.get("/configs", response_model=list[Mem0ConfigItem], summary="列出 mem0 配置")
async def list_configs(
    _: None = Depends(require_admin_token),
) -> list[Mem0ConfigItem]:
    """列出所有 mem0 配置项(含硬编码默认值,即使 DB 中尚未写入)。"""
    items = await list_all_configs()
    return [Mem0ConfigItem(**item) for item in items]


@router.get("/configs/{key}", response_model=Mem0ConfigItem, summary="获取单个配置")
async def get_config(
    key: str,
    _: None = Depends(require_admin_token),
) -> Mem0ConfigItem:
    """获取单个配置项的当前值。"""
    value = await get_config_value(key)
    if not value and key not in DEFAULT_CONFIGS:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "TB-1004", "message": f"Config key '{key}' not found"}},
        )
    default_desc = DEFAULT_CONFIGS.get(key, ("", ""))[1]
    return Mem0ConfigItem(
        key=key,
        value=value,
        description=default_desc,
        is_active=True,
    )


@router.put("/configs/{key}", summary="更新 mem0 配置")
async def update_config(
    key: str,
    request: UpdateConfigRequest,
    _: None = Depends(require_admin_token),
) -> dict[str, str]:
    """更新配置项。下次 append 时立即生效(无需重启)。"""
    await set_config_value(key, request.value)
    return {"status": "updated", "key": key}


@router.post("/configs/{key}/reset", summary="恢复默认值")
async def reset_config(
    key: str,
    _: None = Depends(require_admin_token),
) -> dict[str, str]:
    """恢复某个配置项为硬编码默认值。"""
    if key not in DEFAULT_CONFIGS:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "TB-1004", "message": f"No default for key '{key}'"}},
        )
    default_value = DEFAULT_CONFIGS[key][0]
    await set_config_value(key, default_value)
    return {"status": "reset", "key": key}
