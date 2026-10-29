"""保留期物理清理作业的契约测试。

``memory/retention.py`` 是**删数据**的作业 —— 它的每个参数都决定「删哪一条、
删多少条、删完记什么账」。参数传错的后果不是报错，是**静默删错东西**或者
**静默不删**。

作业此前只被功能用例盖到过一层皮（能跑完、能计数），参数级的契约没有钉住：
- 传给仓储的 ``limit`` / ``user_id`` / ``before`` 传错 → 删错范围；
- 传给引擎的 ``purge_all_messages`` 传错 → **把原始消息也删了**（模块文档
  明确说原始消息不清，消息属于轮次，按正文删会连带删掉别的记忆）；
- 审计的 ``target`` / ``operator`` / ``action`` 传错 → 治理台上看不出谁删的。

这一层把参数逐个钉死。
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from loguru import logger

from thinkback.domain.entities import AdminAuditEntry, MemoryIndexEntry
from thinkback.memory.retention import (
    DEFAULT_GRACE_DAYS,
    PurgeReport,
    RetentionCleaner,
    is_purgeable,
    purge_cutoff,
)

NOW = datetime(2026, 10, 28, tzinfo=UTC)


class _RecordingBackend:
    """记录每次调用的参数 —— 参数传错是这里的头号风险。"""

    def __init__(self, *, fail_on: str | None = None, history: int | None = 1) -> None:
        self.fail_on = fail_on
        self.history = history
        self.deleted: list[str] = []
        self.purged: list[dict[str, Any]] = []

    def delete(self, memory_id: str) -> None:
        if memory_id == self.fail_on:
            raise RuntimeError("backend delete failed")
        self.deleted.append(memory_id)

    def purge_history(self, **kwargs: Any) -> dict[str, int]:
        self.purged.append(kwargs)
        if self.history is None:
            return {}
        return {"history": self.history, "messages": 0, "supported": True}


class _RecordingRepository:
    """记录 ``list_expired_memories`` 收到的参数。"""

    def __init__(self, entries: list[MemoryIndexEntry]) -> None:
        self.entries = entries
        self.calls: list[dict[str, Any]] = []
        self.audits: list[AdminAuditEntry] = []
        self.deleted_rows: list[str] = []

    def list_expired_memories(self, **kwargs: Any) -> list[MemoryIndexEntry]:
        self.calls.append(kwargs)
        return list(self.entries)

    def delete_memory_rows(self, memory_ids: list[str]) -> None:
        self.deleted_rows.extend(memory_ids)

    def save_admin_audit(self, entry: AdminAuditEntry) -> None:
        self.audits.append(entry)


def _entry(
    memory_id: str,
    *,
    expires_at: datetime | None,
    scope: str = "thinkback",
    user_id: str = "user-1",
) -> MemoryIndexEntry:
    return MemoryIndexEntry(
        memory_id=memory_id,
        backend_memory_id=f"backend-{memory_id}",
        user_id=user_id,
        memory_scope_id=scope,
        source_refs=[],
        memory_text=f"事实 {memory_id}",
        expires_at=expires_at,
    )


@pytest.fixture()
def log_sink() -> Generator[list[dict[str, Any]], None, None]:
    """捕获日志记录（含 extra），用来钉住作业打出去的账。"""
    records: list[dict[str, Any]] = []
    handler_id = logger.add(
        lambda message: records.append(
            {"message": str(message.record["message"]), **message.record["extra"]}
        ),
        level="DEBUG",
    )
    try:
        yield records
    finally:
        logger.remove(handler_id)


# ── is_purgeable ───────────────────────────────────────────────────────────


def test_an_entry_without_expires_at_is_not_purgeable() -> None:
    """没有到期时刻（永不过期）永远不清理。

    也不该抛 —— 仓储里可能有还没写 expires_at 的行。
    """

    class _NoExpiry:
        pass

    assert is_purgeable(_NoExpiry(), now=NOW) is False


def test_a_naive_expiry_is_read_as_utc() -> None:
    """naive 时间按 UTC 处理（asyncpg/SQLite 可能丢 tzinfo）。"""
    naive_expiry = (NOW - timedelta(days=30)).replace(tzinfo=None)
    entry = _entry("m1", expires_at=naive_expiry)
    assert is_purgeable(entry, now=NOW) is True


def test_purge_cutoff_uses_the_given_grace_days() -> None:
    assert purge_cutoff(now=NOW, grace_days=30) == NOW - timedelta(days=30)
    assert purge_cutoff(now=NOW, grace_days=0) == NOW


def test_the_grace_boundary_is_inclusive() -> None:
    """到期时刻正好等于 cutoff → 可清理（``<=``）。"""
    entry = _entry("m1", expires_at=NOW - timedelta(days=DEFAULT_GRACE_DAYS))
    assert is_purgeable(entry, now=NOW) is True


def test_just_inside_the_grace_period_is_not_purgeable() -> None:
    entry = _entry("m1", expires_at=NOW - timedelta(days=DEFAULT_GRACE_DAYS) + timedelta(seconds=1))
    assert is_purgeable(entry, now=NOW) is False


# ── PurgeReport ────────────────────────────────────────────────────────────


def test_the_audit_detail_has_exactly_the_five_counters() -> None:
    """审计键名就是治理台看板的字段名 —— 改名会让历史记录查不到。"""
    report = PurgeReport(
        scanned=3,
        purged=2,
        failed=1,
        backend_records_deleted=2,
        history_rows_deleted=5,
    )
    assert report.as_audit_detail() == {
        "scanned": 3,
        "purged": 2,
        "failed": 1,
        "backend_records_deleted": 2,
        "history_rows_deleted": 5,
    }


def test_the_audit_detail_does_not_leak_the_failed_memory_ids() -> None:
    """审计只留计数，不留条目标识。"""
    report = PurgeReport(failed_memory_ids=["m1"])
    assert "failed_memory_ids" not in report.as_audit_detail()


# ── RetentionCleaner.plan ──────────────────────────────────────────────────


def test_plan_passes_the_configured_grace_days_to_the_repository() -> None:
    """仓储的 ``before`` 必须按配置的宽限期算，不是默认值。

    宽限期配 30 天却按 7 天取，会把还没到期 20 天的记忆删掉。
    """
    repository = _RecordingRepository([])
    cleaner = RetentionCleaner(
        repository=repository, backend=_RecordingBackend(), grace_days=30, batch_size=7
    )

    cleaner.plan(now=NOW)

    assert repository.calls[0]["before"] == NOW - timedelta(days=30)


def test_plan_passes_the_batch_size_as_the_limit() -> None:
    repository = _RecordingRepository([])
    cleaner = RetentionCleaner(repository=repository, backend=_RecordingBackend(), batch_size=7)

    cleaner.plan(now=NOW)

    assert repository.calls[0]["limit"] == 7


def test_plan_passes_the_scope_and_user_filters() -> None:
    repository = _RecordingRepository([])
    cleaner = RetentionCleaner(
        repository=repository,
        backend=_RecordingBackend(),
        memory_scope_id="acme",
        user_id="user-7",
    )

    cleaner.plan(now=NOW)

    assert repository.calls[0]["memory_scope_id"] == "acme"
    assert repository.calls[0]["user_id"] == "user-7"


def test_plan_applies_the_configured_grace_days_when_filtering() -> None:
    """仓储多给了条目时，本地二次过滤也要按配置的宽限期。

    10 天前到期、宽限期 30 天 → 还不该删。
    """
    repository = _RecordingRepository([_entry("m1", expires_at=NOW - timedelta(days=10))])
    cleaner = RetentionCleaner(repository=repository, backend=_RecordingBackend(), grace_days=30)

    assert cleaner.plan(now=NOW) == []


def test_plan_keeps_entries_past_the_configured_grace_period() -> None:
    repository = _RecordingRepository([_entry("m1", expires_at=NOW - timedelta(days=31))])
    cleaner = RetentionCleaner(repository=repository, backend=_RecordingBackend(), grace_days=30)

    assert [entry.memory_id for entry in cleaner.plan(now=NOW)] == ["m1"]


def test_an_empty_user_id_becomes_none() -> None:
    """空串 user_id 归一成 None —— 传空串会让仓储按「用户是空串」过滤。"""
    cleaner = RetentionCleaner(
        repository=_RecordingRepository([]), backend=_RecordingBackend(), user_id=""
    )
    assert cleaner.user_id is None


def test_the_configured_user_id_is_kept() -> None:
    cleaner = RetentionCleaner(
        repository=_RecordingRepository([]), backend=_RecordingBackend(), user_id="user-7"
    )
    assert cleaner.user_id == "user-7"


def test_the_batch_size_is_stored() -> None:
    cleaner = RetentionCleaner(repository=_RecordingRepository([]), backend=_RecordingBackend())
    assert cleaner.batch_size == 500


# ── RetentionCleaner.run ───────────────────────────────────────────────────


def test_run_deletes_the_backend_record_the_history_and_the_index_row() -> None:
    repository = _RecordingRepository([_entry("m1", expires_at=NOW - timedelta(days=30))])
    backend = _RecordingBackend()
    cleaner = RetentionCleaner(repository=repository, backend=backend)

    report = cleaner.run(now=NOW)

    assert report.purged == 1
    assert backend.deleted == ["backend-m1"]
    assert repository.deleted_rows == ["m1"]


def test_run_never_purges_the_original_messages() -> None:
    """``purge_all_messages`` 必须是 False。

    原始消息属于轮次，按正文删会把别的记忆的来源一起删掉（模块文档的硬约定）。
    """
    repository = _RecordingRepository([_entry("m1", expires_at=NOW - timedelta(days=30))])
    backend = _RecordingBackend()
    cleaner = RetentionCleaner(repository=repository, backend=backend)

    cleaner.run(now=NOW)

    assert backend.purged[0]["purge_all_messages"] is False
    assert backend.purged[0]["message_texts"] == []


def test_run_passes_the_entry_scope_and_user_to_the_history_purge() -> None:
    repository = _RecordingRepository(
        [_entry("m1", expires_at=NOW - timedelta(days=30), scope="acme", user_id="user-9")]
    )
    backend = _RecordingBackend()
    cleaner = RetentionCleaner(repository=repository, backend=backend)

    cleaner.run(now=NOW)

    assert backend.purged[0]["namespace"] == "acme"
    assert backend.purged[0]["user_id"] == "user-9"
    assert backend.purged[0]["memory_ids"] == ["backend-m1"]


def test_run_counts_every_scanned_entry() -> None:
    """扫描数是「这一轮看了多少条」，不是「删了多少条」。"""
    repository = _RecordingRepository(
        [
            _entry("m1", expires_at=NOW - timedelta(days=30)),
            _entry("m2", expires_at=NOW - timedelta(days=31)),
            _entry("m3", expires_at=NOW - timedelta(days=32)),
        ]
    )
    cleaner = RetentionCleaner(repository=repository, backend=_RecordingBackend())

    report = cleaner.run(now=NOW)

    assert report.scanned == 3
    assert report.purged == 3
    assert report.backend_records_deleted == 3


def test_run_accumulates_the_deleted_history_rows() -> None:
    """history_rows_deleted 是**累加**值，不是最后一条的值。"""
    repository = _RecordingRepository(
        [
            _entry("m1", expires_at=NOW - timedelta(days=30)),
            _entry("m2", expires_at=NOW - timedelta(days=31)),
        ]
    )
    cleaner = RetentionCleaner(repository=repository, backend=_RecordingBackend(history=3))

    report = cleaner.run(now=NOW)

    assert report.history_rows_deleted == 6


def test_run_counts_zero_history_rows_when_the_backend_reports_none() -> None:
    """后端没返回 ``history`` 键 → 记 0，不是 1。"""
    repository = _RecordingRepository([_entry("m1", expires_at=NOW - timedelta(days=30))])
    cleaner = RetentionCleaner(repository=repository, backend=_RecordingBackend(history=None))

    report = cleaner.run(now=NOW)

    assert report.history_rows_deleted == 0


def test_a_dry_run_scans_but_deletes_nothing() -> None:
    """dry_run 只统计不删 —— 上线前先看清楚要删哪些。"""
    repository = _RecordingRepository(
        [
            _entry("m1", expires_at=NOW - timedelta(days=30)),
            _entry("m2", expires_at=NOW - timedelta(days=31)),
        ]
    )
    backend = _RecordingBackend()
    cleaner = RetentionCleaner(repository=repository, backend=backend)

    report = cleaner.run(now=NOW, dry_run=True)

    assert report.scanned == 2
    assert report.purged == 0
    assert backend.deleted == []
    assert repository.deleted_rows == []


def test_one_failure_does_not_stop_the_batch() -> None:
    """单条失败不中断整批：失败记下来让下一轮重试，成功的照常清。"""
    repository = _RecordingRepository(
        [
            _entry("m1", expires_at=NOW - timedelta(days=30)),
            _entry("m2", expires_at=NOW - timedelta(days=31)),
            _entry("m3", expires_at=NOW - timedelta(days=32)),
        ]
    )
    backend = _RecordingBackend(fail_on="backend-m2")
    cleaner = RetentionCleaner(repository=repository, backend=backend)

    report = cleaner.run(now=NOW)

    assert report.scanned == 3
    assert report.purged == 2
    assert report.failed == 1
    assert report.failed_memory_ids == ["m2"]


def test_every_failure_is_counted_separately() -> None:
    """多条失败要逐条计数 —— 记成 1 就看不出这批烂了几条。"""
    repository = _RecordingRepository(
        [
            _entry("m1", expires_at=NOW - timedelta(days=30)),
            _entry("m2", expires_at=NOW - timedelta(days=31)),
        ]
    )
    backend = _RecordingBackend(fail_on="backend-m1")
    cleaner = RetentionCleaner(repository=repository, backend=backend)
    backend.fail_on = None

    class _AlwaysFailing(_RecordingBackend):
        def delete(self, memory_id: str) -> None:
            raise RuntimeError("backend delete failed")

    backend = _AlwaysFailing()
    cleaner = RetentionCleaner(repository=repository, backend=backend)

    report = cleaner.run(now=NOW)

    assert report.failed == 2
    assert report.failed_memory_ids == ["m1", "m2"]
    assert report.purged == 0


def test_run_without_an_explicit_now_uses_utc() -> None:
    """不传 now 时按 UTC 当前时间算 —— 用本地时间会让边界差几个小时。"""
    repository = _RecordingRepository([])
    cleaner = RetentionCleaner(repository=repository, backend=_RecordingBackend())

    cleaner.run()

    before = repository.calls[0]["before"]
    assert before.tzinfo is not None
    assert before.utcoffset() == timedelta(0)


def test_run_logs_the_counts_it_deleted(log_sink: list[dict[str, Any]]) -> None:
    """作业结束要打出一条带计数的日志 —— 排障时看的就是这一行。"""
    repository = _RecordingRepository([_entry("m1", expires_at=NOW - timedelta(days=30))])
    cleaner = RetentionCleaner(repository=repository, backend=_RecordingBackend())

    cleaner.run(now=NOW)

    finished = [record for record in log_sink if "retention purge finished" in record["message"]]
    assert len(finished) == 1
    record = finished[0]
    assert record["message"] == "retention purge finished"
    assert record["scanned"] == 1
    assert record["purged"] == 1
    assert record["dry_run"] is False
    assert record["grace_days"] == DEFAULT_GRACE_DAYS
    assert record["memory_scope_id"] == "*"


def test_run_logs_the_failure_with_the_memory_id_and_the_error(
    log_sink: list[dict[str, Any]],
) -> None:
    """失败日志要带上是哪条、什么错 —— 否则只能重跑一遍才知道。"""
    repository = _RecordingRepository([_entry("m1", expires_at=NOW - timedelta(days=30))])
    cleaner = RetentionCleaner(
        repository=repository, backend=_RecordingBackend(fail_on="backend-m1")
    )

    cleaner.run(now=NOW)

    failures = [record for record in log_sink if "purge failed" in record["message"]]
    assert len(failures) == 1
    record = failures[0]
    assert record["message"] == "retention purge failed for one memory"
    assert record["memory_id"] == "m1"
    assert record["error_type"] == "RuntimeError"
    assert "backend delete failed" in record["error"]


def test_the_failure_log_truncates_a_long_error(log_sink: list[dict[str, Any]]) -> None:
    """错误文本截到 200 字符 —— 更长的会把日志行撑爆。"""
    long_error = "x" * 500

    class _LongError(_RecordingBackend):
        def delete(self, memory_id: str) -> None:
            raise RuntimeError(long_error)

    repository = _RecordingRepository([_entry("m1", expires_at=NOW - timedelta(days=30))])
    cleaner = RetentionCleaner(repository=repository, backend=_LongError())

    cleaner.run(now=NOW)

    failures = [record for record in log_sink if "purge failed" in record["message"]]
    assert len(failures[0]["error"]) == 200


# ── RetentionCleaner.record_audit ──────────────────────────────────────────


def test_the_audit_entry_names_the_purge_action() -> None:
    """动作名是 ``memory.retention.purge`` —— 不与擦除混用，两者在审计上要能分开看。"""
    repository = _RecordingRepository([])
    cleaner = RetentionCleaner(repository=repository, backend=_RecordingBackend())

    cleaner.record_audit(PurgeReport(scanned=1, purged=1))

    assert repository.audits[0].action == "memory.retention.purge"


def test_the_audit_entry_defaults_to_the_retention_operator() -> None:
    repository = _RecordingRepository([])
    cleaner = RetentionCleaner(repository=repository, backend=_RecordingBackend())

    cleaner.record_audit(PurgeReport())

    assert repository.audits[0].operator == "retention-job"


def test_the_audit_entry_keeps_an_explicit_operator() -> None:
    repository = _RecordingRepository([])
    cleaner = RetentionCleaner(repository=repository, backend=_RecordingBackend())

    cleaner.record_audit(PurgeReport(), operator="alice")

    assert repository.audits[0].operator == "alice"


def test_the_audit_target_is_the_wildcard_without_a_scope() -> None:
    """没配 scope → target 是 ``*``（全局作业）。"""
    repository = _RecordingRepository([])
    cleaner = RetentionCleaner(repository=repository, backend=_RecordingBackend())

    cleaner.record_audit(PurgeReport())

    assert repository.audits[0].target == "*"


def test_the_audit_target_is_the_scope_when_configured() -> None:
    repository = _RecordingRepository([])
    cleaner = RetentionCleaner(
        repository=repository, backend=_RecordingBackend(), memory_scope_id="acme"
    )

    cleaner.record_audit(PurgeReport())

    assert repository.audits[0].target == "acme"


def test_the_audit_id_is_timestamped() -> None:
    """审计 ID 要带时间戳 —— 同一秒跑两轮时 ID 不能撞。"""
    repository = _RecordingRepository([])
    cleaner = RetentionCleaner(repository=repository, backend=_RecordingBackend())

    cleaner.record_audit(PurgeReport())

    audit_id = repository.audits[0].audit_id
    assert audit_id.startswith("audit-retention-")
    assert len(audit_id) == len("audit-retention-") + 20
    assert audit_id[len("audit-retention-") :].isdigit()


def test_the_audit_id_carries_the_utc_timestamp() -> None:
    """审计 ID 里的时间戳是 UTC —— 用本地时间会让跨时区检索对不上。"""
    repository = _RecordingRepository([])
    cleaner = RetentionCleaner(repository=repository, backend=_RecordingBackend())

    before = datetime.now(UTC)
    cleaner.record_audit(PurgeReport())
    after = datetime.now(UTC)

    stamp = repository.audits[0].audit_id.removeprefix("audit-retention-")
    parsed = datetime.strptime(stamp, "%Y%m%d%H%M%S%f").replace(tzinfo=UTC)
    assert before - timedelta(seconds=1) <= parsed <= after + timedelta(seconds=1)


def test_the_audit_created_at_is_timezone_aware() -> None:
    """created_at 必须是 aware 的 UTC —— naive 时间会被当成本地时间。"""
    repository = _RecordingRepository([])
    cleaner = RetentionCleaner(repository=repository, backend=_RecordingBackend())

    cleaner.record_audit(PurgeReport())

    created_at = repository.audits[0].created_at
    assert created_at.tzinfo is not None
    assert created_at.utcoffset() == timedelta(0)


def test_the_audit_detail_carries_the_counters_only() -> None:
    repository = _RecordingRepository([])
    cleaner = RetentionCleaner(repository=repository, backend=_RecordingBackend())

    cleaner.record_audit(PurgeReport(scanned=4, purged=2, failed=1))

    assert repository.audits[0].detail == {
        "scanned": 4,
        "purged": 2,
        "failed": 1,
        "backend_records_deleted": 0,
        "history_rows_deleted": 0,
    }
