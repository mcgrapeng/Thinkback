"""add ins_admin_audit (治理台 M3：admin POST 动作审计留痕)

Revision ID: 20260924_0007
Revises: 20260909_0006
Create Date: 2026-09-24
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260924_0007"
down_revision: str | None = "20260909_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ins_admin_audit",
        sa.Column("audit_id", sa.String(64), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("operator", sa.String(128), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target", sa.String(256), nullable=False, server_default=""),
        sa.Column("detail", sa.JSON(), nullable=False),
    )
    op.create_index("ix_ins_admin_audit_created_at", "ins_admin_audit", ["created_at"])
    op.create_index("ix_ins_admin_audit_operator", "ins_admin_audit", ["operator"])
    op.create_index("ix_ins_admin_audit_action", "ins_admin_audit", ["action"])


def downgrade() -> None:
    op.drop_index("ix_ins_admin_audit_action", table_name="ins_admin_audit")
    op.drop_index("ix_ins_admin_audit_operator", table_name="ins_admin_audit")
    op.drop_index("ix_ins_admin_audit_created_at", table_name="ins_admin_audit")
    op.drop_table("ins_admin_audit")
