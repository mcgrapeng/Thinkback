"""Proto ↔ pydantic schema conversion helpers.

集中放置 protobuf 与 pydantic schema 之间的转换函数，目的是：
- 把 enum 映射放在文件顶部，避免散落各处；
- 让 servicer 层只关心业务编排，不再纠缠数据类型转换；
- 单元测试可以脱离 gRPC 直接覆盖转换函数。
"""

from __future__ import annotations

import json
from typing import Any

from thinkback.memory.schemas import (
    AppendMemoryRequest,
    DataClassification,
    DeleteMemoryRequest,
    DeleteScope,
    GetMemoryResponse,
    ListMemoriesResponse,
    ManagedMemoryItem,
    MemoryMessage,
    MemoryStatus,
    MemoryType,
    OperationType,
    RebuildMemoryRequest,
    RecallIntent,
    RecallMemoryRequest,
    SourceType,
    SummaryState,
    TaskResponse,
    TaskStatus,
    UpdateMemoryRequest,
)
from thinkback.rpc import memory_pb2 as pb

# ── enum maps (proto → domain) ──────────────────────────────────────────────

_ROLE = {
    pb.MESSAGE_ROLE_USER: "user",
    pb.MESSAGE_ROLE_ASSISTANT: "assistant",
    pb.MESSAGE_ROLE_SYSTEM: "system",
}
_RECALL_INTENT = {
    pb.RECALL_INTENT_CHAT: RecallIntent.CHAT,
    pb.RECALL_INTENT_SENSITIVE: RecallIntent.SENSITIVE,
}
_DELETE_SCOPE = {
    pb.DELETE_SCOPE_MEMORY: DeleteScope.MEMORY,
    pb.DELETE_SCOPE_SESSION: DeleteScope.SESSION,
    pb.DELETE_SCOPE_ALL: DeleteScope.ALL,
}
_MEMORY_TYPE_FROM_PB = {
    pb.MEMORY_TYPE_PROFILE: MemoryType.PROFILE,
    pb.MEMORY_TYPE_PREFERENCE: MemoryType.PREFERENCE,
    pb.MEMORY_TYPE_EVENT: MemoryType.EVENT,
    pb.MEMORY_TYPE_PLAN: MemoryType.PLAN,
    pb.MEMORY_TYPE_CONSTRAINT: MemoryType.CONSTRAINT,
}

# ── enum maps (domain → proto) ──────────────────────────────────────────────

_MEMORY_STATUS_TO_PB = {
    MemoryStatus.ACTIVE: pb.MEMORY_STATUS_ACTIVE,
    MemoryStatus.DELETED: pb.MEMORY_STATUS_DELETED,
    MemoryStatus.SUPPRESSED: pb.MEMORY_STATUS_SUPPRESSED,
    MemoryStatus.SUPERSEDED: pb.MEMORY_STATUS_SUPERSEDED,
}
_SOURCE_TYPE_TO_PB = {
    SourceType.CHAT_ROUND: pb.SOURCE_TYPE_CHAT_ROUND,
    SourceType.SESSION_REBUILD: pb.SOURCE_TYPE_SESSION_REBUILD,
    SourceType.MANUAL_FIX: pb.SOURCE_TYPE_MANUAL_FIX,
    SourceType.IMPORT: pb.SOURCE_TYPE_IMPORT,
    SourceType.SYSTEM_MIGRATION: pb.SOURCE_TYPE_SYSTEM_MIGRATION,
}
_DATA_CLASS_TO_PB = {
    DataClassification.NORMAL: pb.DATA_CLASSIFICATION_NORMAL,
    DataClassification.PERSONAL: pb.DATA_CLASSIFICATION_PERSONAL,
    DataClassification.SENSITIVE: pb.DATA_CLASSIFICATION_SENSITIVE,
    DataClassification.RESTRICTED: pb.DATA_CLASSIFICATION_RESTRICTED,
}
_MEMORY_TYPE_TO_PB = {
    MemoryType.PROFILE: pb.MEMORY_TYPE_PROFILE,
    MemoryType.PREFERENCE: pb.MEMORY_TYPE_PREFERENCE,
    MemoryType.EVENT: pb.MEMORY_TYPE_EVENT,
    MemoryType.PLAN: pb.MEMORY_TYPE_PLAN,
    MemoryType.CONSTRAINT: pb.MEMORY_TYPE_CONSTRAINT,
}
_TASK_STATUS_TO_PB = {
    TaskStatus.PENDING: pb.TASK_STATUS_PENDING,
    TaskStatus.RUNNING: pb.TASK_STATUS_RUNNING,
    TaskStatus.COMPLETED: pb.TASK_STATUS_COMPLETED,
    TaskStatus.FAILED: pb.TASK_STATUS_FAILED,
    TaskStatus.DEAD_LETTER: pb.TASK_STATUS_DEAD_LETTER,
}
_OP_TYPE_TO_PB = {
    OperationType.WRITE_ROUND: pb.OPERATION_TYPE_WRITE_ROUND,
    OperationType.UPDATE_MEMORY: pb.OPERATION_TYPE_UPDATE_MEMORY,
    OperationType.DELETE_MEMORY: pb.OPERATION_TYPE_DELETE_MEMORY,
    OperationType.DELETE_SESSION: pb.OPERATION_TYPE_DELETE_SESSION,
    OperationType.DELETE_ALL: pb.OPERATION_TYPE_DELETE_ALL,
    OperationType.REBUILD_L2: pb.OPERATION_TYPE_REBUILD_L2,
    OperationType.REBUILD_L3: pb.OPERATION_TYPE_REBUILD_L3,
}
_SUMMARY_STATE_TO_PB = {
    SummaryState.ACTIVE: pb.SUMMARY_STATE_ACTIVE,
    SummaryState.STALE: pb.SUMMARY_STATE_STALE,
    SummaryState.DIRTY: pb.SUMMARY_STATE_DIRTY,
    SummaryState.REBUILDING: pb.SUMMARY_STATE_REBUILDING,
}


# ── request converters ───────────────────────────────────────────────────────


def to_append_request(req: pb.AppendRequest) -> AppendMemoryRequest:
    from datetime import datetime

    messages = [
        MemoryMessage(
            message_id=m.message_id,
            role=_ROLE[m.role],
            content=m.content,
            timestamp=datetime.fromisoformat(m.timestamp),
        )
        for m in req.messages
    ]
    return AppendMemoryRequest(
        request_id=req.request_id,
        user_id=req.user_id,
        session_id=req.session_id,
        round_id=req.round_id,
        messages=messages,
        source_timestamp=datetime.fromisoformat(req.source_timestamp),
        round_index=req.round_index if req.HasField("round_index") else None,
        metadata=dict(req.metadata),
    )


def to_recall_request(req: pb.RecallRequest) -> RecallMemoryRequest:
    return RecallMemoryRequest(
        user_id=req.user_id,
        session_id=req.session_id,
        query=req.query,
        intent=_RECALL_INTENT.get(req.intent, RecallIntent.CHAT),
        l3_limit=req.l3_limit or 5,
        l3_score_threshold=req.l3_score_threshold,
        token_budget=req.token_budget or 1200,
    )


def to_delete_request(req: pb.DeleteRequest) -> DeleteMemoryRequest:
    return DeleteMemoryRequest(
        request_id=req.request_id,
        user_id=req.user_id,
        scope=_DELETE_SCOPE[req.scope],
        operation_id=req.operation_id,
        memory_id=req.memory_id if req.HasField("memory_id") else None,
        session_id=req.session_id if req.HasField("session_id") else None,
    )


def to_update_request(req: pb.UpdateMemoryRequest) -> UpdateMemoryRequest:
    memory_type = _MEMORY_TYPE_FROM_PB.get(req.memory_type) if req.HasField("memory_type") else None
    return UpdateMemoryRequest(
        request_id=req.request_id,
        user_id=req.user_id,
        operation_id=req.operation_id,
        memory_id=req.memory_id,
        content=req.content,
        memory_type=memory_type,
    )


def to_rebuild_request(req: pb.RebuildRequest) -> RebuildMemoryRequest:
    return RebuildMemoryRequest(
        request_id=req.request_id,
        user_id=req.user_id,
        operation_id=req.operation_id,
        session_id=req.session_id if req.HasField("session_id") else None,
        history_version=req.history_version if req.HasField("history_version") else None,
        rebuild_l2=req.rebuild_l2,
        rebuild_l3=req.rebuild_l3,
    )


# ── response converters ──────────────────────────────────────────────────────


def _managed_item_to_pb(item: ManagedMemoryItem) -> pb.ManagedMemoryItem:
    kwargs: dict[str, Any] = {
        "memory_id": item.memory_id,
        "content": item.content,
        "status": _MEMORY_STATUS_TO_PB[item.status],
        "source_type": _SOURCE_TYPE_TO_PB[item.source_type],
        "data_classification": _DATA_CLASS_TO_PB[item.data_classification],
        "backend_categories": list(item.backend_categories),
    }
    if item.memory_type is not None:
        kwargs["memory_type"] = _MEMORY_TYPE_TO_PB[item.memory_type]
    return pb.ManagedMemoryItem(**kwargs)


def task_response_to_pb(task: TaskResponse) -> pb.TaskResponse:
    kwargs: dict[str, Any] = {
        "task_id": task.task_id,
        "request_id": task.request_id,
        "op_type": _OP_TYPE_TO_PB[task.op_type],
        "status": _TASK_STATUS_TO_PB[task.status],
        "scope": json.dumps(task.scope, default=str),
        "result": json.dumps(task.result, default=str),
    }
    if task.last_error is not None:
        kwargs["last_error"] = task.last_error
    return pb.TaskResponse(**kwargs)


def list_response_to_pb(resp: ListMemoriesResponse) -> pb.ListMemoriesResponse:
    return pb.ListMemoriesResponse(
        status=resp.status,
        items=[_managed_item_to_pb(item) for item in resp.items],
    )


def get_response_to_pb(resp: GetMemoryResponse) -> pb.GetMemoryResponse:
    return pb.GetMemoryResponse(
        status=resp.status,
        memory=_managed_item_to_pb(resp.memory),
    )
