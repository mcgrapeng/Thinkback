"""add summary_kind to ins_current_summary (P0: L2 LLM 化)

Revision ID: 20260907_0003
Revises: 20260512_0002
Create Date: 2026-09-07
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260907_0003"
down_revision: str | None = "20260512_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ins_current_summary",
        sa.Column(
            "summary_kind",
            sa.String(length=32),
            nullable=False,
            server_default="concat",
        ),
    )


def downgrade() -> None:
    op.drop_column("ins_current_summary", "summary_kind")
