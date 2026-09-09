"""Async database engine and readiness helpers.

集中提供：
- ``engine``：进程级 async 引擎（asyncpg + 连接池），仅由
  ``SqlAlchemyMemoryRepository`` 的专属事件循环线程使用；
- ``SessionLocal``：session 工厂；
- ``readiness_engine`` + ``check_database``：readiness 探针专用引擎。

R-4 修复（loop 绑定隔离）：asyncpg 连接绑定创建它的事件循环。此前
``check_database`` 直接在 uvicorn 主循环上从**共享池** checkout 连接，
归还后该连接仍绑定主循环；业务仓储的专属循环随后 checkout 到这条连接，
``pool_pre_ping`` 便会抛
``RuntimeError: Future attached to a different loop`` → 首个业务请求 500。
k8s readinessProbe 周期性探测会让该错绑连接反复回池，线上表现为
随机 500。修复：readiness 探测使用独立 ``NullPool`` 引擎（每次探测
新建/关闭连接），与业务池彻底隔离；并移除无人使用、且在主循环上
共享业务池的 ``get_session`` FastAPI 依赖（同类隐患）。
"""

from __future__ import annotations

from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from innies_memory.infra.config import settings

# SqlAlchemyMemoryRepository 通过单一事件循环串行化所有数据库操作，
# 因此同一时刻最多只有 1 个活跃连接。
# pool_size=5 + max_overflow=5 = 最大 10 个连接，同时避免默认值
# (pool_size=5, max_overflow=10) 在高负载下的连接争用。
engine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=5,
    pool_timeout=10,
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

# readiness 探针专用：NullPool 保证每次探测都是独立短命连接，
# 永远不会把非业务循环创建的连接放回业务池。
readiness_engine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,
    poolclass=NullPool,
)


async def check_database() -> dict[str, str]:
    """数据库健康检查：独立连接上执行 ``SELECT 1`` 心跳。

    出错不会抛异常，而是返回 ``not_ready`` + 异常字符串，
    让 readiness 探针可以稳定返回 HTTP 503。
    """

    check_log = logger.bind(database=settings.postgres_database, host=settings.postgres_host)
    check_log.debug("database readiness check started")
    try:
        async with readiness_engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:
        check_log.bind(error_type=type(exc).__name__).warning("database readiness check failed")
        return {"status": "not_ready", "detail": str(exc)}
    check_log.debug("database readiness check completed")
    return {"status": "ready", "detail": "ok"}
