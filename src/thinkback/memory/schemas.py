"""Memory API schemas and architecture enums.

核心数据结构定义，支撑三层记忆架构：
- L1 (短期）：回合日志流，保留最近 N 轮对话
- L2 (中期）：会话摘要，周期性编译为递进式文本摘要
- L3 (长期）：后端向量索引，支持语义检索与跨会话回忆

领域枚举已下沉到 ``thinkback.domain.enums``；本模块 re-export
保持 ``from thinkback.memory.schemas import MemoryStatus`` 兼容。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from thinkback.domain.enums import (
    DataClassification,
    DeleteScope,
    MemoryStatus,
    MemoryType,
    MessageRole,
    OperationType,
    RecallIntent,
    SourceType,
    SummaryState,
    TaskStatus,
)

__all__ = [
    "DataClassification",
    "DeleteScope",
    "MessageRole",
    "MemoryStatus",
    "MemoryType",
    "OperationType",
    "RecallIntent",
    "SourceType",
    "SummaryState",
    "TaskStatus",
]

# PG model 端 id 字段统一为 String(128)；schema 端必须先于 DB 拒绝超长 id，
# 否则客户端 bug 就会直接触发 PG DataError 500。
ID_MAX_LENGTH = 128
IdStr = Annotated[str, StringConstraints(max_length=ID_MAX_LENGTH)]

# AppendMemoryRequest.metadata 软上限：避免超大 JSON 触发 PG JSONB TOAST / 失败。
# 64KB 既能覆盖合理 metadata，也能在请求体层早期失败。
METADATA_MAX_BYTES = 65536


def _validate_metadata_size(value: dict[str, Any]) -> dict[str, Any]:
    """校验 metadata 序列化后大小 <= 64KB。"""
    if value is None:
        return value
    encoded = json.dumps(value, default=str, ensure_ascii=False)
    encoded_bytes = encoded.encode("utf-8")
    if len(encoded_bytes) > METADATA_MAX_BYTES:
        raise ValueError(
            f"metadata must be at most {METADATA_MAX_BYTES} bytes when serialized "
            f"(actual: {len(encoded_bytes)} bytes)"
        )
    return value


class MemoryMessage(BaseModel):
    """单条消息。对话回合中的原子单位"""

    model_config = ConfigDict(populate_by_name=True, use_enum_values=False, extra="forbid")

    message_id: IdStr = Field(description="上游消息 ID，用于定位一轮对话中的单条消息。")
    role: MessageRole = Field(description="消息角色；append 必须按 user -> assistant 顺序提交。")
    content: str = Field(
        description="消息正文；兼容 normalized_content 入参别名，服务端会去除首尾空白。",
        validation_alias=AliasChoices("content", "normalized_content"),
    )
    timestamp: datetime = Field(description="消息产生时间，使用 ISO 8601 时间格式。")

    @field_validator("content", mode="before")
    @classmethod
    def accept_content_alias(cls, value: Any) -> Any:
        """支持 normalized_content 别名入参"""
        return value

    @field_validator("content")
    @classmethod
    def strip_content(cls, value: str) -> str:
        """去除首尾空白字符，确保内容规范化"""
        return value.strip()


class AppendMemoryRequest(BaseModel):
    """追加对话请求。写入流程的入口"""

    model_config = ConfigDict(populate_by_name=True, use_enum_values=False, extra="forbid")

    request_id: IdStr = Field(description="调用方请求 ID，用于链路追踪和幂等排查。")
    user_id: IdStr = Field(description="用户 ID；L1/L2/L3 读写都以该用户维度隔离。")
    session_id: IdStr = Field(description="会话 ID；L1/L2 使用该会话维度维护上下文和摘要。")
    round_id: IdStr = Field(description="对话轮次 ID；同一 round_id 重复提交会做幂等或冲突校验。")
    messages: list[MemoryMessage] = Field(
        description="完整 user -> assistant 两条消息；append 只接受完整轮次。"
    )
    source_timestamp: datetime = Field(description="该轮对话的源事件时间，使用 ISO 8601 格式。")
    round_index: int | None = Field(default=None, description="会话内轮次序号；无序号时可为空。")
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="调用方附加元数据；只用于索引和后端 metadata，不应放入密钥或大对象。",
    )

    @field_validator("user_id", "session_id", "round_id", "request_id")
    @classmethod
    def _reject_empty_or_whitespace(cls, value: str) -> str:
        """防御纵深：阻断空串/纯空白 user_id / session_id / round_id / request_id。

        历史上仅 IdStr.max_length=128 兜底，导致空串也能写入。gRPC / curl / SDK 等
        非 web 入口绕过前端表单校验时，service.append 会创建 round_id='' 的空
        主键记录并触发 idempotency 冲突——早失败更安全。
        """
        if not value or not value.strip():
            raise ValueError("required id field must be non-empty")
        return value

    @field_validator("metadata", mode="before")
    @classmethod
    def enforce_metadata_size(cls, value: Any) -> Any:
        """metadata 序列化大小 <= 64KB，防止 PG JSONB 静默 TOAST/失败。"""
        if value is None:
            return value
        if not isinstance(value, dict):
            return value
        return _validate_metadata_size(value)

    @property
    def scope_key(self) -> str:
        """用户-会话维度的组合键，用于范围查询和隔离"""
        return f"{self.user_id}:{self.session_id}"

    @model_validator(mode="after")
    def validate_complete_round(self) -> AppendMemoryRequest:
        """验证这是完整的对话回合：恰好 user->assistant 两条消息，且都非空"""
        if len(self.messages) != 2:
            raise ValueError("append requires a complete user -> assistant round")
        if (
            self.messages[0].role is not MessageRole.USER
            or self.messages[1].role is not MessageRole.ASSISTANT
        ):
            raise ValueError(
                "append requires messages ordered as a complete user -> assistant round"
            )
        if not all(message.content for message in self.messages):
            raise ValueError("append requires non-empty content for each message in the round")
        return self


class RecallMemoryRequest(BaseModel):
    """召回请求

    支持两种召回意图：
    - ``CHAT``（默认）：常规对话召回，L3 失败可降级为只返回 L1/L2。
    - ``SENSITIVE``：隐私相关场景，采用 fail-closed 策略，错误直接向上抛。
    """

    model_config = ConfigDict(extra="forbid")

    user_id: IdStr = Field(description="用户 ID；召回结果只返回该用户范围内的记忆。")
    session_id: IdStr = Field(description="当前会话 ID；用于召回 L1 和 L2 会话上下文。")
    query: str = Field(description="本次召回查询文本；会传给 L3 后端做语义检索。")
    intent: RecallIntent = Field(
        default=RecallIntent.CHAT,
        description="召回意图；隐私、敏感和删除确认类意图会 fail-closed。",
    )
    l3_limit: int = Field(default=5, ge=0, le=50, description="L3 语义检索最多返回条数。")
    l3_score_threshold: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="L3 语义检索分数阈值，范围 0 到 1。",
    )
    token_budget: int = Field(
        default=1200,
        ge=100,
        le=12000,
        description="返回记忆内容的粗略 token 预算，用于裁剪召回结果。",
    )

    @field_validator("user_id", "session_id")
    @classmethod
    def _reject_empty_or_whitespace(cls, value: str) -> str:
        """防御纵深：阻断空 user_id / session_id（query 允许空，向后端传空查询）。"""
        if not value or not value.strip():
            raise ValueError("required id field must be non-empty")
        return value


class DeleteMemoryRequest(BaseModel):
    """删除请求

    三种 ``scope``：
    - ``MEMORY``：单条删除，必须提供 ``memory_id``，不允许带 ``session_id``。
    - ``SESSION``：单会话删除，必须提供 ``session_id``，不允许带 ``memory_id``。
    - ``ALL``：用户全局删除，``memory_id`` 和 ``session_id`` 都必须为空。

    ``operation_id`` 是删除级幂等键，重复提交会被合并到同一后台任务。
    """

    model_config = ConfigDict(extra="forbid")

    request_id: IdStr = Field(description="调用方请求 ID，用于链路追踪。")
    user_id: IdStr = Field(description="用户 ID；删除操作只能影响该用户范围内的记忆。")
    scope: DeleteScope = Field(description="删除范围：单条记忆、单会话或用户全局。")
    operation_id: IdStr = Field(description="删除操作 ID；重复提交同一 operation_id 会做幂等处理。")
    memory_id: IdStr | None = Field(default=None, description="单条删除时必填的业务记忆 ID。")
    session_id: IdStr | None = Field(default=None, description="会话删除时使用的会话 ID。")

    @field_validator("user_id", "request_id", "operation_id")
    @classmethod
    def _reject_empty_or_whitespace(cls, value: str) -> str:
        """防御纵深：阻断空串/纯空白 user_id / request_id / operation_id。

        admin web govern.tsx 已在 UI 层 trim 后判 length>0（deleteReady 校验），
        但 gRPC、curl、SDK 等非 web 入口可能绕过；这里 server-side 兜底。
        """
        if not value or not value.strip():
            raise ValueError("required id field must be non-empty")
        return value

    @model_validator(mode="after")
    def validate_scope_identifiers(self) -> DeleteMemoryRequest:
        if self.scope is DeleteScope.MEMORY and not self.memory_id:
            raise ValueError("memory_id is required for memory deletion")
        if self.scope is DeleteScope.MEMORY and self.session_id is not None:
            raise ValueError("memory deletion must not include session_id")
        if self.scope is DeleteScope.SESSION and not self.session_id:
            raise ValueError("session_id is required for session deletion")
        if self.scope is DeleteScope.SESSION and self.memory_id is not None:
            raise ValueError("session deletion must not include memory_id")
        if self.scope is DeleteScope.ALL and self.memory_id is not None:
            raise ValueError("all deletion must not include memory_id")
        if self.scope is DeleteScope.ALL and self.session_id is not None:
            raise ValueError("all deletion must not include session_id")
        return self


class UpdateMemoryRequest(BaseModel):
    """编辑请求

    通过 ``operation_id`` 实现幂等；``content`` 必须非空（去除首尾空白后判断）。
    编辑同步写入后端 + 本地索引，并把相关 L1/L2 fail-safe 置脏。
    """

    model_config = ConfigDict(extra="forbid")

    request_id: IdStr = Field(description="调用方请求 ID，用于链路追踪。")
    user_id: IdStr = Field(description="用户 ID；编辑只能影响该用户范围内的长期记忆。")
    operation_id: IdStr = Field(description="编辑操作 ID；重复提交同一 operation_id 会做幂等处理。")
    memory_id: IdStr = Field(description="要编辑的业务记忆 ID，必须来自管理列表或召回结果。")
    content: str = Field(description="编辑后的长期记忆正文；服务端会去除首尾空白。")
    memory_type: MemoryType | None = Field(default=None, description="可选的新记忆分类。")

    @field_validator("user_id", "request_id", "operation_id", "memory_id")
    @classmethod
    def _reject_empty_or_whitespace(cls, value: str) -> str:
        """防御纵深：阻断空串/纯空白 ID。

        admin web govern.tsx 已在 UI 层做 length>0 校验（updateReady），
        但 gRPC / curl / SDK 等非 web 入口可能绕过；这里 server-side 兜底。
        """
        if not value or not value.strip():
            raise ValueError("required id field must be non-empty")
        return value

    @field_validator("content")
    @classmethod
    def strip_and_validate_content(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("memory update requires non-empty content")
        return stripped


class RebuildMemoryRequest(BaseModel):
    """重建请求

    用于从回合流水重新生成 L2 摘要和/或 L3 索引。可通过 ``history_version``
    与外部历史源做一致性校验，避免读到陈旧历史。
    """

    model_config = ConfigDict(extra="forbid")

    request_id: IdStr = Field(description="调用方请求 ID，用于链路追踪。")
    user_id: IdStr = Field(description="用户 ID；重建只处理该用户范围内的记忆。")
    operation_id: IdStr = Field(description="重建操作 ID；重复提交同一 operation_id 会做幂等处理。")
    session_id: IdStr | None = Field(
        default=None, description="指定会话重建；为空时按用户范围重建。"
    )
    history_version: str | None = Field(
        default=None,
        description="外部历史源版本；提供时会与当前历史版本校验一致性。",
    )
    rebuild_l2: bool = Field(default=True, description="是否重建 L2 阶段摘要。")
    rebuild_l3: bool = Field(default=True, description="是否重建 L3 长期记忆索引。")

    @field_validator("user_id", "request_id", "operation_id")
    @classmethod
    def _reject_empty_or_whitespace(cls, value: str) -> str:
        """防御纵深：阻断空串/纯空白 user_id / request_id / operation_id。

        前端 govern.tsx 已经在 UI 层做了 trim+length>0 校验（rebuildReady / deleteReady），
        但 admin web 之外的入口（gRPC、curl、SDK）依然可能发空串进来。
        这里再补一刀避免 backend 静默接受空 user_id 触发空范围重建任务。
        """
        if not value or not value.strip():
            raise ValueError(
                f"{cls.__name__.replace('MemoryRequest', '').lower()} field must be non-empty"
            )
        return value


class MemoryItem(BaseModel):
    """单条召回记忆项

    ``layer`` 表示来源层级（L1/L2/L3），``content`` 为脱敏后正文。
    L3 召回额外提供 ``memory_id`` 与 ``score``，用于调用方做来源回溯和置信度筛选。
    ``recall_count`` / ``last_recalled_at`` 仅 L3 有值，用于 S1 强度排序
    (recency × frequency)。
    """

    layer: str = Field(description="记忆来源层级：L1、L2 或 L3。")
    content: str = Field(description="召回给上游模型使用的记忆文本。")
    source: str = Field(
        default="",
        max_length=ID_MAX_LENGTH,
        description="记忆来源标识，例如 round_id、summary_id 或 mem0。",
    )
    memory_id: IdStr | None = Field(
        default=None, description="L3 记忆的业务记忆 ID；非 L3 可为空。"
    )
    score: float | None = Field(default=None, description="L3 语义检索分数；非 L3 可为空。")
    metadata: dict[str, Any] = Field(default_factory=dict, description="召回项附加元数据。")
    recall_count: int = Field(
        default=0,
        ge=0,
        description="累计召回次数；非 L3 为 0。S1 强度排序 (recency × frequency) 输入。",
    )
    last_recalled_at: datetime | None = Field(
        default=None,
        description="最近一次召回时间；非 L3 为 None。S1 强度排序 (recency × frequency) 输入。",
    )


class ManagedMemoryItem(BaseModel):
    """可管理的长期记忆详情

    用于 ``/items``、``/items/{id}`` 与 ``/update`` 的返回结构，
    暴露给客户端做"我的记忆"列表查看与人工编辑。
    """

    memory_id: IdStr = Field(description="L3 业务记忆 ID，可用于编辑或删除。")
    content: str = Field(description="当前长期记忆正文。")
    status: MemoryStatus = Field(description="业务索引状态；只有 ACTIVE 会进入召回。")
    source_type: SourceType = Field(description="记忆来源类型。")
    data_classification: DataClassification = Field(description="数据敏感等级。")
    memory_type: MemoryType | None = Field(default=None, description="记忆分类；未知时为空。")
    backend_categories: list[str] = Field(
        default_factory=list,
        description="后端返回或调用方提供的分类标签。",
    )


class AppendMemoryResponse(BaseModel):
    """写入响应

    同步模式下 ``status`` 反映 L3 真实落盘结果（completed / already_done）；
    异步模式下 ``status`` 始终为 completed，由后台写入线程异步处理失败。
    """

    status: str = Field(description="写入结果状态，例如 completed 或 already_done。")
    task_id: str = Field(description="对应的记忆写入任务 ID。")
    round_id: str = Field(description="本次写入的对话轮次 ID。")
    l3_events: list[dict[str, Any]] = Field(default_factory=list, description="L3 写入事件摘要。")


class RecallMemoryResponse(BaseModel):
    """召回响应

    ``degraded=True`` 时表示发生了降级（典型场景：L3 后端不可用，只返回 L1/L2）；
    ``degradation_reasons`` 给出降级原因供调用方排查。
    """

    status: str = Field(description="召回结果状态。")
    degraded: bool = Field(default=False, description="是否发生降级召回。")
    degradation_reasons: list[str] = Field(default_factory=list, description="降级原因列表。")
    items: list[MemoryItem] = Field(default_factory=list, description="召回到的记忆条目。")


class DeleteMemoryResponse(BaseModel):
    """删除响应

    ``status`` 取值：
    - ``completed``：本次删除已成功完成。
    - ``running``：删除任务仍在后台运行（异步或重试路径）。
    - ``already_done``：同 ``operation_id`` 之前已经完成，复用结果。
    - ``failed``：重试预算耗尽（dead_letter），客户端应停止重试。
    """

    status: str = Field(description="删除结果状态，例如 completed、running 或 already_done。")
    task_id: str = Field(description="对应的删除任务 ID。")
    affected_memories: int = Field(default=0, description="本次删除影响的记忆数量。")
    summary_state: SummaryState | None = Field(default=None, description="删除后的摘要状态。")


class ListMemoriesResponse(BaseModel):
    """记忆管理列表响应

    返回用户在 L3 范围内的所有可管理长期记忆（含 ACTIVE 之外的状态），
    系统级墓碑（delete-all / delete-session / deleted-source）会被服务端过滤掉。
    """

    status: str = Field(description="查询结果状态。")
    items: list[ManagedMemoryItem] = Field(default_factory=list, description="可管理长期记忆列表。")


class GetMemoryResponse(BaseModel):
    """单条记忆详情响应"""

    status: str = Field(description="查询结果状态。")
    memory: ManagedMemoryItem = Field(description="可管理长期记忆详情。")


class UpdateMemoryResponse(BaseModel):
    """编辑响应

    返回编辑后的记忆详情，并将相关 L2 摘要标记为 ``DIRTY`` 以触发下一次重建。
    """

    status: str = Field(description="编辑结果状态，例如 completed、running 或 already_done。")
    task_id: str = Field(description="对应的编辑任务 ID。")
    memory: ManagedMemoryItem = Field(description="编辑后的长期记忆详情。")
    summary_state: SummaryState | None = Field(default=None, description="编辑后的摘要状态。")


class RebuildMemoryResponse(BaseModel):
    """重建响应

    ``rebuilt_l2`` / ``rebuilt_l3`` 反映请求参数中实际触发的层级；
    当任务处于 running 状态时返回请求参数值，完成后再返回真实结果。
    """

    status: str = Field(description="重建结果状态，例如 completed、running 或 already_done。")
    task_id: str = Field(description="对应的重建任务 ID。")
    rebuilt_l2: bool = Field(description="本次请求是否重建了 L2。")
    rebuilt_l3: bool = Field(description="本次请求是否重建了 L3。")


class TaskResponse(BaseModel):
    """后台任务查询响应

    适用于写、删、改、重建等长生命周期任务的查询；
    ``result`` 中包含任务特有的结果字段（例如 affected_count、l3_extract_task_ids）。
    """

    task_id: str = Field(description="后台任务 ID。")
    request_id: str = Field(description="创建该任务的调用方请求 ID。")
    op_type: OperationType = Field(description="任务操作类型。")
    status: TaskStatus = Field(description="任务当前状态。")
    scope: dict[str, Any] = Field(description="任务作用域摘要，例如 user_id、session_id。")
    last_error: str | None = Field(default=None, description="任务失败时记录的错误信息。")
    result: dict[str, Any] = Field(default_factory=dict, description="任务完成后的结果摘要。")
    retry_count: int = Field(default=0, description="重试次数（治理台 retry 色阶用）。")


class AdminMemoryItem(BaseModel):
    """治理台记忆检索条目（本地索引视图，只读）。"""

    memory_id: str = Field(description="本地业务记忆 ID。")
    backend_memory_id: str = Field(description="L3 后端（mem0）记忆 ID 反链。")
    user_id: str = Field(description="用户维度。")
    memory_scope_id: str = Field(description="范围维度。")
    memory_text: str = Field(description="记忆文本。")
    memory_status: MemoryStatus = Field(description="记忆状态。")
    memory_type: str | None = Field(default=None, description="记忆分类。")
    data_classification: str = Field(default="normal", description="敏感等级。")
    source_refs: list[dict[str, str]] = Field(default_factory=list, description="来源回合引用。")
    valid_at: datetime | None = Field(default=None, description="事实生效时刻（双时态）。")
    invalid_at: datetime | None = Field(default=None, description="事实失效时刻（双时态）。")
    last_recalled_at: datetime | None = Field(
        default=None, description="最近召回时刻（None=从未召回）。"
    )
    recall_count: int = Field(default=0, description="累计召回次数。")
    conflict_slot: str | None = Field(
        default=None, description="冲突槽位（由记忆文本派生，治理台展示用）。"
    )


class L3BackgroundStatusResponse(BaseModel):
    """L3 后台队列状态响应

    用于运维/监控端点查看 L3 写入线程池、待处理任务数和剩余容量，
    方便判断是否需要扩容或是否被 mem0 拖慢。
    """

    write_mode: str = Field(description="L3 写入模式，例如 async 或 sync。")
    executor_workers: int = Field(description="L3 后台执行器线程数。")
    max_pending_tasks: int = Field(description="L3 后台写入队列最大容量。")
    pending_write_tasks: int = Field(description="当前待完成的 L3 后台写入任务数。")
    cleanup_tasks: int = Field(description="当前待完成的 L3 后台清理任务数。")
    available_capacity: int = Field(description="L3 后台写入队列剩余容量。")
