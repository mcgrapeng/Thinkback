"""Unit tests for the gRPC memory layer."""

from __future__ import annotations

from typing import Any, cast

import grpc
import pytest

from thinkback.memory.backends import FakeMemoryBackend
from thinkback.memory.repositories import InMemoryMemoryRepository
from thinkback.memory.service import MemoryService
from thinkback.rpc import memory_pb2 as pb
from thinkback.rpc import memory_pb2_grpc


@pytest.fixture(scope="module")
def grpc_channel():  # type: ignore[no-untyped-def]
    from concurrent.futures import ThreadPoolExecutor

    from grpc_reflection.v1alpha import reflection

    from thinkback.rpc.servicer import MemoryServicer

    svc = MemoryService(
        repository=InMemoryMemoryRepository(),
        backend=FakeMemoryBackend(),
        l3_write_mode="sync",
    )
    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    memory_pb2_grpc.add_MemoryServiceServicer_to_server(MemoryServicer(svc), server)  # type: ignore[no-untyped-call]
    reflection.enable_server_reflection(
        [pb.DESCRIPTOR.services_by_name["MemoryService"].full_name, reflection.SERVICE_NAME],
        server,
    )
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    # grpc channel 默认读取 http_proxy/https_proxy 环境变量；本机代理（如 7890）
    # 会劫持 127.0.0.1 直连导致 UNAVAILABLE。内部直连必须显式关闭 HTTP 代理路由。
    channel = grpc.insecure_channel(
        f"127.0.0.1:{port}",
        options=[("grpc.enable_http_proxy", 0)],
    )
    yield channel
    channel.close()
    server.stop(grace=0)


@pytest.fixture(scope="module")
def stub(grpc_channel: grpc.Channel):  # type: ignore[no-untyped-def]
    return memory_pb2_grpc.MemoryServiceStub(grpc_channel)  # type: ignore[no-untyped-call]


def _append_request(round_id: str = "round-1") -> pb.AppendRequest:
    return pb.AppendRequest(
        request_id="req-1",
        user_id="u1",
        session_id="s1",
        round_id=round_id,
        messages=[
            pb.MemoryMessage(
                message_id="m1",
                role=pb.MESSAGE_ROLE_USER,
                content="hello",
                timestamp="2026-01-01T00:00:00+00:00",
            ),
            pb.MemoryMessage(
                message_id="m2",
                role=pb.MESSAGE_ROLE_ASSISTANT,
                content="hi",
                timestamp="2026-01-01T00:00:01+00:00",
            ),
        ],
        source_timestamp="2026-01-01T00:00:01+00:00",
    )


def test_append_returns_completed(stub: memory_pb2_grpc.MemoryServiceStub) -> None:
    resp = stub.Append(_append_request())
    assert resp.status in {"completed", "already_done"}
    assert resp.round_id == "round-1"
    assert resp.task_id


def test_append_idempotent(stub: memory_pb2_grpc.MemoryServiceStub) -> None:
    stub.Append(_append_request("round-idem"))
    resp2 = stub.Append(_append_request("round-idem"))
    assert resp2.status == "already_done"


def test_recall_returns_response(stub: memory_pb2_grpc.MemoryServiceStub) -> None:
    resp = stub.Recall(
        pb.RecallRequest(
            user_id="u1",
            session_id="s1",
            query="hello",
            intent=pb.RECALL_INTENT_CHAT,
            l3_limit=5,
            l3_score_threshold=0.5,
            token_budget=1200,
        )
    )
    assert resp.status == "ok"
    assert len(resp.items) >= 0  # protobuf repeated field


def test_recall_sensitive_intent_rejected(stub: memory_pb2_grpc.MemoryServiceStub) -> None:
    with pytest.raises(grpc.RpcError) as exc_info:
        stub.Recall(
            pb.RecallRequest(
                user_id="u1",
                session_id="s1",
                query="secret",
                intent=pb.RECALL_INTENT_SENSITIVE,
            )
        )
    assert exc_info.value.code() == grpc.StatusCode.PERMISSION_DENIED


def test_delete_memory_not_found_returns_empty(stub: memory_pb2_grpc.MemoryServiceStub) -> None:
    resp = stub.Delete(
        pb.DeleteRequest(
            request_id="req-del",
            user_id="u1",
            scope=pb.DELETE_SCOPE_MEMORY,
            operation_id="op-del-1",
            memory_id="nonexistent-id",
        )
    )
    assert resp.task_id
    assert resp.affected_memories == 0


def test_list_memories(stub: memory_pb2_grpc.MemoryServiceStub) -> None:
    resp = stub.ListMemories(pb.ListMemoriesRequest(user_id="u1", include_deleted=False))
    assert resp.status == "ok"


def test_get_memory_not_found(stub: memory_pb2_grpc.MemoryServiceStub) -> None:
    with pytest.raises(grpc.RpcError) as exc_info:
        stub.GetMemory(pb.GetMemoryRequest(user_id="u1", memory_id="no-such-id"))
    assert exc_info.value.code() == grpc.StatusCode.NOT_FOUND


def test_get_task_not_found(stub: memory_pb2_grpc.MemoryServiceStub) -> None:
    with pytest.raises(grpc.RpcError) as exc_info:
        stub.GetTask(pb.GetTaskRequest(task_id="no-such-task"))
    assert exc_info.value.code() == grpc.StatusCode.NOT_FOUND


def test_l3_background_status(stub: memory_pb2_grpc.MemoryServiceStub) -> None:
    resp = stub.GetL3BackgroundStatus(pb.L3BackgroundStatusRequest())
    assert resp.write_mode in {"sync", "async"}
    assert resp.executor_workers > 0


def test_rebuild_returns_response(stub: memory_pb2_grpc.MemoryServiceStub) -> None:
    """Rebuild RPC 已上线：调用返回 task_id，proto 里声明的语义与业务路由一致。"""
    resp = stub.Rebuild(
        pb.RebuildRequest(
            request_id="req-rb",
            user_id="u1",
            operation_id="op-rebuild-1",
            rebuild_l2=True,
            rebuild_l3=True,
        )
    )
    assert resp.task_id
    assert resp.status in {"completed", "running", "already_done"}


class _RecordingContext:
    """记录 abort 调用的假 ServicerContext。"""

    def __init__(self) -> None:
        self.code: object = None
        self.detail: str = ""

    def abort(self, code: object, detail: str) -> None:
        self.code = code
        self.detail = detail


def test_handle_error_maps_dead_letter_to_aborted() -> None:
    """回归：dead_letter（重试预算耗尽）gRPC 侧必须映射 ABORTED（对齐 HTTP 409），
    而不是落进 INTERNAL 让客户端无法区分"该停止重试"和"服务端 bug"。"""
    from thinkback.rpc.servicer import _handle_error

    context = _RecordingContext()
    _handle_error(
        RuntimeError("dead_letter: operation memory-delete:op-x exceeded retry budget of 5"),
        cast("grpc.ServicerContext", context),
    )
    assert context.code is grpc.StatusCode.ABORTED
    assert "dead_letter" in context.detail


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (ValueError("fail-closed recall intent"), grpc.StatusCode.PERMISSION_DENIED),
        (ValueError("operation_id conflict"), grpc.StatusCode.ABORTED),
        (ValueError("memory not found"), grpc.StatusCode.NOT_FOUND),
        (ValueError("bad field"), grpc.StatusCode.INVALID_ARGUMENT),
        (RuntimeError("dead_letter: budget exceeded"), grpc.StatusCode.ABORTED),
        (RuntimeError("stale write detected"), grpc.StatusCode.ABORTED),
        (RuntimeError("queue full"), grpc.StatusCode.UNAVAILABLE),
        (RuntimeError("operation timed out"), grpc.StatusCode.UNAVAILABLE),
        (RuntimeError("mem0 library add failed"), grpc.StatusCode.INTERNAL),
        (RuntimeError("memory backend unavailable"), grpc.StatusCode.INTERNAL),
        (RuntimeError("other runtime failure"), grpc.StatusCode.INTERNAL),
        (ZeroDivisionError("unknown"), grpc.StatusCode.INTERNAL),
    ],
)
def test_handle_error_maps_exception_to_grpc_status(
    exc: Exception, expected: grpc.StatusCode
) -> None:
    from thinkback.rpc.servicer import _handle_error

    context = _RecordingContext()
    _handle_error(exc, cast("grpc.ServicerContext", context))
    assert context.code is expected
    assert context.detail == str(exc)


class _ExplodingService:
    """每个被调用的方法都抛错，用于覆盖各 RPC 的 except 路径。"""

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        def _explode(*args: object, **kwargs: object) -> None:
            raise ValueError(f"boom in {name}")

        return _explode


@pytest.mark.parametrize(
    ("rpc_call", "pb_request"),
    [
        ("Append", _append_request()),
        (
            "Recall",
            pb.RecallRequest(
                user_id="u1", session_id="s1", query="q", intent=pb.RECALL_INTENT_CHAT
            ),
        ),
        (
            "Delete",
            pb.DeleteRequest(
                request_id="req-d",
                user_id="u1",
                scope=pb.DELETE_SCOPE_MEMORY,
                operation_id="op-d",
                memory_id="m-1",
            ),
        ),
        ("ListMemories", pb.ListMemoriesRequest(user_id="u1")),
        ("GetMemory", pb.GetMemoryRequest(user_id="u1", memory_id="m-1")),
        (
            "UpdateMemory",
            pb.UpdateMemoryRequest(
                request_id="req-u",
                user_id="u1",
                operation_id="op-u",
                memory_id="m-1",
                content="新内容",
            ),
        ),
        (
            "Rebuild",
            pb.RebuildRequest(
                request_id="req-r",
                user_id="u1",
                operation_id="op-r",
                rebuild_l2=True,
                rebuild_l3=False,
            ),
        ),
        ("GetTask", pb.GetTaskRequest(task_id="task-1")),
        ("GetL3BackgroundStatus", pb.L3BackgroundStatusRequest()),
    ],
)
def test_servicer_error_paths_abort_then_unreachable(rpc_call: str, pb_request: object) -> None:
    """业务异常必须先经 _handle_error 映射 abort，再走 unreachable 兜底。"""
    from thinkback.rpc.servicer import MemoryServicer

    servicer = MemoryServicer(_ExplodingService())  # type: ignore[arg-type]
    context = _RecordingContext()
    method = getattr(servicer, rpc_call)

    with pytest.raises(RuntimeError, match="unreachable"):
        method(pb_request, cast("grpc.ServicerContext", context))
    assert context.code is grpc.StatusCode.INVALID_ARGUMENT
    assert "boom" in context.detail


def test_update_memory_over_grpc(stub: memory_pb2_grpc.MemoryServiceStub) -> None:
    stub.Append(_append_request("round-update"))
    listed = stub.ListMemories(pb.ListMemoriesRequest(user_id="u1", include_deleted=True))
    target = next(item for item in listed.items if item.content)
    original_id = target.memory_id

    resp = stub.UpdateMemory(
        pb.UpdateMemoryRequest(
            request_id="req-upd",
            user_id="u1",
            operation_id="op-upd",
            memory_id=original_id,
            content="修正后的记忆内容",
        )
    )

    assert resp.status in {"completed", "already_done"}
    assert resp.memory.memory_id == original_id
    assert resp.memory.content == "修正后的记忆内容"

    fetched = stub.GetMemory(pb.GetMemoryRequest(user_id="u1", memory_id=original_id))
    assert fetched.status == "ok"
    assert fetched.memory.content == "修正后的记忆内容"


def test_get_task_returns_task_snapshot(stub: memory_pb2_grpc.MemoryServiceStub) -> None:
    appended = stub.Append(_append_request("round-task"))
    resp = stub.GetTask(pb.GetTaskRequest(task_id=appended.task_id))
    assert resp.task_id == appended.task_id
    assert resp.status


# ---------------------------------------------------------------------------
# server 生命周期
# ---------------------------------------------------------------------------


def test_create_server_binds_and_stops() -> None:
    from thinkback.rpc.server import create_server

    service = MemoryService(
        repository=InMemoryMemoryRepository(),
        backend=FakeMemoryBackend(),
        l3_write_mode="sync",
    )
    server, address = create_server(
        service, host="127.0.0.1", port=0, max_workers=2, max_concurrent_rpcs=4
    )
    assert address == "127.0.0.1:0"
    server.start()
    server.stop(grace=0)


class _FakeGrpcServer:
    def __init__(self) -> None:
        self.started = False
        self.stopped: float | None = None

    def start(self) -> None:
        self.started = True

    def stop(self, grace: float | None = None) -> None:
        self.stopped = grace

    def wait_for_termination(self) -> None:
        # 模拟 SIGTERM 到达：触发 serve 注册的处理器后返回。
        import signal as signal_module

        handler = _SIGNAL_HANDLERS[signal_module.SIGTERM]
        assert callable(handler)
        handler(signal_module.SIGTERM, None)


_SIGNAL_HANDLERS: dict[int, Any] = {}


def test_serve_stops_gracefully_on_sigterm(monkeypatch: pytest.MonkeyPatch) -> None:
    """serve() 的阻塞壳：注册 SIGTERM/SIGINT handler，收到信号后 graceful stop。"""
    from thinkback.rpc import server as server_module

    fake = _FakeGrpcServer()
    monkeypatch.setattr(server_module, "create_server", lambda *a, **k: (fake, "127.0.0.1:0"))

    def fake_signal(sig: int, handler: Any) -> None:
        _SIGNAL_HANDLERS[sig] = handler

    monkeypatch.setattr("signal.signal", fake_signal)

    server_module.serve(
        MemoryService(
            repository=InMemoryMemoryRepository(),
            backend=FakeMemoryBackend(),
            l3_write_mode="sync",
        ),
        shutdown_grace_seconds=0.5,
    )

    assert fake.started is True
    assert fake.stopped == 0.5
