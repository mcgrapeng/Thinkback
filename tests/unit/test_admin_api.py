"""治理台 M1 API 契约测试：/admin/api/overview + /admin/api/tasks。

覆盖：聚合形状、任务过滤/分页、非法参数早失败、Bearer token 开关。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from thinkback.api.admin import router as admin_router
from thinkback.api.dependencies import get_memory_service
from thinkback.domain.entities import TaskEntry
from thinkback.domain.enums import OperationType, TaskStatus
from thinkback.infra import config
from thinkback.memory.backends import FakeMemoryBackend
from thinkback.memory.repositories import InMemoryMemoryRepository
from thinkback.memory.schemas import AppendMemoryRequest
from thinkback.memory.service import MemoryService


def make_task(task_id: str, *, status: TaskStatus) -> TaskEntry:
    return TaskEntry(
        task_id=task_id,
        request_id=f"req-{task_id}",
        op_type=OperationType.DELETE_MEMORY,
        scope={},
        status=status,
        operation_id=f"op-{task_id}",
        result={},
    )


def build_client(**service_kwargs: Any) -> tuple[TestClient, MemoryService]:
    service = MemoryService(
        repository=InMemoryMemoryRepository(),
        backend=FakeMemoryBackend(),
        **service_kwargs,
    )
    app = FastAPI()
    app.include_router(admin_router)
    app.dependency_overrides[get_memory_service] = lambda: service
    return TestClient(app), service


def seed(service: MemoryService) -> None:
    repository = service.repository
    assert isinstance(repository, InMemoryMemoryRepository)
    repository.claim_task(make_task("t-running", status=TaskStatus.RUNNING))
    repository.claim_task(make_task("t-failed", status=TaskStatus.FAILED))
    repository.claim_task(make_task("t-dead", status=TaskStatus.DEAD_LETTER))
    repository.add_memory_index(
        backend_memory_id="b-1",
        user_id="user-1",
        source_refs=[{"session_id": "s1", "round_id": "r1"}],
        memory_text="User likes coffee",
    )
    repository.add_memory_index(
        backend_memory_id="b-2",
        user_id="user-1",
        source_refs=[{"session_id": "s1", "round_id": "r2"}],
        memory_text="User moved to Shanghai",
    )


def test_overview_aggregates_memories_tasks_and_l3() -> None:
    client, service = build_client()
    seed(service)

    response = client.get("/admin/api/overview")

    assert response.status_code == 200
    body = response.json()
    assert body["memories"] == {"ACTIVE": 2}
    assert body["tasks"] == {"running": 1, "failed": 1, "dead_letter": 1}
    assert set(body["l3"]) == {
        "write_mode",
        "executor_workers",
        "max_pending_tasks",
        "pending_write_tasks",
        "cleanup_tasks",
        "available_capacity",
    }
    # P0-1+2: 扩展字段用于「需关注」面板与「最近治理动作」卡片
    assert "recent_failed_tasks" in body
    assert isinstance(body["recent_failed_tasks"], list)
    assert "recent_audit_actions" in body
    assert isinstance(body["recent_audit_actions"], list)


def test_admin_overview_failed_tasks_have_truncated_last_error() -> None:
    """Overview 「需关注」面板的失败任务 last_error 截断 200 字符，避免撑爆卡片。"""
    client, service = build_client()
    long_error = "X" * 500
    service.repository.save_task(
        TaskEntry(
            task_id="memory-extract:longfail",
            request_id="r1",
            op_type=OperationType.WRITE_ROUND,
            scope={"user_id": "user-1"},
            status=TaskStatus.FAILED,
            retry_count=3,
            last_error=long_error,
        )
    )
    body = client.get("/admin/api/overview").json()
    failed = body["recent_failed_tasks"]
    assert any(t["task_id"] == "memory-extract:longfail" for t in failed)
    task = next(t for t in failed if t["task_id"] == "memory-extract:longfail")
    assert len(task["last_error"]) == 200


def test_admin_overview_includes_by_classification_source_type_and_throughput() -> None:
    """Overview 扩展字段：数据分类 / 来源类型 / 5min 吞吐 — P1/P2 落地。

    前端 Overview 服务身份卡 / 4 张分布图 / L3 5min 吞吐卡直接消费这些字段。
    """
    client, service = build_client(l3_write_mode="sync")
    seed_memories_and_rounds(service)

    # 在仓储里塞不同 classification / source_type 的记录
    from thinkback.domain.enums import DataClassification, SourceType

    repo = service.repository
    repo.add_memory_index(
        backend_memory_id="b-class-1",
        user_id="user-1",
        source_refs=[{"session_id": "s1", "round_id": "r-class-1"}],
        memory_text="User likes coffee",
        data_classification=DataClassification.SENSITIVE.value,
        source_type=SourceType.MANUAL_FIX.value,
    )
    repo.add_memory_index(
        backend_memory_id="b-class-2",
        user_id="user-1",
        source_refs=[{"session_id": "s1", "round_id": "r-class-2"}],
        memory_text="User birthday is 1990-01-01",
        data_classification=DataClassification.PERSONAL.value,
        source_type=SourceType.CHAT_ROUND.value,
    )

    response = client.get("/admin/api/overview")
    assert response.status_code == 200
    body = response.json()

    # 分类分布
    cls = body.get("by_classification", {})
    assert "personal" in cls or "sensitive" in cls, cls
    # 至少敏感 + 个人 + 普通三种（seed 制造的）
    assert sum(cls.values()) >= 2

    # 来源分布
    src = body.get("by_source_type", {})
    assert "chat_round" in src or "manual_fix" in src, src

    # 5min 吞吐（in-memory 滑动窗口：本测试进程中所有 append 都被记录）
    tp = body.get("throughput_5min", {})
    assert set(tp.keys()) == {"append_ok", "append_fail", "recall_ok", "recall_fail"}
    for kind in tp.values():
        assert "count" in kind
        assert "per_minute" in kind


def test_admin_health_detail_returns_service_identity() -> None:
    """Overview 服务身份卡：版本 / uptime / alembic / DB 池 / gRPC / 关键开关。"""
    client, _service = build_client()

    response = client.get("/admin/api/health/detail")
    assert response.status_code == 200
    body = response.json()

    # 必填字段
    assert "uptime_seconds" in body
    assert body["uptime_seconds"] >= 0
    assert "process_started_at" in body
    assert body["app"]["name"].lower() == "thinkback"
    assert "version" in body["app"]
    assert body["app"]["environment"] in {"development", "staging", "production"}

    # alembic 字段（可能为 None 表示还没跑过迁移）
    assert "alembic_current" in body

    # DB 池字段
    assert body["db_pool"]["size"] > 0
    assert body["db_pool"]["max_overflow"] is not None

    # gRPC 字段
    assert "port" in body["grpc"]
    assert isinstance(body["grpc"]["port"], int)

    # 关键开关
    flags = body["flags"]
    assert isinstance(flags["memory_l2_llm_enabled"], bool)
    assert isinstance(flags["memory_decay_enabled"], bool)
    assert isinstance(flags["memory_infer_facts"], bool)
    assert isinstance(flags["memory_p0_slots"], list)


def test_admin_reclaim_orphan_tasks_endpoint() -> None:
    """运维端点：手动触发孤儿 running 任务回收。"""
    client, service = build_client()
    service.repository.save_task(
        TaskEntry(
            task_id="memory-extract:orphan-test",
            request_id="r-orphan",
            op_type=OperationType.WRITE_ROUND,
            scope={"user_id": "user-1"},
            status=TaskStatus.RUNNING,
            retry_count=0,
        )
    )
    response = client.post("/admin/api/maintenance/reclaim-orphan-tasks")
    assert response.status_code == 200
    body = response.json()
    assert "reclaimed_count" in body
    assert "reclaimed_task_ids" in body
    assert isinstance(body["reclaimed_count"], int)
    assert isinstance(body["reclaimed_task_ids"], list)


def test_admin_tasks_list_filters_and_paginates() -> None:
    client, service = build_client()
    seed(service)

    body = client.get("/admin/api/tasks").json()
    assert [task["task_id"] for task in body] == ["t-dead", "t-failed", "t-running"]

    body = client.get("/admin/api/tasks", params={"statuses": "failed,dead_letter"}).json()
    assert {task["task_id"] for task in body} == {"t-dead", "t-failed"}

    body = client.get("/admin/api/tasks", params={"limit": 2, "offset": 1}).json()
    assert [task["task_id"] for task in body] == ["t-failed", "t-running"]

    assert client.get("/admin/api/tasks", params={"statuses": "bogus"}).status_code == 400


def test_admin_token_enforced_when_configured(monkeypatch: Any) -> None:
    monkeypatch.setattr(config.settings, "admin_token", "secret-token")
    client, _service = build_client()

    assert client.get("/admin/api/overview").status_code == 401
    assert (
        client.get("/admin/api/overview", headers={"Authorization": "Bearer wrong"}).status_code
        == 401
    )
    assert (
        client.get(
            "/admin/api/overview", headers={"Authorization": "Bearer secret-token"}
        ).status_code
        == 200
    )


def test_admin_token_empty_allows_anonymous(monkeypatch: Any) -> None:
    monkeypatch.setattr(config.settings, "admin_token", "")
    client, _service = build_client()

    assert client.get("/admin/api/overview").status_code == 200


# ── M2：记忆检索 + 来源反链 ──────────────────────────────────────────


def seed_memories_and_rounds(service: MemoryService) -> None:
    repository = service.repository
    assert isinstance(repository, InMemoryMemoryRepository)
    repository.add_memory_index(
        backend_memory_id="b-1",
        user_id="user-1",
        source_refs=[{"session_id": "s1", "round_id": "r1"}],
        memory_text="User likes coffee",
    )
    repository.add_memory_index(
        backend_memory_id="b-2",
        user_id="user-1",
        source_refs=[{"session_id": "s1", "round_id": "r2"}],
        memory_text="User moved to Shanghai",
    )
    repository.add_memory_index(
        backend_memory_id="b-3",
        user_id="user-2",
        source_refs=[{"session_id": "s9", "round_id": "r9"}],
        memory_text="User has a cat named Mochi",
    )
    request = AppendMemoryRequest(
        request_id="req-r1",
        user_id="user-1",
        session_id="s1",
        round_id="r1",
        round_index=1,
        messages=[
            {
                "message_id": "m1",
                "role": "user",
                "content": "我平时最喜欢喝拿铁咖啡",
                "timestamp": "2026-09-24T10:00:00+00:00",
            },
            {
                "message_id": "m2",
                "role": "assistant",
                "content": "记住了，你喜欢拿铁。",
                "timestamp": "2026-09-24T10:00:05+00:00",
            },
        ],
        source_timestamp=datetime.now(UTC),
    )
    repository.save_round(request)


def test_admin_memories_filter_pagination_and_total() -> None:
    client, service = build_client()
    seed_memories_and_rounds(service)

    body = client.get("/admin/api/memories").json()
    assert body["total"] == 3
    assert sorted(item["memory_text"] for item in body["items"]) == [
        "User has a cat named Mochi",
        "User likes coffee",
        "User moved to Shanghai",
    ]

    body = client.get("/admin/api/memories", params={"user_id": "user-1"}).json()
    assert body["total"] == 2

    body = client.get(
        "/admin/api/memories", params={"user_id": "user-1", "limit": 1, "offset": 1}
    ).json()
    assert body["total"] == 2
    assert [item["memory_text"] for item in body["items"]] in (
        ["User likes coffee"],
        ["User moved to Shanghai"],
    )

    assert client.get("/admin/api/memories", params={"statuses": "bogus"}).status_code == 400


def test_admin_memory_source_backlinks_journal_round() -> None:
    client, service = build_client()
    seed_memories_and_rounds(service)

    user1_items = client.get("/admin/api/memories", params={"user_id": "user-1"}).json()["items"]
    memory_id = next(
        item["memory_id"] for item in user1_items if item["memory_text"] == "User likes coffee"
    )

    body = client.get(f"/admin/api/memories/{memory_id}/source").json()
    assert body["memory"]["memory_id"] == memory_id
    assert isinstance(body["memory"]["conflict_slot"], (str, type(None)))
    assert len(body["rounds"]) == 1
    assert body["rounds"][0]["round_id"] == "r1"
    assert body["rounds"][0]["messages"][0]["content"] == "我平时最喜欢喝拿铁咖啡"

    other_id = client.get("/admin/api/memories", params={"user_id": "user-2"}).json()["items"][0][
        "memory_id"
    ]
    assert client.get(f"/admin/api/memories/{other_id}/source").json()["rounds"] == []

    assert client.get("/admin/api/memories/missing/source").status_code == 404


# ── M3：治理操作 + 审计 + 配置 ────────────────────────────────────────


def test_admin_delete_writes_audit_and_deletes() -> None:
    client, service = build_client(l3_write_mode="sync")
    seed_memories_and_rounds(service)

    response = client.post(
        "/admin/api/delete",
        json={
            "request_id": "req-admin-1",
            "user_id": "user-1",
            "scope": "memory",
            "operation_id": "op-admin-del-1",
            "memory_id": next(
                item["memory_id"]
                for item in client.get("/admin/api/memories", params={"user_id": "user-1"}).json()[
                    "items"
                ]
                if item["memory_text"] == "User likes coffee"
            ),
        },
    )
    assert response.status_code == 200

    audit = client.get("/admin/api/audit", params={"action": "delete"}).json()
    assert len(audit) == 1
    assert audit[0]["action"] == "delete"
    assert audit[0]["operator"] == "admin"
    assert audit[0]["target"] == "user-1"
    assert audit[0]["detail"]["scope"] == "memory"
    # affected_count must reflect the actual number deleted (not None)
    assert audit[0]["detail"]["affected_count"] == 1


def test_admin_update_audit_does_not_leak_content() -> None:
    """admin/update 审计留痕**不写正文**：只记指纹+长度+memory_type。
    防 audit 接口被滥用窥探用户记忆原文（PII / 凭据泄漏面）。"""
    client, service = build_client(l3_write_mode="sync")
    seed_memories_and_rounds(service)
    secret_content = "User's bank account password is 123456 and SSN is 999-99-9999"

    target_memory_id = next(
        item["memory_id"]
        for item in client.get("/admin/api/memories", params={"user_id": "user-1"}).json()["items"]
        if item["memory_text"] == "User likes coffee"
    )
    response = client.post(
        "/admin/api/update",
        json={
            "request_id": "req-admin-upd",
            "user_id": "user-1",
            "operation_id": "op-admin-upd-1",
            "memory_id": target_memory_id,
            "content": secret_content,
        },
    )
    assert response.status_code == 200

    audit = client.get("/admin/api/audit", params={"action": "update"}).json()
    assert len(audit) == 1
    audit_detail = audit[0]["detail"]
    # 正文必须不在 audit 里
    assert "content" not in audit_detail
    assert secret_content not in str(audit_detail)
    # 必须留可审计的指纹与长度
    assert audit_detail["content_length"] == len(secret_content)
    assert (
        isinstance(audit_detail["content_sha256"], str)
        and len(audit_detail["content_sha256"]) == 64
    )
    assert audit_detail["operation_id"] == "op-admin-upd-1"


def test_admin_audit_invalid_action_returns_400() -> None:
    """audit?action= 校验：非法动作必须 400，与 tasks 的 status 校验对齐。

    修复前：admin_list_audit 不校验 action，非法值返回 200 + []，运营人员
    误以为"动作没产生数据"——BUG #5。"""
    client, _service = build_client()

    response = client.get("/admin/api/audit", params={"action": "no-such-action"})
    assert response.status_code == 400
    assert "invalid audit action" in response.json()["detail"]

    # 合法动作不应 400
    for valid_action in ("delete", "update", "rebuild"):
        response = client.get("/admin/api/audit", params={"action": valid_action})
        assert response.status_code == 200, f"action={valid_action} should be valid"


@pytest.mark.parametrize(
    "endpoint",
    [
        "/admin/api/delete",
        "/admin/api/update",
        "/admin/api/rebuild",
    ],
)
def test_admin_endpoints_reject_empty_or_whitespace_user_id(endpoint: str) -> None:
    """防御纵深：admin 操作类端点必须拒绝空串/纯空白 user_id。

    修复前：IdStr 只有 max_length=128 约束，空串能过校验，rebuild 会创建空
    user_id 的任务并触发空范围重建——BUG #6。前端 govern.tsx 已有 trim+length>0
    校验（按钮 disabled），但 gRPC / curl / SDK 等非 web 入口绕过。
    """
    client, service = build_client(l3_write_mode="sync")
    seed_memories_and_rounds(service)

    if endpoint == "/admin/api/delete":
        body = {
            "request_id": "r1",
            "user_id": "",
            "scope": "memory",
            "operation_id": "op1",
            "memory_id": "x",
        }
    elif endpoint == "/admin/api/update":
        body = {
            "request_id": "r1",
            "user_id": "",
            "operation_id": "op1",
            "memory_id": "x",
            "content": "x",
        }
    else:  # /admin/api/rebuild
        body = {
            "request_id": "r1",
            "user_id": "",
            "operation_id": "op1",
            "rebuild_l2": True,
            "rebuild_l3": True,
        }

    response = client.post(endpoint, json=body)
    assert response.status_code in {400, 422}, (
        f"empty user_id should be rejected, got {response.status_code}: {response.json()}"
    )

    # Whitespace-only user_id also rejected
    body["user_id"] = "   "
    response = client.post(endpoint, json=body)
    assert response.status_code in {400, 422}, (
        f"whitespace user_id should be rejected, got {response.status_code}: {response.json()}"
    )


def test_admin_rebuild_route_and_audit() -> None:
    client, service = build_client(l3_write_mode="sync")
    seed_memories_and_rounds(service)

    response = client.post(
        "/admin/api/rebuild",
        json={
            "request_id": "req-admin-2",
            "user_id": "user-1",
            "operation_id": "op-admin-rebuild-1",
            "session_id": "s1",
            "rebuild_l2": False,
            "rebuild_l3": False,
        },
    )
    assert response.status_code == 200

    audit = client.get("/admin/api/audit", params={"action": "rebuild"}).json()
    assert len(audit) == 1
    assert audit[0]["detail"]["operation_id"] == "op-admin-rebuild-1"


def test_admin_config_masks_sensitive_values() -> None:
    client, _service = build_client()

    response = client.get("/admin/api/config")

    assert response.status_code == 200
    body = response.json()
    assert body["environment"]
    fields = {field["name"]: field["value"] for field in body["fields"]}
    assert fields.get("postgres_password") == "***"
    assert "app_name" in fields and fields["app_name"]
