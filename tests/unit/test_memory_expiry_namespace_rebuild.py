"""生产审计三条修复的回归测试。

- Fix A：TTL 也要落本地索引（过期事实不再从本地召回/列表回流）
- Fix B：namespace 参与 append 幂等（同 round 换 namespace = 冲突，不是静默丢写）
- Fix C：rebuild 用源时间与源类型策略（外部历史里旧事实不能盖掉新事实）
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from thinkback.memory.backends import FakeMemoryBackend
from thinkback.memory.repositories import InMemoryMemoryRepository
from thinkback.memory.schemas import (
    AppendMemoryRequest,
    MessageRole,
    RebuildMemoryRequest,
    RecallMemoryRequest,
)
from thinkback.memory.service import MemoryService

# ── helpers ───────────────────────────────────────────────────────────


def _append_request(
    round_id: str,
    content: str,
    *,
    namespace: str = "thinkback",
    metadata: dict[str, Any] | None = None,
    source_timestamp: str = "2026-05-04T10:00:03Z",
    session_id: str = "session-1",
) -> AppendMemoryRequest:
    return AppendMemoryRequest(
        request_id=f"req-{round_id}-{namespace}",
        user_id="user-1",
        session_id=session_id,
        round_id=round_id,
        messages=[
            {
                "message_id": f"{round_id}-u",
                "role": MessageRole.USER,
                "content": content,
                "timestamp": source_timestamp,
            },
            {
                "message_id": f"{round_id}-a",
                "role": MessageRole.ASSISTANT,
                "content": "好的。",
                "timestamp": source_timestamp,
            },
        ],
        source_timestamp=source_timestamp,
        namespace=namespace,
        metadata=metadata or {},
    )


class _AlwaysReturnsBackend(FakeMemoryBackend):
    """语义检索无视 query 一律返回后端记忆（隔离「后端命中」这条读路径）。"""

    def search(
        self,
        query: str,
        *,
        user_id: str,
        memory_scope_id: str,
        limit: int,
        threshold: float | None = None,
        explain: bool = False,
    ) -> list[dict[str, Any]]:
        _ = (query, threshold)
        return [
            dict(memory)
            for memory in self.memories.values()
            if memory["user_id"] == user_id and memory["agent_id"] == memory_scope_id
        ][:limit]


def _recall(service: MemoryService, query: str) -> Any:
    return service.recall(
        RecallMemoryRequest(user_id="user-1", session_id="session-1", query=query)
    )


def _expired_row(
    repository: InMemoryMemoryRepository,
    *,
    backend_id: str,
    text: str,
    round_id: str = "round-x",
) -> Any:
    return repository.add_memory_index(
        backend_memory_id=backend_id,
        user_id="user-1",
        memory_scope_id="thinkback",
        source_refs=[{"session_id": "session-1", "round_id": round_id}],
        memory_text=text,
        expires_at=datetime.now(UTC) - timedelta(days=1),
    )


# ── Fix A：TTL 落本地索引 ─────────────────────────────────────────────


def test_expired_memory_is_not_returned_by_slot_backfill() -> None:
    """过期事实不能从「精确槽位回填」这条本地读路径回流。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    _expired_row(repository, backend_id="expired-location", text="User lives in 北京")

    response = _recall(service, "你现在住在哪里？")

    assert [item.content for item in response.items if item.layer == "L3"] == []


def test_expired_memory_is_not_returned_by_semantic_recall() -> None:
    """后端仍命中过期记录时，本地索引的过期判定同样要挡住它。"""
    repository = InMemoryMemoryRepository()
    backend = _AlwaysReturnsBackend()
    service = MemoryService(repository=repository, backend=backend)
    _expired_row(repository, backend_id="expired-note", text="用户偏好简短直接的回复")
    backend.memories["expired-note"] = {
        "id": "expired-note",
        "memory": "用户偏好简短直接的回复",
        "event": "ADD",
        "user_id": "user-1",
        "agent_id": "thinkback",
        "metadata": {},
        "score": 0.9,
    }

    response = _recall(service, "简短")

    assert [item.content for item in response.items if item.layer == "L3"] == []


def test_expired_memory_is_not_listed_by_items() -> None:
    """过期事实不出现在 /items 列表里。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    _expired_row(repository, backend_id="expired-item", text="User lives in 北京")

    listed = service.list_memory_items(user_id="user-1")

    assert listed.items == []


def test_append_with_ttl_type_stores_expiry_on_index_row() -> None:
    """TTL 类型写入时，本地索引行必须带上过期时刻与记录时刻。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())

    service.append(
        _append_request("r-state", "我现在住在杭州。", metadata={"memory_type": "state"})
    )

    rows = repository.list_memories("user-1", "thinkback")
    assert rows, "state 类型应落本地索引"
    now = datetime.now(UTC)
    for row in rows:
        assert row.memory_type == "state"
        assert row.expires_at is not None
        assert row.recorded_at is not None
        # 默认注册表：state.ttl_days = 90
        assert row.expires_at > now + timedelta(days=89)
        assert row.recorded_at <= now


def test_admin_list_exposes_expiry_without_hiding_the_entry() -> None:
    """治理台保留过期条目（否则擦除不了），但必须说清它什么时候到期。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    _expired_row(repository, backend_id="expired-admin", text="User lives in 北京")

    result = service.admin_list_memories(user_id="user-1")

    assert result["items"], "治理台要保留过期条目，否则运营无法发现并擦除它"
    assert result["items"][0].expires_at is not None


def test_forever_type_has_no_expiry_on_index_row() -> None:
    """非 TTL 类型不带过期时刻（None = 永不过期）。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())

    service.append(_append_request("r-profile", "我现在住在杭州。"))

    rows = repository.list_memories("user-1", "thinkback")
    assert rows
    assert all(row.expires_at is None for row in rows)


# ── Fix B：namespace 参与 append 幂等 ─────────────────────────────────


def test_same_round_under_other_namespace_raises_conflict() -> None:
    """同一 round_id 换 namespace 重放 = 冲突，不是静默丢写。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    service.append(_append_request("r-ns", "我叫阿鹏。", namespace="alpha"))

    with pytest.raises(ValueError, match="conflict") as excinfo:
        service.append(_append_request("r-ns", "我叫阿鹏。", namespace="beta"))

    assert "namespace" in str(excinfo.value)
    assert repository.active_memories("user-1", "beta") == []
    assert repository.active_memories("user-1", "alpha")


def test_same_round_same_namespace_stays_idempotent() -> None:
    """同 namespace 重放仍是 already_done（不回归）。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    first = service.append(_append_request("r-ns-same", "我叫阿鹏。", namespace="alpha"))
    second = service.append(_append_request("r-ns-same", "我叫阿鹏。", namespace="alpha"))

    assert first.status == "completed"
    assert second.status == "already_done"


# ── Fix C：rebuild 用源时间与源类型策略 ───────────────────────────────


class _HistorySource:
    """外部历史源（rebuild 输入）。"""

    def __init__(self, rounds: list[Any]) -> None:
        self.rounds = list(rounds)

    def current_version(self, user_id: str, memory_scope_id: str) -> str:
        _ = (user_id, memory_scope_id)
        return "history-v1"

    def list_rounds(
        self, user_id: str, memory_scope_id: str, session_id: str | None = None
    ) -> list[Any]:
        if memory_scope_id == "__all_sessions__":
            return [entry for entry in self.rounds if entry.user_id == user_id]
        return [
            entry
            for entry in self.rounds
            if entry.user_id == user_id
            and entry.memory_scope_id == memory_scope_id
            and (session_id is None or entry.session_id == session_id)
        ]


def _history_round(
    round_id: str,
    content: str,
    *,
    source_timestamp: str,
    metadata: dict[str, Any] | None = None,
) -> Any:
    seed = InMemoryMemoryRepository()
    return seed.save_round(
        _append_request(
            round_id,
            content,
            metadata=metadata,
            source_timestamp=source_timestamp,
        )
    )


def test_rebuild_prefers_newer_source_timestamp_over_round_id_order() -> None:
    """外部历史：round-a（2026-02）比 round-z（2026-01）新，旧事实不能赢。"""
    older = _history_round("round-z", "我现在住在上海。", source_timestamp="2026-01-01T10:00:00Z")
    newer = _history_round("round-a", "我现在住在杭州。", source_timestamp="2026-02-01T10:00:00Z")
    repository = InMemoryMemoryRepository()
    service = MemoryService(
        repository=repository,
        backend=FakeMemoryBackend(),
        history_source=_HistorySource([older, newer]),
    )

    service.rebuild(
        RebuildMemoryRequest(
            request_id="rebuild-ts",
            user_id="user-1",
            operation_id="op-rebuild-ts",
            rebuild_l2=False,
            rebuild_l3=True,
        )
    )

    active = repository.active_memories("user-1", "thinkback")
    assert [row.memory_text for row in active] == ["User lives in 杭州"]


def test_rebuild_preserves_custom_memory_type_policy() -> None:
    """重建回放沿用原记忆类型（state）及其 TTL 策略。"""
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository=repository, backend=FakeMemoryBackend())
    service.append(
        _append_request("r-state-2", "我现在住在杭州。", metadata={"memory_type": "state"})
    )

    service.rebuild(
        RebuildMemoryRequest(
            request_id="rebuild-type",
            user_id="user-1",
            operation_id="op-rebuild-type",
            rebuild_l2=False,
            rebuild_l3=True,
        )
    )

    rows = repository.list_memories("user-1", "thinkback")
    assert rows
    assert all(row.memory_type == "state" for row in rows)
    assert all(row.expires_at is not None for row in rows)
