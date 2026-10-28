"""记忆生命周期列：TTL 到期、记录时刻、轮次的 namespace 与生效元数据

Revision ID: 20261028_0015
Revises: 20261027_0014
Create Date: 2026-10-28

为什么加（2026-10-27 生产审计）：

- ``ins_memory.expires_at``：TTL 之前只传给 Mem0，本地索引没有到期时刻。
  后端隐藏了过期记录，本地精确槽位回填和 /items 仍会把它端出来——过期事实
  照样进模型上下文。
- ``ins_memory.recorded_at``：区分「事实何时为真」（valid_at）与「我们何时
  记下它」。没有它就无法做 as-of 查询。
- ``ins_summary_round_journal.namespace``：namespace 是长期记忆的隔离维度，
  必须进 append 幂等身份，否则同一 round 换 namespace 会被当成已写过的同一
  轮而静默丢写。
- ``ins_summary_round_journal.request_metadata``：rebuild 回放要按原记忆类型
  （state/event/…）与 TTL 策略重放，没有它只能退回默认 profile。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261028_0015"
down_revision: str | None = "20261027_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ins_memory",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "ins_memory",
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "ins_summary_round_journal",
        sa.Column(
            "namespace",
            sa.String(length=128),
            nullable=False,
            server_default="thinkback",
        ),
    )
    op.add_column(
        "ins_summary_round_journal",
        sa.Column("request_metadata", sa.JSON(), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("ins_summary_round_journal", "request_metadata")
    op.drop_column("ins_summary_round_journal", "namespace")
    op.drop_column("ins_memory", "recorded_at")
    op.drop_column("ins_memory", "expires_at")
