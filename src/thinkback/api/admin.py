"""管理后台（治理台）API：只读聚合端点，M1 范围 = overview + tasks。

设计约束（见 docs/管理后台设计方案-2026-09.md §1）：
- 薄聚合：业务逻辑在 ``MemoryService`` / 仓储层，这里只做参数解析与编排；
- 鉴权：``settings.admin_token`` 非空时强制共享 Bearer token（防御纵深，
  Q8 决策）；为空放行（本地开发 / 网关隔离完备的内网）；
- 同步 service 调用经 ``asyncio.to_thread`` 下桥，管理面流量极低，
  不复用 /memory 路由的专用线程池预算（那是为业务读写延迟兜底的）；
- 审计留痕**不写记忆正文**：update 路径只记录内容长度与指纹，
  防止任何人凭 admin/audit 接口窥探用户原文（PII / 凭据泄漏面）。
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from loguru import logger

from thinkback.api.dependencies import get_memory_service
from thinkback.api.errors import service_error_to_http
from thinkback.api.errors_standard import COMMON_ERROR_RESPONSES
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

router = APIRouter(prefix="/admin/api", tags=["admin"], responses=COMMON_ERROR_RESPONSES)


def require_admin_token(authorization: Annotated[str, Header()] = "") -> None:
    """共享 Bearer token 校验（token 未配置时放行）。"""

    from thinkback.infra.config import settings

    token = settings.admin_token
    if not token:
        return
    if authorization != f"Bearer {token}":
        raise HTTPException(status_code=401, detail="invalid admin token")


_TASK_STATUS_CSV_PATTERN = (
    r"^$|^\s*(pending|running|completed|failed|dead_letter)"
    r"(\s*,\s*(pending|running|completed|failed|dead_letter))*\s*$"
)
_MEMORY_STATUS_CSV_PATTERN = (
    r"^$|^\s*(ACTIVE|DELETED|SUPERSEDED|SUPPRESSED)"
    r"(\s*,\s*(ACTIVE|DELETED|SUPERSEDED|SUPPRESSED))*\s*$"
)


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


def _parse_audit_action(raw: str | None) -> str | None:
    """audit?action= 校验：值必须在已知动作集合内（delete / update / rebuild）。

    与 tasks 的 _parse_statuses 走同一契约：非法值 400，避免静默返回 []
    让操作员误以为"动作没产生数据"。"""
    if not raw:
        return None
    known = {"delete", "update", "rebuild"}
    if raw not in known:
        raise HTTPException(
            status_code=400,
            detail=f"invalid audit action: {raw!r} (expected one of {sorted(known)})",
        )
    return raw


@router.get("/overview", operation_id="admin_get_overview")
async def admin_overview(
    _: None = Depends(require_admin_token),
    service: MemoryService = Depends(get_memory_service),
) -> dict[str, Any]:
    """总览聚合：记忆/任务状态计数 + 数据分类 + 来源类型 + L3 后台写 +
    5min 吞吐 + Top-5 失败任务 + 最近 5 条治理审计。

    30s 轮询源：每个字段都做轻量后端查询（O(N) over 已存在的状态计数 + Top-K）。
    Top-5 失败任务聚合 failed + dead_letter，按 updated_at 倒序，方便维护者
    不跳 Tasks 页就看到故障根因（last_error 前 200 字）。
    """

    from thinkback.api.metrics import snapshot as metrics_snapshot

    def _shape_task(task: Any) -> dict[str, Any]:
        return {
            "task_id": task.task_id,
            "request_id": task.request_id,
            "op_type": task.op_type.value if hasattr(task.op_type, "value") else str(task.op_type),
            "status": task.status.value if hasattr(task.status, "value") else str(task.status),
            "scope": task.scope,
            "last_error": (task.last_error or "")[:200],
            "updated_at": task.scope.get("updated_at"),
            "retry_count": int(task.retry_count or 0),
        }

    def _collect_failed() -> list[dict[str, Any]]:
        try:
            failed = service.repository.list_tasks(statuses=["failed", "dead_letter"], limit=5)
        except Exception:
            return []
        return [_shape_task(t) for t in failed]

    def _collect_audit() -> list[dict[str, Any]]:
        try:
            entries = service.repository.list_admin_audit(limit=5)
        except Exception:
            return []
        return [
            {
                "audit_id": e.audit_id,
                "created_at": e.created_at.isoformat() if e.created_at else None,
                "operator": e.operator,
                "action": e.action,
                "target": e.target,
            }
            for e in entries
        ]

    stats = await asyncio.to_thread(service.overview_stats)
    l3 = await asyncio.to_thread(service.l3_background_status)
    recent_failed_tasks = await asyncio.to_thread(_collect_failed)
    recent_audit_actions = await asyncio.to_thread(_collect_audit)
    throughput = await asyncio.to_thread(metrics_snapshot)
    return {
        **stats,
        "l3": l3,
        "throughput_5min": throughput,
        "recent_failed_tasks": recent_failed_tasks,
        "recent_audit_actions": recent_audit_actions,
    }


@router.get("/tasks", operation_id="admin_list_tasks")
async def admin_list_tasks(
    _: None = Depends(require_admin_token),
    service: MemoryService = Depends(get_memory_service),
    statuses: str | None = Query(
        default=None,
        description="逗号分隔：pending,running,...",
        json_schema_extra={"pattern": _TASK_STATUS_CSV_PATTERN},
    ),
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
    statuses: str | None = Query(
        default=None,
        description="逗号分隔：ACTIVE,DELETED,...",
        json_schema_extra={"pattern": _MEMORY_STATUS_CSV_PATTERN},
    ),
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
            "affected_count": response.affected_memories,
        },
    )
    return response


@router.post("/update", operation_id="admin_update_memory")
async def admin_update_memory(
    request: UpdateMemoryRequest,
    _: None = Depends(require_admin_token),
    service: MemoryService = Depends(get_memory_service),
) -> UpdateMemoryResponse:
    """受控更新单条记忆（幂等同业务路由）+ 审计留痕（不存正文，仅留指纹）。"""
    response = await _run_admin_call(service.update_memory, request)
    # 审计日志只保留指纹 + 长度 + memory_type，不写原文——防止任何人凭
    # audit 接口窥探用户记忆正文（PII / 凭据 / 隐私泄漏面）。
    content_fingerprint = hashlib.sha256(request.content.encode("utf-8")).hexdigest()
    _audit(
        service,
        action="update",
        target=request.memory_id,
        detail={
            "operation_id": request.operation_id,
            "content_length": len(request.content),
            "content_sha256": content_fingerprint,
            "memory_type": request.memory_type.value if request.memory_type else None,
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
    action: str | None = Query(
        default=None, json_schema_extra={"pattern": r"^(|delete|update|rebuild)$"}
    ),
    operator: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[dict[str, Any]]:
    """审计查询：created_at 倒序分页（只读，不可篡改）。"""
    validated_action = _parse_audit_action(action)
    entries = await asyncio.to_thread(
        service.repository.list_admin_audit,
        action=validated_action,
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


@router.post("/maintenance/reclaim-orphan-tasks", operation_id="admin_reclaim_orphan_tasks")
async def admin_reclaim_orphan_tasks(
    _: None = Depends(require_admin_token),
    service: MemoryService = Depends(get_memory_service),
) -> dict[str, Any]:
    """维护：把超过 ``task_orphan_running_seconds`` 没更新的 running 任务回收为 failed。

    触发场景：进程被 SIGKILL / OOM / 节点驱逐后，in-flight 任务永远停在 running，
    GetTask 客户端会无限轮询。lifespan 启动期已自动跑过一次，运维在
    Overview 看到 running 数 > 0 且没推进时，可手动再触发一次。
    """
    reclaimed = await asyncio.to_thread(service.reclaim_orphan_running_tasks)
    return {"reclaimed_count": len(reclaimed), "reclaimed_task_ids": reclaimed}


@router.get("/health/detail", operation_id="admin_get_health_detail")
async def admin_health_detail() -> dict[str, Any]:
    """运行时身份 / 健康详情：服务卡直接消费。

    返回：进程启动时间、当前 uptime、alembic head、DB 连接池占用、gRPC 端口、
    关键开关（decay / L2 LLM / P0 白名单）—— 让维护者一眼看到
    "代码版本 / 数据库 schema / 运行配置" 是否一致。
    """
    import time

    from thinkback.infra.config import settings
    from thinkback.infra.database.engine import engine

    # alembic current head（轻量查询，只读）
    # 必须用 readiness_engine（NullPool，每查询新建连接），它绑定到 uvicorn
    # 主事件循环；SessionLocal 用的 engine 被 SqlAlchemyMemoryRepository 的
    # 专属 worker 线程 loop 占用，主循环直接 await 会触发
    # "got Future ... attached to a different loop"。
    alembic_current: str | None = None
    try:
        from sqlalchemy import text as _sql_text

        from thinkback.infra.database.engine import readiness_engine

        async with readiness_engine.connect() as conn:
            row = await conn.execute(_sql_text("SELECT version_num FROM alembic_version LIMIT 1"))
            alembic_current = row.scalar_one_or_none()
    except Exception as exc:
        # 表不存在或迁移没跑过 → 留 None（不阻塞 Overview 渲染）
        logger.opt(exception=exc).debug("alembic version probe failed")
        alembic_current = None

    # DB pool（Any 绕过 SQLAlchemy Pool 抽象基类无 size/checkedout 的 LSP 报警）
    pool: Any = engine.pool
    db_pool = {
        "size": getattr(pool, "size", lambda: 0)(),
        "checked_out": getattr(pool, "checkedout", lambda: 0)(),
        "overflow": getattr(pool, "overflow", lambda: 0)(),
        "max_overflow": getattr(pool, "_max_overflow", None),
    }
    # 解绑：让 lifespan/handler 之外的引用消失（无 perf 影响，标记依赖收口）
    _ = engine

    # uptime
    from thinkback.api import app as _app_module

    started_at = _app_module.PROCESS_STARTED_AT or time.time()
    uptime_seconds = time.time() - started_at if started_at else 0

    return {
        "uptime_seconds": uptime_seconds,
        "process_started_at": started_at if started_at else None,
        "app": {
            "name": settings.app_name,
            "version": settings.app_version,
            "environment": settings.environment,
            "log_level": settings.log_level,
        },
        "alembic_current": alembic_current,
        "db_pool": db_pool,
        "grpc": {
            "enabled": settings.grpc_enabled,
            "host": settings.grpc_host,
            "port": settings.grpc_port,
            "max_workers": settings.grpc_max_workers,
        },
        "flags": {
            "memory_l2_llm_enabled": settings.memory_l2_llm_enabled,
            "memory_decay_enabled": settings.memory_decay_enabled,
            "memory_infer_facts": settings.memory_infer_facts,
            "memory_p0_slots": settings.memory_p0_slots_list,
        },
    }


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
