"""Unit tests for the gRPC memory layer."""

from __future__ import annotations

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
        context,  # type: ignore[arg-type]
    )
    assert context.code is grpc.StatusCode.ABORTED
    assert "dead_letter" in context.detail
