"""D1 接入层身份绑定：受信 user 头（HTTP）与受信 metadata（gRPC）。

内网免鉴权（D10）下 user_id 由调用方自报 —— 接到服务上的任何调用方都能
读写任意用户。插件形态（任何企业 AI 助手可接入）要求把边界从「网络」挪到
「接入层的一个字段」：网关注入受信头，服务校验自报值与之一致。

三面同一语义（MCP 面是启动参数 ``--user-id``，见 test_mcp_server.py）：
- 头缺失 → 403 / PERMISSION_DENIED（配置承诺了绑定，缺失 = 网关没配对）；
- 自报 user_id 与头不一致 → 403 / PERMISSION_DENIED；
- 一致 → 正常放行；
- 未配置（默认）→ 中间件不挂 / 守卫直通，行为与从前一致。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import grpc
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from thinkback.api.trusted_user import TrustedUserHeaderMiddleware


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(TrustedUserHeaderMiddleware, header_name="x-user-id")

    @app.post("/v2/memory/append")
    async def append(request: Request) -> dict[str, bool]:
        # 不解析 body：这个文件测的是中间件的行为（放行 / 403），
        # 端点解析与否属于 FastAPI 自己的职责。
        return {"ok": True}

    @app.get("/v2/memory/items")
    async def items(user_id: str = "") -> dict[str, str | bool]:
        return {"ok": True, "user_id": user_id}

    return app


def test_missing_trusted_header_is_rejected_fail_closed() -> None:
    """配置承诺了绑定而头没来：网关没配对，403 而不是放行。"""
    with TestClient(_app()) as client:
        response = client.post("/v2/memory/append", json={"user_id": "u1", "payload": "hi"})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "TB-1003"


def test_claimed_user_id_must_match_the_trusted_header() -> None:
    """助手自报别人时响亮拒绝：静默改写会掩盖「以为在写 A 实际写了 B」。"""
    with TestClient(_app()) as client:
        ok = client.post(
            "/v2/memory/append",
            json={"user_id": "u1", "payload": "hi"},
            headers={"x-user-id": "u1"},
        )
        rejected = client.post(
            "/v2/memory/append",
            json={"user_id": "someone-else", "payload": "hi"},
            headers={"x-user-id": "u1"},
        )

    assert ok.status_code == 200
    assert rejected.status_code == 403
    assert "does not match" in rejected.json()["error"]["message"]


def test_get_requests_verify_the_query_user_id() -> None:
    """GET 的 user_id 在查询串里，同样要过校验。"""
    with TestClient(_app()) as client:
        ok = client.get("/v2/memory/items", params={"user_id": "u1"}, headers={"x-user-id": "u1"})
        rejected = client.get(
            "/v2/memory/items", params={"user_id": "u2"}, headers={"x-user-id": "u1"}
        )

    assert ok.status_code == 200
    assert rejected.status_code == 403


def test_malformed_body_falls_back_to_the_header_only() -> None:
    """非 JSON 请求体不该炸中间件：按未自报处理，端点正常收到请求。"""
    with TestClient(_app()) as client:
        response = client.post(
            "/v2/memory/append",
            content=b"\xff\xfe not json",
            headers={"x-user-id": "u1"},
        )

    # 中间件放行（没有 403 / 500），端点照常 200。
    assert response.status_code == 200


def test_app_wires_the_middleware_only_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """未配置（默认）不挂中间件：D10 内网前提下的行为一字不变。"""
    from thinkback.api.app import create_app
    from thinkback.infra.config import settings

    monkeypatch.setattr(settings, "trusted_user_header", "")
    app_plain = create_app()
    assert not any(
        middleware.cls is TrustedUserHeaderMiddleware for middleware in app_plain.user_middleware
    )

    monkeypatch.setattr(settings, "trusted_user_header", "x-user-id")
    app_bound = create_app()
    assert any(
        middleware.cls is TrustedUserHeaderMiddleware for middleware in app_bound.user_middleware
    )


# ---------------------------------------------------------------------------
# gRPC 面
# ---------------------------------------------------------------------------


@pytest.fixture
def trusted_stub(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """启了受信头校验的进程内 gRPC 服务（function 级，别污染共享 fixture）。"""
    from thinkback.infra.config import settings
    from thinkback.memory.backends import FakeMemoryBackend
    from thinkback.memory.repositories import InMemoryMemoryRepository
    from thinkback.memory.service import MemoryService
    from thinkback.rpc import memory_pb2_grpc
    from thinkback.rpc.servicer import MemoryServicer

    monkeypatch.setattr(settings, "trusted_user_header", "x-user-id")

    svc = MemoryService(
        repository=InMemoryMemoryRepository(),
        backend=FakeMemoryBackend(),
        l3_write_mode="sync",
    )
    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    memory_pb2_grpc.add_MemoryServiceServicer_to_server(MemoryServicer(svc), server)  # type: ignore[no-untyped-call]
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    channel = grpc.insecure_channel(f"127.0.0.1:{port}", options=[("grpc.enable_http_proxy", 0)])
    yield memory_pb2_grpc.MemoryServiceStub(channel)  # type: ignore[no-untyped-call]
    channel.close()
    server.stop(grace=0)


def _grpc_append(user_id: str) -> object:
    from thinkback.rpc import memory_pb2 as pb

    return pb.AppendEventRequest(
        request_id="req-1",
        user_id=user_id,
        session_id="s1",
        event=pb.SourceEvent(
            event_id="event-1",
            source="chat_round",
            role=pb.MESSAGE_ROLE_USER,
            payload="hello",
            occurred_at="2026-01-01T00:00:00+00:00",
        ),
    )


def test_grpc_rejects_a_user_id_that_does_not_match_trusted_metadata(
    trusted_stub: object,
) -> None:
    """gRPC 面同一语义：metadata 与自报 user_id 不一致 → PERMISSION_DENIED。"""
    with pytest.raises(grpc.RpcError) as excinfo:
        trusted_stub.AppendEvent(  # type: ignore[attr-defined]
            _grpc_append("someone-else"), metadata=(("x-user-id", "u1"),)
        )

    assert excinfo.value.code() == grpc.StatusCode.PERMISSION_DENIED


def test_grpc_requires_the_trusted_metadata_when_configured(trusted_stub: object) -> None:
    """配置了绑定而 metadata 没带：网关没配对，fail-closed。"""
    with pytest.raises(grpc.RpcError) as excinfo:
        trusted_stub.AppendEvent(_grpc_append("u1"))  # type: ignore[attr-defined]

    assert excinfo.value.code() == grpc.StatusCode.PERMISSION_DENIED


def test_grpc_allows_matching_metadata(trusted_stub: object) -> None:
    """一致时正常放行 —— 绑定不是把面堵死。"""
    response = trusted_stub.AppendEvent(  # type: ignore[attr-defined]
        _grpc_append("u1"), metadata=(("x-user-id", "u1"),)
    )

    assert response.status == "completed"


def test_settings_parse_the_trusted_user_header_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from thinkback.infra.config import Settings

    monkeypatch.setenv("TRUSTED_USER_HEADER", "X-User-Id")
    assert Settings().trusted_user_header == "X-User-Id"
