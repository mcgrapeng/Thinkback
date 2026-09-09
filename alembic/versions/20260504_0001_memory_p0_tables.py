"""create memory p0 tables

Revision ID: 20260504_0001
Revises:
Create Date: 2026-05-04
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260504_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ins_current_summary",
        sa.Column("summary_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("memory_scope_id", sa.String(length=128), nullable=False),
        sa.Column("summary_text", sa.Text(), nullable=False),
        sa.Column("summary_cursor_round", sa.String(length=128), nullable=True),
        sa.Column("latest_source_round_id", sa.String(length=128), nullable=True),
        sa.Column("latest_source_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("summary_state", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("summary_id"),
        sa.UniqueConstraint("user_id", "memory_scope_id", name="uq_ins_current_summary_scope"),
    )
    op.create_index("ix_ins_current_summary_user_id", "ins_current_summary", ["user_id"])
    op.create_index(
        "ix_ins_current_summary_memory_scope_id", "ins_current_summary", ["memory_scope_id"]
    )

    op.create_table(
        "ins_summary_round_journal",
        sa.Column("journal_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("memory_scope_id", sa.String(length=128), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("round_id", sa.String(length=128), nullable=False),
        sa.Column("round_index", sa.Integer(), nullable=True),
        sa.Column("round_fingerprint", sa.String(length=128), nullable=False),
        sa.Column("messages", sa.JSON(), nullable=False),
        sa.Column("source_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("round_state", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("journal_id"),
    )
    op.create_index(
        "ix_ins_summary_round_journal_user_id", "ins_summary_round_journal", ["user_id"]
    )
    op.create_index(
        "ix_ins_summary_round_journal_memory_scope_id",
        "ins_summary_round_journal",
        ["memory_scope_id"],
    )
    op.create_index(
        "ix_ins_summary_round_journal_session_id", "ins_summary_round_journal", ["session_id"]
    )
    op.create_index(
        "ix_ins_summary_round_journal_round_id",
        "ins_summary_round_journal",
        ["round_id"],
        unique=True,
    )

    op.create_table(
        "ins_memory",
        sa.Column("memory_id", sa.String(length=64), nullable=False),
        sa.Column("backend_memory_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("memory_scope_id", sa.String(length=128), nullable=False),
        sa.Column("source_refs", sa.JSON(), nullable=False),
        sa.Column("source_type", sa.String(length=64), nullable=False),
        sa.Column("memory_status", sa.String(length=32), nullable=False),
        sa.Column("data_classification", sa.String(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("memory_text", sa.Text(), nullable=False),
        sa.Column("memory_type", sa.String(length=64), nullable=True),
        sa.Column("backend_categories", sa.JSON(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("memory_id"),
    )
    op.create_index("ix_ins_memory_backend_memory_id", "ins_memory", ["backend_memory_id"])
    op.create_index("ix_ins_memory_user_id", "ins_memory", ["user_id"])
    op.create_index("ix_ins_memory_memory_scope_id", "ins_memory", ["memory_scope_id"])

    op.create_table(
        "ins_memory_task",
        sa.Column("task_id", sa.String(length=128), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("op_type", sa.String(length=64), nullable=False),
        sa.Column("scope", sa.JSON(), nullable=False),
        sa.Column("operation_id", sa.String(length=128), nullable=True),
        sa.Column("history_version", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("task_id"),
    )
    op.create_index("ix_ins_memory_task_request_id", "ins_memory_task", ["request_id"])
    op.create_index("ix_ins_memory_task_op_type", "ins_memory_task", ["op_type"])
    op.create_index("ix_ins_memory_task_operation_id", "ins_memory_task", ["operation_id"])
    op.create_index("ix_ins_memory_task_status", "ins_memory_task", ["status"])


def downgrade() -> None:
    op.drop_table("ins_memory_task")
    op.drop_table("ins_memory")
    op.drop_table("ins_summary_round_journal")
    op.drop_table("ins_current_summary")
