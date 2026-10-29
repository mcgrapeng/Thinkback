"""gRPC 面：未知的 proto3 enum 值是客户端错误，不是服务端故障。

proto3 的 enum 允许未知值 —— 客户端版本比服务端新时会遇到。
原来的实现直接查映射字典，抛 ``KeyError``；它既不是 ValueError 也不是
RuntimeError，一路落到「未知异常 → INTERNAL」。

INTERNAL 对客户端的含义是「服务端坏了，重试」—— 但实际是它自己发错了枚举值，
于是客户端会拿着同一个坏请求一直重试。正确的状态码是 INVALID_ARGUMENT。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import grpc
import pytest
from grpc_reflection.v1alpha import reflection

from thinkback.memory.backends import FakeMemoryBackend
from thinkback.memory.repositories import InMemoryMemoryRepository
from thinkback.memory.service import MemoryService
from thinkback.rpc import memory_pb2 as pb
from thinkback.rpc import memory_pb2_grpc
from thinkback.rpc.servicer import MemoryServicer


@pytest.fixture(scope="module")
def grpc_channel():
    svc = MemoryService(
        repository=InMemoryMemoryRepository(),
        backend=FakeMemoryBackend(),
        l3_write_mode="sync",
    )
    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    memory_pb2_grpc.add_MemoryServiceServicer_to_server(  # type: ignore[no-untyped-call]
        MemoryServicer(svc), server
    )
    reflection.enable_server_reflection(
        [pb.DESCRIPTOR.services_by_name["MemoryService"].full_name, reflection.SERVICE_NAME],
        server,
    )
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    # grpc channel 默认读取 http_proxy 环境变量，本机代理会劫持 127.0.0.1 直连。
    channel = grpc.insecure_channel(
        f"127.0.0.1:{port}",
        options=[("grpc.enable_http_proxy", 0)],
    )
    yield channel
    channel.close()
    server.stop(grace=0)


@pytest.fixture(scope="module")
def stub(grpc_channel: grpc.Channel):
    return memory_pb2_grpc.MemoryServiceStub(grpc_channel)  # type: ignore[no-untyped-call]


def test_unknown_role_enum_returns_invalid_argument(stub) -> None:
    """未知的 role 枚举值 → INVALID_ARGUMENT（不是 INTERNAL）。"""
    request = pb.AppendEventRequest(
        request_id="req-bad-role",
        user_id="u1",
        session_id="s1",
        event=pb.SourceEvent(
            event_id="event-bad-role",
            source="chat_round",
            role=99,  # proto3 允许的未知枚举值
            payload="hello",
            occurred_at="2026-01-01T00:00:00+00:00",
        ),
    )

    with pytest.raises(grpc.RpcError) as excinfo:
        stub.AppendEvent(request)

    assert excinfo.value.code() == grpc.StatusCode.INVALID_ARGUMENT, (
        f"实际状态码 {excinfo.value.code()}：客户端发错枚举值不该被报成服务端故障"
    )


def test_unknown_delete_scope_enum_returns_invalid_argument(stub) -> None:
    """未知的删除范围枚举值 → INVALID_ARGUMENT。"""
    request = pb.DeleteRequest(
        request_id="req-bad-scope",
        user_id="u1",
        scope=77,  # proto3 允许的未知枚举值
        operation_id="op-bad-scope",
    )

    with pytest.raises(grpc.RpcError) as excinfo:
        stub.Delete(request)

    assert excinfo.value.code() == grpc.StatusCode.INVALID_ARGUMENT
