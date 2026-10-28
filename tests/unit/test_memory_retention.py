"""保留期三段的第三段：物理清理作业。

钉住四件事：
- 宽限期内不删（到期 ≠ 可删）；
- 永不过期的记忆永远不删；
- 清理要同时删向量、引擎侧事实历史与治理行；
- 单条失败不拖垮整批，失败条目留给下一轮。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from thinkback.domain.entities import MemoryIndexEntry
from thinkback.memory.repositories.in_memory import InMemoryMemoryRepository
from thinkback.memory.retention import (
    DEFAULT_GRACE_DAYS,
    RetentionCleaner,
    is_purgeable,
    purge_cutoff,
)


class _FakeBackend:
    def __init__(self, *, fail_on: str | None = None) -> None:
        self.fail_on = fail_on
        self.deleted: list[str] = []
        self.purged: list[dict[str, Any]] = []

    def delete(self, memory_id: str) -> None:
        if memory_id == self.fail_on:
            raise RuntimeError("backend delete failed")
        self.deleted.append(memory_id)

    def purge_history(self, **kwargs: Any) -> dict[str, int]:
        self.purged.append(kwargs)
        return {"history": 1, "messages": 0, "supported": True}


def _entry(
    memory_id: str,
    *,
    expires_at: datetime | None,
    scope: str = "thinkback",
) -> MemoryIndexEntry:
    return MemoryIndexEntry(
        memory_id=memory_id,
        backend_memory_id=f"backend-{memory_id}",
        user_id="user-1",
        memory_scope_id=scope,
        source_refs=[],
        memory_text=f"事实 {memory_id}",
        expires_at=expires_at,
    )


def _repository(entries: list[MemoryIndexEntry]) -> InMemoryMemoryRepository:
    repository = InMemoryMemoryRepository()
    for entry in entries:
        repository.memories[entry.memory_id] = entry
    return repository


def test_purge_cutoff_is_grace_days_before_now() -> None:
    now = datetime(2026, 10, 28, tzinfo=UTC)
    assert purge_cutoff(now=now, grace_days=DEFAULT_GRACE_DAYS) == now - timedelta(days=7)


def test_expired_but_within_grace_is_not_purgeable() -> None:
    now = datetime(2026, 10, 28, tzinfo=UTC)
    entry = _entry("m1", expires_at=now - timedelta(days=3))

    assert is_purgeable(entry, now=now) is False


def test_expired_beyond_grace_is_purgeable() -> None:
    now = datetime(2026, 10, 28, tzinfo=UTC)
    entry = _entry("m1", expires_at=now - timedelta(days=8))

    assert is_purgeable(entry, now=now) is True


def test_never_expiring_memory_is_not_purgeable() -> None:
    now = datetime(2026, 10, 28, tzinfo=UTC)

    assert is_purgeable(_entry("m1", expires_at=None), now=now) is False


def test_run_purges_backend_history_and_index_row() -> None:
    now = datetime(2026, 10, 28, tzinfo=UTC)
    repository = _repository([_entry("m1", expires_at=now - timedelta(days=30))])
    backend = _FakeBackend()
    cleaner = RetentionCleaner(repository=repository, backend=backend)

    report = cleaner.run(now=now)

    assert report.purged == 1
    assert backend.deleted == ["backend-m1"]
    assert backend.purged[0]["memory_ids"] == ["backend-m1"]
    assert "m1" not in repository.memories


def test_run_keeps_memory_within_grace() -> None:
    now = datetime(2026, 10, 28, tzinfo=UTC)
    repository = _repository([_entry("m1", expires_at=now - timedelta(days=2))])
    backend = _FakeBackend()
    cleaner = RetentionCleaner(repository=repository, backend=backend)

    report = cleaner.run(now=now)

    assert report.scanned == 0
    assert report.purged == 0
    assert backend.deleted == []
    assert "m1" in repository.memories


def test_dry_run_does_not_delete() -> None:
    now = datetime(2026, 10, 28, tzinfo=UTC)
    repository = _repository([_entry("m1", expires_at=now - timedelta(days=30))])
    backend = _FakeBackend()
    cleaner = RetentionCleaner(repository=repository, backend=backend)

    report = cleaner.run(now=now, dry_run=True)

    assert report.scanned == 1
    assert report.purged == 0
    assert backend.deleted == []
    assert "m1" in repository.memories


def test_one_failure_does_not_stop_the_batch() -> None:
    now = datetime(2026, 10, 28, tzinfo=UTC)
    repository = _repository(
        [
            _entry("m1", expires_at=now - timedelta(days=30)),
            _entry("m2", expires_at=now - timedelta(days=31)),
        ]
    )
    backend = _FakeBackend(fail_on="backend-m1")
    cleaner = RetentionCleaner(repository=repository, backend=backend)

    report = cleaner.run(now=now)

    assert report.purged == 1
    assert report.failed == 1
    assert report.failed_memory_ids == ["m1"]
    assert "m1" in repository.memories
    assert "m2" not in repository.memories


def test_namespace_filter_limits_scope() -> None:
    now = datetime(2026, 10, 28, tzinfo=UTC)
    repository = _repository(
        [
            _entry("m1", expires_at=now - timedelta(days=30), scope="acme"),
            _entry("m2", expires_at=now - timedelta(days=30), scope="thinkback"),
        ]
    )
    cleaner = RetentionCleaner(
        repository=repository, backend=_FakeBackend(), memory_scope_id="acme"
    )

    entries = cleaner.plan(now=now)

    assert [entry.memory_id for entry in entries] == ["m1"]


def test_audit_records_counts_only() -> None:
    now = datetime(2026, 10, 28, tzinfo=UTC)
    repository = _repository([_entry("m1", expires_at=now - timedelta(days=30))])
    cleaner = RetentionCleaner(repository=repository, backend=_FakeBackend())

    report = cleaner.run(now=now)
    cleaner.record_audit(report)

    audits = repository.list_admin_audit(limit=5)
    assert len(audits) == 1
    assert audits[0].action == "memory.retention.purge"
    assert audits[0].detail["purged"] == 1
    # 审计只留计数，不留原文。
    assert "事实" not in str(audits[0].detail)
