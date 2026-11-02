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


def _append_event_request(event_id: str = "event-1") -> pb.AppendEventRequest:
    """v2 写入契约：一次一条原始事件（与 HTTP /v2/memory/events 同形）。"""
    return pb.AppendEventRequest(
        request_id="req-1",
        user_id="u1",
        session_id="s1",
        event=pb.SourceEvent(
            event_id=event_id,
            source="chat_round",
            role=pb.MESSAGE_ROLE_USER,
            payload="hello",
            occurred_at="2026-01-01T00:00:00+00:00",
        ),
    )


def _live_memory_id(stub: memory_pb2_grpc.MemoryServiceStub) -> str:
    """从列表里挑一条**还活着**的记忆（没被擦除、没被下架）。

    这个模块的 gRPC 服务是 module 级的，整个文件共用一份仓库。于是别的
    用例擦掉 / 下架的记忆会留在 ``include_deleted=True`` 的列表里 ——
    下架的那条还在列表中，只是 ``status`` 变成了 ``MEMORY_STATUS_SUPPRESSED``。

    「第一条有内容的」因此会挑到别人处理过的记忆，用例就变成**顺序依赖**的：
    按文件顺序跑没事，按别的顺序跑就红。变异审计按覆盖图跑用例（按名字排序），
    顺序跟文件里不一样 —— 这条用例一红，整个批次的变异体全被判成「被杀」。
    2026-11-01 实测：干净代码在这批用例里也是红的。
    """
    listed = stub.ListMemories(pb.ListMemoriesRequest(user_id="u1", include_deleted=True))
    return next(
        item.memory_id
        for item in listed.items
        if item.content and item.status == pb.MEMORY_STATUS_ACTIVE
    )


def test_append_returns_completed(stub: memory_pb2_grpc.MemoryServiceStub) -> None:
    resp = stub.AppendEvent(_append_event_request())
    assert resp.status in {"completed", "already_done"}
    assert resp.round_id == "event-1"  # 事件 ID 即幂等锚点，内部轮次 ID 用它
    assert resp.task_id


def test_append_idempotent(stub: memory_pb2_grpc.MemoryServiceStub) -> None:
    stub.AppendEvent(_append_event_request("event-idem"))
    resp2 = stub.AppendEvent(_append_event_request("event-idem"))
    assert resp2.status == "already_done"


def test_recall_returns_response(stub: memory_pb2_grpc.MemoryServiceStub) -> None:
    resp = stub.Recall(
        pb.RecallRequest(
            user_id="u1",
            session_id="s1",
            query="hello",
            l3_limit=5,
            l3_score_threshold=0.5,
            token_budget=1200,
        )
    )
    assert resp.status == "ok"
    assert len(resp.items) >= 0  # protobuf repeated field


def test_recall_has_no_intent_axis(stub: memory_pb2_grpc.MemoryServiceStub) -> None:
    """召回不再有 intent 轴：该轴在域模型里已删除，proto 里一并撤掉。

    旧口径是 SENSITIVE 无条件拒绝 —— 那是 fail-useless（连本地安全的
    L1/L2 都不返回）。隐私诉求交给调用方决定要不要带敏感内容。
    """
    assert not hasattr(pb, "RECALL_INTENT_SENSITIVE")
    resp = stub.Recall(
        pb.RecallRequest(
            user_id="u1",
            session_id="s1",
            query="secret",
        )
    )
    assert resp.status == "ok"


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


def test_namespace_isolates_memories_over_grpc(stub: memory_pb2_grpc.MemoryServiceStub) -> None:
    """gRPC 侧 namespace 生效：不同作用域的记忆互不可见。"""
    request = _append_event_request("event-ns-semi")
    request.request_id = "req-ns-semi"
    request.namespace = "semiconductor"
    stub.AppendEvent(request)

    scoped = stub.ListMemories(pb.ListMemoriesRequest(user_id="u1", namespace="semiconductor"))
    assert scoped.items

    other = stub.ListMemories(pb.ListMemoriesRequest(user_id="u1", namespace="hr"))
    assert not other.items


def test_empty_namespace_falls_back_to_default(stub: memory_pb2_grpc.MemoryServiceStub) -> None:
    """proto3 标量没有 presence：空串按「没传」处理，走默认作用域。"""
    resp = stub.ListMemories(pb.ListMemoriesRequest(user_id="u1"))
    assert resp.status == "ok"


def test_invalid_namespace_rejected_over_grpc(stub: memory_pb2_grpc.MemoryServiceStub) -> None:
    """非法 namespace 明确报错（INVALID_ARGUMENT），不静默归一化。"""
    request = _append_event_request("event-ns-bad")
    request.request_id = "req-ns-bad"
    request.namespace = "a:b"
    with pytest.raises(grpc.RpcError) as exc_info:
        stub.AppendEvent(request)
    assert exc_info.value.code() == grpc.StatusCode.INVALID_ARGUMENT


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

    def __init__(self, metadata: tuple[tuple[str, str], ...] = ()) -> None:
        self.code: object = None
        self.detail: str = ""
        self._metadata = metadata

    def abort(self, code: object, detail: str) -> None:
        self.code = code
        self.detail = detail

    def invocation_metadata(self) -> tuple[tuple[str, str], ...]:
        return self._metadata


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
        ("AppendEvent", _append_event_request()),
        (
            "Recall",
            pb.RecallRequest(user_id="u1", session_id="s1", query="q"),
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
    context = _RecordingContext(metadata=())
    method = getattr(servicer, rpc_call)

    with pytest.raises(RuntimeError, match="unreachable"):
        method(pb_request, cast("grpc.ServicerContext", context))
    assert context.code is grpc.StatusCode.INVALID_ARGUMENT
    assert "boom" in context.detail


def test_update_memory_over_grpc(stub: memory_pb2_grpc.MemoryServiceStub) -> None:
    stub.AppendEvent(_append_event_request("event-update"))
    original_id = _live_memory_id(stub)

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
    appended = stub.AppendEvent(_append_event_request("event-task"))
    resp = stub.GetTask(pb.GetTaskRequest(task_id=appended.task_id))
    assert resp.task_id == appended.task_id
    assert resp.status


def test_get_task_reports_retry_count(stub: memory_pb2_grpc.MemoryServiceStub) -> None:
    """任务查询必须带重试次数：客户端据此判断是否接近重试预算上限。"""
    appended = stub.AppendEvent(_append_event_request("event-retry-count"))
    resp = stub.GetTask(pb.GetTaskRequest(task_id=appended.task_id))
    assert resp.retry_count >= 0


def test_erase_task_is_queryable_over_grpc(stub: memory_pb2_grpc.MemoryServiceStub) -> None:
    """擦除任务必须能通过 gRPC 查询：此前 op_type 缺 ERASE，转换直接 KeyError。"""
    stub.AppendEvent(_append_event_request("event-erase-grpc"))
    target_id = _live_memory_id(stub)

    resp = stub.Erase(
        pb.EraseRequest(
            request_id="req-erase-grpc",
            user_id="u1",
            scope=pb.DELETE_SCOPE_MEMORY,
            operation_id="op-erase-grpc",
            memory_id=target_id,
        )
    )
    assert resp.status in {"completed", "already_done"}
    assert resp.task_id

    task = stub.GetTask(pb.GetTaskRequest(task_id=resp.task_id))
    assert task.status
    assert task.op_type == pb.OPERATION_TYPE_ERASE


def test_suppress_over_grpc(stub: memory_pb2_grpc.MemoryServiceStub) -> None:
    """逻辑下架在 gRPC 上与 HTTP 等价。"""
    stub.AppendEvent(_append_event_request("event-suppress-grpc"))
    target_id = _live_memory_id(stub)

    resp = stub.Suppress(
        pb.SuppressRequest(
            request_id="req-suppress-grpc",
            user_id="u1",
            scope=pb.DELETE_SCOPE_MEMORY,
            operation_id="op-suppress-grpc",
            memory_id=target_id,
        )
    )
    assert resp.status in {"completed", "already_done"}
    assert resp.task_id


def test_custom_memory_type_name_survives_proto_conversion() -> None:
    """自定义类型名必须原样穿过 gRPC 协议：枚举字段装不下行业类型。"""
    from thinkback.memory.schemas import (
        DataClassification,
        ListMemoriesResponse,
        ManagedMemoryItem,
        MemoryStatus,
        SourceType,
    )
    from thinkback.rpc.converters import list_response_to_pb

    item = ManagedMemoryItem(
        memory_id="m1",
        content="我的猫叫豆包",
        status=MemoryStatus.ACTIVE,
        source_type=SourceType.CHAT_ROUND,
        data_classification=DataClassification.PERSONAL,
        memory_type=None,
        memory_type_name="process_preference",
    )
    converted = list_response_to_pb(
        ListMemoriesResponse(status="ok", items=[item])  # type: ignore[arg-type]
    )
    assert converted.items[0].memory_type_name == "process_preference"


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


def test_list_memory_types_over_grpc(stub: memory_pb2_grpc.MemoryServiceStub) -> None:
    """gRPC 面也能发现可写的记忆类型（与 HTTP /v2/memory/types 同形）。

    gRPC 客户端拿到的类型名必须和 HTTP 一致 —— 两面对同一注册表给两份
    清单，接入方按哪份写都行。
    """
    response = stub.ListMemoryTypes(pb.ListMemoryTypesRequest(namespace="thinkback"))

    assert response.status == "ok"
    assert response.namespace == "thinkback"
    assert response.policy_version
    names = {item.name for item in response.types}
    assert "profile" in names
    profile = next(item for item in response.types if item.name == "profile")
    assert profile.extractor == "mem0"
    assert profile.source_roles == ["user"]
    assert profile.conflict == "overwrite"
    assert profile.retention == "forever"
    assert profile.classification == "personal"
    assert profile.review_required is False
    # optional 字段：没配就是「没设」，不是 0。
    assert not profile.HasField("ttl_days")
    assert not profile.HasField("recall_budget")
    assert profile.extraction_prompt_version == "default"


def test_list_memory_types_over_grpc_reports_a_ttl_type(
    stub: memory_pb2_grpc.MemoryServiceStub,
) -> None:
    """TTL 类型要把 ttl_days 带出来（optional 字段，有值才设）。"""
    response = stub.ListMemoryTypes(pb.ListMemoryTypesRequest(namespace="thinkback"))

    state = next((item for item in response.types if item.name == "state"), None)
    assert state is not None
    assert state.retention == "ttl"
    assert state.HasField("ttl_days")
    assert state.ttl_days == 90
