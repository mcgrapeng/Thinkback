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
        "tb_current_summary",
        sa.Column("summary_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("character_id", sa.String(length=128), nullable=False),
        sa.Column("summary_text", sa.Text(), nullable=False),
        sa.Column("summary_cursor_round", sa.String(length=128), nullable=True),
        sa.Column("latest_source_round_id", sa.String(length=128), nullable=True),
        sa.Column("latest_source_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("summary_state", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("summary_id"),
        sa.UniqueConstraint("user_id", "character_id", name="uq_current_summary_scope"),
    )
    op.create_index("ix_tb_current_summary_user_id", "tb_current_summary", ["user_id"])
    op.create_index("ix_tb_current_summary_character_id", "tb_current_summary", ["character_id"])

    op.create_table(
        "tb_summary_round_journal",
        sa.Column("journal_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("character_id", sa.String(length=128), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("round_id", sa.String(length=128), nullable=False),
        sa.Column("round_index", sa.Integer(), nullable=True),
        sa.Column("round_fingerprint", sa.String(length=128), nullable=False),
        sa.Column("messages", sa.JSON(), nullable=False),
        sa.Column("source_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("round_state", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("journal_id"),
    )
    op.create_index("ix_tb_summary_round_journal_user_id", "tb_summary_round_journal", ["user_id"])
    op.create_index(
        "ix_tb_summary_round_journal_character_id", "tb_summary_round_journal", ["character_id"]
    )
    op.create_index("ix_tb_summary_round_journal_session_id", "tb_summary_round_journal", ["session_id"])
    op.create_index("ix_tb_summary_round_journal_round_id", "tb_summary_round_journal", ["round_id"], unique=True)

    op.create_table(
        "tb_memory",
        sa.Column("memory_id", sa.String(length=64), nullable=False),
        sa.Column("backend_memory_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("character_id", sa.String(length=128), nullable=False),
        sa.Column("source_refs", sa.JSON(), nullable=False),
        sa.Column("source_type", sa.String(length=64), nullable=False),
        sa.Column("fact_subject", sa.String(length=64), nullable=False),
        sa.Column("context_type", sa.String(length=64), nullable=False),
        sa.Column("roleplay_mode", sa.String(length=64), nullable=False),
        sa.Column("memory_status", sa.String(length=32), nullable=False),
        sa.Column("data_classification", sa.String(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("memory_text", sa.Text(), nullable=False),
        sa.Column("memory_type", sa.String(length=64), nullable=True),
        sa.Column("backend_categories", sa.JSON(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("memory_id"),
    )
    op.create_index("ix_tb_memory_backend_memory_id", "tb_memory", ["backend_memory_id"])
    op.create_index("ix_tb_memory_user_id", "tb_memory", ["user_id"])
    op.create_index("ix_tb_memory_character_id", "tb_memory", ["character_id"])

    op.create_table(
        "tb_user_character_state",
        sa.Column("relationship_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("character_id", sa.String(length=128), nullable=False),
        sa.Column("relationship_summary", sa.Text(), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("last_interaction_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("data_classification", sa.String(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("relationship_id"),
        sa.UniqueConstraint("user_id", "character_id", name="uq_relationship_scope"),
    )
    op.create_index("ix_tb_user_character_state_user_id", "tb_user_character_state", ["user_id"])
    op.create_index("ix_tb_user_character_state_character_id", "tb_user_character_state", ["character_id"])

    op.create_table(
        "tb_memory_task",
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
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("task_id"),
    )
    op.create_index("ix_tb_memory_task_request_id", "tb_memory_task", ["request_id"])
    op.create_index("ix_tb_memory_task_op_type", "tb_memory_task", ["op_type"])
    op.create_index("ix_tb_memory_task_operation_id", "tb_memory_task", ["operation_id"])
    op.create_index("ix_tb_memory_task_status", "tb_memory_task", ["status"])


def downgrade() -> None:
    op.drop_table("tb_memory_task")
    op.drop_table("tb_user_character_state")
    op.drop_table("tb_memory")
    op.drop_table("tb_summary_round_journal")
    op.drop_table("tb_current_summary")
