"""物理清理作业的**作用域参数**契约。

``memory/retention.py`` 的 ``RetentionCleaner`` 是 ADR-0010 第三段：它把过了
宽限期的事实**真的删掉** —— 向量、引擎侧历史、治理行一起删。

这个作业的参数一旦传错，代价不是「召回不准」，是**删错东西**：

- ``user_id`` 没传给仓储 → 清掉**别的用户**的记忆；
- ``grace_days`` 没传下去 → 用默认 7 天，配置里写的 30 天形同虚设，
  记忆比预期早 23 天被删；
- ``batch_size`` 没传 → 一次清空整张表，没有批次边界。

这些全是「传参」层面的事，示例测试很难碰到 —— 变异审计把它们全标成了
存活（82 个，本轮新增存活里最大的一块）。这里逐条钉住。

另外钉住审计记录的**字段契约**：``as_audit_detail`` 的键名是运维看板的
读取契约，改名不会让任何功能用例变红，但会让历史计数对不上。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from thinkback.domain.entities import AdminAuditEntry, MemoryIndexEntry
from thinkback.memory.repositories.in_memory import InMemoryMemoryRepository
from thinkback.memory.retention import (
    DEFAULT_GRACE_DAYS,
    PurgeReport,
    RetentionCleaner,
)

_NOW = datetime(2026, 10, 5, 12, 0, 0, tzinfo=UTC)


class _RecordingRepository(InMemoryMemoryRepository):
    """记录 ``list_expired_memories`` 收到了什么参数。"""

    def __init__(self) -> None:
        super().__init__()
        self.list_calls: list[dict[str, Any]] = []

    def list_expired_memories(self, **kwargs: Any) -> list[MemoryIndexEntry]:
        self.list_calls.append(kwargs)
        return super().list_expired_memories(**kwargs)


class _FakeBackend:
    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.purged: list[dict[str, Any]] = []

    def delete(self, memory_id: str) -> None:
        self.deleted.append(memory_id)

    def purge_history(self, **kwargs: Any) -> dict[str, int]:
        self.purged.append(kwargs)
        return {"history": 1, "messages": 0, "supported": True}


def _entry(
    memory_id: str, *, expires_at: datetime | None, user_id: str = "user-1"
) -> MemoryIndexEntry:
    return MemoryIndexEntry(
        memory_id=memory_id,
        backend_memory_id=f"backend-{memory_id}",
        user_id=user_id,
        memory_scope_id="scope-1",
        source_refs=[],
        memory_text=f"事实 {memory_id}",
        expires_at=expires_at,
    )


def _cleaner(
    repository: Any,
    backend: Any,
    *,
    grace_days: int = 7,
    batch_size: int = 500,
    memory_scope_id: str | None = "scope-1",
    user_id: str | None = "user-1",
) -> RetentionCleaner:
    return RetentionCleaner(
        repository=repository,
        backend=backend,
        grace_days=grace_days,
        batch_size=batch_size,
        memory_scope_id=memory_scope_id,
        user_id=user_id,
    )


# ---------------------------------------------------------------------------
# 作用域参数必须原样传给仓储
# ---------------------------------------------------------------------------


def test_the_user_filter_is_passed_to_the_repository() -> None:
    """不传 user_id → 清理作业会删掉**别的用户**的记忆。"""
    repository = _RecordingRepository()
    _cleaner(repository, _FakeBackend(), user_id="user-42").plan(now=_NOW)
    assert repository.list_calls
    assert repository.list_calls[0]["user_id"] == "user-42"


def test_the_scope_filter_is_passed_to_the_repository() -> None:
    repository = _RecordingRepository()
    _cleaner(repository, _FakeBackend(), memory_scope_id="scope-7").plan(now=_NOW)
    assert repository.list_calls[0]["memory_scope_id"] == "scope-7"


def test_the_batch_size_is_passed_as_the_limit() -> None:
    """不传 batch_size → 一次清空整张表，没有批次边界。"""
    repository = _RecordingRepository()
    _cleaner(repository, _FakeBackend(), batch_size=17).plan(now=_NOW)
    assert repository.list_calls[0]["limit"] == 17


def test_the_grace_days_reach_the_cutoff() -> None:
    """grace_days 没传下去 → 用默认 7 天，配置的 30 天形同虚设。"""
    repository = _RecordingRepository()
    _cleaner(repository, _FakeBackend(), grace_days=30).plan(now=_NOW)
    expected = _NOW - timedelta(days=30)
    assert repository.list_calls[0]["before"] == expected


def test_a_grace_period_longer_than_the_default_is_actually_honoured() -> None:
    """端到端：30 天宽限下，20 天前到期的记忆**不该**被清理。"""
    repository = InMemoryMemoryRepository()
    repository.memories["m-1"] = _entry("m-1", expires_at=_NOW - timedelta(days=20))
    cleaner = _cleaner(repository, _FakeBackend(), grace_days=30)
    report = cleaner.run(now=_NOW)
    assert report.purged == 0
    assert report.scanned == 0


def test_the_default_grace_period_is_seven_days() -> None:
    assert DEFAULT_GRACE_DAYS == 7


# ---------------------------------------------------------------------------
# 审计记录的字段契约
# ---------------------------------------------------------------------------


def test_the_audit_detail_keys_are_fixed() -> None:
    """键名是运维看板的读取契约 —— 改名不会让功能用例变红，但计数会对不上。"""
    report = PurgeReport(
        scanned=3,
        purged=2,
        failed=1,
        backend_records_deleted=2,
        history_rows_deleted=4,
    )
    assert report.as_audit_detail() == {
        "scanned": 3,
        "purged": 2,
        "failed": 1,
        "backend_records_deleted": 2,
        "history_rows_deleted": 4,
    }


def test_the_audit_record_carries_the_documented_action_and_target() -> None:
    repository = InMemoryMemoryRepository()
    cleaner = _cleaner(repository, _FakeBackend(), memory_scope_id="scope-9")
    report = PurgeReport(scanned=1)
    cleaner.record_audit(report)
    entries = repository.list_admin_audit()
    assert len(entries) == 1
    entry = entries[0]
    assert entry.action == "memory.retention.purge"
    assert entry.target == "scope-9"
    assert entry.operator == "retention-job"
    assert entry.detail["scanned"] == 1


def test_the_audit_target_falls_back_to_a_wildcard() -> None:
    """没有 scope 时目标记成 ``*`` —— 审计要能看出这是全量清理。"""
    repository = InMemoryMemoryRepository()
    cleaner = _cleaner(repository, _FakeBackend(), memory_scope_id=None)
    cleaner.record_audit(PurgeReport())
    entry = repository.list_admin_audit()[0]
    assert entry.target == "*"


def test_the_audit_operator_can_be_overridden() -> None:
    repository = InMemoryMemoryRepository()
    cleaner = _cleaner(repository, _FakeBackend())
    cleaner.record_audit(PurgeReport(), operator="cron-job")
    assert repository.list_admin_audit()[0].operator == "cron-job"


def test_the_audit_id_is_unique_per_call() -> None:
    """两次清理的审计 id 必须不同 —— 否则后一条会覆盖前一条。"""
    repository = InMemoryMemoryRepository()
    cleaner = _cleaner(repository, _FakeBackend())
    cleaner.record_audit(PurgeReport())
    cleaner.record_audit(PurgeReport())
    assert len(repository.list_admin_audit()) == 2


def test_the_audit_timestamp_is_timezone_aware() -> None:
    """审计时间带 tzinfo —— naive 时间混进审计表会跟别的行对不上。"""
    repository = InMemoryMemoryRepository()
    cleaner = _cleaner(repository, _FakeBackend())
    cleaner.record_audit(PurgeReport())
    entry = repository.list_admin_audit()[0]
    assert isinstance(entry.created_at, datetime)
    assert entry.created_at.tzinfo is not None


def test_the_audit_id_uses_the_documented_format() -> None:
    """审计 id 形如 ``audit-retention-20261005120000000000`` —— 便于按时间排序。"""
    repository = InMemoryMemoryRepository()
    cleaner = _cleaner(repository, _FakeBackend())
    cleaner.record_audit(PurgeReport())
    audit_id = repository.list_admin_audit()[0].audit_id
    assert audit_id.startswith("audit-retention-")
    suffix = audit_id.removeprefix("audit-retention-")
    assert len(suffix) == 20
    assert suffix.isdigit()


def test_the_audit_entry_type_is_preserved() -> None:
    repository = InMemoryMemoryRepository()
    cleaner = _cleaner(repository, _FakeBackend())
    cleaner.record_audit(PurgeReport())
    entry = repository.list_admin_audit()[0]
    assert isinstance(entry, AdminAuditEntry)
