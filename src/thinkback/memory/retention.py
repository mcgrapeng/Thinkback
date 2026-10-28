"""保留期三段：不可达 → 宽限 → 物理清理（ADR-0010）。

第一段（不可达）在写入路径上已经做完：mem0 的 ``expiration_date`` 把到期记忆
藏起来，本地索引记同一份到期时间，召回与列表按它过滤。第二段是宽限：到期之后
还要留 7 天，「用户改口」这类场景才挽回得回来。第三段是物理清理：独立的
作业把过了宽限期的事实真的删掉 —— 向量、引擎侧事实历史、治理行一起删。

边界（有意为之）：
- 原始消息（mem0 messages 表）不清。消息属于轮次，同一轮次可能还挂着没过期的
  事实，按正文删会把别的记忆的来源一起删掉。单条擦除路径也是同样口径。
- 审计只留计数与操作元数据。抑制（decay 置 SUPERSEDED）与删除在审计上必须
  能分开看，所以动作名是 ``memory.retention.purge``，不与擦除混用。
- 作业默认关闭，按 namespace 配置。开了就要有 CronJob 或人工定期跑。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger

from thinkback.domain.entities import AdminAuditEntry, MemoryIndexEntry

#: 到期后到物理清理之间的宽限天数（ADR-0010）。
DEFAULT_GRACE_DAYS = 7

#: 单批清理的条数上限。
DEFAULT_BATCH_SIZE = 500


def purge_cutoff(*, now: datetime, grace_days: int = DEFAULT_GRACE_DAYS) -> datetime:
    """到期时刻早于这个时刻的记忆可以物理清理。

    宽限期是「到期之后」再等 N 天：到期只是不可达，宽限期满了才动手。
    """

    return now - timedelta(days=grace_days)


def is_purgeable(entry: Any, *, now: datetime, grace_days: int = DEFAULT_GRACE_DAYS) -> bool:
    """这条记忆是不是已经过了宽限期、可以物理清理。

    没有到期时间的记忆（永不过期）永远不清理。
    """

    expires_at = getattr(entry, "expires_at", None)
    if not isinstance(expires_at, datetime):
        return False
    if expires_at.tzinfo is None:
        # naive 时间按 UTC 处理（asyncpg/SQLite 可能丢掉 tzinfo）。
        expires_at = expires_at.replace(tzinfo=UTC)
    return bool(expires_at <= purge_cutoff(now=now, grace_days=grace_days))


@dataclass
class PurgeReport:
    """一次清理的结果。计数进审计，失败条目留给运维重跑。"""

    scanned: int = 0
    purged: int = 0
    failed: int = 0
    backend_records_deleted: int = 0
    history_rows_deleted: int = 0
    failed_memory_ids: list[str] = field(default_factory=list)

    def as_audit_detail(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "purged": self.purged,
            "failed": self.failed,
            "backend_records_deleted": self.backend_records_deleted,
            "history_rows_deleted": self.history_rows_deleted,
        }


class RetentionCleaner:
    """保留期物理清理作业。

    依赖注入而不是自己造：作业要能离线跑（单元测试），也要能被脚本或
    CronJob 调用。
    """

    def __init__(
        self,
        *,
        repository: Any,
        backend: Any,
        grace_days: int = DEFAULT_GRACE_DAYS,
        batch_size: int = DEFAULT_BATCH_SIZE,
        memory_scope_id: str | None = None,
        user_id: str | None = None,
    ) -> None:
        self.repository = repository
        self.backend = backend
        self.grace_days = grace_days
        self.batch_size = batch_size
        self.memory_scope_id = memory_scope_id or None
        self.user_id = user_id or None

    def plan(self, *, now: datetime) -> list[MemoryIndexEntry]:
        """列出这批可以物理清理的记忆（不改动任何东西）。"""

        entries = self.repository.list_expired_memories(
            before=purge_cutoff(now=now, grace_days=self.grace_days),
            limit=self.batch_size,
            memory_scope_id=self.memory_scope_id,
            user_id=self.user_id,
        )
        return [
            entry for entry in entries if is_purgeable(entry, now=now, grace_days=self.grace_days)
        ]

    def run(self, *, now: datetime | None = None, dry_run: bool = False) -> PurgeReport:
        """跑一轮物理清理。

        dry_run 只统计不删 —— 上线前先看清楚要删哪些。
        单条失败不中断整批：失败的记下来让下一轮重试，成功的照常清。
        """

        moment = now or datetime.now(UTC)
        report = PurgeReport()
        entries = self.plan(now=moment)
        for entry in entries:
            report.scanned += 1
            if dry_run:
                continue
            try:
                self.backend.delete(entry.backend_memory_id)
                purged = self.backend.purge_history(
                    user_id=entry.user_id,
                    memory_ids=[entry.backend_memory_id],
                    namespace=entry.memory_scope_id,
                    message_texts=[],
                    purge_all_messages=False,
                )
                self.repository.delete_memory_rows([entry.memory_id])
            except Exception as exc:  # noqa: BLE001 - 单条失败不拖垮整批
                report.failed += 1
                report.failed_memory_ids.append(entry.memory_id)
                logger.bind(
                    memory_id=entry.memory_id,
                    error_type=type(exc).__name__,
                    error=str(exc)[:200],
                ).warning("retention purge failed for one memory")
                continue
            report.purged += 1
            report.backend_records_deleted += 1
            report.history_rows_deleted += int(purged.get("history", 0))
        logger.bind(
            **report.as_audit_detail(),
            dry_run=dry_run,
            grace_days=self.grace_days,
            memory_scope_id=self.memory_scope_id or "*",
        ).info("retention purge finished")
        return report

    def record_audit(self, report: PurgeReport, *, operator: str = "retention-job") -> None:
        """审计只留计数与操作元数据，不留原文。"""

        self.repository.save_admin_audit(
            AdminAuditEntry(
                audit_id=f"audit-retention-{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}",
                created_at=datetime.now(UTC),
                operator=operator,
                action="memory.retention.purge",
                target=self.memory_scope_id or "*",
                detail=report.as_audit_detail(),
            )
        )
