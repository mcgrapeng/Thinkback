"""管理后台（治理台）API：只读聚合端点，M1 范围 = overview + tasks。

设计约束（见 docs/管理后台设计方案-2026-09.md §1）：
- 薄聚合：业务逻辑在 ``MemoryService`` / 仓储层，这里只做参数解析与编排；
- 鉴权：``settings.admin_token`` 非空时强制共享 Bearer token（防御纵深，
  Q8 决策）；为空放行（本地开发 / 网关隔离完备的内网）；
- 同步 service 调用经 ``asyncio.to_thread`` 下桥，管理面流量极低，
  不复用 /memory 路由的专用线程池预算（那是为业务读写延迟兜底的）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from loguru import logger

from thinkback.api.dependencies import get_memory_service
from thinkback.api.errors import service_error_to_http
from thinkback.domain.entities import AdminAuditEntry
from thinkback.domain.enums import MemoryStatus, TaskStatus
from thinkback.memory.schemas import (
    DeleteMemoryRequest,
    DeleteMemoryResponse,
    RebuildMemoryRequest,
    RebuildMemoryResponse,
    TaskResponse,
    UpdateMemoryRequest,
    UpdateMemoryResponse,
)
from thinkback.memory.service import MemoryService

router = APIRouter(prefix="/admin/api", tags=["admin"])


def require_admin_token(authorization: Annotated[str, Header()] = "") -> None:
    """共享 Bearer token 校验（token 未配置时放行）。"""

    from thinkback.infra.config import settings

    token = settings.admin_token
    if not token:
        return
    if authorization != f"Bearer {token}":
        raise HTTPException(status_code=401, detail="invalid admin token")


def _parse_statuses(raw: str | None) -> list[str] | None:
    """逗号分隔状态串 → 小写状态值列表；非法值直接 400（早失败）。"""

    if not raw:
        return None
    valid = {status.value for status in TaskStatus}
    statuses = [part.strip() for part in raw.split(",") if part.strip()]
    invalid = [part for part in statuses if part not in valid]
    if invalid:
        raise HTTPException(status_code=400, detail=f"invalid task status: {invalid}")
    return statuses or None


@router.get("/overview", operation_id="admin_get_overview")
async def admin_overview(
    _: None = Depends(require_admin_token),
    service: MemoryService = Depends(get_memory_service),
) -> dict[str, Any]:
    """总览聚合：记忆/任务状态计数 + L3 后台写状态（30s 轮询源）。"""

    stats = await asyncio.to_thread(service.overview_stats)
    l3 = await asyncio.to_thread(service.l3_background_status)
    return {**stats, "l3": l3}


@router.get("/tasks", operation_id="admin_list_tasks")
async def admin_list_tasks(
    _: None = Depends(require_admin_token),
    service: MemoryService = Depends(get_memory_service),
    statuses: str | None = Query(default=None, description="逗号分隔：pending,running,..."),
    older_than_seconds: float | None = Query(default=None, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[TaskResponse]:
    """任务列表：状态/年龄过滤，updated_at 倒序分页。"""

    return await asyncio.to_thread(
        service.list_tasks,
        statuses=_parse_statuses(statuses),
        older_than_seconds=older_than_seconds,
        limit=limit,
        offset=offset,
    )


def _parse_memory_statuses(raw: str | None) -> list[str] | None:
    """逗号分隔记忆状态串 → 大写状态值列表；非法值 400（早失败）。"""

    if not raw:
        return None
    valid = {status.value for status in MemoryStatus}
    statuses = [part.strip().upper() for part in raw.split(",") if part.strip()]
    invalid = [part for part in statuses if part not in valid]
    if invalid:
        raise HTTPException(status_code=400, detail=f"invalid memory status: {invalid}")
    return statuses or None


@router.get("/memories", operation_id="admin_list_memories")
async def admin_list_memories(
    _: None = Depends(require_admin_token),
    service: MemoryService = Depends(get_memory_service),
    user_id: str | None = Query(default=None, description="用户 ID（不传=全量）"),
    memory_scope_id: str | None = Query(default=None),
    statuses: str | None = Query(default=None, description="逗号分隔：ACTIVE,DELETED,..."),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """记忆检索：本地索引多用户视图（服务端分页 + 总数）。"""

    return await asyncio.to_thread(
        service.admin_list_memories,
        user_id=user_id,
        memory_scope_id=memory_scope_id,
        statuses=_parse_memory_statuses(statuses),
        limit=limit,
        offset=offset,
    )


@router.get("/memories/{memory_id}/source", operation_id="admin_memory_source")
async def admin_memory_source(
    memory_id: str,
    _: None = Depends(require_admin_token),
    service: MemoryService = Depends(get_memory_service),
) -> dict[str, Any]:
    """来源反链：记忆条目 + source_refs 指向的 journal 原文回合。"""

    source = await asyncio.to_thread(service.memory_source, memory_id)
    if source is None:
        raise HTTPException(status_code=404, detail="memory not found")
    return source


# ── M3：治理操作 + 审计 + 配置 ────────────────────────────────────────

_SENSITIVE_MARKERS = ("password", "token", "secret", "api_key", "url")


def _audit(service: MemoryService, action: str, target: str, detail: dict[str, Any]) -> None:
    """治理动作留痕（best-effort：审计写失败不阻断操作，落 error 日志）。"""
    from datetime import UTC, datetime

    try:
        service.repository.save_admin_audit(
            AdminAuditEntry(
                audit_id=f"audit-{uuid4()}",
                created_at=datetime.now(UTC),
                operator="admin",
                action=action,
                target=target[:256],
                detail=detail,
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.opt(exception=exc).error("admin audit write failed")


async def _run_admin_call[R](method: Callable[..., R], *args: Any) -> R:
    """to_thread 下桥 + 与 /memory 路由同一异常映射契约。"""
    try:
        return await asyncio.to_thread(method, *args)
    except Exception as exc:
        mapped = service_error_to_http(exc)
        if mapped is not None:
            raise mapped from exc
        logger.opt(exception=exc).error("admin call failed with unclassified error")
        raise


@router.post("/delete", operation_id="admin_delete_memory")
async def admin_delete_memory(
    request: DeleteMemoryRequest,
    _: None = Depends(require_admin_token),
    service: MemoryService = Depends(get_memory_service),
) -> DeleteMemoryResponse:
    """受控删除（单条/session/all，幂等与业务路由同语义）+ 审计留痕。"""
    response = await _run_admin_call(service.delete, request)
    _audit(
        service,
        action="delete",
        target=request.user_id,
        detail={
            "scope": request.scope.value,
            "memory_id": getattr(request, "memory_id", None),
            "session_id": request.session_id,
            "operation_id": request.operation_id,
            "affected_count": getattr(response, "affected_count", None),
        },
    )
    return response


@router.post("/update", operation_id="admin_update_memory")
async def admin_update_memory(
    request: UpdateMemoryRequest,
    _: None = Depends(require_admin_token),
    service: MemoryService = Depends(get_memory_service),
) -> UpdateMemoryResponse:
    """受控更新单条记忆（幂等同业务路由）+ 审计留痕（含改写内容）。"""
    response = await _run_admin_call(service.update_memory, request)
    _audit(
        service,
        action="update",
        target=request.memory_id,
        detail={
            "operation_id": request.operation_id,
            "content": request.content,
        },
    )
    return response


@router.post("/rebuild", operation_id="admin_rebuild_memory")
async def admin_rebuild_memory(
    request: RebuildMemoryRequest,
    _: None = Depends(require_admin_token),
    service: MemoryService = Depends(get_memory_service),
) -> RebuildMemoryResponse:
    """受控 rebuild 触发（此前 service 有能力但无 HTTP 路由）+ 审计留痕。"""
    response = await _run_admin_call(service.rebuild, request)
    _audit(
        service,
        action="rebuild",
        target=request.user_id,
        detail={
            "operation_id": request.operation_id,
            "session_id": request.session_id,
            "rebuild_l2": getattr(request, "rebuild_l2", None),
            "rebuild_l3": getattr(request, "rebuild_l3", None),
        },
    )
    return response


@router.get("/audit", operation_id="admin_list_audit")
async def admin_list_audit(
    _: None = Depends(require_admin_token),
    service: MemoryService = Depends(get_memory_service),
    action: str | None = Query(default=None),
    operator: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[dict[str, Any]]:
    """审计查询：created_at 倒序分页（只读，不可篡改）。"""
    entries = await asyncio.to_thread(
        service.repository.list_admin_audit,
        action=action,
        operator=operator,
        limit=limit,
        offset=offset,
    )
    return [
        {
            "audit_id": entry.audit_id,
            "created_at": entry.created_at,
            "operator": entry.operator,
            "action": entry.action,
            "target": entry.target,
            "detail": entry.detail,
        }
        for entry in entries
    ]


@router.get("/config", operation_id="admin_get_config")
async def admin_get_config(
    _: None = Depends(require_admin_token),
) -> dict[str, Any]:
    """运行配置只读视图：敏感值脱敏（显隐在治理台 P6 页完成，本端点不回明文）。"""

    from thinkback.infra.config import settings

    def masked(name: str, value: object) -> object:
        lowered = name.lower()
        if any(marker in lowered for marker in _SENSITIVE_MARKERS):
            return "***" if value else value
        return value

    fields = sorted(
        (name, masked(name, value))
        for name, value in settings.model_dump().items()
        if not name.startswith("_")
    )
    return {
        "environment": settings.environment,
        "fields": [{"name": name, "value": value} for name, value in fields],
    }
