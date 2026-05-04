"""SQLAlchemy declarative base."""

from contextlib import suppress

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


# Import models so Alembic target metadata sees application tables.
with suppress(ImportError):
    import infra.database.models  # noqa: F401
