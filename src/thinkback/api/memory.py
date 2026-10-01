"""Memory workflow routes.

记忆业务 HTTP 接口层。把请求转换为 ``MemoryService`` 的同步方法调用，
并通过独立的读/写线程池做并发限流：

- 读接口（recall / list / get / task / l3-status）共用 ``_memory_read_*``
- 写接口（append / delete / update）共用 ``_memory_write_*``

线程池容量来自 ``settings.memory_api_worker_limit``，超时来自
``settings.memory_api_worker_wait_seconds``；超时统一映射为 HTTP 503。
"""

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from functools import partial
from time import monotonic
from typing import Any, ParamSpec, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from loguru import logger

from thinkback.api.dependencies import (
    get_memory_service,
)
from thinkback.api.errors import service_error_to_http
from thinkback.api.metrics import record_event
from thinkback.infra.config import settings
from thinkback.memory.schemas import (
    AppendMemoryRequest,
    AppendMemoryResponse,
    DeleteMemoryRequest,
    DeleteMemoryResponse,
    GetMemoryResponse,
    L3BackgroundStatusResponse,
    ListMemoriesResponse,
    RecallMemoryRequest,
    RecallMemoryResponse,
    TaskResponse,
    UpdateMemoryRequest,
    UpdateMemoryResponse,
)
from thinkback.memory.service import MemoryService

router = APIRouter(
    prefix="/memory",
    tags=["memory"],
)
P = ParamSpec("P")
R = TypeVar("R")
_memory_read_call_slots = asyncio.BoundedSemaphore(settings.memory_api_worker_limit)
_memory_read_call_executor = ThreadPoolExecutor(
    max_workers=settings.memory_api_worker_limit,
    thread_name_prefix="thinkback-api-read",
)
_memory_write_call_slots = asyncio.BoundedSemaphore(settings.memory_api_worker_limit)
_memory_write_call_executor = ThreadPoolExecutor(
    max_workers=settings.memory_api_worker_limit,
    thread_name_prefix="thinkback-api-write",
)
logger.bind(
    worker_limit=settings.memory_api_worker_limit,
    wait_seconds=settings.memory_api_worker_wait_seconds,
).info("memory api worker pools initialized")


async def _run_read_memory_call[**P, R](
    method: Callable[P, R], *args: P.args, **kwargs: P.kwargs
) -> R:
    """在读线程池里执行 ``MemoryService`` 的同步方法。"""

    return await _run_memory_call_in_pool(
        _memory_read_call_slots,
        _memory_read_call_executor,
        method,
        *args,
        **kwargs,
    )


async def _run_write_memory_call[**P, R](
    method: Callable[P, R], *args: P.args, **kwargs: P.kwargs
) -> R:
    """在写线程池里执行 ``MemoryService`` 的同步方法。

    流程图::

        method + *args + **kwargs
            │
            ▼
        委托给 _run_memory_call_in_pool
        ┌──────────────────────────────────────────────┐
        │ slots   = _memory_write_call_slots           │
        │ exec    = _memory_write_call_executor        │
        │ method  = 透传                                │
        │ *args, **kwargs = 透传                        │
        └──────────────────────────────────────────────┘
            │
            ▼
        返回 R（与 ``method(*args, **kwargs)`` 同型）

    与 :func:`_run_read_memory_call` 对称；共享同一条
    :func:`_run_memory_call_in_pool` 的调度/异常映射路径，
    唯一的差别在于使用的 ``slots`` 和 ``executor``。
    """

    return await _run_memory_call_in_pool(
        _memory_write_call_slots,
        _memory_write_call_executor,
        method,
        *args,
        **kwargs,
    )


async def _run_memory_call_in_pool[**P, R](
    slots: asyncio.BoundedSemaphore,
    executor: ThreadPoolExecutor,
    method: Callable[P, R],
    *args: P.args,
    **kwargs: P.kwargs,
) -> R:
    """在线程池中执行同步调用，并把异常映射到合适的 HTTP 状态码。

    异常映射规则：
    - ``ValueError`` 含 ``fail-closed`` → 403；含 ``conflict`` → 409；含 ``not found`` → 404；其它 → 400
    - ``RuntimeError`` 含 ``dead_letter`` → 409；``queue full`` / ``timed out`` → 503；
      含 ``mem0`` / ``memory backend`` → 502；其它保持 500

    流程图::

        进入 (slots, executor, method, *args, **kwargs)
            │
            ▼
        计算 deadline = monotonic() + wait_seconds
            │
            ▼
        ┌─── 阶段 1: 准入 ─────────────────────────────┐
        │  await slots.acquire() (timeout=wait_seconds) │
        └───────────────────────┬─────────────────────┘
                │ 超时 TimeoutError
                │   └─► WARNING "slot acquire timed out"
                │       └─► 503 queue full
                │
                ▼ 拿到槽位
        ┌─── 阶段 2: 准备与提交 ────────────────────────┐
        │  call    = partial(method, *args, **kwargs)   │
        │  context = copy_context()  (传递 loguru ctx)  │
        │  method_name = method.__name__                │
        │  DEBUG "call submitted"                       │
        │  try executor.submit(context.run, call)       │
        └───────────────────────┬─────────────────────┘
                │ submit 抛异常
                │   └─► slots.release()
                │       WARNING "submit failed"
                │       向上抛 (→ FastAPI 默认 500)
                │
                ▼ submit 成功
        注册 done_callback:
          future 完成时 → loop.call_soon_threadsafe(slots.release)
        (无论后续成功/失败/超时，槽位都会归还)
                │
                ▼
        ┌─── 阶段 3: 等待结果 ──────────────────────────┐
        │  remaining = max(0, deadline - monotonic())   │
        │  await asyncio.wait_for(wrapped_future,       │
        │                        timeout=remaining)     │
        └───────────────────────┬─────────────────────┘
                │ 超时 TimeoutError
                │   └─► WARNING "call timed out"
                │       └─► 503 queue full
                │
                ▼ 拿到结果 / 异常
        ┌─── 阶段 4: 异常映射 ──────────────────────────┐
        │  ValueError                                    │
        │    [fail-closed] → 403                         │
        │    [conflict]    → 409                         │
        │    [not found]   → 404                         │
        │    [其它]        → 400                         │
        │  RuntimeError                                  │
        │    [dead_letter]                       → 409  │
        │    [l3 background queue full]          → 503  │
        │    [repository operation timed out]    → 503  │
        │    [mem0 library] / [memory backend]  → 502  │
        │    [其它]                                 → 500│
        │  其它正常返回                                  │
        └───────────────────────┬─────────────────────┘
                                ▼
                        返回 R
    """
    wait_seconds = settings.memory_api_worker_wait_seconds
    deadline = monotonic() + wait_seconds
    try:
        await asyncio.wait_for(slots.acquire(), timeout=wait_seconds)
    except TimeoutError as exc:
        logger.bind(
            method_name=getattr(method, "__name__", type(method).__name__),
            wait_seconds=wait_seconds,
        ).warning("memory api worker slot acquire timed out")
        raise HTTPException(
            status_code=503,
            detail="memory api worker queue full: temporarily unavailable",
        ) from exc

    call = partial(method, *args, **kwargs)
    context = copy_context()
    method_name = getattr(method, "__name__", type(method).__name__)
    try:
        logger.bind(method_name=method_name, wait_seconds=wait_seconds).debug(
            "memory api worker call submitted"
        )
        future = executor.submit(context.run, call)
    except Exception:
        slots.release()
        logger.bind(method_name=method_name).warning("memory api worker submit failed")
        raise
    loop = asyncio.get_running_loop()
    future.add_done_callback(lambda _future: loop.call_soon_threadsafe(slots.release))

    try:
        wrapped_future = asyncio.wrap_future(future)
        return await asyncio.wait_for(
            wrapped_future,
            timeout=max(0.0, deadline - monotonic()),
        )
    except TimeoutError as exc:
        logger.bind(method_name=method_name, wait_seconds=wait_seconds).warning(
            "memory api worker call timed out"
        )
        raise HTTPException(
            status_code=503,
            detail="memory api worker queue full: temporarily unavailable",
        ) from exc
    except ValueError as exc:
        mapped = service_error_to_http(exc)
        assert mapped is not None  # ValueError 恒有映射
        raise mapped from exc
    except RuntimeError as exc:
        mapped = service_error_to_http(exc)
        if mapped is not None:
            raise mapped from exc
        # 未分类 RuntimeError：带 traceback 落日志后再上抛。
        # 没有这里的服务端日志，线上一次性 500 无法定位根因。
        logger.opt(exception=exc).bind(method_name=method_name).error(
            "memory api worker call failed with unclassified error"
        )
        raise
    except Exception as exc:
        # 非 ValueError / RuntimeError 的未知异常同样必须留下 traceback。
        logger.opt(exception=exc).bind(method_name=method_name).error(
            "memory api worker call failed with unexpected error"
        )
        raise


@router.post(
    "/append",
    response_model=AppendMemoryResponse,
    summary="写入一轮对话记忆",
    operation_id="memory_append",
    description=(
        "写入一个完整 user -> assistant 轮次，生成可靠轮次记录，"
        "并触发 L1/L2 更新和 L3 长期记忆沉淀。"
    ),
)
async def append_memory(
    request: AppendMemoryRequest,
    service: MemoryService = Depends(get_memory_service),
) -> AppendMemoryResponse:
    """写入一轮对话记忆，触发 L1/L2 更新与 L3 沉淀。

    流程图::

        请求到达
            │
            ▼
        绑定日志上下文
        (user_id, session_id, round_id, message_count)
            │
            ▼
        DEBUG: "memory append request received"
            │
            ▼
        写入线程池 _run_write_memory_call
        (service.append, request)
            │
            ├─► 槽位获取 (slots.acquire)
            │      超时 wait_seconds → 503 queue full
            │
            ├─► executor.submit(context.run, call)
            │      失败 → 释放槽位并向上抛
            │
            ├─► await 结果 (asyncio.wait_for)
            │      超时 → 503 queue full
            │
            └─► 异常映射
                   ValueError:
                     [fail-closed] → 403
                     [conflict]    → 409
                     [not found]   → 404
                     [其它]        → 400
                   RuntimeError:
                     [dead_letter] → 409
                     [queue full]  → 503
                     [backend]     → 502
            │
            ▼
        返回 AppendMemoryResponse
    """
    logger.bind(
        user_id=request.user_id,
        session_id=request.session_id,
        round_id=request.round_id,
        message_count=len(request.messages),
    ).debug("memory append request received")
    try:
        response = await _run_write_memory_call(service.append, request)
    except Exception:
        record_event("append_fail")
        raise
    record_event("append_ok")
    return response


@router.post(
    "/recall",
    response_model=RecallMemoryResponse,
    summary="召回用户相关记忆",
    operation_id="memory_recall",
    description=("按 user、session 和 query 召回 L1/L2/L3 记忆，并返回降级状态和降级原因。"),
)
async def recall_memory(
    request: RecallMemoryRequest,
    service: MemoryService = Depends(get_memory_service),
) -> RecallMemoryResponse:
    """按 user、session 与 query 召回 L1/L2/L3 记忆。

    流程图::

        请求到达
            │
            ▼
        绑定日志上下文
        (user_id, session_id, intent, query_length)
            │
            ▼
        DEBUG: "memory recall request received"
            │
            ▼
        读取线程池 _run_read_memory_call
        (service.recall, request)
            │
            ├─► 槽位获取 (slots.acquire)
            │      超时 wait_seconds → 503 queue full
            │
            ├─► executor.submit(context.run, call)
            │      失败 → 释放槽位并向上抛
            │
            ├─► await 结果 (asyncio.wait_for)
            │      超时 → 503 queue full
            │
            └─► 异常映射
                   ValueError:
                     [fail-closed] → 403
                     [not found]   → 404
                     [其它]        → 400
                   RuntimeError:
                     [queue full]  → 503
                     [backend]     → 502
            │
            ▼
        返回 RecallMemoryResponse
        (含 L1/L2/L3 命中、降级状态 degraded、
         降级原因 reasons)
    """
    logger.bind(
        user_id=request.user_id,
        session_id=request.session_id,
        intent=request.intent.value,
        query_length=len(request.query),
    ).debug("memory recall request received")
    try:
        response = await _run_read_memory_call(service.recall, request)
    except Exception:
        record_event("recall_fail")
        raise
    record_event("recall_ok")
    return response


@router.post(
    "/delete",
    response_model=DeleteMemoryResponse,
    summary="删除记忆或记忆范围",
    operation_id="memory_delete",
    description=("按单条 memory、session 或用户全局范围删除记忆，并记录删除任务和摘要脏标记。"),
)
async def delete_memory(
    request: DeleteMemoryRequest,
    service: MemoryService = Depends(get_memory_service),
) -> DeleteMemoryResponse:
    """按 memory_id / session / 全局范围删除记忆。

    流程图::

        请求到达
            │
            ▼
        绑定日志上下文
        (user_id, session_id, scope,
         operation_id, has_memory_id)
            │
            ▼
        DEBUG: "memory delete request received"
            │
            ▼
        写入线程池 _run_write_memory_call
        (service.delete, request)
            │
            ├─► 槽位获取 (slots.acquire)
            │      超时 wait_seconds → 503 queue full
            │
            ├─► executor.submit(context.run, call)
            │      失败 → 释放槽位并向上抛
            │
            ├─► await 结果 (asyncio.wait_for)
            │      超时 → 503 queue full
            │
            └─► 异常映射
                   ValueError:
                     [not found]   → 404
                     [fail-closed] → 403
                     [conflict]    → 409
                     [其它]        → 400
                   RuntimeError:
                     [dead_letter] → 409
                     [queue full]  → 503
                     [backend]     → 502
            │
            ▼
        返回 DeleteMemoryResponse
        (含删除任务 task_id、
         摘要脏标记 dirty_summary)
    """
    logger.bind(
        user_id=request.user_id,
        session_id=request.session_id,
        scope=request.scope.value,
        operation_id=request.operation_id,
        has_memory_id=request.memory_id is not None,
    ).debug("memory delete request received")
    return await _run_write_memory_call(service.delete, request)


@router.get(
    "/items",
    response_model=ListMemoriesResponse,
    summary="列出可管理的长期记忆",
    operation_id="memory_list_items",
    description="按 user_id 列出可编辑或删除的长期记忆；默认只返回 ACTIVE 记忆。",
)
async def list_memory_items(
    user_id: str = Query(description="用户 ID；只返回该用户范围内的长期记忆。"),
    include_deleted: bool = Query(
        default=False,
        description="是否包含已删除或已取代的业务记忆；系统墓碑不会返回。",
    ),
    service: MemoryService = Depends(get_memory_service),
) -> ListMemoriesResponse:
    logger.bind(user_id=user_id, include_deleted=include_deleted).debug(
        "memory management list request received"
    )
    return await _run_read_memory_call(
        service.list_memory_items,
        user_id=user_id,
        include_deleted=include_deleted,
    )


@router.get(
    "/items/{memory_id}",
    response_model=GetMemoryResponse,
    summary="查询单条可管理记忆",
    operation_id="memory_get_item",
    description="按业务 memory_id 查询单条长期记忆详情，不暴露内部作用域或后端 ID。",
)
async def get_memory_item(
    memory_id: str = Path(description="要查询的业务记忆 ID。"),
    user_id: str = Query(description="用户 ID；只查询该用户范围内的长期记忆。"),
    service: MemoryService = Depends(get_memory_service),
) -> GetMemoryResponse:
    logger.bind(user_id=user_id, memory_id=memory_id).debug(
        "memory management get request received"
    )
    response = await _run_read_memory_call(
        service.get_memory_item,
        user_id=user_id,
        memory_id=memory_id,
    )
    if response is None:
        raise HTTPException(status_code=404, detail="memory not found")
    return response


@router.post(
    "/update",
    response_model=UpdateMemoryResponse,
    summary="编辑单条长期记忆",
    operation_id="memory_update_item",
    description="按业务 memory_id 编辑一条 ACTIVE 长期记忆，并将相关 L1/L2 fail-safe 置脏。",
)
async def update_memory_item(
    request: UpdateMemoryRequest,
    service: MemoryService = Depends(get_memory_service),
) -> UpdateMemoryResponse:
    """编辑单条 ACTIVE 长期记忆，并将相关 L1/L2 fail-safe 置脏。

    流程图::

        请求到达
            │
            ▼
        绑定日志上下文
        (user_id, memory_id,
         operation_id, content_length)
            │
            ▼
        DEBUG: "memory management update request received"
            │
            ▼
        写入线程池 _run_write_memory_call
        (service.update_memory, request)
            │
            ├─► 槽位获取 (slots.acquire)
            │      超时 wait_seconds → 503 queue full
            │
            ├─► executor.submit(context.run, call)
            │      失败 → 释放槽位并向上抛
            │
            ├─► await 结果 (asyncio.wait_for)
            │      超时 → 503 queue full
            │
            └─► 异常映射
                   ValueError:
                     [not found]   → 404
                     [fail-closed] → 403
                     [conflict]    → 409
                     [其它]        → 400
                   RuntimeError:
                     [queue full]  → 503
                     [backend]     → 502
            │
            ▼
        返回 UpdateMemoryResponse
        (含 L1/L2 fail-safe 脏标记 dirty_failsafe)
    """
    logger.bind(
        user_id=request.user_id,
        memory_id=request.memory_id,
        operation_id=request.operation_id,
        content_length=len(request.content),
    ).debug("memory management update request received")
    return await _run_write_memory_call(service.update_memory, request)


@router.get(
    "/tasks/{task_id}",
    response_model=TaskResponse,
    summary="查询记忆后台任务",
    operation_id="memory_get_task",
    description="按 task_id 查询写入、删除或重建任务的执行状态、错误和结果。",
)
async def get_memory_task(
    task_id: str = Path(description="要查询的记忆后台任务 ID。"),
    service: MemoryService = Depends(get_memory_service),
) -> TaskResponse:
    logger.bind(task_id=task_id).debug("memory task request received")
    task = await _run_read_memory_call(service.get_task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="memory task not found")
    return task


@router.get(
    "/l3/background-status",
    response_model=L3BackgroundStatusResponse,
    summary="查看 L3 后台队列状态",
    operation_id="memory_l3_background_status",
    description="返回 L3 后台写入模式、线程池、排队任务数和剩余容量。",
)
async def get_l3_background_status(
    service: MemoryService = Depends(get_memory_service),
) -> dict[str, Any]:
    """查询 L3 后台写入队列状态。

    流程图::

        请求到达
            │
            ▼
        DEBUG: "memory l3 background status request received"
            │
            ▼
        读取线程池 _run_read_memory_call
        (service.l3_background_status)
            │
            ├─► 槽位获取 (slots.acquire)
            │      超时 wait_seconds → 503 queue full
            │
            ├─► executor.submit(context.run, call)
            │      失败 → 释放槽位并向上抛
            │
            ├─► await 结果 (asyncio.wait_for)
            │      超时 → 503 queue full
            │
            └─► 异常映射
                   ValueError:
                     [fail-closed] → 403
                     [其它]        → 400
                   RuntimeError:
                     [queue full]  → 503
                     [backend]     → 502
            │
            ▼
        返回 L3BackgroundStatusResponse
        (mode / workers / queued / free_slots)
    """
    logger.debug("memory l3 background status request received")
    return await _run_read_memory_call(service.l3_background_status)
