from memory.backends import FakeMemoryBackend
from memory.repositories import InMemoryMemoryRepository
from memory.service import MemoryService


class FailingProviderBackend(FakeMemoryBackend):
    def add(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("mem0 library add failed: 502 provider_bad_request")


class SaturatedMemoryService:
    def append(self, _request):  # type: ignore[no-untyped-def]
        raise RuntimeError("l3 background queue full: pending=1 max=1")


class StatusMemoryService:
    def l3_background_status(self) -> dict:
        return {
            "write_mode": "async",
            "executor_workers": 2,
            "max_pending_tasks": 64,
            "pending_write_tasks": 7,
            "cleanup_tasks": 1,
            "available_capacity": 56,
        }


def append_payload() -> dict:
    return {
        "request_id": "req-r1",
        "user_id": "user-1",
        "character_id": "char-1",
        "session_id": "session-1",
        "round_id": "round-1",
        "messages": [
            {
                "message_id": "m1",
                "role": "user",
                "content": "我不喜欢被催睡觉",
                "timestamp": "2026-05-04T10:00:00Z",
            },
            {
                "message_id": "m2",
                "role": "assistant",
                "content": "我会记住这个边界。",
                "timestamp": "2026-05-04T10:00:03Z",
            },
        ],
        "source_timestamp": "2026-05-04T10:00:03Z",
    }


def test_append_recall_delete_rebuild_task_api(client, monkeypatch) -> None:
    service = MemoryService(repository=InMemoryMemoryRepository(), backend=FakeMemoryBackend())
    monkeypatch.setattr("api.dependencies._memory_service", service)

    append_response = client.post("/memory/append", json=append_payload())

    assert append_response.status_code == 200
    assert append_response.json()["status"] == "completed"

    recall_response = client.post(
        "/memory/recall",
        json={
            "user_id": "user-1",
            "character_id": "char-1",
            "session_id": "session-1",
            "query": "睡觉偏好",
            "intent": "preference",
        },
    )

    assert recall_response.status_code == 200
    assert any(item["layer"] == "L3" for item in recall_response.json()["items"])

    memory_id = next(iter(service.repository.memories))
    delete_response = client.post(
        "/memory/delete",
        json={
            "request_id": "delete-1",
            "user_id": "user-1",
            "character_id": "char-1",
            "scope": "memory",
            "operation_id": "delete-op-1",
            "memory_id": memory_id,
        },
    )

    assert delete_response.status_code == 200
    assert delete_response.json()["summary_state"] == "dirty"

    rebuild_response = client.post(
        "/memory/rebuild",
        json={
            "request_id": "rebuild-1",
            "user_id": "user-1",
            "character_id": "char-1",
            "operation_id": "rebuild-op-1",
        },
    )

    assert rebuild_response.status_code == 200
    task_id = rebuild_response.json()["task_id"]
    task_response = client.get(f"/memory/tasks/{task_id}")

    assert task_response.status_code == 200
    assert task_response.json()["status"] == "completed"


def test_append_round_conflict_returns_409(client, monkeypatch) -> None:
    service = MemoryService(repository=InMemoryMemoryRepository(), backend=FakeMemoryBackend())
    monkeypatch.setattr("api.dependencies._memory_service", service)
    payload = append_payload()
    assert client.post("/memory/append", json=payload).status_code == 200
    payload["messages"][0]["content"] = "我喜欢被提醒早睡"

    response = client.post("/memory/append", json=payload)

    assert response.status_code == 409
    assert "round_id conflict" in response.json()["detail"]


def test_delete_missing_memory_id_returns_400(client, monkeypatch) -> None:
    service = MemoryService(repository=InMemoryMemoryRepository(), backend=FakeMemoryBackend())
    monkeypatch.setattr("api.dependencies._memory_service", service)

    response = client.post(
        "/memory/delete",
        json={
            "request_id": "delete-1",
            "user_id": "user-1",
            "character_id": "char-1",
            "scope": "memory",
            "operation_id": "delete-op-1",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "memory_id is required for memory deletion"


def test_privacy_recall_fail_closed_returns_403(client, monkeypatch) -> None:
    service = MemoryService(repository=InMemoryMemoryRepository(), backend=FakeMemoryBackend())
    monkeypatch.setattr("api.dependencies._memory_service", service)

    response = client.post(
        "/memory/recall",
        json={
            "user_id": "user-1",
            "character_id": "char-1",
            "session_id": "session-1",
            "query": "你记住了我的哪些隐私",
            "intent": "privacy",
        },
    )

    assert response.status_code == 403
    assert "fail-closed" in response.json()["detail"]


def test_mem0_provider_failure_returns_502_not_unhandled_500(client, monkeypatch) -> None:
    service = MemoryService(repository=InMemoryMemoryRepository(), backend=FailingProviderBackend())
    monkeypatch.setattr("api.dependencies._memory_service", service)

    response = client.post("/memory/append", json=append_payload())

    assert response.status_code == 502
    assert "mem0 library add failed" in response.json()["detail"]


def test_l3_background_queue_saturation_returns_503(client, monkeypatch) -> None:
    monkeypatch.setattr("api.dependencies._memory_service", SaturatedMemoryService())

    response = client.post("/memory/append", json=append_payload())

    assert response.status_code == 503
    assert "l3 background queue full" in response.json()["detail"]


def test_l3_background_status_route_returns_queue_state(client, monkeypatch) -> None:
    monkeypatch.setattr("api.dependencies._memory_service", StatusMemoryService())

    response = client.get("/memory/l3/background-status")

    assert response.status_code == 200
    assert response.json() == {
        "write_mode": "async",
        "executor_workers": 2,
        "max_pending_tasks": 64,
        "pending_write_tasks": 7,
        "cleanup_tasks": 1,
        "available_capacity": 56,
    }
