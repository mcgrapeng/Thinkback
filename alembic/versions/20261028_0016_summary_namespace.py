"""L2 摘要按 namespace 隔离

Revision ID: 20261028_0016
Revises: 20261028_0015
Create Date: 2026-10-28

为什么加（2026-10-28 复审）：

``ins_current_summary`` 的唯一约束是 ``(user_id, memory_scope_id)``，而
``memory_scope_id`` 存的是 session_id。同一 user、同一 session_id 在不同
namespace 下会撞进**同一条**摘要行：alpha 的 L2 摘要被 beta 覆盖，两个
namespace 的召回互相串数据。L1 轮次表已有 ``namespace`` 列，但 L2 摘要
表没有，所以摘要维度缺一个隔离键。

改动：

- ``ins_current_summary.namespace``：L2 摘要的命名空间维度。
- 唯一约束改为 ``(user_id, namespace, memory_scope_id)``。

已有行的 namespace 取 ``thinkback``：升级前 L2 摘要不区分 namespace，
升级后按 namespace 分开；历史行归入默认 namespace 是保守选择（不会把
内容外泄给别的 namespace），需要精确归属时可按 journal.namespace 回填。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261028_0016"
down_revision: str | None = "20261028_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ins_current_summary",
        sa.Column(
            "namespace",
            sa.String(length=128),
            nullable=False,
            server_default="thinkback",
        ),
    )
    op.create_index("ix_ins_current_summary_namespace", "ins_current_summary", ["namespace"])
    op.drop_constraint("uq_ins_current_summary_scope", "ins_current_summary", type_="unique")
    op.create_unique_constraint(
        "uq_ins_current_summary_scope",
        "ins_current_summary",
        ["user_id", "namespace", "memory_scope_id"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_ins_current_summary_scope", "ins_current_summary", type_="unique")
    op.create_unique_constraint(
        "uq_ins_current_summary_scope",
        "ins_current_summary",
        ["user_id", "memory_scope_id"],
    )
    op.drop_index("ix_ins_current_summary_namespace", table_name="ins_current_summary")
    op.drop_column("ins_current_summary", "namespace")
