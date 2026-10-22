"""Proto ↔ pydantic schema conversion helpers.

集中放置 protobuf 与 pydantic schema 之间的转换函数，目的是：
- 把 enum 映射放在文件顶部，避免散落各处；
- 让 servicer 层只关心业务编排，不再纠缠数据类型转换；
- 单元测试可以脱离 gRPC 直接覆盖转换函数。
"""

from __future__ import annotations

import json
from typing import Any

from thinkback.domain.tenant import scoped_user_id
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
    RebuildMemoryResponse,
    RecallMemoryRequest,
    SourceType,
    SummaryState,
    TaskResponse,
    TaskStatus,
    UpdateMemoryRequest,
)
from thinkback.rpc import memory_pb2 as pb

# ── gRPC ↔ HTTP 的取值口径（必须与 memory/schemas.py 的 RecallMemoryRequest 一致）──
#
# proto3 的标量字段没有 presence：分不清「客户端没传」与「客户端传了 0」。
# 约定：**0 / 0.0 一律按「没传」处理**，取 HTTP 侧的默认值；越界值直接报错
# （HTTP 侧是 422，gRPC 侧是 INVALID_ARGUMENT），不静默夹取 —— 静默夹取会让
# 调用方以为自己拿到了 50 条，实际拿到的是被悄悄改过的值。
L3_LIMIT_DEFAULT = 5
L3_LIMIT_MAX = 50
L3_SCORE_THRESHOLD_DEFAULT = 0.5
TOKEN_BUDGET_DEFAULT = 1200
TOKEN_BUDGET_MIN = 100
TOKEN_BUDGET_MAX = 12000


def resolve_l3_limit(value: int) -> int:
    """gRPC 的 l3_limit → HTTP 同口径（默认 5、上限 50）。"""

    if value == 0:
        return L3_LIMIT_DEFAULT
    if value < 0 or value > L3_LIMIT_MAX:
        raise ValueError(f"l3_limit must be between 0 and {L3_LIMIT_MAX}, got {value}")
    return value


def resolve_l3_score_threshold(value: float) -> float:
    """gRPC 的 l3_score_threshold → HTTP 同口径（默认 0.5、范围 0-1）。

    原实现直接透传 proto 的 0.0 默认值，等于「不过滤」—— HTTP 侧是 0.5，
    两边对同一条记忆的召回结果不同。
    """

    if value == 0.0:
        return L3_SCORE_THRESHOLD_DEFAULT
    if value < 0.0 or value > 1.0:
        raise ValueError(f"l3_score_threshold must be between 0.0 and 1.0, got {value}")
    return value


def resolve_token_budget(value: int) -> int:
    """gRPC 的 token_budget → HTTP 同口径（默认 1200、范围 100-12000）。"""

    if value == 0:
        return TOKEN_BUDGET_DEFAULT
    if value < TOKEN_BUDGET_MIN or value > TOKEN_BUDGET_MAX:
        raise ValueError(
            f"token_budget must be between {TOKEN_BUDGET_MIN} and {TOKEN_BUDGET_MAX}, got {value}"
        )
    return value


# ── enum maps (proto → domain) ──────────────────────────────────────────────

_ROLE = {
    pb.MESSAGE_ROLE_USER: "user",
    pb.MESSAGE_ROLE_ASSISTANT: "assistant",
    pb.MESSAGE_ROLE_SYSTEM: "system",
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


def to_append_request(req: pb.AppendRequest, *, tenant_id: str = "") -> AppendMemoryRequest:
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
        user_id=scoped_user_id(tenant_id, req.user_id),
        session_id=req.session_id,
        round_id=req.round_id,
        messages=messages,
        source_timestamp=datetime.fromisoformat(req.source_timestamp),
        round_index=req.round_index if req.HasField("round_index") else None,
        metadata=dict(req.metadata),
    )


def to_recall_request(req: pb.RecallRequest, *, tenant_id: str = "") -> RecallMemoryRequest:
    if req.intent == pb.RECALL_INTENT_SENSITIVE:
        # 域模型的 intent 轴已删除（枚举只有两个值，SENSITIVE 无条件 raise，
        # 是 fail-useless 不是 fail-closed）。proto 里的枚举值还在（生成代码
        # 不重新生成），这里显式拒绝，保持对外行为不变：PERMISSION_DENIED。
        raise ValueError("fail-closed recall intent: sensitive is not supported")
    return RecallMemoryRequest(
        user_id=scoped_user_id(tenant_id, req.user_id),
        session_id=req.session_id,
        query=req.query,
        l3_limit=resolve_l3_limit(req.l3_limit),
        l3_score_threshold=resolve_l3_score_threshold(req.l3_score_threshold),
        token_budget=resolve_token_budget(req.token_budget),
    )


def to_delete_request(req: pb.DeleteRequest, *, tenant_id: str = "") -> DeleteMemoryRequest:
    return DeleteMemoryRequest(
        request_id=req.request_id,
        user_id=scoped_user_id(tenant_id, req.user_id),
        scope=_DELETE_SCOPE[req.scope],
        operation_id=req.operation_id,
        memory_id=req.memory_id if req.HasField("memory_id") else None,
        session_id=req.session_id if req.HasField("session_id") else None,
    )


def to_update_request(req: pb.UpdateMemoryRequest, *, tenant_id: str = "") -> UpdateMemoryRequest:
    memory_type = _MEMORY_TYPE_FROM_PB.get(req.memory_type) if req.HasField("memory_type") else None
    return UpdateMemoryRequest(
        request_id=req.request_id,
        user_id=scoped_user_id(tenant_id, req.user_id),
        operation_id=req.operation_id,
        memory_id=req.memory_id,
        content=req.content,
        memory_type=memory_type,
    )


def to_rebuild_request(req: pb.RebuildRequest, *, tenant_id: str = "") -> RebuildMemoryRequest:
    return RebuildMemoryRequest(
        request_id=req.request_id,
        user_id=scoped_user_id(tenant_id, req.user_id),
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


def rebuild_response_to_pb(resp: RebuildMemoryResponse) -> pb.RebuildResponse:
    """RebuildMemoryRequest → RebuildResponse（与 proto 字段一一对应）。"""

    return pb.RebuildResponse(
        status=resp.status,
        task_id=resp.task_id,
        rebuilt_l2=resp.rebuilt_l2,
        rebuilt_l3=resp.rebuilt_l3,
    )
