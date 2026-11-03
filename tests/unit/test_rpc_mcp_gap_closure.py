"""RPC / MCP 接入面此前无测试触达的行。

覆盖目标：
- ``rpc.servicer``：Suppress / Erase / ListMemoryTypes 三个 RPC 的异常路径，
  以及「乐观锁重试耗尽 → ABORTED」这条状态映射。
- ``rpc.server.serve``：启动时的 pending write 恢复（失败不挡服务、成功要记账）。
- ``mcp.server``：``occurred_at`` 非法时间戳的报错、``list_memory_types`` 工具、
  ``get_operation`` 查到真实任务时的返回。

不碰网络：gRPC 走进程内 servicer 直调，MCP 走内存传输 + 客户端 session。
"""

from __future__ import annotations

import signal
from typing import Any, cast

import grpc
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from thinkback.mcp.server import create_mcp_server
from thinkback.memory.backends import FakeMemoryBackend
from thinkback.memory.repositories import InMemoryMemoryRepository
from thinkback.memory.service import MemoryService
from thinkback.rpc import memory_pb2 as pb
from thinkback.rpc.servicer import MemoryServicer, _handle_error

# ── gRPC：错误路径与状态映射 ──────────────────────────────────────────


class _RecordingContext:
    """最小 ServicerContext：只记 abort 的 code 与 detail。"""

    def __init__(self, metadata: Any = ()) -> None:
        self.code: grpc.StatusCode | None = None
        self.detail: str | None = None
        self._metadata = metadata

    def abort(self, code: grpc.StatusCode, details: str) -> None:
        self.code = code
        self.detail = details

    def invocation_metadata(self) -> Any:
        return self._metadata


class _ExplodingService:
    """每个被调用的方法都抛错，用于覆盖各 RPC 的 except 路径。"""

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        def _explode(*args: object, **kwargs: object) -> None:
            raise ValueError(f"boom in {name}")

        return _explode


@pytest.mark.parametrize(
    "rpc_call,pb_request",
    [
        (
            "Suppress",
            pb.SuppressRequest(
                request_id="req-s",
                user_id="u1",
                scope=pb.DELETE_SCOPE_MEMORY,
                operation_id="op-s",
                memory_id="m-1",
            ),
        ),
        (
            "Erase",
            pb.EraseRequest(
                request_id="req-e",
                user_id="u1",
                scope=pb.DELETE_SCOPE_MEMORY,
                operation_id="op-e",
                memory_id="m-1",
            ),
        ),
        ("ListMemoryTypes", pb.ListMemoryTypesRequest()),
    ],
)
def test_servicer_error_paths_abort_then_unreachable(rpc_call: str, pb_request: object) -> None:
    """Suppress / Erase / ListMemoryTypes 的业务异常也必须先 abort 再走 unreachable。"""
    servicer = MemoryServicer(_ExplodingService())  # type: ignore[arg-type]
    context = _RecordingContext(metadata=())
    method = getattr(servicer, rpc_call)

    with pytest.raises(RuntimeError, match="unreachable"):
        method(pb_request, cast("grpc.ServicerContext", context))
    assert context.code is grpc.StatusCode.INVALID_ARGUMENT
    assert "boom" in (context.detail or "")


def test_optimistic_lock_contention_exhausted_is_aborted() -> None:
    """乐观锁重试耗尽 = 终态写没落库：调用方必须看到失败并重试，不能当成功。"""
    context = _RecordingContext()

    _handle_error(
        RuntimeError("optimistic lock contention exhausted"),
        cast("grpc.ServicerContext", context),
    )

    assert context.code is grpc.StatusCode.ABORTED


# ── gRPC server：启动时的 pending write 恢复 ──────────────────────────


class _FakeServer:
    """只记录生命周期调用，不真的监听端口。"""

    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self, grace: float) -> None:
        self.stopped = True

    def wait_for_termination(self) -> None:
        return None


def _serve_with(monkeypatch: pytest.MonkeyPatch, service: Any) -> _FakeServer:
    """跑一遍 serve() 的启动路径：替身 server + 屏蔽 signal 注册。"""
    from thinkback.rpc import server as server_module

    fake = _FakeServer()
    monkeypatch.setattr(server_module, "create_server", lambda *a, **k: (fake, "127.0.0.1:0"))
    monkeypatch.setattr(signal, "signal", lambda *a, **k: None)
    server_module.serve(service)
    return fake


def test_serve_still_starts_when_pending_write_recovery_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """恢复失败不该挡住服务启动（否则一次 DB 抖动就让整个进程起不来）。"""
    import threading

    done = threading.Event()

    class _FailingService:
        def recover_pending_writes(self) -> int:
            done.set()
            raise RuntimeError("db down")

    fake = _serve_with(monkeypatch, _FailingService())
    assert done.wait(timeout=5)
    assert fake.started is True


def test_serve_starts_and_reports_recovered_pending_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    """恢复有结果时照常启动（记账在日志里，不影响启动流程）。"""
    import threading

    done = threading.Event()

    class _RecoveringService:
        def recover_pending_writes(self) -> int:
            done.set()
            return 3

    fake = _serve_with(monkeypatch, _RecoveringService())
    assert done.wait(timeout=5)
    assert fake.started is True


# ── MCP ───────────────────────────────────────────────────────────────


def _service() -> MemoryService:
    return MemoryService(
        repository=InMemoryMemoryRepository(),
        backend=FakeMemoryBackend(),
        l3_write_mode="sync",
    )


@pytest.fixture
def mcp_server():
    return create_mcp_server(_service(), namespace="semiconductor")


async def test_append_rejects_a_malformed_occurred_at(mcp_server) -> None:
    """非法时间戳要指名道姓地报错，而不是静默当成「现在」。"""
    async with create_connected_server_and_client_session(mcp_server) as client:
        result = await client.call_tool(
            "append_memory",
            {
                "user_id": "user-1",
                "session_id": "session-1",
                "payload": "时间戳非法的事件",
                "event_id": "evt-mcp-bad-time",
                "occurred_at": "not-a-datetime",
            },
        )

    assert result.isError
    assert "occurred_at must be an ISO 8601 datetime" in result.content[0].text


async def test_list_memory_types_returns_the_registry(mcp_server) -> None:
    """类型清单工具要真的返回本 namespace 的注册表内容。"""
    async with create_connected_server_and_client_session(mcp_server) as client:
        result = await client.call_tool("list_memory_types", {})

    assert not result.isError
    assert "profile" in result.content[0].text


async def test_get_operation_returns_the_real_task(mcp_server) -> None:
    """查到真实任务时如实返回任务句柄（不只是「查无此任务」分支）。"""
    async with create_connected_server_and_client_session(mcp_server) as client:
        appended = await client.call_tool(
            "append_memory",
            {
                "user_id": "user-1",
                "session_id": "session-1",
                "payload": "我负责 3nm 工艺整合",
                "event_id": "evt-mcp-task",
            },
        )
        assert not appended.isError

        task_id = _task_id(appended.content[0].text)
        result = await client.call_tool("get_operation", {"task_id": task_id})

    assert not result.isError
    assert task_id in result.content[0].text


def _task_id(appended_text: str) -> str:
    """从 append_memory 的返回文本里取后台任务 ID。"""
    import json

    payload = json.loads(appended_text)
    task_id = str(payload.get("task_id") or "")
    assert task_id, f"expected a task_id in {appended_text!r}"
    return task_id
