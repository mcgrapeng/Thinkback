"""drop unused expires_at column from ins_memory (S11 cleanup)

Revision ID: 20261005_0008
Revises: 20260924_0007
Create Date: 2026-10-05

``expires_at`` was added in the initial P0 schema for a future TTL sweeper,
but the Celery-based sweeper was removed (no Redis/Celery in stack) and the
column was never read or written. Schema name-space should be reclaimed.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261005_0008"
down_revision: str | None = "20260924_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("ins_memory", "expires_at")


def downgrade() -> None:
    op.add_column(
        "ins_memory",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
