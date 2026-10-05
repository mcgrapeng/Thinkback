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
from typing import Any

from loguru import logger

from thinkback.infra.config import settings
from thinkback.infra.database.engine import check_database

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
    """检查 Milvus 向量库连接 + collection schema 与 BM25 兼容。

    L3 在没 OPENAI_API_KEY 时直接 skip（开发环境）；
    生产环境要求 LLM 凭据齐全，否则 not_ready。

    S8 (W-3 落地): mem0 v2 混合检索（BM25 + 向量）依赖 v3 schema（含 ``text``
    字段 + ``sparse`` 向量）。旧 schema collection 启动期不阻断会**静默**降级
    为纯向量检索（mem0 内部只打 warning，运维无感）。这里在 readiness 上
    探 collection schema —— 缺 ``text`` / ``sparse`` 任一字段即 not_ready，
    把"静默降级"升级为"启动阻断"。运维侧按 W-3 决策切换新 collection
    名 + 迁移存量数据。
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
        collection=settings.memory_milvus_collection,
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
        # S8: 校验 collection 是否具备 mem0 v3 混合检索所需的字段。
        collection_name = settings.memory_milvus_collection
        has_v3_schema = await _collection_has_v3_schema(client, collection_name)
        if not has_v3_schema:
            detail = (
                f"milvus collection {collection_name!r} missing v3 BM25 fields "
                "(text/sparse); mem0 hybrid retrieval will silently downgrade to "
                "pure vector — set MEMORY_MILVUS_COLLECTION to a fresh v3 collection"
            )
            check_log.warning("milvus collection schema missing v3 BM25 fields")
            return {"status": "not_ready", "detail": detail}
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


async def _collection_has_v3_schema(client: Any, collection_name: str) -> bool:
    """判断 Milvus collection 是否含 mem0 v3 混合检索所需字段。

    新 schema 需同时含 ``text`` 字段 + ``sparse`` 向量。任一缺失即视为旧
    schema（v2 之前的纯向量结构）。collection 不存在时返回 True（mem0 会
    按 v3 创建新 collection，符合需求）。
    """
    try:
        describe = await asyncio.to_thread(client.describe_collection, collection_name)
    except Exception:
        # collection 不存在或 pymilvus 版本不支持 describe_collection —— 视为 OK。
        return True
    fields = describe.get("fields", []) if isinstance(describe, dict) else []
    has_text = any(
        isinstance(field, dict) and field.get("name") == "text" for field in fields
    )
    has_sparse = any(
        isinstance(field, dict) and field.get("name") == "sparse" for field in fields
    )
    return has_text and has_sparse
