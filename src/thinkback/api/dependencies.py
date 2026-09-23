"""API dependency accessors.

提供 FastAPI Depends 注入所需的两个工厂：
- ``get_settings``：返回单例 ``Settings``，常用于业务端点按环境分支。
- ``get_memory_backend``：按 ``Settings`` 构造 ``Mem0LibraryMemoryBackend``。
- ``get_memory_service``：单例 ``MemoryService``，懒加载 + 双重检查锁。

单例生命周期与 FastAPI lifespan 绑定；lifespan 退出时会调用
``shutdown_l3_executor`` / ``repository.close()`` 释放线程资源。
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from loguru import logger

from thinkback.domain.summarization import SummaryComposer
from thinkback.infra.config import Settings, settings
from thinkback.infra.llm import OpenAICompatibleLLMClient
from thinkback.memory.backends import (
    Mem0LibraryMemoryBackend,
    MemoryBackend,
    build_mem0_library_config,
)
from thinkback.memory.decay import MemoryDecaySweeper
from thinkback.memory.l2_refresh import build_default_composer_with
from thinkback.memory.repositories import SqlAlchemyMemoryRepository
from thinkback.memory.service import MemoryService

_memory_service: MemoryService | None = None
_memory_service_lock = threading.Lock()


def get_settings() -> Settings:
    """返回当前进程共享的 ``Settings`` 单例。"""

    logger.bind(environment=settings.environment).debug("settings dependency requested")
    return settings


def _build_shared_executor(settings: Settings) -> ThreadPoolExecutor:
    """后台 LLM/清扫共享的线程池（与 service 内部 L3 executor 同参数）。"""

    global _shared_bg_executor, _shared_bg_executor_lock
    with _shared_bg_executor_lock:
        if _shared_bg_executor is None:
            _shared_bg_executor = ThreadPoolExecutor(
                max_workers=settings.memory_l3_executor_workers,
                thread_name_prefix="thinkback-bg",
            )
        return _shared_bg_executor


_shared_bg_executor: ThreadPoolExecutor | None = None
_shared_bg_executor_lock = threading.Lock()


def build_decay_sweeper(
    config: Settings | None = None, repository: Any = None
) -> MemoryDecaySweeper | None:
    """按配置构造遗忘清扫器；``memory_decay_enabled=false`` 返回 None（默认）。"""

    current_settings = config or settings
    if not current_settings.memory_decay_enabled:
        logger.info("memory decay disabled: keeping all ACTIVE memories reachable")
        return None
    assert repository is not None
    return MemoryDecaySweeper(
        repository=repository,
        executor=_build_shared_executor(current_settings),
        min_age_days=current_settings.memory_decay_min_age_days,
        unrecalled_days=current_settings.memory_decay_unrecalled_days,
        sweep_interval_seconds=current_settings.memory_decay_sweep_interval_seconds,
    )


def get_l2_summary_composer(config: Settings | None = None) -> SummaryComposer | None:
    """按配置构造 L2 LLM 综合摘要合成器；``memory_l2_llm_enabled=false`` 时返回 None（拼接降级）。"""

    current_settings = config or settings
    if not current_settings.memory_l2_llm_enabled:
        logger.info("memory l2 llm summary disabled: keeping concat fallback")
        return None
    client = OpenAICompatibleLLMClient(
        base_url=current_settings.memory_llm_base_url,
        api_key=current_settings.openai_api_key or "not-required",
        model=current_settings.memory_llm_model,
        timeout_seconds=current_settings.memory_l2_llm_timeout_seconds,
        max_tokens=current_settings.memory_l2_llm_max_tokens,
    )
    return build_default_composer_with(client.complete)


def get_memory_backend(config: Settings | None = None) -> MemoryBackend:
    """按 ``Settings`` 构造 ``Mem0LibraryMemoryBackend``。

    每次调用都会返回一个新实例（依赖图层面），但 Mem0 内部的 ``Memory.from_config``
    仍然昂贵，因此生产调用方应复用 ``get_memory_service`` 的后端引用。
    """

    current_settings = config or settings
    backend_log = logger.bind(
        backend_type="mem0_library",
        milvus_database=current_settings.milvus_database,
        milvus_collection=current_settings.memory_milvus_collection,
        llm_model=current_settings.memory_llm_model,
        embedding_model=current_settings.memory_embedding_model,
        embedding_dims=current_settings.memory_embedding_dims,
        max_concurrent_calls=current_settings.memory_backend_max_concurrent_calls,
    )
    backend_log.debug("memory backend initialization started")
    mem0_config = build_mem0_library_config(
        llm_api_key=current_settings.openai_api_key,
        llm_base_url=current_settings.memory_llm_base_url,
        embedding_api_key=current_settings.memory_embedding_api_key,
        embedding_base_url=current_settings.memory_embedding_base_url,
        milvus_url=current_settings.milvus_url,
        milvus_token=current_settings.milvus_token,
        milvus_database=current_settings.milvus_database,
        collection_name=current_settings.memory_milvus_collection,
        llm_model=current_settings.memory_llm_model,
        embedding_model=current_settings.memory_embedding_model,
        embedding_model_dims=current_settings.memory_embedding_dims,
        history_db_path=current_settings.mem0_history_db_path,
    )
    backend = Mem0LibraryMemoryBackend(
        config=mem0_config,
        max_concurrent_calls=current_settings.memory_backend_max_concurrent_calls,
    )
    backend_log.debug("memory backend initialized")
    return backend


def get_memory_service() -> MemoryService:
    """懒加载 ``MemoryService`` 单例。

    使用双重检查锁保证：
    - 第一次访问触发真实构造（包含 SQLAlchemy 后端 + Mem0 客户端）。
    - 后续访问零成本命中。
    """

    global _memory_service
    if _memory_service is None:
        with _memory_service_lock:
            if _memory_service is None:  # double-checked locking
                service_log = logger.bind(
                    repository_type="sqlalchemy",
                    backend_type="mem0_library",
                    l3_write_mode=settings.memory_l3_write_mode,
                    l3_executor_workers=settings.memory_l3_executor_workers,
                    l3_max_pending_tasks=settings.memory_l3_max_pending_tasks,
                    l3_queue_wait_seconds=settings.memory_l3_queue_wait_seconds,
                    l2_llm_enabled=settings.memory_l2_llm_enabled,
                )
                service_log.info("memory service initialization started")
                repository = SqlAlchemyMemoryRepository()
                backend = get_memory_backend(settings)
                _memory_service = MemoryService(
                    repository=repository,
                    backend=backend,
                    l3_write_mode=settings.memory_l3_write_mode,
                    l3_executor_workers=settings.memory_l3_executor_workers,
                    l3_max_pending_tasks=settings.memory_l3_max_pending_tasks,
                    l3_queue_wait_seconds=settings.memory_l3_queue_wait_seconds,
                    l2_summary_composer=get_l2_summary_composer(settings),
                    l2_refresh_interval_rounds=settings.memory_l2_refresh_interval_rounds,
                    decay_sweeper=build_decay_sweeper(settings, repository),
                )
                service_log.info("memory service initialized")
    return _memory_service


def reset_memory_service() -> None:
    """lifespan 退出时重置 ``MemoryService`` 单例。

    shutdown 已把旧单例的 repository 事件循环关闭、L3 executor 释放；
    若单例残留，同进程的下一次生命周期（测试逐用例的 app、开发期
    reload）会在 startup 复用已关闭的仓储 —— reclaim 等首次调用拿到
    "event loop is closed" 而静默失效。旧对象引用（如 gRPC servicer
    持有的）行为不变：关闭后调用本就应 fail-fast。
    """

    global _memory_service
    with _memory_service_lock:
        _memory_service = None
