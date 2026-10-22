"""删除 ins_mem0_config（mem0 自定义配置整条链是死的）

Revision ID: 20261022_0013
Revises: 20261022_0012
Create Date: 2026-10-22

为什么删：这张表存的是「治理台可编辑的 mem0 抽取提示词」，但抽取路径
（mem0_library._build_custom_instructions）不接任何参数，直接返回硬编码
常量 —— 治理台改的提示词**从不生效**。留着这张表只会让人以为配置是活的：
改完不生效，还要排查半天才发现根本没接线。

抽取提示词现在只有一份来源：mem0_library.THINKBACK_CUSTOM_INSTRUCTIONS。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261022_0013"
down_revision: str | None = "20261022_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_table("ins_mem0_config")


def downgrade() -> None:
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
