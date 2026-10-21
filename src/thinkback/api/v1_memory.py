"""v1 公开接入 API — 业务系统集成。

协议详见 docs/specs/2026-10-08-thinkback-integration-protocol-v1.md

与内部 /memory/* 路由的区别:
- 路径前缀: /v1/memory/*
- 强制 API Key 认证(Authorization: Bearer tbk_...)
- 强制 Tenant ID 头(X-Tenant-Id)
- 支持 Idempotency-Key 幂等保证
- 限流(按 Plan: Free/Pro/Enterprise)
- 标准化错误码响应(TB-1001 ~ TB-2003)

内部 /memory/* 路由保留,无认证(供 admin web / gRPC / 内部服务调用)。
"""

from __future__ import annotations

from typing import Any, Protocol, Self, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from thinkback.api.auth import Scope, require_auth, require_scope, require_tenant
from thinkback.api.dependencies import get_memory_service
from thinkback.api.errors_standard import COMMON_ERROR_RESPONSES
from thinkback.api.memory import (
    append_memory,
    delete_memory,
    get_l3_background_status,
    get_memory_item,
    get_memory_task,
    list_memory_items,
    recall_memory,
    update_memory_item,
)
from thinkback.domain.tenant import scoped_user_id
from thinkback.memory.schemas import (
    AppendMemoryRequest,
    AppendMemoryResponse,
    DeleteMemoryRequest,
    DeleteMemoryResponse,
    GetMemoryResponse,
    ListMemoriesResponse,
    RecallMemoryRequest,
    RecallMemoryResponse,
    UpdateMemoryRequest,
    UpdateMemoryResponse,
)
from thinkback.memory.service import MemoryService

router = APIRouter(
    prefix="/v1/memory",
    tags=["v1-memory"],
    responses={
        **COMMON_ERROR_RESPONSES,
        401: {"description": "API key 缺失或无效"},
        403: {"description": "Scope 不足或 Tenant 不匹配"},
        429: {"description": "限流"},
    },
)


# ─── 路径/身体一致性包装 ───────────────────────────────────────────
# 内部 /memory/update、/memory/delete 端点不绑定 URL 路径段；v1 把它们挂在
# /{memory_id} 下时必须声明并校验该段，否则 URL 可以是任意值（契约 fuzz
# 抓到的 InvalidSchema 根因）。URL 路径是定位语义的真值。


# ─── 租户命名空间包装 ──────────────────────────────────────────────
# /v1/* 是多租户接入面：请求里的 user_id 必须先加上租户命名空间才能进 service 层。
# 否则 tenant 只是鉴权层的一个装饰品 —— 所有查询都只按请求体的 user_id 走，
# 跨租户读写已经实测复现。
#
# 这里给每个带 user_id 的端点都套一层显式包装（而不是在 service 里隐式处理）：
# 「哪些端点做了命名空间」必须一眼看得见 —— 漏掉一个就是一个跨租户漏洞。


class _HasUserId(Protocol):
    """带 user_id 的请求体（Append / Recall / Update / Delete）。"""

    user_id: str

    def model_copy(self, *, update: dict[str, Any]) -> Self: ...


TRequest = TypeVar("TRequest", bound=_HasUserId)


def _scoped[TRequest: _HasUserId](request: TRequest, tenant_id: str) -> TRequest:
    """把请求里的 user_id 加上租户命名空间。"""
    return request.model_copy(update={"user_id": scoped_user_id(tenant_id, request.user_id)})


async def v1_append_memory(
    request: AppendMemoryRequest,
    service: MemoryService = Depends(get_memory_service),
    tenant_id: str = Depends(require_tenant),
) -> AppendMemoryResponse:
    return await append_memory(request=_scoped(request, tenant_id), service=service)


async def v1_recall_memory(
    request: RecallMemoryRequest,
    service: MemoryService = Depends(get_memory_service),
    tenant_id: str = Depends(require_tenant),
) -> RecallMemoryResponse:
    return await recall_memory(request=_scoped(request, tenant_id), service=service)


async def v1_list_memory_items(
    user_id: str = Query(description="用户 ID；只返回该用户范围内的长期记忆。"),
    include_deleted: bool = Query(
        default=False,
        description="是否包含已删除或已取代的业务记忆；系统墓碑不会返回。",
    ),
    service: MemoryService = Depends(get_memory_service),
    tenant_id: str = Depends(require_tenant),
) -> ListMemoriesResponse:
    return await list_memory_items(
        user_id=scoped_user_id(tenant_id, user_id),
        include_deleted=include_deleted,
        service=service,
    )


async def v1_get_memory_item(
    memory_id: str = Path(description="要查询的业务记忆 ID。"),
    user_id: str = Query(description="用户 ID；只查询该用户范围内的长期记忆。"),
    service: MemoryService = Depends(get_memory_service),
    tenant_id: str = Depends(require_tenant),
) -> GetMemoryResponse:
    return await get_memory_item(
        memory_id=memory_id,
        user_id=scoped_user_id(tenant_id, user_id),
        service=service,
    )


async def v1_update_memory_item(
    request: UpdateMemoryRequest,
    memory_id: str = Path(min_length=1, max_length=128, description="要编辑的业务记忆 ID。"),
    service: MemoryService = Depends(get_memory_service),
    tenant_id: str = Depends(require_tenant),
) -> UpdateMemoryResponse:
    if request.memory_id != memory_id:
        raise HTTPException(status_code=400, detail="path/body memory_id mismatch")
    return await update_memory_item(request=_scoped(request, tenant_id), service=service)


async def v1_delete_memory(
    request: DeleteMemoryRequest,
    memory_id: str = Path(min_length=1, max_length=128, description="要删除的业务记忆 ID。"),
    service: MemoryService = Depends(get_memory_service),
    tenant_id: str = Depends(require_tenant),
) -> DeleteMemoryResponse:
    if request.memory_id is not None and request.memory_id != memory_id:
        raise HTTPException(status_code=400, detail="path/body memory_id mismatch")
    if request.memory_id is None:
        request = request.model_copy(update={"memory_id": memory_id})
    return await delete_memory(request=_scoped(request, tenant_id), service=service)


# ─── 写入/召回/读:需要对应 scope ──────────────────────────────────────

router.add_api_route(
    "/append",
    endpoint=v1_append_memory,
    methods=["POST"],
    summary="写入对话轮次(抽取记忆)",
    operation_id="v1_memory_append",
    dependencies=[Depends(require_auth), Depends(require_scope(Scope.MEMORY_APPEND))],
)

router.add_api_route(
    "/recall",
    endpoint=v1_recall_memory,
    methods=["POST"],
    summary="召回相关记忆",
    operation_id="v1_memory_recall",
    dependencies=[Depends(require_auth), Depends(require_scope(Scope.MEMORY_RECALL))],
)

router.add_api_route(
    "",
    endpoint=v1_list_memory_items,
    methods=["GET"],
    summary="列出记忆(分页)",
    operation_id="v1_memory_list",
    dependencies=[Depends(require_auth), Depends(require_scope(Scope.MEMORY_READ))],
)

router.add_api_route(
    "/{memory_id}",
    endpoint=v1_get_memory_item,
    methods=["GET"],
    summary="获取单条记忆详情",
    operation_id="v1_memory_get",
    dependencies=[Depends(require_auth), Depends(require_scope(Scope.MEMORY_READ))],
)

router.add_api_route(
    "/{memory_id}",
    endpoint=v1_update_memory_item,
    methods=["PUT"],
    summary="更新记忆正文",
    operation_id="v1_memory_update",
    dependencies=[Depends(require_auth), Depends(require_scope(Scope.MEMORY_UPDATE))],
)

router.add_api_route(
    "/{memory_id}",
    endpoint=v1_delete_memory,
    methods=["DELETE"],
    summary="删除记忆",
    operation_id="v1_memory_delete",
    dependencies=[Depends(require_auth), Depends(require_scope(Scope.MEMORY_DELETE))],
)


# ─── 异步任务查询(读权限) ──────────────────────────────────────────────

router.add_api_route(
    "/tasks/{task_id}",
    endpoint=get_memory_task,
    methods=["GET"],
    summary="查询异步任务状态",
    operation_id="v1_memory_task",
    dependencies=[Depends(require_auth), Depends(require_scope(Scope.MEMORY_READ))],
)

router.add_api_route(
    "/l3/status",
    endpoint=get_l3_background_status,
    methods=["GET"],
    summary="L3 后台写入状态",
    operation_id="v1_memory_l3_status",
    dependencies=[Depends(require_auth), Depends(require_scope(Scope.MEMORY_READ))],
)
