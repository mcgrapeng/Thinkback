"""给 ins_current_summary 加乐观锁版本列

Revision ID: 20261027_0014
Revises: 20261022_0013
Create Date: 2026-10-27

为什么加：L2 摘要的写入方有两个（每次 append 的同步拼接写、后台 LLM 刷新），
删除还会把摘要置 DIRTY。原来只有「preserve_llm 不覆盖 llm 版」一个条件，
没有版本判断，于是：

- 删除后，在飞中的旧 LLM 刷新仍能把旧快照写回，把 DIRTY 改回 ACTIVE，
  已删内容从摘要里复活；
- 两个写入者拿到不同时刻的快照时，后写的旧快照会覆盖新快照，摘要游标回退。

row_version 每次成功写入 +1（删除、状态变更同样 +1），写入方带上
expected_row_version，版本不匹配就写不进来。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261027_0014"
down_revision: str | None = "20261022_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ins_current_summary",
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("ins_current_summary", "row_version")
