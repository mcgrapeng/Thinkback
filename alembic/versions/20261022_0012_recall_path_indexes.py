"""recall 热路径索引。

两处按时间过滤/排序的查询缺索引：

- ``ins_memory_task.updated_at``：两处孤儿回收路径按它过滤
  （repositories/sqlalchemy.py 的 reclaim 分支），``list_tasks`` 按它排序；
- ``ins_memory(user_id, memory_scope_id, created_at)``：``active_memories`` 与
  ``list_memories`` 的过滤 + 排序走这条。

没有索引时这两条查询随表规模线性扫描 —— 召回与治理台列表是高频路径。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20261022_0012"
down_revision: str | None = "20261008_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_ins_memory_task_updated_at",
        "ins_memory_task",
        ["updated_at"],
        unique=False,
    )
    op.create_index(
        "ix_ins_memory_user_scope_created_at",
        "ins_memory",
        ["user_id", "memory_scope_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_ins_memory_user_scope_created_at", table_name="ins_memory")
    op.drop_index("ix_ins_memory_task_updated_at", table_name="ins_memory_task")
