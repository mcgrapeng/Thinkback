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

from fastapi import APIRouter, Depends

from thinkback.api.auth import Scope, require_auth, require_scope
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

router = APIRouter(
    prefix="/v1/memory",
    tags=["v1-memory"],
    responses={
        401: {"description": "API key 缺失或无效"},
        403: {"description": "Scope 不足或 Tenant 不匹配"},
        429: {"description": "限流"},
    },
)


# ─── 写入/召回/读:需要对应 scope ──────────────────────────────────────

router.add_api_route(
    "/append",
    endpoint=append_memory,
    methods=["POST"],
    summary="写入对话轮次(抽取记忆)",
    operation_id="v1_memory_append",
    dependencies=[Depends(require_auth), Depends(require_scope(Scope.MEMORY_APPEND))],
)

router.add_api_route(
    "/recall",
    endpoint=recall_memory,
    methods=["POST"],
    summary="召回相关记忆",
    operation_id="v1_memory_recall",
    dependencies=[Depends(require_auth), Depends(require_scope(Scope.MEMORY_RECALL))],
)

router.add_api_route(
    "",
    endpoint=list_memory_items,
    methods=["GET"],
    summary="列出记忆(分页)",
    operation_id="v1_memory_list",
    dependencies=[Depends(require_auth), Depends(require_scope(Scope.MEMORY_READ))],
)

router.add_api_route(
    "/{memory_id}",
    endpoint=get_memory_item,
    methods=["GET"],
    summary="获取单条记忆详情",
    operation_id="v1_memory_get",
    dependencies=[Depends(require_auth), Depends(require_scope(Scope.MEMORY_READ))],
)

router.add_api_route(
    "/{memory_id}",
    endpoint=update_memory_item,
    methods=["PUT"],
    summary="更新记忆正文",
    operation_id="v1_memory_update",
    dependencies=[Depends(require_auth), Depends(require_scope(Scope.MEMORY_UPDATE))],
)

router.add_api_route(
    "/{memory_id}",
    endpoint=delete_memory,
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
