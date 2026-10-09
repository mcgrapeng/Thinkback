"""add ins_mem0_config (mem0 自定义配置: 提示词/抽取指令等)

Revision ID: 20261008_0011
Revises: 20261005_0010
Create Date: 2026-10-08

``ins_mem0_config`` 存储 mem0 的自定义提示词(如 custom_instructions),
治理台可编辑;每次 append 操作时实时读取,无需重启服务。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261008_0011"
down_revision: str | None = "20261005_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ins_mem0_config",
        sa.Column("config_key", sa.String(64), primary_key=True),
        sa.Column("value", sa.Text, nullable=False, server_default=""),
        sa.Column("description", sa.String(256), nullable=False, server_default=""),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("ins_mem0_config")
