"""/v2/memory 业务面的缺口行为：suppress 路由、未分类异常、召回失败与 404。

这些是接入协议里调用方真的会撞上的分支：

- suppress 路由：逻辑下架后不可再列出（与 delete 的语义差异要测得出来）；
- 写路径的未分类异常：必须落成标准化 500（TB-2001），不能把裸 traceback
  回给调用方，也不能静默成功；
- recall 失败：走统一映射（memory backend → 502），失败事件入指标；
- 单条记忆 / 任务查不到：404 与错误码 TB-1004，客户端按错误分支处理。
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from thinkback.api.app import create_app
from thinkback.memory.backends import FakeMemoryBackend
from thinkback.memory.repositories import InMemoryMemoryRepository
from thinkback.memory.service import MemoryService


def real_service() -> MemoryService:
    return MemoryService(
        repository=InMemoryMemoryRepository(),
        backend=FakeMemoryBackend(),
        l3_write_mode="sync",
    )


def append_payload() -> dict[str, Any]:
    return {
        "request_id": "req-gap-1",
        "user_id": "user-1",
        "session_id": "session-1",
        "event": {
            "event_id": "round-gap-1",
            "source": "chat_round",
            "role": "user",
            "payload": "我负责 3nm 工艺整合",
            "occurred_at": "2026-05-04T10:00:03Z",
        },
    }


def _make_client(monkeypatch, service: Any, *, raise_server_exceptions: bool = True) -> TestClient:
    monkeypatch.setattr("thinkback.api.dependencies._memory_service", service)
    return TestClient(
        create_app(run_startup_tasks=False),
        raise_server_exceptions=raise_server_exceptions,
    )


class _UnexpectedFailureService:
    """写路径抛出映射表之外的异常：未知异常必须留下服务端日志并 500。"""

    def append(self, _request: Any) -> Any:
        raise KeyError("unexpected explosion")

    def drain_l3_background_tasks(self, timeout: float | None = None) -> None:
        _ = timeout

    def shutdown_l3_executor(self, *, wait: bool = True) -> None:
        _ = wait


class _FailingRecallService:
    """召回路径的后端故障：memory backend → 502。"""

    def recall(self, _request: Any) -> Any:
        raise RuntimeError("memory backend offline: provider refused")

    def drain_l3_background_tasks(self, timeout: float | None = None) -> None:
        _ = timeout

    def shutdown_l3_executor(self, *, wait: bool = True) -> None:
        _ = wait


def test_append_with_unclassified_error_returns_standard_500(monkeypatch) -> None:
    """映射表之外的异常 → 标准化 500 TB-2001，不回显内部细节。"""
    client = _make_client(monkeypatch, _UnexpectedFailureService(), raise_server_exceptions=False)

    with client:
        response = client.post("/v2/memory/events", json=append_payload())

    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "TB-2001"
    assert body["error"]["message"] == "Internal server error"


def test_recall_backend_failure_maps_to_502(client, monkeypatch) -> None:
    """召回撞上后端故障 → 502（上游依赖故障），失败路径照常计数。"""
    monkeypatch.setattr("thinkback.api.dependencies._memory_service", _FailingRecallService())

    response = client.post(
        "/v2/memory/recall",
        json={
            "user_id": "user-1",
            "session_id": "session-1",
            "query": "工艺整合",
        },
    )

    assert response.status_code == 502
    assert "memory backend" in response.json()["detail"]


def test_suppress_hides_memory_from_default_listing(client, monkeypatch) -> None:
    """POST /v2/memory/suppress：逻辑下架后默认列表不可见（原文保留供审计）。"""
    service = real_service()
    monkeypatch.setattr("thinkback.api.dependencies._memory_service", service)
    assert client.post("/v2/memory/events", json=append_payload()).status_code == 200
    memory_id = next(iter(service.repository.memories))

    response = client.post(
        "/v2/memory/suppress",
        json={
            "request_id": "suppress-1",
            "user_id": "user-1",
            "scope": "memory",
            "operation_id": "suppress-op-1",
            "memory_id": memory_id,
        },
    )

    assert response.status_code == 200
    assert response.json()["task_id"]
    listed = client.get("/v2/memory/items", params={"user_id": "user-1"}).json()
    assert all(item["memory_id"] != memory_id for item in listed["items"])


def test_get_missing_memory_item_returns_404(client, monkeypatch) -> None:
    monkeypatch.setattr("thinkback.api.dependencies._memory_service", real_service())

    response = client.get("/v2/memory/items/m-does-not-exist", params={"user_id": "user-1"})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "TB-1004"


def test_get_missing_task_returns_404(client, monkeypatch) -> None:
    monkeypatch.setattr("thinkback.api.dependencies._memory_service", real_service())

    response = client.get("/v2/memory/tasks/task-does-not-exist")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "TB-1004"
