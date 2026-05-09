"""Memory workflow routes."""

from collections.abc import Callable
from typing import ParamSpec, TypeVar

from fastapi import APIRouter, Depends, HTTPException
from starlette.concurrency import run_in_threadpool

from api.dependencies import get_memory_service
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


async def _run_memory_call(method: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
    try:
        return await run_in_threadpool(method, *args, **kwargs)
    except ValueError as exc:
        detail = str(exc)
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


@router.post("/append", response_model=AppendMemoryResponse)
async def append_memory(
    request: AppendMemoryRequest,
    service: MemoryService = Depends(get_memory_service),
) -> AppendMemoryResponse:
    return await _run_memory_call(service.append, request)


@router.post("/recall", response_model=RecallMemoryResponse)
async def recall_memory(
    request: RecallMemoryRequest,
    service: MemoryService = Depends(get_memory_service),
) -> RecallMemoryResponse:
    return await _run_memory_call(service.recall, request)


@router.post("/delete", response_model=DeleteMemoryResponse)
async def delete_memory(
    request: DeleteMemoryRequest,
    service: MemoryService = Depends(get_memory_service),
) -> DeleteMemoryResponse:
    return await _run_memory_call(service.delete, request)


@router.post("/rebuild", response_model=RebuildMemoryResponse)
async def rebuild_memory(
    request: RebuildMemoryRequest,
    service: MemoryService = Depends(get_memory_service),
) -> RebuildMemoryResponse:
    return await _run_memory_call(service.rebuild, request)


@router.get("/tasks/{task_id}", response_model=TaskResponse)
async def get_memory_task(
    task_id: str,
    service: MemoryService = Depends(get_memory_service),
) -> TaskResponse:
    task = await _run_memory_call(service.get_task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="memory task not found")
    return task


@router.get("/l3/background-status")
async def get_l3_background_status(
    service: MemoryService = Depends(get_memory_service),
) -> dict:
    return await _run_memory_call(service.l3_background_status)
