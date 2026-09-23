"""SQLAlchemy models for innies-memory tables.

服务侧核心数据模型 4 张表的 ORM 映射。所有表都按 ``user_id`` / ``memory_scope_id``
分区索引，便于按租户快速定位；每张表都带 ``created_at`` / ``updated_at``，
由 ``onupdate=func.now()`` 自动维护。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from thinkback.infra.database.base import Base


class CurrentSummaryRecord(Base):
    """L2 当前摘要记录（每个 user+scope 一行）。

    字段含义：
    - ``summary_text``：当前生效的摘要文本；
    - ``summary_cursor_round``：该摘要基于的最后一条 round_id；
    - ``summary_state``：active/stale/dirty/rebuilding 状态机；
    - ``latest_source_round_id`` / ``latest_source_timestamp``：用于判断是否需要重算。
    ``(user_id, memory_scope_id)`` 唯一约束保证每个 scope 只有一条当前摘要。
    """

    __tablename__ = "ins_current_summary"

    summary_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    memory_scope_id: Mapped[str] = mapped_column(String(128), index=True)
    summary_text: Mapped[str] = mapped_column(Text, default="")
    summary_cursor_round: Mapped[str | None] = mapped_column(String(128), nullable=True)
    latest_source_round_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    latest_source_timestamp: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    summary_state: Mapped[str] = mapped_column(String(32), default="active")
    # L2 生成方式：concat（拼接降级）| llm（LLM 综合摘要，后台异步刷新）
    summary_kind: Mapped[str] = mapped_column(String(32), default="concat", server_default="concat")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("user_id", "memory_scope_id", name="uq_ins_current_summary_scope"),
    )


class SummaryRoundJournalRecord(Base):
    """L2 摘要 round 日志（每个 round 一行）。

    字段含义：
    - ``messages``：该 round 的所有消息原文（JSON list）；
    - ``round_fingerprint``：用于幂等去重（同一 round 写入会被识别为重复）；
    - ``round_state``：active / superseded / tombstoned；
    - ``round_id`` 唯一约束确保一个 round 只写一次。
    重建摘要时按 ``round_index`` 顺序回放即可。
    """

    __tablename__ = "ins_summary_round_journal"

    journal_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    memory_scope_id: Mapped[str] = mapped_column(String(128), index=True)
    session_id: Mapped[str] = mapped_column(String(128), index=True)
    round_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    round_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    round_fingerprint: Mapped[str] = mapped_column(String(128))
    messages: Mapped[list[dict[str, str]]] = mapped_column(JSON)
    source_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    round_state: Mapped[str] = mapped_column(String(32), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class MemoryRecord(Base):
    """L3 记忆条目主表。

    字段含义：
    - ``backend_memory_id``：mem0 / Milvus 端 id，用于同步到向量库；
    - ``source_refs``：上游来源引用（round_id / session_id 等），用于追溯与重建；
    - ``memory_status``：active / deleted / superseded / suppressed；
    - ``data_classification``：normal / personal / sensitive / restricted，控制召回脱敏；
    - ``expires_at``：可选 TTL，过期后由清理任务转 tombstone；
    - ``backend_categories``：mem0 端返回的分类标签列表；
    - ``memory_metadata``：透传给后端的元数据。
    唯一索引 ``uq_ins_memory_active_backend_scope`` 仅在 ``ACTIVE`` 时生效，
    允许同一 backend_memory_id 出现在多行（被删除/被替换）但不冲突。
    """

    __tablename__ = "ins_memory"

    memory_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    backend_memory_id: Mapped[str] = mapped_column(String(128), index=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    memory_scope_id: Mapped[str] = mapped_column(String(128), index=True)
    source_refs: Mapped[list[dict[str, str]]] = mapped_column(JSON, default=list)
    source_type: Mapped[str] = mapped_column(String(64), default="chat_round")
    memory_status: Mapped[str] = mapped_column(String(32), default="ACTIVE")
    data_classification: Mapped[str] = mapped_column(String(32), default="normal")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    memory_text: Mapped[str] = mapped_column(Text, default="")
    memory_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    backend_categories: Mapped[list[str]] = mapped_column(JSON, default=list)
    memory_metadata: Mapped[dict[str, object]] = mapped_column("metadata", JSON, default=dict)
    # P2#6 轻量双时态：事实生效/失效时间。invalid_at 非空 = 该事实已不再为真
    # （被同槽位新事实取代或被删除）；召回端据此做"新事实优先"仲裁。
    valid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    invalid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # P2#7 遗忘信号：最近召回时刻与累计召回次数（艾宾浩斯式——久未召回
    # 的记忆才会被 decay 置为 SUPPRESSED；被召回即强化）。
    last_recalled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    recall_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index(
            "uq_ins_memory_active_backend_scope",
            "user_id",
            "memory_scope_id",
            "backend_memory_id",
            unique=True,
            postgresql_where=(memory_status == "ACTIVE"),
        ),
    )


class MemoryTaskRecord(Base):
    """后台任务表（L2/L3 写入/重建/删除等异步工作）。

    字段含义：
    - ``op_type``：write_round / update_memory / delete_* / rebuild_*；
    - ``scope``：任务作用范围（JSON）；
    - ``operation_id``：与请求幂等键绑定，避免重复入队；
    - ``history_version``：重建时锁定的历史版本；
    - ``status``：pending / running / completed / failed / dead_letter；
    - ``retry_count`` / ``last_error``：重试预算与最近错误；
    - ``result``：任务完成时的输出（重建条数等）。
    """

    __tablename__ = "ins_memory_task"

    task_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(128), index=True)
    op_type: Mapped[str] = mapped_column(String(64), index=True)
    scope: Mapped[dict[str, str]] = mapped_column(JSON)
    operation_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    history_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    # P1a 乐观锁版本：UPDATE ... WHERE row_version=? CAS 写入，成功 +1
    row_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
