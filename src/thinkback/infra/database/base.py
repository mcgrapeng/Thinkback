"""SQLAlchemy declarative base.

统一所有 ORM 模型的 ``Base``，同时导入 ``models`` 让 Alembic 的
``target_metadata`` 自动发现到所有表（无需手工注册）。
"""

from contextlib import suppress

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """项目内所有 ORM 模型的父类，供 Alembic 自动迁移发现使用。"""

    pass


# Import models so Alembic target metadata sees application tables.
with suppress(ImportError):
    import thinkback.infra.database.models  # noqa: F401
