"""Async database engine and readiness helpers."""

from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from infra.config import settings

engine = create_async_engine(settings.database_url, pool_pre_ping=True, poolclass=NullPool)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session


async def check_database() -> dict[str, str]:
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:
        return {"status": "not_ready", "detail": str(exc)}
    return {"status": "ready", "detail": "ok"}
