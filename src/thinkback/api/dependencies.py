"""API dependency accessors.

提供 FastAPI Depends 注入所需的两个工厂：
- ``get_settings``：返回单例 ``Settings``，常用于业务端点按环境分支。
- ``get_memory_backend``：按 ``Settings`` 构造 ``Mem0LibraryMemoryBackend``。
- ``get_memory_service``：单例 ``MemoryService``，懒加载 + 双重检查锁。

单例生命周期与 FastAPI lifespan 绑定；lifespan 退出时会调用
``shutdown_l3_executor`` / ``repository.close()`` 释放线程资源。
"""

import json
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from loguru import logger

from thinkback.domain.keys import DEFAULT_NAMESPACE
from thinkback.domain.memory_types import (
    MemoryTypeRegistry,
    default_registry,
    has_profile,
    load_registry,
)
from thinkback.domain.summarization import SummaryComposer
from thinkback.infra.config import Settings, settings
from thinkback.infra.llm import OpenAICompatibleLLMClient
from thinkback.infra.milvus_collection import MilvusMirror
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
        api_key=current_settings.llm_api_key or "not-required",
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
        llm_api_key=current_settings.llm_api_key or "not-required",
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
    mirror = None
    if current_settings.memory_milvus_mirror_collection:
        mirror = MilvusMirror(
            url=current_settings.milvus_url,
            database=current_settings.milvus_database,
            collection=current_settings.memory_milvus_mirror_collection,
            token=current_settings.milvus_token,
        )
    backend = Mem0LibraryMemoryBackend(
        config=mem0_config,
        max_concurrent_calls=current_settings.memory_backend_max_concurrent_calls,
        collection_analyzer=current_settings.memory_milvus_analyzer,
        mirror=mirror,
    )
    backend_log.debug("memory backend initialized")
    return backend


def build_registry_provider(
    config: Settings | None = None,
) -> Callable[[str], MemoryTypeRegistry]:
    """按 namespace 加载记忆类型注册表。

    profile 目录约定：``<dir>/<namespace>.json``。

    两种失败分开处理：
    - **配置缺失**（该 namespace 没有 profile 文件）：生产环境报错；开发
      环境回落默认注册表，因为开发期 namespace 可能还没配。
    - **配置非法**（解析失败、字段未知、取值非法）：**任何环境都报错**。
      静默换一套策略等于让企业以为在跑自己的记忆类型规则，实际跑的是
      内置默认规则 —— 这类「配置看起来生效了」比启动失败危险得多。
      ADR-0004 / D29。
    """
    current_settings = config or settings
    profile_dir = Path(current_settings.memory_type_profile_dir)
    strict = current_settings.environment == "production"

    def provider(namespace: str) -> MemoryTypeRegistry:
        if not has_profile(profile_dir, namespace):
            if strict and namespace != DEFAULT_NAMESPACE:
                # 非默认 namespace 却没有任何 profile：说明部署漏了配置。
                # 静默用默认注册表 = 行业类型全部「未注册」，而调用方以为
                # 自己的类型规则在生效。
                raise RuntimeError(
                    f"no memory type profile for namespace {namespace!r} in {profile_dir}"
                )
            return default_registry()
        try:
            return load_registry(profile_dir, namespace)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            logger.bind(
                namespace=namespace,
                profile_dir=str(profile_dir),
                error_type=type(exc).__name__,
            ).error("memory type profile load failed")
            raise RuntimeError(
                f"memory type profile for namespace {namespace!r} could not be loaded: {exc}"
            ) from exc

    return provider


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
                repository = SqlAlchemyMemoryRepository(
                    l1_cache_enabled=settings.memory_l1_cache_enabled
                )
                backend = get_memory_backend(settings)
                _memory_service = MemoryService(
                    repository=repository,
                    backend=backend,
                    mem0_infer_facts=settings.memory_infer_facts,
                    registry_provider=build_registry_provider(settings),
                    l3_write_mode=settings.memory_l3_write_mode,
                    l3_executor_workers=settings.memory_l3_executor_workers,
                    l3_max_pending_tasks=settings.memory_l3_max_pending_tasks,
                    l3_queue_wait_seconds=settings.memory_l3_queue_wait_seconds,
                    l2_summary_composer=get_l2_summary_composer(settings),
                    l2_refresh_interval_rounds=settings.memory_l2_refresh_interval_rounds,
                    decay_sweeper=build_decay_sweeper(settings, repository),
                    # 心跳必须显著快于孤儿回收阈值：否则健康的长任务会被
                    # 误判成孤儿并被标成 failed。
                    task_heartbeat_seconds=max(5.0, settings.task_orphan_running_seconds / 3.0),
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
