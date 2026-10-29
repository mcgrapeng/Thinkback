"""MCP 接入层：把 Thinkback 的记忆能力投影成 MCP 工具。

薄适配层：工具直接调用 ``MemoryService``，不重写业务逻辑 —— 语义、校验、
幂等、预算裁剪、擦除口径与 ``/v2/memory/*`` 完全一致（同一个服务实例）。

工具面刻意克制：读 2（召回 / 列表）、写 1（追加）、删 1（删除）、状态 1
（查任务）。MCP 客户端靠工具清单做发现，工具一多就没人看得完 —— Cognee
只 pin 3 个是最好参照。

namespace 由**服务端**决定，不作为工具参数暴露：namespace 是租户/业务分组
的边界，让调用方自己传等于任何助手都能换租户。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from thinkback.api.memory import _run_read_memory_call, _run_write_memory_call
from thinkback.domain.keys import DEFAULT_NAMESPACE
from thinkback.memory.schemas import (
    AppendEventRequest,
    AppendMemoryResponse,
    DeleteMemoryRequest,
    DeleteMemoryResponse,
    DeleteScope,
    ListMemoriesResponse,
    MessageRole,
    RecallMemoryRequest,
    RecallMemoryResponse,
    SourceEvent,
    TaskResponse,
)
from thinkback.memory.service import MemoryService

__all__ = ["create_mcp_server"]


def _parse_datetime(value: str | None, *, field: str) -> datetime:
    if value is None:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO 8601 datetime") from exc


def create_mcp_server(service: MemoryService, namespace: str = DEFAULT_NAMESPACE) -> FastMCP:
    """构造绑定到某个 namespace 的 MCP 服务器。

    一个服务器实例 = 一个 namespace。部署方决定挂哪个 namespace，助手
    侧无法切换。
    """
    server = FastMCP("thinkback-memory")

    @server.tool(
        name="append_memory",
        title="写入一条记忆事件",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True),
    )
    async def append_memory(
        user_id: Annotated[str, Field(description="用户 ID；记忆读写都以该用户维度隔离。")],
        session_id: Annotated[str, Field(description="会话 ID；L1/L2 上下文与摘要按会话维护。")],
        payload: Annotated[str, Field(description="事件正文：用户说了什么，或工具返回了什么。")],
        role: Annotated[
            str,
            Field(description="事件角色：user / assistant / tool / system。"),
        ] = "user",
        event_id: Annotated[
            str | None,
            Field(description="幂等锚点，必须全局唯一。同一事件重试请传同一个值。"),
        ] = None,
        memory_type: Annotated[
            str | None,
            Field(description="记忆类型名；必须是本 namespace 已注册的类型，未注册直接报错。"),
        ] = None,
        occurred_at: Annotated[
            str | None,
            Field(description="事件发生时间（ISO 8601）；缺省按当前时间。"),
        ] = None,
        source: Annotated[
            str,
            Field(description="事件来源标识，如 mcp / chat_round / tool_result。"),
        ] = "mcp",
    ) -> AppendMemoryResponse:
        """写入一条原始事件，触发 L1/L2 更新与 L3 沉淀。

        返回的是**后台任务**句柄：写成功不等于全链路成功，用 get_operation
        查最终状态。
        """
        request = AppendEventRequest(
            request_id=f"mcp-{uuid.uuid4()}",
            user_id=user_id,
            session_id=session_id,
            event=SourceEvent(
                event_id=event_id or f"mcp-{uuid.uuid4()}",
                source=source,
                role=MessageRole(role),
                payload=payload,
                occurred_at=_parse_datetime(occurred_at, field="occurred_at"),
            ),
            namespace=namespace,
            memory_type=memory_type,
        )
        return await _run_write_memory_call(service.append, request.to_internal())

    @server.tool(
        name="recall_memories",
        title="召回用户相关记忆",
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
    )
    async def recall_memories(
        user_id: Annotated[str, Field(description="用户 ID；只返回该用户范围内的记忆。")],
        session_id: Annotated[str, Field(description="当前会话 ID；用于召回 L1 与 L2 上下文。")],
        query: Annotated[str, Field(description="召回查询文本；会传给后端做语义检索。")],
        token_budget: Annotated[
            int,
            Field(description="返回记忆内容的 token 预算（100~12000）。", ge=100, le=12000),
        ] = 1200,
        l3_limit: Annotated[
            int, Field(description="L3 语义检索最多返回条数（0~50）。", ge=0, le=50)
        ] = 5,
        l3_score_threshold: Annotated[
            float,
            Field(description="L3 语义检索分数阈值（0~1）。", ge=0.0, le=1.0),
        ] = 0.5,
    ) -> RecallMemoryResponse:
        """按 user、session 与 query 召回 L1/L2/L3 记忆。

        返回带降级状态与降级原因：召回为空时也要看 degradation 字段，
        区分「没有记忆」与「记忆还没写进去」。
        """
        request = RecallMemoryRequest(
            user_id=user_id,
            session_id=session_id,
            query=query,
            token_budget=token_budget,
            l3_limit=l3_limit,
            l3_score_threshold=l3_score_threshold,
            namespace=namespace,
        )
        return await _run_read_memory_call(service.recall, request)

    @server.tool(
        name="list_memories",
        title="列出长期记忆",
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
    )
    async def list_memories(
        user_id: Annotated[str, Field(description="用户 ID；只返回该用户范围内的长期记忆。")],
        include_deleted: Annotated[
            bool, Field(description="是否包含已删除或已取代的记忆。")
        ] = False,
    ) -> ListMemoriesResponse:
        """列出可管理的长期记忆（默认只返回 ACTIVE）。"""
        return await _run_read_memory_call(
            service.list_memory_items,
            user_id=user_id,
            include_deleted=include_deleted,
            namespace=namespace,
        )

    @server.tool(
        name="delete_memory",
        title="删除一条长期记忆",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True),
    )
    async def delete_memory(
        user_id: Annotated[str, Field(description="用户 ID；只能影响该用户范围内的记忆。")],
        memory_id: Annotated[str, Field(description="要删除的记忆 ID（先 list_memories 取）。")],
        operation_id: Annotated[
            str | None,
            Field(description="删除级幂等键；重复提交同一 operation_id 会合并到同一后台任务。"),
        ] = None,
    ) -> DeleteMemoryResponse:
        """删除一条长期记忆（不可召回，保留原文与墓碑）。

        这是**逻辑删除**。要物理擦除各层原文，需要走管理面 /v2/memory/erase。
        """
        request = DeleteMemoryRequest(
            request_id=f"mcp-{uuid.uuid4()}",
            user_id=user_id,
            scope=DeleteScope.MEMORY,
            operation_id=operation_id or f"mcp-{uuid.uuid4()}",
            memory_id=memory_id,
            namespace=namespace,
        )
        return await _run_write_memory_call(service.delete, request)

    @server.tool(
        name="get_operation",
        title="查询后台任务状态",
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
    )
    async def get_operation(
        task_id: Annotated[str, Field(description="任务 ID，来自 append_memory / delete_memory。")],
    ) -> TaskResponse | dict[str, Any]:
        """查询记忆后台任务的最终状态。

        写工具返回的是任务句柄：只有这里能确认「真的写进去了」。
        """
        task = await _run_read_memory_call(service.get_task, task_id)
        if task is None:
            return {"status": "not_found", "task_id": task_id}
        return task

    return server
