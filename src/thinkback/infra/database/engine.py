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

from thinkback.infra.config import settings

# 池的大小跟着后端读预算走：召回一次要并发取四处数据（排除集 / L1 / 摘要 /
# 本地索引），读预算 8 就是最多 8 × 4 = 32 个连接同时要。池比它小的话，
# 并发召回就在池上排队（pool_timeout=10s 后才报错），等于把预算挪了个地方。
# pool_size=10 + max_overflow=15 = 最大 25 个连接，PG 侧默认 max_connections=100
# 留得住；再大就只是把排队从信号量挪到连接池。
engine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=15,
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
