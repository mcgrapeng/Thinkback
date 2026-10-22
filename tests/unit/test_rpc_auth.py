"""gRPC 鉴权与 HTTP 语义对齐的回归测试。

覆盖两件事：

1. **鉴权**：gRPC 端口此前完全裸奔（``add_insecure_port`` + 无 interceptor）——
   任何能连到端口的调用方都能读写任意 user_id 的记忆。现在必须带 API Key。
2. **语义对齐**：``l3_limit`` / ``l3_score_threshold`` / ``token_budget`` 的
   默认值与上限，gRPC 侧此前与 HTTP 侧不一致。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import grpc
import pytest

from thinkback.api.auth import ApiKey, Scope, generate_api_key, register_api_key, revoke_api_key
from thinkback.memory.backends import FakeMemoryBackend
from thinkback.memory.repositories import InMemoryMemoryRepository
from thinkback.memory.service import MemoryService
from thinkback.rpc import memory_pb2 as pb
from thinkback.rpc import memory_pb2_grpc
from thinkback.rpc.auth import ApiKeyAuthInterceptor
from thinkback.rpc.converters import (
    resolve_l3_limit,
    resolve_l3_score_threshold,
    resolve_token_budget,
)
from thinkback.rpc.servicer import MemoryServicer

ALL_SCOPES = {
    Scope.MEMORY_APPEND,
    Scope.MEMORY_RECALL,
    Scope.MEMORY_READ,
    Scope.MEMORY_UPDATE,
    Scope.MEMORY_DELETE,
}


@pytest.fixture(scope="module")
def keys() -> dict[str, tuple[str, ApiKey]]:
    """两个租户的 API Key：tenant_a / tenant_b。"""

    result: dict[str, tuple[str, ApiKey]] = {}
    for tenant in ("tenant_a", "tenant_b"):
        result[tenant] = generate_api_key(
            tenant_id=tenant,
            scopes=set(ALL_SCOPES),
            plan="pro",
            env="test",
        )
    return result


@pytest.fixture(autouse=True)
def _keep_keys_registered(keys: dict[str, tuple[str, ApiKey]]) -> None:
    """其它模块会 ``_API_KEYS.clear()`` —— 每个用例前重新注册。"""

    for _, record in keys.values():
        register_api_key(record)


@pytest.fixture(scope="module")
def grpc_port() -> int:
    service = MemoryService(
        repository=InMemoryMemoryRepository(),
        backend=FakeMemoryBackend(),
        l3_write_mode="sync",
    )
    server = grpc.server(ThreadPoolExecutor(max_workers=4), interceptors=[ApiKeyAuthInterceptor()])
    memory_pb2_grpc.add_MemoryServiceServicer_to_server(MemoryServicer(service), server)  # type: ignore[no-untyped-call]
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    yield port
    server.stop(grace=0)


@pytest.fixture(scope="module")
def stub_factory(grpc_port: int):  # type: ignore[no-untyped-def]
    channels: list[grpc.Channel] = []

    def _make() -> memory_pb2_grpc.MemoryServiceStub:
        channel = grpc.insecure_channel(
            f"127.0.0.1:{grpc_port}",
            options=[("grpc.enable_http_proxy", 0)],
        )
        channels.append(channel)
        return memory_pb2_grpc.MemoryServiceStub(channel)

    yield _make
    for channel in channels:
        channel.close()


def _append_request(user_id: str = "u1", round_id: str = "round-auth") -> pb.AppendRequest:
    return pb.AppendRequest(
        request_id=f"req-{round_id}",
        user_id=user_id,
        session_id="s1",
        round_id=round_id,
        messages=[
            pb.MemoryMessage(
                message_id="m1",
                role=pb.MESSAGE_ROLE_USER,
                content="用户每周三晚上打篮球",
                timestamp="2026-01-01T00:00:00+00:00",
            ),
            pb.MemoryMessage(
                message_id="m2",
                role=pb.MESSAGE_ROLE_ASSISTANT,
                content="好的，记下了",
                timestamp="2026-01-01T00:00:01+00:00",
            ),
        ],
        source_timestamp="2026-01-01T00:00:01+00:00",
    )


def _auth(key: str) -> tuple[tuple[str, str], ...]:
    return (("authorization", f"Bearer {key}"),)


def test_call_without_authorization_is_rejected(stub_factory) -> None:  # type: ignore[no-untyped-def]
    """没有 Authorization → UNAUTHENTICATED（此前是完全放行）。"""

    stub = stub_factory()
    with pytest.raises(grpc.RpcError) as exc_info:
        stub.ListMemories(pb.ListMemoriesRequest(user_id="u1"))
    assert exc_info.value.code() == grpc.StatusCode.UNAUTHENTICATED


def test_call_with_wrong_secret_is_rejected(stub_factory, keys) -> None:  # type: ignore[no-untyped-def]
    stub = stub_factory()
    with pytest.raises(grpc.RpcError) as exc_info:
        stub.ListMemories(
            pb.ListMemoriesRequest(user_id="u1"),
            metadata=(("authorization", "Bearer tbk_test_deadbeef_00"),),
        )
    assert exc_info.value.code() == grpc.StatusCode.UNAUTHENTICATED


def test_call_with_revoked_key_is_rejected(stub_factory, keys) -> None:  # type: ignore[no-untyped-def]
    plaintext, record = keys["tenant_b"]
    revoke_api_key(record.key_id)
    try:
        stub = stub_factory()
        with pytest.raises(grpc.RpcError) as exc_info:
            stub.ListMemories(pb.ListMemoriesRequest(user_id="u1"), metadata=_auth(plaintext))
        assert exc_info.value.code() == grpc.StatusCode.UNAUTHENTICATED
    finally:
        register_api_key(record)


def test_call_with_valid_key_succeeds(stub_factory, keys) -> None:  # type: ignore[no-untyped-def]
    plaintext, _ = keys["tenant_a"]
    stub = stub_factory()
    resp = stub.ListMemories(pb.ListMemoriesRequest(user_id="u1"), metadata=_auth(plaintext))
    assert resp.status == "ok"


def test_tenant_mismatch_is_denied(stub_factory, keys) -> None:  # type: ignore[no-untyped-def]
    """拿 A 租户的 key 声明 B 租户 → PERMISSION_DENIED。"""

    plaintext, _ = keys["tenant_a"]
    stub = stub_factory()
    with pytest.raises(grpc.RpcError) as exc_info:
        stub.ListMemories(
            pb.ListMemoriesRequest(user_id="u1"),
            metadata=(("authorization", f"Bearer {plaintext}"), ("x-tenant-id", "tenant_b")),
        )
    assert exc_info.value.code() == grpc.StatusCode.PERMISSION_DENIED


def test_declared_tenant_matching_key_is_accepted(stub_factory, keys) -> None:  # type: ignore[no-untyped-def]
    plaintext, _ = keys["tenant_a"]
    stub = stub_factory()
    resp = stub.ListMemories(
        pb.ListMemoriesRequest(user_id="u1"),
        metadata=(("authorization", f"Bearer {plaintext}"), ("x-tenant-id", "tenant_a")),
    )
    assert resp.status == "ok"


def test_tenants_cannot_read_each_others_memories(stub_factory, keys) -> None:  # type: ignore[no-untyped-def]
    """租户隔离：A 写进去的记忆，B 按同样的 user_id 读不到。"""

    key_a, _ = keys["tenant_a"]
    key_b, _ = keys["tenant_b"]
    stub_a = stub_factory()
    stub_b = stub_factory()

    stub_a.Append(_append_request(user_id="u1", round_id="round-tenant-iso"), metadata=_auth(key_a))

    own = stub_a.ListMemories(pb.ListMemoriesRequest(user_id="u1"), metadata=_auth(key_a))
    other = stub_b.ListMemories(pb.ListMemoriesRequest(user_id="u1"), metadata=_auth(key_b))
    assert len(own.items) >= 1
    assert len(other.items) == 0


def test_task_of_other_tenant_is_denied(stub_factory, keys) -> None:  # type: ignore[no-untyped-def]
    """别的租户的任务不能按 task_id 读走（任务作用域带 user_id）。"""

    key_a, _ = keys["tenant_a"]
    key_b, _ = keys["tenant_b"]
    stub_a = stub_factory()
    stub_b = stub_factory()

    appended = stub_a.Append(
        _append_request(user_id="u1", round_id="round-task-tenant"), metadata=_auth(key_a)
    )
    assert appended.task_id
    with pytest.raises(grpc.RpcError) as exc_info:
        stub_b.GetTask(pb.GetTaskRequest(task_id=appended.task_id), metadata=_auth(key_b))
    assert exc_info.value.code() == grpc.StatusCode.PERMISSION_DENIED


def test_recall_of_other_tenant_returns_no_items(stub_factory, keys) -> None:  # type: ignore[no-untyped-def]
    key_a, _ = keys["tenant_a"]
    key_b, _ = keys["tenant_b"]
    stub_a = stub_factory()
    stub_b = stub_factory()

    stub_a.Append(
        _append_request(user_id="u2", round_id="round-recall-tenant"), metadata=_auth(key_a)
    )
    resp_b = stub_b.Recall(
        pb.RecallRequest(user_id="u2", session_id="s1", query="篮球", intent=pb.RECALL_INTENT_CHAT),
        metadata=_auth(key_b),
    )
    assert all("篮球" not in item.content for item in resp_b.items)


# ── 与 HTTP 的取值口径对齐 ────────────────────────────────────────────────


def test_l3_limit_default_and_cap() -> None:
    """默认 5（HTTP 同值）；上限 50；越界报错而不是静默夹取。"""

    assert resolve_l3_limit(0) == 5
    assert resolve_l3_limit(7) == 7
    assert resolve_l3_limit(50) == 50
    with pytest.raises(ValueError):
        resolve_l3_limit(51)
    with pytest.raises(ValueError):
        resolve_l3_limit(-1)


def test_l3_score_threshold_default_and_range() -> None:
    """默认 0.5（HTTP 同值，gRPC 此前是 0.0 = 不过滤）；范围 0-1。"""

    assert resolve_l3_score_threshold(0.0) == 0.5
    assert resolve_l3_score_threshold(0.8) == 0.8
    assert resolve_l3_score_threshold(1.0) == 1.0
    with pytest.raises(ValueError):
        resolve_l3_score_threshold(1.5)
    with pytest.raises(ValueError):
        resolve_l3_score_threshold(-0.1)


def test_token_budget_default_and_range() -> None:
    assert resolve_token_budget(0) == 1200
    assert resolve_token_budget(2000) == 2000
    with pytest.raises(ValueError):
        resolve_token_budget(99)
    with pytest.raises(ValueError):
        resolve_token_budget(12001)
