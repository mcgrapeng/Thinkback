"""部署边界：本部署服务哪些 namespace（``MEMORY_ALLOWED_NAMESPACES``）。

内网免鉴权（D10）下 namespace 只是业务分组标签，不是访问控制 —— 任何能连到
本服务的人都能读写任何 namespace 下的记忆。设置 ``MEMORY_ALLOWED_NAMESPACES``
之后，三个传输面（HTTP / gRPC / MCP）都只服务名单内的 namespace：把「一个
部署服务一个租户」从口头约定变成可执行的配置。

这里钉住三件事：

1. 名单外的 namespace 在**每一个**带 namespace 的业务端点上都 403。九个端点
   各写一次守卫，漏一个就是一条绕过路径 —— 所以用参数化用例把九个全走一遍，
   而不是抽查一个。
2. 守卫在**服务方法之前**生效：名单外的请求根本不该走到业务逻辑（断言服务
   方法一次都没被调用）。
3. gRPC 侧同义地给 PERMISSION_DENIED；MCP 侧在绑定 namespace 时就拒开。

边界不是访问控制：白名单内的 namespace 里 user_id 仍由调用方自报。它只保证
「本部署不服务别的租户」这一条。
"""

from __future__ import annotations

from typing import Any

import grpc
import pytest
from fastapi.testclient import TestClient

from thinkback.api import memory as memory_api
from thinkback.memory.backends import FakeMemoryBackend
from thinkback.memory.repositories import InMemoryMemoryRepository
from thinkback.memory.schemas import ListMemoryTypesResponse
from thinkback.memory.service import MemoryService
from thinkback.rpc import memory_pb2 as pb
from thinkback.rpc import memory_pb2_grpc
from thinkback.rpc.servicer import MemoryServicer

DISALLOWED = "acme"
ALLOWED = "beta"


class _ServiceMustNotBeCalled:
    """任何业务方法被调用都说明守卫没生效。

    生命周期方法（排空队列 / 关 executor）是应用关停时调用的，与守卫无关，
    放行它们 —— 否则会在 teardown 里炸，把断言盖掉。
    """

    def drain_l3_background_tasks(self, timeout: float | None = None) -> None:
        return None

    def shutdown_l3_executor(self, *, wait: bool = True) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        def _boom(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError(
                f"service.{name} must not be reached for a namespace this deployment does not serve"
            )

        return _boom


class _TypesOnlyService:
    """只够回答「类型清单」这一个端点的最小服务。"""

    def list_memory_types(self, namespace: str) -> ListMemoryTypesResponse:
        return ListMemoryTypesResponse(
            status="ok",
            namespace=namespace,
            policy_version="1",
            types=[],
        )

    def drain_l3_background_tasks(self, timeout: float | None = None) -> None:
        return None

    def shutdown_l3_executor(self, *, wait: bool = True) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        def _boom(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError(f"service.{name} is not expected in this test")

        return _boom


def _event_body(namespace: str) -> dict[str, Any]:
    return {
        "request_id": "req-1",
        "user_id": "u1",
        "session_id": "s1",
        "namespace": namespace,
        "event": {
            "event_id": "ev-1",
            "source": "chat_round",
            "role": "user",
            "payload": "预算 15 万",
            "occurred_at": "2026-05-04T10:00:03Z",
        },
    }


def _delete_body(namespace: str) -> dict[str, Any]:
    return {
        "request_id": "req-1",
        "user_id": "u1",
        "scope": "all",
        "operation_id": "op-1",
        "namespace": namespace,
    }


def _update_body(namespace: str) -> dict[str, Any]:
    return {
        "request_id": "req-1",
        "user_id": "u1",
        "operation_id": "op-1",
        "memory_id": "m-1",
        "content": "预算 20 万",
        "namespace": namespace,
    }


def _recall_body(namespace: str) -> dict[str, Any]:
    return {
        "user_id": "u1",
        "session_id": "s1",
        "query": "预算",
        "namespace": namespace,
    }


#: 每一个带 namespace 的业务端点（读 3 / 写 6）。少一条就是少一个守卫落点。
NAMESPACE_BEARING_ROUTES: list[tuple[str, str, dict[str, Any]]] = [
    ("POST", "/v2/memory/events", {"json": _event_body(DISALLOWED)}),
    ("POST", "/v2/memory/recall", {"json": _recall_body(DISALLOWED)}),
    ("POST", "/v2/memory/delete", {"json": _delete_body(DISALLOWED)}),
    ("POST", "/v2/memory/suppress", {"json": _delete_body(DISALLOWED)}),
    ("POST", "/v2/memory/erase", {"json": _delete_body(DISALLOWED)}),
    ("POST", "/v2/memory/update", {"json": _update_body(DISALLOWED)}),
    ("GET", "/v2/memory/types", {"params": {"namespace": DISALLOWED}}),
    ("GET", "/v2/memory/items", {"params": {"user_id": "u1", "namespace": DISALLOWED}}),
    (
        "GET",
        "/v2/memory/items/m-1",
        {"params": {"user_id": "u1", "namespace": DISALLOWED}},
    ),
]


@pytest.fixture()
def allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    """本部署只服务 ``beta``。"""

    monkeypatch.setattr(memory_api.settings, "memory_allowed_namespaces", ALLOWED)


@pytest.mark.parametrize(("method", "path", "kwargs"), NAMESPACE_BEARING_ROUTES)
def test_http_rejects_a_namespace_this_deployment_does_not_serve(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    allowlist: None,
    method: str,
    path: str,
    kwargs: dict[str, Any],
) -> None:
    """名单外的 namespace 在每个业务端点上都 403。"""

    monkeypatch.setattr("thinkback.api.dependencies._memory_service", _ServiceMustNotBeCalled())

    response = client.request(method, path, **kwargs)

    assert response.status_code == 403, response.text
    assert DISALLOWED in response.text


def test_http_serves_a_namespace_on_the_allowlist(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, allowlist: None
) -> None:
    """名单内的 namespace 照常走业务逻辑。"""

    monkeypatch.setattr("thinkback.api.dependencies._memory_service", _TypesOnlyService())

    response = client.get("/v2/memory/types", params={"namespace": ALLOWED})

    assert response.status_code == 200, response.text
    assert response.json()["namespace"] == ALLOWED


def test_http_default_namespace_is_rejected_when_not_on_the_allowlist(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, allowlist: None
) -> None:
    """请求不带 namespace 时落默认作用域 —— 默认作用域不在名单里也要拒。

    这是最容易漏的一条：调用方不传 namespace，契约给的是默认作用域，
    守卫必须按同一个口径判，而不是「没传就不判」。
    """

    monkeypatch.setattr("thinkback.api.dependencies._memory_service", _ServiceMustNotBeCalled())

    response = client.get("/v2/memory/types")

    assert response.status_code == 403, response.text


def test_http_without_allowlist_serves_every_namespace(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没设白名单 = 不限制（保持内网免鉴权下的既有行为）。"""

    monkeypatch.setattr(memory_api.settings, "memory_allowed_namespaces", "")
    monkeypatch.setattr("thinkback.api.dependencies._memory_service", _TypesOnlyService())

    response = client.get("/v2/memory/types", params={"namespace": DISALLOWED})

    assert response.status_code == 200, response.text


@pytest.fixture()
def grpc_stub():
    from concurrent.futures import ThreadPoolExecutor

    svc = MemoryService(
        repository=InMemoryMemoryRepository(),
        backend=FakeMemoryBackend(),
        l3_write_mode="sync",
    )
    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    memory_pb2_grpc.add_MemoryServiceServicer_to_server(MemoryServicer(svc), server)  # type: ignore[arg-type]
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    channel = grpc.insecure_channel(
        f"127.0.0.1:{port}",
        options=[("grpc.enable_http_proxy", 0)],
    )
    try:
        yield memory_pb2_grpc.MemoryServiceStub(channel)  # type: ignore[arg-type]
    finally:
        channel.close()
        server.stop(grace=0)


def test_grpc_rejects_a_namespace_this_deployment_does_not_serve(
    grpc_stub: Any, allowlist: None
) -> None:
    """gRPC 面同义地给 PERMISSION_DENIED（HTTP 面是 403）。"""

    with pytest.raises(grpc.RpcError) as excinfo:
        grpc_stub.AppendEvent(
            pb.AppendEventRequest(
                request_id="req-1",
                user_id="u1",
                session_id="s1",
                namespace=DISALLOWED,
                event=pb.SourceEvent(
                    event_id="ev-1",
                    source="chat_round",
                    role="MESSAGE_ROLE_USER",
                    payload="预算 15 万",
                    occurred_at="2026-05-04T10:00:03Z",
                ),
            )
        )

    assert excinfo.value.code() == grpc.StatusCode.PERMISSION_DENIED
    assert DISALLOWED in (excinfo.value.details() or "")


def test_grpc_serves_a_namespace_on_the_allowlist(grpc_stub: Any, allowlist: None) -> None:
    """名单内的 namespace 照常走业务逻辑。"""

    response = grpc_stub.ListMemoryTypes(pb.ListMemoryTypesRequest(namespace=ALLOWED))

    assert response.namespace == ALLOWED


def test_mcp_refuses_to_serve_a_namespace_this_deployment_does_not_serve(
    allowlist: None,
) -> None:
    """MCP 面的 namespace 是服务端绑定的 —— 绑定时就该拒开。"""

    from thinkback.mcp.server import create_mcp_server

    service = MemoryService(
        repository=InMemoryMemoryRepository(),
        backend=FakeMemoryBackend(),
        l3_write_mode="sync",
    )

    with pytest.raises(ValueError) as excinfo:
        create_mcp_server(service, namespace=DISALLOWED)

    assert "fail-closed" in str(excinfo.value)
    assert DISALLOWED in str(excinfo.value)


def test_mcp_serves_a_namespace_on_the_allowlist(allowlist: None) -> None:
    from thinkback.mcp.server import create_mcp_server

    service = MemoryService(
        repository=InMemoryMemoryRepository(),
        backend=FakeMemoryBackend(),
        l3_write_mode="sync",
    )

    server = create_mcp_server(service, namespace=ALLOWED)

    assert server is not None


def test_mcp_cli_refuses_to_start_on_an_unserved_namespace(
    monkeypatch: pytest.MonkeyPatch, allowlist: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI 入口在碰数据层之前就退出（退出码 2，与已有的非回环拒绝同义）。"""

    from thinkback.mcp import __main__ as mcp_main

    exit_code = mcp_main.main(["--namespace", DISALLOWED])

    assert exit_code == 2
    assert DISALLOWED in capsys.readouterr().err


# ── Settings 层的解析与提示 ────────────────────────────────────────────────


def test_allowlist_is_parsed_into_a_namespace_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """逗号分隔、空白容忍、空串 = 不限制。"""

    monkeypatch.setattr(memory_api.settings, "memory_allowed_namespaces", " beta , acme ")
    assert memory_api.settings.memory_allowed_namespaces_list == ["beta", "acme"]

    monkeypatch.setattr(memory_api.settings, "memory_allowed_namespaces", "")
    assert memory_api.settings.memory_allowed_namespaces_list == []


def test_require_namespace_is_fail_closed_and_names_the_namespace() -> None:
    """被拒时抛的错必须带 ``fail-closed`` 前缀 —— HTTP/gRPC 都靠它映射状态码。"""

    import pytest as _pytest

    from thinkback.infra.config import get_settings

    settings = get_settings()
    original = settings.memory_allowed_namespaces
    try:
        settings.memory_allowed_namespaces = "beta"
        settings.require_namespace("beta")  # 名单内 → 不抛
        with _pytest.raises(ValueError) as excinfo:
            settings.require_namespace("acme")
    finally:
        settings.memory_allowed_namespaces = original

    assert "fail-closed" in str(excinfo.value)
    assert "acme" in str(excinfo.value)


def test_namespace_boundary_warning_only_appears_in_production_without_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没设白名单的生产部署必须被提醒：本服务不鉴权，namespace 不是边界。"""

    monkeypatch.setattr(memory_api.settings, "environment", "production")
    monkeypatch.setattr(memory_api.settings, "memory_allowed_namespaces", "")

    warning = memory_api.settings.memory_namespace_boundary_warning

    assert warning is not None
    assert "MEMORY_ALLOWED_NAMESPACES" in warning


def test_namespace_boundary_warning_is_silent_when_it_does_not_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """开发环境不吵；设了白名单也不吵（边界已经成立）。"""

    monkeypatch.setattr(memory_api.settings, "environment", "development")
    monkeypatch.setattr(memory_api.settings, "memory_allowed_namespaces", "")
    assert memory_api.settings.memory_namespace_boundary_warning is None

    monkeypatch.setattr(memory_api.settings, "environment", "production")
    monkeypatch.setattr(memory_api.settings, "memory_allowed_namespaces", "beta")
    assert memory_api.settings.memory_namespace_boundary_warning is None
