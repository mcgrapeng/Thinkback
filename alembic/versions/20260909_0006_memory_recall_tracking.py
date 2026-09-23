"""add last_recalled_at / recall_count to ins_memory (P2#7: decay signals)

Revision ID: 20260909_0006
Revises: 20260908_0005
Create Date: 2026-09-09
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260909_0006"
down_revision: str | None = "20260908_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ins_memory",
        sa.Column("last_recalled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "ins_memory",
        sa.Column("recall_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("ins_memory", "recall_count")
    op.drop_column("ins_memory", "last_recalled_at")
