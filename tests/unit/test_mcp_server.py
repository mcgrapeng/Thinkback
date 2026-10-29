"""MCP 接入层的契约测试。

走真实的 MCP 协议路径（内存传输 + 客户端 session），不是直接调函数：
工具清单、参数 schema、结构化返回都要真的过一遍客户端才算数。

断言覆盖三件事：
1. 工具面克制且带 annotations（客户端靠它决定能不能自动执行）；
2. namespace 由服务端决定，工具参数改不了；
3. 写 / 读 / 删 / 查状态这条链路端到端通。
"""

from __future__ import annotations

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from thinkback.mcp.server import create_mcp_server
from thinkback.memory.backends import FakeMemoryBackend
from thinkback.memory.repositories import InMemoryMemoryRepository
from thinkback.memory.service import MemoryService


def _service() -> MemoryService:
    return MemoryService(
        repository=InMemoryMemoryRepository(),
        backend=FakeMemoryBackend(),
        l3_write_mode="sync",
    )


@pytest.fixture
def server():
    return create_mcp_server(_service(), namespace="semiconductor")


async def test_tool_surface_is_minimal_and_annotated(server) -> None:
    """工具面固定为 5 个，且每个都声明了 readOnly / destructive 提示。"""
    async with create_connected_server_and_client_session(server) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}

    assert set(tools) == {
        "append_memory",
        "recall_memories",
        "list_memories",
        "delete_memory",
        "get_operation",
    }
    assert tools["recall_memories"].annotations is not None
    assert tools["recall_memories"].annotations.readOnlyHint is True
    assert tools["delete_memory"].annotations is not None
    assert tools["delete_memory"].annotations.destructiveHint is True
    assert tools["append_memory"].annotations is not None
    assert tools["append_memory"].annotations.idempotentHint is True


async def test_no_tool_exposes_namespace(server) -> None:
    """namespace 是服务端决定的：调用方换不了租户。

    任何暴露 namespace 的工具参数，都等于让助手侧任意切换租户。
    """
    async with create_connected_server_and_client_session(server) as client:
        tools = (await client.list_tools()).tools

    for tool in tools:
        assert "namespace" not in (tool.inputSchema.get("properties") or {})


async def test_append_recall_delete_roundtrip(server) -> None:
    """写入 → 召回 → 列出 → 删除 → 不可再召回。"""
    async with create_connected_server_and_client_session(server) as client:
        appended = await client.call_tool(
            "append_memory",
            {
                "user_id": "user-1",
                "session_id": "session-1",
                "payload": "我负责 3nm 工艺整合",
                "event_id": "evt-mcp-1",
            },
        )
        assert not appended.isError

        recalled = await client.call_tool(
            "recall_memories",
            {
                "user_id": "user-1",
                "session_id": "session-1",
                "query": "工艺整合",
            },
        )
        assert not recalled.isError
        assert "3nm" in recalled.content[0].text

        listed = await client.call_tool("list_memories", {"user_id": "user-1"})
        assert not listed.isError
        assert "3nm" in listed.content[0].text

        memory_id = _first_memory_id(listed.content[0].text)
        deleted = await client.call_tool(
            "delete_memory",
            {"user_id": "user-1", "memory_id": memory_id, "operation_id": "op-mcp-1"},
        )
        assert not deleted.isError

        after = await client.call_tool("list_memories", {"user_id": "user-1"})
        assert memory_id not in after.content[0].text


async def test_repeated_append_with_same_event_id_is_idempotent(server) -> None:
    """同一 event_id 重放两次不会写出两条记忆。"""
    async with create_connected_server_and_client_session(server) as client:
        for _ in range(2):
            result = await client.call_tool(
                "append_memory",
                {
                    "user_id": "user-1",
                    "session_id": "session-1",
                    "payload": "我常用 EDA 工具是 Innovus",
                    "event_id": "evt-mcp-idem",
                },
            )
            assert not result.isError

        listed = await client.call_tool("list_memories", {"user_id": "user-1"})
        assert listed.content[0].text.count("Innovus") == 1


async def test_get_operation_reports_unknown_task(server) -> None:
    """查不到的任务如实报 not_found，不谎报成功。"""
    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool("get_operation", {"task_id": "memory-extract:nope"})

    assert not result.isError
    assert "not_found" in result.content[0].text


async def test_namespace_isolation_between_servers() -> None:
    """两个 namespace 互不可见：namespace 由服务端决定。"""
    alpha = create_mcp_server(_service(), namespace="alpha")
    beta = create_mcp_server(_service(), namespace="beta")

    async with create_connected_server_and_client_session(alpha) as client:
        await client.call_tool(
            "append_memory",
            {
                "user_id": "user-1",
                "session_id": "session-1",
                "payload": "alpha 的独有事实",
                "event_id": "evt-alpha-1",
            },
        )

    async with create_connected_server_and_client_session(beta) as client:
        listed = await client.call_tool("list_memories", {"user_id": "user-1"})
        recalled = await client.call_tool(
            "recall_memories",
            {"user_id": "user-1", "session_id": "session-1", "query": "alpha"},
        )

    assert "alpha 的独有事实" not in listed.content[0].text
    assert "alpha 的独有事实" not in recalled.content[0].text


def _first_memory_id(listed_text: str) -> str:
    """从 list_memories 的返回里取第一条记忆 ID。"""
    import json

    payload = json.loads(listed_text)
    items = payload.get("items") or []
    assert items, f"expected at least one memory item, got {listed_text!r}"
    return str(items[0]["memory_id"])
