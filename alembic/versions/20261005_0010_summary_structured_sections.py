"""add structured_sections to ins_current_summary (G4 L2 schema 化)

Revision ID: 20261005_0010
Revises: 20261005_0009
Create Date: 2026-10-05

``structured_sections`` 是 G4 路线图落地的 schema 字段，承载 4 段画像
（主题/进行中事项/行为偏好/近期状态）。llm 综合版本填；concat 降级
版本保持空 dict。下游可机器消费各段（"行为偏好高亮"等读路径零成本用例）。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261005_0010"
down_revision: str | None = "20261005_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ins_current_summary",
        sa.Column(
            "structured_sections",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("ins_current_summary", "structured_sections")
