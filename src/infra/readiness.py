"""Readiness aggregation for runtime dependencies.

集中编排三项外部依赖（Postgres / Milvus / mem0）的健康检查：

- 每项探针独立并发执行（``asyncio.gather``），单项超时不会拖垮整个
  readiness 端点；
- 通过 ``asyncio.wait_for`` 强制单测上限为
  ``settings.readiness_timeout_seconds``，避免某项依赖卡死把请求拖到挂起；
- 仅当 **所有** 依赖都 ready 才返回 ready；任一 not_ready 就让上层 503。

短期记忆与会话摘要只落 PostgreSQL，不再依赖任何外部缓存。
"""

import asyncio
from collections.abc import Awaitable, Callable

from loguru import logger

from innies_memory.infra.config import settings
from innies_memory.infra.database.engine import check_database

try:
    from pymilvus import MilvusClient
except ImportError:  # pragma: no cover - exercised only when packaging is broken
    MilvusClient = None

DependencyCheck = Callable[[], Awaitable[dict[str, str]]]


async def _run_dependency_check(
    name: str, check: DependencyCheck, timeout_seconds: float
) -> tuple[str, dict[str, str]]:
    """单依赖探针执行器：套上超时，把异常/超时归一为 ``not_ready``。"""

    check_log = logger.bind(dependency=name, timeout_seconds=timeout_seconds)
    check_log.debug("readiness dependency check started")
    try:
        result = await asyncio.wait_for(check(), timeout=timeout_seconds)
        completed_log = check_log.bind(status=result["status"])
        if result["status"] == "ready":
            completed_log.debug("readiness dependency check completed")
        else:
            completed_log.warning("readiness dependency check completed")
        return name, result
    except TimeoutError:
        check_log.warning("readiness dependency check timed out")
        return (
            name,
            {
                "status": "not_ready",
                "detail": f"timed out after {timeout_seconds:g}s",
            },
        )
    except Exception as exc:
        check_log.bind(error_type=type(exc).__name__).warning("readiness dependency check failed")
        return name, {"status": "not_ready", "detail": str(exc)}


async def collect_readiness(timeout_seconds: float | None = None) -> dict[str, object]:
    """聚合四项依赖探针，返回 ``{status, dependencies}``。

    任一 not_ready → 整体 not_ready，便于 K8s readiness 探针触发摘流。
    """

    effective_timeout_seconds = (
        settings.readiness_timeout_seconds if timeout_seconds is None else timeout_seconds
    )
    readiness_log = logger.bind(timeout_seconds=effective_timeout_seconds)
    readiness_log.debug("readiness aggregation started")
    checks: dict[str, DependencyCheck] = {
        "database": check_database,
        "milvus": check_milvus,
        "mem0": check_mem0_library,
    }
    dependencies = dict(
        await asyncio.gather(
            *(
                _run_dependency_check(name, check, effective_timeout_seconds)
                for name, check in checks.items()
            )
        )
    )
    status = (
        "ready" if all(item["status"] == "ready" for item in dependencies.values()) else "not_ready"
    )
    completed_log = readiness_log.bind(
        status=status,
        not_ready_dependencies=[
            name for name, item in dependencies.items() if item["status"] != "ready"
        ],
    )
    if status == "ready":
        completed_log.debug("readiness aggregation completed")
    else:
        completed_log.warning("readiness aggregation completed")
    return {"status": status, "dependencies": dependencies}


async def check_mem0_library() -> dict[str, str]:
    """检查 mem0 所需的 LLM 凭据是否可用。

    开发环境允许 LLM 未配置（skip，返回 ready）；
    生产/预发环境必须配置，否则返回 not_ready 让 readiness 失败。
    """

    if not settings.openai_api_key:
        if settings.environment == "development":
            logger.debug("mem0 library readiness check skipped: OPENAI_API_KEY not configured")
            return {"status": "ready", "detail": "skipped: OPENAI_API_KEY not configured"}
        logger.warning("mem0 library readiness check failed: OPENAI_API_KEY not configured")
        return {"status": "not_ready", "detail": "OPENAI_API_KEY is required"}
    logger.debug("mem0 library readiness check started")
    try:
        from openai import OpenAI

        def _probe_llm() -> None:
            client = OpenAI(
                api_key=settings.openai_api_key,
                base_url=settings.memory_llm_base_url or None,
            )
            try:
                client.models.list()
            finally:
                client.close()

        await asyncio.to_thread(_probe_llm)
    except Exception as exc:
        logger.bind(error_type=type(exc).__name__).warning("mem0 library readiness check failed")
        return {"status": "not_ready", "detail": str(exc)}
    logger.bind(status="ready").debug("mem0 library readiness check completed")
    return {"status": "ready", "detail": "configured"}


async def check_milvus() -> dict[str, str]:
    """检查 Milvus 向量库连接：list_collections 探针。

    L3 在没 OPENAI_API_KEY 时直接 skip（开发环境）；
    生产环境要求 LLM 凭据齐全，否则 not_ready。
    """

    if not settings.openai_api_key:
        if settings.environment == "development":
            logger.debug("milvus readiness check skipped: L3 disabled (no OPENAI_API_KEY)")
            return {"status": "ready", "detail": "skipped: L3 disabled (no OPENAI_API_KEY)"}
        logger.warning("milvus readiness check failed: OPENAI_API_KEY not configured")
        return {"status": "not_ready", "detail": "OPENAI_API_KEY is required"}
    if MilvusClient is None:
        logger.warning("milvus readiness check skipped")
        return {"status": "not_ready", "detail": "pymilvus is not installed"}
    check_log = logger.bind(
        milvus_database=settings.milvus_database,
        has_token=bool(settings.milvus_token),
    )
    check_log.debug("milvus readiness check started")
    client = None
    try:
        client = MilvusClient(
            uri=settings.milvus_url,
            token=settings.milvus_token,
            db_name=settings.milvus_database,
        )
        await asyncio.to_thread(client.list_collections)
    except Exception as exc:
        check_log.bind(error_type=type(exc).__name__).warning("milvus readiness check failed")
        return {"status": "not_ready", "detail": str(exc)}
    finally:
        if client is not None:
            close = getattr(client, "close", None)
            if close is not None:
                close()
    check_log.debug("milvus readiness check completed")
    return {"status": "ready", "detail": "ok"}
