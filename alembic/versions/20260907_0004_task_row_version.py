"""add row_version to ins_memory_task (P1a: task optimistic lock)

Revision ID: 20260907_0004
Revises: 20260907_0003
Create Date: 2026-09-07
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260907_0004"
down_revision: str | None = "20260907_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ins_memory_task",
        sa.Column(
            "row_version",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("ins_memory_task", "row_version")
