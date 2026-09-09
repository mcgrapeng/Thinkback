"""Database migration utilities for automatic schema upgrades on startup.

Mirrors ``innies-biz``'s ``migration::Migrator::up()`` call during app
startup: on FastAPI lifespan entry we block until ``alembic upgrade head``
has applied all pending revisions, then proceed to serve traffic.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from alembic.config import Config
from loguru import logger

from alembic import command

_PROJECT_ROOT = Path(__file__).resolve().parents[4]


def _build_alembic_config() -> Config:
    """构建指向项目根目录的 Alembic ``Config``。"""

    alembic_ini = _PROJECT_ROOT / "alembic.ini"
    if not alembic_ini.exists():
        raise FileNotFoundError(
            f"alembic.ini not found at {alembic_ini}. "
            "Cannot run migrations without Alembic configuration."
        )
    cfg = Config(str(alembic_ini))
    cfg.set_main_option("script_location", str(_PROJECT_ROOT / "alembic"))
    return cfg


def run_migrations() -> None:
    """执行 ``alembic upgrade head``：把数据库升到最新版本。

    Raises:
        Exception: If migration fails for any reason.
    """
    logger.info("Starting database migration")
    try:
        cfg = _build_alembic_config()
        logger.info("Executing alembic upgrade head")
        command.upgrade(cfg, "head")
        logger.info("Database migration completed successfully")
    except Exception as exc:
        logger.error(f"Database migration failed: {exc}")
        raise


async def run_migrations_async() -> None:
    """FastAPI lifespan 兼容的异步包装。

    Alembic 的 Python API 是同步的，而 ``env.py`` 通过 ``asyncio.run()``
    驱动 async 引擎——后者拒绝在已有事件循环中运行（FastAPI lifespan
    就是这种情况）。因此把同步 ``run_migrations`` 丢到工作线程（没有
    事件循环）里跑，再 ``await`` 结果。
    """
    await asyncio.to_thread(run_migrations)


__all__ = ["run_migrations", "run_migrations_async"]
