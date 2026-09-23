"""gRPC MemoryService servicer — delegates to the domain MemoryService.

薄包装层：每个 RPC 方法接收 pb.Request → 转 pydantic → 调 ``MemoryService``
同步方法 → 转回 pb.Response。错误统一走 ``_handle_error`` 映射到 gRPC 状态码。
"""

from __future__ import annotations

import json
from typing import Any

import grpc
from loguru import logger

from thinkback.memory.service import MemoryService
from thinkback.rpc import memory_pb2 as pb
from thinkback.rpc import memory_pb2_grpc
from thinkback.rpc.converters import (
    _SUMMARY_STATE_TO_PB,
    _managed_item_to_pb,
    get_response_to_pb,
    list_response_to_pb,
    task_response_to_pb,
    to_append_request,
    to_delete_request,
    to_recall_request,
    to_update_request,
)


def _handle_error(exc: Exception, context: grpc.ServicerContext) -> None:
    """把域层异常映射到 gRPC 状态码（与 HTTP 映射保持同义）。

    - ``ValueError`` 含 ``fail-closed`` → PERMISSION_DENIED；``conflict`` → ABORTED；
      ``not found`` → NOT_FOUND；其它 → INVALID_ARGUMENT。
    - ``RuntimeError`` 含 ``dead_letter`` → ABORTED（HTTP 侧为 409，客户端应停止重试）；
      含 ``queue full`` / ``timed out`` → UNAVAILABLE；
      含 ``mem0`` / ``memory backend`` → INTERNAL；其它 → INTERNAL。
    - 未知异常 → INTERNAL。
    """

    detail = str(exc)
    if isinstance(exc, ValueError):
        if "fail-closed" in detail:
            context.abort(grpc.StatusCode.PERMISSION_DENIED, detail)
        elif "conflict" in detail:
            context.abort(grpc.StatusCode.ABORTED, detail)
        elif "not found" in detail:
            context.abort(grpc.StatusCode.NOT_FOUND, detail)
        else:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, detail)
    elif isinstance(exc, RuntimeError):
        if "dead_letter" in detail or "stale write" in detail:
            context.abort(grpc.StatusCode.ABORTED, detail)
        elif "queue full" in detail or "timed out" in detail:
            context.abort(grpc.StatusCode.UNAVAILABLE, detail)
        elif "mem0 library" in detail or "memory backend" in detail:
            context.abort(grpc.StatusCode.INTERNAL, detail)
        else:
            context.abort(grpc.StatusCode.INTERNAL, detail)
    else:
        context.abort(grpc.StatusCode.INTERNAL, detail)


class MemoryServicer(memory_pb2_grpc.MemoryServiceServicer):
    """gRPC MemoryService 实现。

    业务逻辑全部委托给 ``MemoryService``，本类只负责：
    - 接收 protobuf 请求 → 转 pydantic
    - 调用同步域方法
    - 把 pydantic 响应 → 转 protobuf
    - 异常映射 + 日志
    """

    def __init__(self, service: MemoryService) -> None:
        self._svc = service

    def Append(self, request: pb.AppendRequest, context: grpc.ServicerContext) -> pb.AppendResponse:
        try:
            resp = self._svc.append(to_append_request(request))
        except Exception as exc:
            logger.bind(rpc="Append", error=str(exc)).warning("grpc append failed")
            _handle_error(exc, context)
        return pb.AppendResponse(
            status=resp.status,
            task_id=resp.task_id,
            round_id=resp.round_id,
            l3_events=[json.dumps(e, default=str) for e in (resp.l3_events or [])],
        )

    def Recall(self, request: pb.RecallRequest, context: grpc.ServicerContext) -> pb.RecallResponse:
        try:
            resp = self._svc.recall(to_recall_request(request))
        except Exception as exc:
            logger.bind(rpc="Recall", error=str(exc)).warning("grpc recall failed")
            _handle_error(exc, context)
        items = []
        for item in resp.items:
            kw: dict[str, Any] = {
                "layer": item.layer,
                "content": item.content,
                "source": item.source,
                "metadata": {k: str(v) for k, v in (item.metadata or {}).items()},
            }
            if item.memory_id is not None:
                kw["memory_id"] = item.memory_id
            if item.score is not None:
                kw["score"] = item.score
            items.append(pb.MemoryItem(**kw))
        return pb.RecallResponse(
            status=resp.status,
            degraded=resp.degraded,
            degradation_reasons=resp.degradation_reasons,
            items=items,
        )

    def Delete(self, request: pb.DeleteRequest, context: grpc.ServicerContext) -> pb.DeleteResponse:
        try:
            resp = self._svc.delete(to_delete_request(request))
        except Exception as exc:
            logger.bind(rpc="Delete", error=str(exc)).warning("grpc delete failed")
            _handle_error(exc, context)
        kw: dict[str, Any] = {
            "status": resp.status,
            "task_id": resp.task_id,
            "affected_memories": resp.affected_memories,
        }
        if resp.summary_state is not None:
            kw["summary_state"] = _SUMMARY_STATE_TO_PB[resp.summary_state]
        return pb.DeleteResponse(**kw)

    def ListMemories(
        self, request: pb.ListMemoriesRequest, context: grpc.ServicerContext
    ) -> pb.ListMemoriesResponse:
        try:
            resp = self._svc.list_memory_items(
                user_id=request.user_id, include_deleted=request.include_deleted
            )
        except Exception as exc:
            logger.bind(rpc="ListMemories", error=str(exc)).warning("grpc list_memories failed")
            _handle_error(exc, context)
        return list_response_to_pb(resp)

    def GetMemory(
        self, request: pb.GetMemoryRequest, context: grpc.ServicerContext
    ) -> pb.GetMemoryResponse:
        try:
            resp = self._svc.get_memory_item(user_id=request.user_id, memory_id=request.memory_id)
        except Exception as exc:
            logger.bind(rpc="GetMemory", error=str(exc)).warning("grpc get_memory failed")
            _handle_error(exc, context)
        if resp is None:
            context.abort(grpc.StatusCode.NOT_FOUND, "memory not found")
        return get_response_to_pb(resp)  # type: ignore[arg-type]

    def UpdateMemory(
        self, request: pb.UpdateMemoryRequest, context: grpc.ServicerContext
    ) -> pb.UpdateMemoryResponse:
        try:
            resp = self._svc.update_memory(to_update_request(request))
        except Exception as exc:
            logger.bind(rpc="UpdateMemory", error=str(exc)).warning("grpc update_memory failed")
            _handle_error(exc, context)
        kw: dict[str, Any] = {
            "status": resp.status,
            "task_id": resp.task_id,
            "memory": _managed_item_to_pb(resp.memory),
        }
        if resp.summary_state is not None:
            kw["summary_state"] = _SUMMARY_STATE_TO_PB[resp.summary_state]
        return pb.UpdateMemoryResponse(**kw)

    def GetTask(self, request: pb.GetTaskRequest, context: grpc.ServicerContext) -> pb.TaskResponse:
        try:
            resp = self._svc.get_task(request.task_id)
        except Exception as exc:
            logger.bind(rpc="GetTask", error=str(exc)).warning("grpc get_task failed")
            _handle_error(exc, context)
        if resp is None:
            context.abort(grpc.StatusCode.NOT_FOUND, "task not found")
        return task_response_to_pb(resp)  # type: ignore[arg-type]

    def GetL3BackgroundStatus(
        self,
        request: pb.L3BackgroundStatusRequest,  # noqa: ARG002
        context: grpc.ServicerContext,
    ) -> pb.L3BackgroundStatusResponse:
        try:
            s = self._svc.l3_background_status()
        except Exception as exc:
            logger.bind(rpc="GetL3BackgroundStatus", error=str(exc)).warning(
                "grpc l3_background_status failed"
            )
            _handle_error(exc, context)
        return pb.L3BackgroundStatusResponse(
            write_mode=s["write_mode"],
            executor_workers=s["executor_workers"],
            max_pending_tasks=s["max_pending_tasks"],
            pending_write_tasks=s["pending_write_tasks"],
            cleanup_tasks=s["cleanup_tasks"],
            available_capacity=s["available_capacity"],
        )
