"""SQLAlchemy models for Thinkback memory tables."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from infra.database.base import Base


class CurrentSummaryRecord(Base):
    __tablename__ = "tb_current_summary"

    summary_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    character_id: Mapped[str] = mapped_column(String(128), index=True)
    summary_text: Mapped[str] = mapped_column(Text, default="")
    summary_cursor_round: Mapped[str | None] = mapped_column(String(128), nullable=True)
    latest_source_round_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    latest_source_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    summary_state: Mapped[str] = mapped_column(String(32), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (UniqueConstraint("user_id", "character_id", name="uq_current_summary_scope"),)


class SummaryRoundJournalRecord(Base):
    __tablename__ = "tb_summary_round_journal"

    journal_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    character_id: Mapped[str] = mapped_column(String(128), index=True)
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
    __tablename__ = "tb_memory"

    memory_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    backend_memory_id: Mapped[str] = mapped_column(String(128), index=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    character_id: Mapped[str] = mapped_column(String(128), index=True)
    source_refs: Mapped[list[dict[str, str]]] = mapped_column(JSON, default=list)
    source_type: Mapped[str] = mapped_column(String(64), default="chat_round")
    fact_subject: Mapped[str] = mapped_column(String(64), default="user")
    context_type: Mapped[str] = mapped_column(String(64), default="real_user")
    roleplay_mode: Mapped[str] = mapped_column(String(64), default="off")
    memory_status: Mapped[str] = mapped_column(String(32), default="ACTIVE")
    data_classification: Mapped[str] = mapped_column(String(32), default="normal")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    memory_text: Mapped[str] = mapped_column(Text, default="")
    memory_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    backend_categories: Mapped[list[str]] = mapped_column(JSON, default=list)
    memory_metadata: Mapped[dict[str, object]] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class UserCharacterStateRecord(Base):
    __tablename__ = "tb_user_character_state"

    relationship_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    character_id: Mapped[str] = mapped_column(String(128), index=True)
    relationship_summary: Mapped[str] = mapped_column(Text, default="")
    state_version: Mapped[int] = mapped_column(Integer, default=1)
    last_interaction_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    data_classification: Mapped[str] = mapped_column(String(32), default="normal")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (UniqueConstraint("user_id", "character_id", name="uq_relationship_scope"),)


class MemoryTaskRecord(Base):
    __tablename__ = "tb_memory_task"

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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
