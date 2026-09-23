"""dedupe active backend memory ids

Revision ID: 20260512_0002
Revises: 20260504_0001
Create Date: 2026-05-12
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260512_0002"
down_revision: str | None = "20260504_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT
                    memory_id,
                    row_number() OVER (
                        PARTITION BY user_id, memory_scope_id, backend_memory_id
                        ORDER BY updated_at DESC, created_at DESC, memory_id DESC
                    ) AS rank
                FROM ins_memory
                WHERE memory_status = 'ACTIVE'
            )
            UPDATE ins_memory
            SET memory_status = 'SUPERSEDED'
            WHERE memory_id IN (
                SELECT memory_id
                FROM ranked
                WHERE rank > 1
            )
            """
        )
    )
    op.create_index(
        "uq_ins_memory_active_backend_scope",
        "ins_memory",
        ["user_id", "memory_scope_id", "backend_memory_id"],
        unique=True,
        postgresql_where=sa.text("memory_status = 'ACTIVE'"),
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_ins_memory_active_backend_scope",
        table_name="ins_memory",
        if_exists=True,
    )
