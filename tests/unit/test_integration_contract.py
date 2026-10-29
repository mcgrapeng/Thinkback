"""接入契约矩阵：第三方 AI 助手接入 Thinkback 时必须成立的断言。

这份文件是**给接入方看的验收清单**。每一节对应契约矩阵的一行，跑一遍
就等于确认「这个记忆服务能接」：

1. 自定义类型全链路（append → items → get → update → recall → erase）类型名不丢
2. 事件模型：user / assistant / tool / system 四种角色都能写；assistant 不成为用户事实
3. metadata：调用方附加的业务属性进后端 metadata，不被吞
4. namespace 矩阵：同 user/session、不同 namespace 的读写删互不可见
5. HTTP ↔ gRPC：同一组操作两面对跑，语义与字段一致
6. 幂等：同锚点重放一致
7. 擦除：逐资源回执 + 各层确实不可见

已由专门文件覆盖的部分不在这里重复（幂等键的 409 语义见
``test_idempotency.py``，namespace 隔离的细节见
``test_memory_namespace_isolation.py``，mem0 history 擦除见
``test_erase_history_coverage.py``）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import grpc
import pytest
from fastapi.testclient import TestClient

from thinkback.api.app import create_app
from thinkback.domain.memory_types import load_registry
from thinkback.memory.backends import FakeMemoryBackend
from thinkback.memory.repositories import InMemoryMemoryRepository
from thinkback.memory.service import MemoryService
from thinkback.rpc import memory_pb2 as pb
from thinkback.rpc import memory_pb2_grpc

PROFILE_DIR = Path(__file__).resolve().parents[2] / "profiles"
NAMESPACE = "semiconductor"


def _service() -> MemoryService:
    """带行业 profile 的服务（自定义类型契约要在真 profile 下验）。"""

    def provider(namespace: str):
        return load_registry(PROFILE_DIR, namespace)

    return MemoryService(
        repository=InMemoryMemoryRepository(),
        backend=FakeMemoryBackend(),
        registry_provider=provider,
        l3_write_mode="sync",
    )


@pytest.fixture
def service() -> MemoryService:
    return _service()


@pytest.fixture
def client(service: MemoryService, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr("thinkback.api.dependencies._memory_service", service)
    with TestClient(create_app(run_startup_tasks=False)) as test_client:
        yield test_client


def _event_payload(
    event_id: str,
    payload: str,
    *,
    role: str = "user",
    namespace: str = NAMESPACE,
    memory_type: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "request_id": f"req-{event_id}",
        "user_id": "user-1",
        "session_id": "session-1",
        "namespace": namespace,
        "event": {
            "event_id": event_id,
            "source": "chat_round",
            "role": role,
            "payload": payload,
            "occurred_at": "2026-10-29T10:00:03Z",
        },
    }
    if memory_type is not None:
        body["memory_type"] = memory_type
    if metadata is not None:
        body["metadata"] = metadata
    return body


# ── 1. 自定义类型全链路 ────────────────────────────────────────────────


def test_custom_memory_type_survives_the_whole_chain(client: TestClient) -> None:
    """行业自定义类型名在 append → items → get → update → recall 全程不丢。

    这是「配置即可接入」的核心承诺：接入方声明了哪些类型，用户后续看到
    的、编辑的、召回的就必须是那些类型，而不是被降级成内置 profile。
    """
    appended = client.post(
        "/v2/memory/events",
        json=_event_payload(
            "evt-custom-1",
            "制程节点：28nm",
            memory_type="process_preference",
        ),
    )
    assert appended.status_code == 200, appended.text

    items = client.get("/v2/memory/items", params={"user_id": "user-1", "namespace": NAMESPACE})
    assert items.status_code == 200, items.text
    listed = items.json()["items"]
    assert listed, items.text
    item = listed[0]
    assert item["memory_type_name"] == "process_preference"
    # 自定义类型不在内置枚举里：memory_type 为空是契约，不是丢字段。
    assert item["memory_type"] is None
    memory_id = item["memory_id"]

    detail = client.get(
        f"/v2/memory/items/{memory_id}",
        params={"user_id": "user-1", "namespace": NAMESPACE},
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["memory"]["memory_type_name"] == "process_preference"

    updated = client.post(
        "/v2/memory/update",
        json={
            "request_id": "req-update-1",
            "user_id": "user-1",
            "operation_id": "op-update-1",
            "memory_id": memory_id,
            "content": "制程节点：14nm",
            "namespace": NAMESPACE,
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["memory"]["memory_type_name"] == "process_preference"

    recalled = client.post(
        "/v2/memory/recall",
        json={
            "user_id": "user-1",
            "session_id": "session-1",
            "query": "制程节点",
            "namespace": NAMESPACE,
        },
    )
    assert recalled.status_code == 200, recalled.text
    contents = [item["content"] for item in recalled.json()["items"]]
    assert "制程节点：14nm" in contents


# ── 2. 事件模型 ────────────────────────────────────────────────────────


@pytest.mark.parametrize("role", ["user", "assistant", "tool", "system"])
def test_every_event_role_can_be_written(client: TestClient, role: str) -> None:
    """四种角色都能写进来：企业助手的事件形态不只有问答。"""
    response = client.post(
        "/v2/memory/events",
        json=_event_payload(f"evt-role-{role}", f"{role} 说的一句话", role=role),
    )
    assert response.status_code == 200, response.text


def test_assistant_turn_does_not_become_a_user_fact(client: TestClient) -> None:
    """assistant 的话不是用户事实：不接受 assistant 来源的类型不抽它。

    准入由类型的 ``source_roles`` 决定（schema 里 source_roles 只有 user）。
    """
    response = client.post(
        "/v2/memory/events",
        json=_event_payload(
            "evt-assistant-1",
            "助手说：用户喜欢 28nm",
            role="assistant",
            memory_type="profile",
        ),
    )
    assert response.status_code == 200, response.text

    items = client.get("/v2/memory/items", params={"user_id": "user-1", "namespace": NAMESPACE})
    assert items.json()["items"] == []


# ── 3. metadata ────────────────────────────────────────────────────────


def test_custom_metadata_reaches_the_backend(client: TestClient, service: MemoryService) -> None:
    """调用方附加的业务属性进后端 metadata，不被吞。"""
    client.post(
        "/v2/memory/events",
        json=_event_payload(
            "evt-meta-1",
            "我负责 CMP 段",
            memory_type="process_preference",
            metadata={"lot_id": "LOT-2026-1029", "fab": "fab7"},
        ),
    )

    backend = service.backend
    assert isinstance(backend, FakeMemoryBackend)
    assert backend.memories, "backend should hold the extracted fact"
    stored = next(iter(backend.memories.values()))
    # 调用方的自定义业务属性保留在 custom_metadata 子字典里（系统保留键
    # 不能被业务键覆盖，所以不与系统键平铺）。
    custom = stored["metadata"]["custom_metadata"]
    assert custom["lot_id"] == "LOT-2026-1029"
    assert custom["fab"] == "fab7"


# ── 4. namespace 矩阵 ──────────────────────────────────────────────────


def test_namespace_matrix_keeps_write_read_delete_apart(client: TestClient) -> None:
    """同 user/session、不同 namespace：写、读、删三面互不可见。"""
    client.post(
        "/v2/memory/events",
        json=_event_payload("evt-ns-a", "只有 alpha 空间能看到的事实", namespace="alpha"),
    )

    alpha = client.get("/v2/memory/items", params={"user_id": "user-1", "namespace": "alpha"})
    beta = client.get("/v2/memory/items", params={"user_id": "user-1", "namespace": "beta"})
    assert len(alpha.json()["items"]) == 1
    assert beta.json()["items"] == []

    beta_recall = client.post(
        "/v2/memory/recall",
        json={
            "user_id": "user-1",
            "session_id": "session-1",
            "query": "alpha",
            "namespace": "beta",
        },
    )
    assert all("alpha 空间" not in item["content"] for item in beta_recall.json()["items"])

    erased = client.post(
        "/v2/memory/erase",
        json={
            "request_id": "req-erase-beta",
            "user_id": "user-1",
            "scope": "all",
            "operation_id": "op-erase-beta",
            "namespace": "beta",
        },
    )
    assert erased.status_code == 200, erased.text

    alpha_after = client.get("/v2/memory/items", params={"user_id": "user-1", "namespace": "alpha"})
    assert len(alpha_after.json()["items"]) == 1, "擦除 beta 不该动 alpha"


# ── 5. HTTP ↔ gRPC 同形 ───────────────────────────────────────────────


@pytest.fixture
def grpc_stub(service: MemoryService):  # type: ignore[no-untyped-def]
    """在内存里起一个 gRPC server（与 HTTP 同一个 service 实例）。"""
    from concurrent.futures import ThreadPoolExecutor

    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    memory_pb2_grpc.add_MemoryServiceServicer_to_server(  # type: ignore[no-untyped-call]
        _ServicerFactory(service), server
    )
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    channel = grpc.insecure_channel(f"127.0.0.1:{port}", options=[("grpc.enable_http_proxy", 0)])
    try:
        yield memory_pb2_grpc.MemoryServiceStub(channel)
    finally:
        channel.close()
        server.stop(grace=0)


def _ServicerFactory(service: MemoryService):  # type: ignore[no-untyped-def]
    from thinkback.rpc.servicer import MemoryServicer

    return MemoryServicer(service)


def test_http_and_grpc_agree_on_the_same_operation(client: TestClient, grpc_stub) -> None:
    """同一个 service 上，两种传输面给出同样的语义与字段。"""
    http_response = client.post(
        "/v2/memory/events",
        json=_event_payload(
            "evt-parity-http",
            "HTTP 面写入的事实",
            memory_type="process_preference",
        ),
    )
    assert http_response.status_code == 200, http_response.text

    grpc_response = grpc_stub.AppendEvent(
        pb.AppendEventRequest(
            request_id="req-parity-grpc",
            user_id="user-1",
            session_id="session-1",
            namespace=NAMESPACE,
            memory_type="process_preference",
            event=pb.SourceEvent(
                event_id="evt-parity-grpc",
                source="chat_round",
                role=pb.MESSAGE_ROLE_USER,
                payload="gRPC 面写入的事实",
                occurred_at="2026-10-29T10:00:03Z",
            ),
        )
    )

    http_body = http_response.json()
    grpc_body = {"status": grpc_response.status, "task_id": grpc_response.task_id}
    assert set(grpc_body) <= set(http_body), "gRPC 返回的字段 HTTP 也要有"
    assert http_body["status"] == grpc_body["status"] == "completed"
    assert http_body["task_id"] == "memory-extract:evt-parity-http"
    assert grpc_body["task_id"] == "memory-extract:evt-parity-grpc"

    items = client.get("/v2/memory/items", params={"user_id": "user-1", "namespace": NAMESPACE})
    assert len(items.json()["items"]) == 2, "两面写的事实都在同一个索引里"

    # 同一事件从另一面重放：幂等锚点跨传输面也认。
    replay = grpc_stub.AppendEvent(
        pb.AppendEventRequest(
            request_id="req-parity-replay",
            user_id="user-1",
            session_id="session-1",
            namespace=NAMESPACE,
            memory_type="process_preference",
            event=pb.SourceEvent(
                event_id="evt-parity-http",
                source="chat_round",
                role=pb.MESSAGE_ROLE_USER,
                payload="HTTP 面写入的事实",
                occurred_at="2026-10-29T10:00:03Z",
            ),
        )
    )
    assert replay.status == "already_done"
    assert replay.task_id == http_body["task_id"]
    after = client.get("/v2/memory/items", params={"user_id": "user-1", "namespace": NAMESPACE})
    assert len(after.json()["items"]) == 2, "重放不产生新记忆"


# ── 6. 幂等 ────────────────────────────────────────────────────────────


def test_replaying_the_same_event_id_does_not_duplicate(client: TestClient) -> None:
    """同一锚点重放：一条事件只落一条记忆。"""
    body = _event_payload("evt-idem-1", "幂等锚点的事实", memory_type="process_preference")
    first = client.post("/v2/memory/events", json=body)
    second = client.post("/v2/memory/events", json=body)
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["task_id"] == second.json()["task_id"]

    items = client.get("/v2/memory/items", params={"user_id": "user-1", "namespace": NAMESPACE})
    assert len(items.json()["items"]) == 1


# ── 7. 擦除：逐资源回执 + 各层不可见 ──────────────────────────────────


def test_erase_returns_per_resource_receipt_and_hides_everything(
    client: TestClient, service: MemoryService
) -> None:
    """擦除要给逐资源回执，且各层都不可见了。

    回执是「对外承诺 vs 实际」的唯一证据面：没有回执就只剩一句
    "已完成"，而那句话曾经说过谎。
    """
    client.post(
        "/v2/memory/events",
        json=_event_payload("evt-erase-1", "要被物理擦除的事实", memory_type="process_preference"),
    )

    erased = client.post(
        "/v2/memory/erase",
        json={
            "request_id": "req-erase-all",
            "user_id": "user-1",
            "scope": "all",
            "operation_id": "op-erase-all",
            "namespace": NAMESPACE,
        },
    )
    assert erased.status_code == 200, erased.text

    task = client.get(f"/v2/memory/tasks/{erased.json()['task_id']}")
    assert task.status_code == 200, task.text
    result = task.json()["result"]
    assert set(result["done_stages"]) >= {"barrier", "backend", "history", "l1", "audit", "index"}
    # 逐资源回执：每个被清的资源都要有数，不能只报一个总数。
    assert isinstance(result["purged"], dict) and result["purged"]

    items = client.get("/v2/memory/items", params={"user_id": "user-1", "namespace": NAMESPACE})
    assert items.json()["items"] == []

    recalled = client.post(
        "/v2/memory/recall",
        json={
            "user_id": "user-1",
            "session_id": "session-1",
            "query": "物理擦除",
            "namespace": NAMESPACE,
        },
    )
    assert all("要被物理擦除" not in item["content"] for item in recalled.json()["items"])

    backend = service.backend
    assert isinstance(backend, FakeMemoryBackend)
    assert backend.memories == {}, "引擎侧也必须没有残留"
