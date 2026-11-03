"""治理台 API 的容错与错误映射路径。

这些分支的共同点：出问题时治理台**不能整个红掉**，也不能把内部异常裸奔成 500。
每个用例都钉一条可观察契约：

- overview 的两个 Top-K 聚合源坏了 → 空列表降级，不是 500；
- 记忆状态过滤值非法 → 400 早失败；
- 治理操作的业务异常走统一映射（ValueError→4xx），未分类异常保留 traceback；
- health/detail 的 alembic 探针：查得到 → 版本号；查不到 → None，都不挡渲染。
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from thinkback.api.admin import router as admin_router
from thinkback.api.dependencies import get_memory_service
from thinkback.domain.entities import AdminAuditEntry
from thinkback.memory.backends import FakeMemoryBackend
from thinkback.memory.repositories import InMemoryMemoryRepository
from thinkback.memory.service import MemoryService


def build_client(service: MemoryService | Any) -> TestClient:
    app = FastAPI()
    app.include_router(admin_router)
    app.dependency_overrides[get_memory_service] = lambda: service
    return TestClient(app)


def real_service(repository: InMemoryMemoryRepository | None = None) -> MemoryService:
    return MemoryService(
        repository=repository or InMemoryMemoryRepository(),
        backend=FakeMemoryBackend(),
        l3_write_mode="sync",
    )


# ---------------------------------------------------------------------------
# GET /admin/api/audit：action 缺省 = 不过滤
# ---------------------------------------------------------------------------


def test_audit_listing_without_action_param_returns_unfiltered_entries() -> None:
    """不传 action 也是合法请求：返回全部审计（repository 收到 action=None）。"""
    service = real_service()
    service.repository.save_admin_audit(
        AdminAuditEntry(
            audit_id="audit-1",
            created_at=datetime.now(UTC),
            operator="admin",
            action="delete",
            target="user-1",
            detail={},
        )
    )
    client = build_client(service)

    response = client.get("/admin/api/audit")

    assert response.status_code == 200
    entries = response.json()
    assert [e["action"] for e in entries] == ["delete"]


# ---------------------------------------------------------------------------
# GET /admin/api/overview：聚合源故障 → 空列表降级
# ---------------------------------------------------------------------------


class _BrokenTaskListingRepository(InMemoryMemoryRepository):
    """任务列表查询挂掉（overview 的 Top-5 失败任务聚合源）。"""

    def list_tasks(self, **kwargs: Any) -> list[Any]:
        raise RuntimeError("task store unavailable")


class _BrokenAuditListingRepository(InMemoryMemoryRepository):
    """审计列表查询挂掉（overview 的最近审计聚合源）。"""

    def list_admin_audit(self, **kwargs: Any) -> list[Any]:
        raise RuntimeError("audit store unavailable")


def test_overview_degrades_to_empty_failed_tasks_when_task_listing_breaks() -> None:
    """失败任务聚合源坏了 → recent_failed_tasks 空列表，Overview 不红。"""
    client = build_client(real_service(_BrokenTaskListingRepository()))

    response = client.get("/admin/api/overview")

    assert response.status_code == 200
    assert response.json()["recent_failed_tasks"] == []


def test_overview_degrades_to_empty_audit_when_audit_listing_breaks() -> None:
    """审计聚合源坏了 → recent_audit_actions 空列表，Overview 不红。"""
    client = build_client(real_service(_BrokenAuditListingRepository()))

    response = client.get("/admin/api/overview")

    assert response.status_code == 200
    assert response.json()["recent_audit_actions"] == []


# ---------------------------------------------------------------------------
# GET /admin/api/memories：非法状态值 400
# ---------------------------------------------------------------------------


def test_memories_listing_rejects_unknown_status_with_400() -> None:
    client = build_client(real_service())

    response = client.get("/admin/api/memories", params={"statuses": "ACTIVE,BOGUS"})

    assert response.status_code == 400
    assert "invalid memory status" in response.json()["detail"]


def test_memories_listing_accepts_valid_statuses_case_insensitively() -> None:
    """合法状态串（小写也要归一成大写）→ 正常过滤返回，不是 400。"""
    service = real_service()
    service.repository.add_memory_index(
        backend_memory_id="b-active",
        user_id="user-1",
        source_refs=[{"session_id": "s1", "round_id": "r1"}],
        memory_text="User likes coffee",
    )
    client = build_client(service)

    response = client.get("/admin/api/memories", params={"statuses": "active"})

    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["memory_text"] for item in items] == ["User likes coffee"]


# ---------------------------------------------------------------------------
# 治理操作异常映射：分类异常 → 4xx；未分类异常 → 保留 traceback 上抛
# ---------------------------------------------------------------------------


class _ExplodingDeleteService:
    """只有 delete 一个能力、且必炸的治理面替身。"""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def delete(self, _request: Any) -> Any:
        raise self._error


def _delete_payload() -> dict[str, Any]:
    return {
        "request_id": "req-admin-err",
        "user_id": "user-1",
        "scope": "memory",
        "operation_id": "op-admin-err",
        "memory_id": "m-missing",
    }


def test_admin_delete_maps_domain_not_found_to_404() -> None:
    """service 的 ValueError(not found) 必须走统一映射成 404，不是 500。"""
    client = build_client(_ExplodingDeleteService(ValueError("memory not found: m-missing")))

    response = client.post("/admin/api/delete", json=_delete_payload())

    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


def test_admin_delete_reraises_unclassified_error_with_traceback() -> None:
    """映射表覆盖不了的异常必须原样上抛（日志里带 traceback），不能吞成 200。"""
    client = build_client(_ExplodingDeleteService(KeyError("boom")))

    with pytest.raises(KeyError, match="boom"):
        client.post("/admin/api/delete", json=_delete_payload())


# ---------------------------------------------------------------------------
# GET /admin/api/health/detail：alembic 探针的成功 / 失败两条路
# ---------------------------------------------------------------------------


class _FakeAlembicConnection:
    def __init__(self, *, version: str | None = None, error: Exception | None = None) -> None:
        self._version = version
        self._error = error

    async def __aenter__(self) -> _FakeAlembicConnection:
        if self._error is not None:
            raise self._error
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def execute(self, _sql: Any) -> Any:
        version = self._version
        return SimpleNamespace(scalar_one_or_none=lambda: version)


class _FakeReadinessEngine:
    def __init__(self, connection: _FakeAlembicConnection) -> None:
        self._connection = connection

    def connect(self) -> _FakeAlembicConnection:
        return self._connection


def test_health_detail_reports_alembic_version_when_probe_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """探针查到版本号 → 原样上屏，治理台能看到 schema 是否对齐。"""
    fake = _FakeReadinessEngine(_FakeAlembicConnection(version="a1b2c3d4"))
    monkeypatch.setattr("thinkback.infra.database.engine.readiness_engine", fake)
    client = build_client(real_service())

    response = client.get("/admin/api/health/detail")

    assert response.status_code == 200
    assert response.json()["alembic_current"] == "a1b2c3d4"


def test_health_detail_keeps_rendering_when_alembic_probe_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """探针失败（迁移没跑 / 表不存在）→ alembic_current=None，不能挡整个卡片。"""
    fake = _FakeReadinessEngine(_FakeAlembicConnection(error=RuntimeError("no such table")))
    monkeypatch.setattr("thinkback.infra.database.engine.readiness_engine", fake)
    client = build_client(real_service())

    response = client.get("/admin/api/health/detail")

    assert response.status_code == 200
    assert response.json()["alembic_current"] is None
