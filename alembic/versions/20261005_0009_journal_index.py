"""add index for L1 fallback list_rounds DESC path (S12 performance)

Revision ID: 20261005_0009
Revises: 20261005_0008
Create Date: 2026-10-05

``get_l1`` 回源时按 ``(user_id, memory_scope_id, session_id, source_timestamp DESC)``
读最近 L1_CACHE_LIMIT 行。原 schema 上只有 session_id 单列索引，长生命周期 session
在缓存 miss 路径下做全量扫描后排序。补充复合索引让缓存回源失效路径退化为索引扫描。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261005_0009"
down_revision: str | None = "20261005_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_ins_journal_user_scope_session_ts",
        "ins_summary_round_journal",
        ["user_id", "memory_scope_id", "session_id", "source_timestamp"],
    )


def downgrade() -> None:
    op.drop_index("ix_ins_journal_user_scope_session_ts", "ins_summary_round_journal")
