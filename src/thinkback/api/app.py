"""innies-memory FastAPI application factory."""

from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from time import perf_counter

from fastapi import FastAPI, Request, Response
from loguru import logger

from thinkback.api.health import router as health_router
from thinkback.api.memory import router as memory_router
from thinkback.infra.config import settings
from thinkback.infra.logging import clear_trace_id, configure_logging, set_trace_id

OPENAPI_DESCRIPTION = """
innies-memory 首版主链路记忆服务 API。

图形化 API 文档：

- Swagger UI: `/docs`
- ReDoc: `/redoc`
- OpenAPI JSON: `/openapi.json`

本 API 覆盖记忆写入、召回、管理、删除、后台任务查询和健康检查。
真实质量、性能与稳定性结论应以脚本输出和发布归档为准。
"""

OPENAPI_TAGS = [
    {
        "name": "health",
        "description": "服务健康、存活和依赖就绪检查。",
    },
    {
        "name": "memory",
        "description": "记忆写入、召回、管理、删除和后台任务查询。",
    },
]


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:  # noqa: ARG001
    """FastAPI 生命周期管理。

    启动阶段：
    1. 运行 Alembic 迁移，确保数据库结构已对齐；
    2. lazy-create Milvus 数据库（避免 mem0 在写时碰到 code=800）；
    3. 如启用 gRPC，则启动内置 gRPC server 并开启 reflection。

    退出阶段：
    1. 排空 L3 后台队列；
    2. 关闭 L3 executor（仅当 service 自身拥有时）；
    3. 关闭 SqlAlchemyMemoryRepository 的 worker 事件循环；
    4. 关闭 gRPC server（带 grace）；
    5. 释放 asyncpg 连接池。

    任何步骤失败都不应阻塞其它步骤 —— 全部用 try/except + warning 兜底。
    """

    logger.info("memory service lifespan startup started")
    # Run database migrations on startup
    from thinkback.infra.database.migration import run_migrations_async

    try:
        await run_migrations_async()
        logger.info("memory service database migrations applied")
    except Exception as exc:
        logger.error(f"Failed to run database migrations: {exc}")
        raise

    # Make sure the configured Milvus database exists; mem0 won't create it
    # itself and would otherwise fail every append with code 800 once it tries
    # to write. Best-effort: log a warning if the lazy-create fails so the
    # service still boots in degraded mode.
    from thinkback.infra.milvus import ensure_milvus_database

    ensure_milvus_database(
        milvus_url=settings.milvus_url,
        milvus_database=settings.milvus_database,
        milvus_token=settings.milvus_password or None,
    )

    # 启动恢复：回收上一个进程实例被硬杀时遗留的孤儿 running 任务，
    # 避免 GetTask 客户端永远轮询。内部已按阈值做条件回收并吞掉失败。
    from thinkback.api.dependencies import get_memory_service

    reclaimed_tasks = get_memory_service().reclaim_orphan_running_tasks()
    if reclaimed_tasks:
        logger.bind(reclaimed_count=len(reclaimed_tasks)).warning(
            "memory orphan running tasks reclaimed on startup"
        )

    grpc_server = None
    if settings.grpc_enabled:
        from thinkback.rpc.server import create_server

        grpc_server, address = create_server(
            get_memory_service(),
            host=settings.grpc_host,
            port=settings.grpc_port,
            max_workers=settings.grpc_max_workers,
            max_concurrent_rpcs=settings.grpc_max_concurrent_rpcs or None,
        )
        grpc_server.start()
        logger.bind(address=address).info("grpc server started")
    logger.info("memory service lifespan startup completed")
    yield
    # Drain in-flight L3 background tasks before shutting down executors,
    # so async-mode append/delete/rebuild tasks don't get stuck in RUNNING.
    # Then explicitly shutdown the L3 executor (for owned executors) so
    # SIGTERM exits promptly instead of hanging on idle workers.
    from thinkback.api.dependencies import _memory_service

    if _memory_service is not None:
        _memory_service.drain_l3_background_tasks(timeout=30)
        _memory_service.shutdown_l3_executor(wait=False)
        logger.info("memory l3 background tasks drained")
        # 防御性关闭 SqlAlchemyMemoryRepository 的 worker 事件循环，
        # 避免 daemon=True 丢弃 in-flight run_coroutine_threadsafe 任务、
        # 导致 DB session 未 close 而泄漏。
        try:
            _memory_service.repository.close(timeout=5.0)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"memory repository close failed: {exc}")
    if grpc_server is not None:
        grpc_server.stop(grace=settings.grpc_shutdown_grace_seconds)
        logger.info("grpc server stopped")

    from thinkback.infra.database.engine import engine as db_engine
    from thinkback.infra.database.engine import readiness_engine

    try:
        await db_engine.dispose()
        await readiness_engine.dispose()
        logger.info("database engine connection pool disposed")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"database engine dispose failed: {exc}")


def _register_request_middleware(app: FastAPI) -> None:
    """注册 trace_id + 访问日志中间件。

    - 从请求头 ``X-Trace-Id`` / ``X-Request-Id`` 取 trace_id，没有则生成；
    - 写入 ContextVar，让所有 loguru 日志自动带上 ``trace_id``；
    - 响应回写 ``X-Trace-Id``，方便调用方串联链路；
    - finally 中清空 ContextVar，避免下一个请求误继承 trace_id。
    """

    @app.middleware("http")
    async def trace_and_access_log(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming_trace_id = request.headers.get("X-Trace-Id") or request.headers.get("X-Request-Id")
        trace_id = set_trace_id(incoming_trace_id.strip() if incoming_trace_id else None)
        started_at = perf_counter()
        response: Response | None = None
        try:
            response = await call_next(request)
            response.headers["X-Trace-Id"] = trace_id
            return response
        finally:
            duration_ms = (perf_counter() - started_at) * 1000
            logger.debug(
                "HTTP request completed",
                method=request.method,
                path=request.url.path,
                status_code=response.status_code if response else 500,
                duration_ms=round(duration_ms, 2),
            )
            clear_trace_id()


def create_app() -> FastAPI:
    """创建并配置 FastAPI 应用实例。

    流程：
    1. 配置 loguru 日志；
    2. 构造 FastAPI（挂 lifespan、中间件、OpenAPI 元数据）；
    3. 注册 trace_id 中间件；
    4. include health + memory 路由；
    5. 添加根路径的 ``/`` 探活接口。
    """

    configure_logging()
    logger.bind(
        app_name=settings.app_name,
        app_version=settings.app_version,
        environment=settings.environment,
        debug=settings.debug,
    ).info("memory service app creation started")
    app = FastAPI(
        lifespan=_lifespan,
        title=f"{settings.app_name} Memory Service API",
        summary="Innies 记忆服务接口文档",
        version=settings.app_version,
        description=OPENAPI_DESCRIPTION,
        openapi_tags=OPENAPI_TAGS,
        servers=[
            {"url": "http://localhost:8000", "description": "本地调试"},
            {"url": "/", "description": "当前部署环境"},
        ],
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        debug=settings.debug,
    )
    _register_request_middleware(app)
    app.include_router(health_router)
    app.include_router(memory_router)

    @app.get("/")
    async def root() -> dict[str, str]:
        """根路径探活：返回服务名 + 版本号 + 状态，不依赖任何外部依赖。"""

        return {
            "name": settings.app_name,
            "version": settings.app_version,
            "status": "running",
            "scope": "memory-service",
        }

    logger.info("memory service app created")
    return app


app = create_app()

__all__ = ["app", "create_app"]
