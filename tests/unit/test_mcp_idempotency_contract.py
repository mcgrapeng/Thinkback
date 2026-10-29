"""MCP 写工具的幂等契约：声明与行为必须一致。

``append_memory`` 对外声明 ``idempotentHint=True`` —— 助手侧会据此决定
「重试这个调用是安全的」。但幂等锚点 ``event_id`` 之前是**可选**的：
不传就现场造一个 UUID，于是每次重试都写成一条新记忆。

声明与行为对不上有两种坏后果：
- 助手按「可安全重试」用 → 重复写入，用户看到同一条事实记了好几遍；
- 助手按「不安全」用 → 自己加去重逻辑，绕过了服务本该提供的保证。

所以 ``event_id`` 必须是必填（ADR-0005 / ADR-0008：锚点由调用方生成、全局唯一）。
"""

from __future__ import annotations

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from thinkback.mcp.server import create_mcp_server
from thinkback.memory.backends import FakeMemoryBackend
from thinkback.memory.repositories import InMemoryMemoryRepository
from thinkback.memory.service import MemoryService


@pytest.fixture
def server():
    service = MemoryService(
        repository=InMemoryMemoryRepository(),
        backend=FakeMemoryBackend(),
        l3_write_mode="sync",
    )
    return create_mcp_server(service, namespace="semiconductor")


async def test_append_memory_schema_requires_event_id(server) -> None:
    """工具 schema 必须把 event_id 标成必填 —— 客户端据此生成 UUID。"""
    async with create_connected_server_and_client_session(server) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}

    schema = tools["append_memory"].inputSchema
    assert "event_id" in schema.get("required", []), (
        f"event_id 是幂等锚点，必须是必填项；当前 schema：{schema.get('required')}"
    )


async def test_append_memory_without_event_id_is_rejected(server) -> None:
    """不传 event_id 要被挡住，而不是悄悄造一个 UUID 写进去。"""
    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool(
            "append_memory",
            {
                "user_id": "user-1",
                "session_id": "session-1",
                "payload": "我负责 3nm 工艺整合",
            },
        )

    assert result.isError, "缺幂等锚点的写必须报错，不能静默变成新事件"
    assert "event_id" in result.content[0].text
