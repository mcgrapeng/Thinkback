"""add valid_at/invalid_at to ins_memory (P2#6: temporal validity)

Revision ID: 20260908_0005
Revises: 20260907_0004
Create Date: 2026-09-08
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260908_0005"
down_revision: str | None = "20260907_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("ins_memory", sa.Column("valid_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("ins_memory", sa.Column("invalid_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("ins_memory", "invalid_at")
    op.drop_column("ins_memory", "valid_at")
