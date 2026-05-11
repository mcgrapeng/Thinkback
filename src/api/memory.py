"""Memory workflow routes."""

from collections.abc import Callable
from functools import partial
from typing import ParamSpec, TypeVar

from anyio import CapacityLimiter, to_thread
from fastapi import APIRouter, Depends, HTTPException

from api.dependencies import get_memory_service
from infra.config import settings
from memory.schemas import (
    AppendMemoryRequest,
    AppendMemoryResponse,
    DeleteMemoryRequest,
    DeleteMemoryResponse,
    RebuildMemoryRequest,
    RebuildMemoryResponse,
    RecallMemoryRequest,
    RecallMemoryResponse,
    TaskResponse,
)
from memory.service import MemoryService

router = APIRouter(prefix="/memory", tags=["memory"])
P = ParamSpec("P")
R = TypeVar("R")
# 中文注释：记忆服务内部仍有同步数据库和 Mem0 调用；这里统一丢进受限线程池，避免阻塞 FastAPI 事件循环。
_memory_call_limiter = CapacityLimiter(settings.memory_api_worker_limit)


async def _run_memory_call(method: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
    try:
        call = partial(method, *args, **kwargs)
        return await to_thread.run_sync(
            call,
            limiter=_memory_call_limiter,
        )
    except ValueError as exc:
        detail = str(exc)
        # 中文注释：领域层抛 ValueError 时，HTTP 层只做状态码映射，不改写英文错误文本，方便日志和调用方排查。
        if "fail-closed" in detail:
            status_code = 403
        elif "conflict" in detail:
            status_code = 409
        else:
            status_code = 400
        raise HTTPException(status_code=status_code, detail=detail) from exc
    except RuntimeError as exc:
        detail = str(exc)
        if "l3 background queue full" in detail:
            raise HTTPException(status_code=503, detail=detail) from exc
        if "mem0 library" in detail or "memory backend" in detail:
            raise HTTPException(status_code=502, detail=detail) from exc
        raise


@router.post(
    "/append",
    response_model=AppendMemoryResponse,
    summary="写入一轮对话记忆",
    description=(
        "写入一个完整 user -> assistant 轮次，生成可靠轮次记录，"
        "并触发 L1/L2 更新和 L3 长期记忆沉淀。"
    ),
)
async def append_memory(
    request: AppendMemoryRequest,
    service: MemoryService = Depends(get_memory_service),
) -> AppendMemoryResponse:
    return await _run_memory_call(service.append, request)


@router.post(
    "/recall",
    response_model=RecallMemoryResponse,
    summary="召回用户相关记忆",
    description=(
        "按 user、character、session 和 query 召回 L1/L2/L3 记忆，"
        "并返回降级状态和降级原因。"
    ),
)
async def recall_memory(
    request: RecallMemoryRequest,
    service: MemoryService = Depends(get_memory_service),
) -> RecallMemoryResponse:
    return await _run_memory_call(service.recall, request)


@router.post(
    "/delete",
    response_model=DeleteMemoryResponse,
    summary="删除记忆或记忆范围",
    description=(
        "按单条 memory、session 或用户角色范围删除记忆，"
        "并记录删除任务和摘要脏标记。"
    ),
)
async def delete_memory(
    request: DeleteMemoryRequest,
    service: MemoryService = Depends(get_memory_service),
) -> DeleteMemoryResponse:
    return await _run_memory_call(service.delete, request)


@router.post(
    "/rebuild",
    response_model=RebuildMemoryResponse,
    summary="重建记忆摘要和长期记忆",
    description="按指定作用域重建 L2 摘要和 L3 长期记忆，并返回后台任务状态。",
)
async def rebuild_memory(
    request: RebuildMemoryRequest,
    service: MemoryService = Depends(get_memory_service),
) -> RebuildMemoryResponse:
    return await _run_memory_call(service.rebuild, request)


@router.get(
    "/tasks/{task_id}",
    response_model=TaskResponse,
    summary="查询记忆后台任务",
    description="按 task_id 查询写入、删除或重建任务的执行状态、错误和结果。",
)
async def get_memory_task(
    task_id: str,
    service: MemoryService = Depends(get_memory_service),
) -> TaskResponse:
    task = await _run_memory_call(service.get_task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="memory task not found")
    return task


@router.get(
    "/l3/background-status",
    summary="查看 L3 后台队列状态",
    description="返回 L3 后台写入模式、线程池、排队任务数和剩余容量。",
)
async def get_l3_background_status(
    service: MemoryService = Depends(get_memory_service),
) -> dict:
    return await _run_memory_call(service.l3_background_status)
