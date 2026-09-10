"""Mem0 Library + Milvus 生产适配器。

所有业务读写都必须经过 ``MemoryBackend`` 协议；本模块是唯一允许
接触 mem0 / Milvus SDK 的位置（readiness 的只读探针除外）。
"""

from __future__ import annotations

import concurrent.futures
import time
from collections.abc import Callable
from contextlib import contextmanager, suppress
from copy import deepcopy
from inspect import signature
from pathlib import Path
from threading import BoundedSemaphore, RLock
from typing import Any

from loguru import logger

INNIES_OPENAI_EMBEDDER_CLASS = (
    "innies_memory.memory.embeddings.OpenAICompatibleEmbeddingNoDimensions"
)

# P1b（mem0 v2 升级）：v2 的 custom_instructions 以「## Custom Instructions」
# 段拼进 user prompt（system prompt 由 v2 自带的 ADDITIVE_EXTRACTION_PROMPT
# 承担，质量规范远比 0.1.x 丰富）。我们只追加三条 innies 特有约束：
# 1) 输出语言跟随对话（v2 默认倾向英文输出，中文产品必须显式要求中文）；
# 2) 输出结构必须严格遵守 v2 的 {"memory": [{"id", "text"}]} 形状——弱模型
#    （本地 qwen 类）偶发嵌套数组/裸字符串，mem0 解析零容错会炸整轮写入
#    （R-7 在 0.1.x 上的教训，v2 换了形状但风险同源）；
# 3) 中文实体（昵称/宠物名/地名/品牌）逐字保留，不得翻译或泛化。
INNIES_CUSTOM_INSTRUCTIONS = """Additional requirements:
1. Language: write each memory text in the SAME language as the conversation (Chinese conversations → Chinese memories). Never translate Chinese names, nicknames, places, or brands into English.
2. Output shape: return exactly {"memory": [{"id": "<sequential string>", "text": "<one self-contained memory>"}]}. Every "text" MUST be a plain string; never nest arrays or objects inside it.
3. Preserve Chinese entities verbatim (宠物名/昵称/地点/品牌逐字保留), including tone particles only when they are part of a name."""


def _build_custom_instructions() -> str:
    """innies 的追加抽取约束（挂到 v2 custom_instructions）。"""

    return INNIES_CUSTOM_INSTRUCTIONS


def build_mem0_library_config(
    *,
    llm_api_key: str,
    llm_base_url: str,
    embedding_api_key: str,
    embedding_base_url: str,
    milvus_url: str,
    milvus_token: str,
    milvus_database: str,
    collection_name: str,
    llm_model: str,
    embedding_model: str,
    history_db_path: str,
    embedding_model_dims: int,
) -> dict[str, Any]:
    """Build the Mem0 Library config used by innies-memory's L3 backend.

    ``embedding_model_dims`` 必须由调用方显式传入并与实际 embedding endpoint 输出维度一致。
    不提供默认值是为了避免与 Milvus collection 维度不匹配的隐式地雷：
    若维度与已建 collection 不符，Mem0 会在写入时抛维度错误。
    """

    milvus_config: dict[str, Any] = {
        "collection_name": collection_name,
        "url": milvus_url,
        "token": milvus_token,
        "db_name": milvus_database,
        "embedding_model_dims": embedding_model_dims,
        "metric_type": "COSINE",
    }
    llm_config = {"model": llm_model, "api_key": llm_api_key}
    embedder_config: dict[str, Any] = {
        "model": embedding_model,
        "api_key": embedding_api_key,
        "embedding_dims": embedding_model_dims,
    }
    if llm_base_url:
        llm_config["openai_base_url"] = llm_base_url
    if embedding_base_url:
        embedder_config["openai_base_url"] = embedding_base_url
    return {
        "vector_store": {
            "provider": "milvus",
            "config": milvus_config,
        },
        "llm": {
            "provider": "openai",
            "config": llm_config,
        },
        "embedder": {
            "provider": "openai",
            "config": embedder_config,
        },
        "history_db_path": history_db_path,
        # P1b：v2 键名为 custom_instructions（原 custom_fact_extraction_prompt
        # 已改名），语义从"替换 system prompt"变为"user prompt 追加段"。
        "custom_instructions": _build_custom_instructions(),
    }


def register_mem0_embedder_provider() -> None:
    """Register innies-memory's OpenAI-compatible embedder with Mem0."""

    try:
        from mem0.utils.factory import EmbedderFactory
    except ImportError as exc:
        raise RuntimeError("mem0 library is not installed; install mem0ai") from exc
    EmbedderFactory.provider_to_class["openai"] = INNIES_OPENAI_EMBEDDER_CLASS


def disable_mem0_telemetry() -> None:
    """Disable Mem0 telemetry clients that create background Posthog threads per call."""

    def noop_capture_event(*args: Any, **kwargs: Any) -> None:
        _ = args
        _ = kwargs

    with suppress(ImportError):
        import mem0.memory.telemetry as mem0_telemetry

        client_telemetry = getattr(mem0_telemetry, "client_telemetry", None)
        if client_telemetry is not None:
            with suppress(Exception):
                client_telemetry.close()
        mem0_telemetry.capture_event = noop_capture_event
        mem0_telemetry.capture_client_event = noop_capture_event
        if hasattr(mem0_telemetry, "MEM0_TELEMETRY"):
            mem0_telemetry.MEM0_TELEMETRY = False

    with suppress(ImportError):
        import mem0.memory.main as mem0_main

        mem0_main.capture_event = noop_capture_event


def _is_not_found_valueerror(exc: RuntimeError, memory_id: str) -> bool:
    """识别 mem0 ``_update_memory`` 的「记忆不存在」ValueError。

    只在异常链源是 ValueError、且消息同时包含目标 ``memory_id`` 与
    "not found" 时判定可重试 —— 双条件把「add 后可见性窗口」和其它
    ValueError 来源（如配置错误）区分开。
    """

    cause = exc.__cause__
    if not isinstance(cause, ValueError):
        return False
    message = str(cause)
    return memory_id in message and "not found" in message


class Mem0LibraryMemoryBackend:
    # update/delete 遇「刚写入的 id 在 pk 点查中暂不可见」时的退避序列；
    # 0.5s/1s/2s 覆盖实测的秒级自愈窗口，总计最多多等 3.5s。
    _NOT_FOUND_RETRY_DELAYS: tuple[float, ...] = (0.5, 1.0, 2.0)

    def __init__(
        self,
        *,
        config: dict[str, Any],
        memory_client: Any | None = None,
        memory_factory: Callable[[dict[str, Any]], Any] | None = None,
        max_concurrent_calls: int = 4,
    ) -> None:
        """初始化 Mem0 Library 后端适配器。

        config 是传给 Mem0 的完整配置，会被 deepcopy 保存以避免外部修改；memory_client
        允许测试或上层注入现成客户端；memory_factory 用于延迟构造真实客户端；
        max_concurrent_calls 限制进入 Mem0 的并发，保护本进程和后端资源。
        """
        if max_concurrent_calls < 1:
            raise ValueError("max_concurrent_calls must be >= 1")
        self.config = deepcopy(config)
        self._memory_client = memory_client
        self._memory_factory = memory_factory
        self._memory_client_lock = RLock()
        self.max_concurrent_calls = max_concurrent_calls
        self._call_capacity = BoundedSemaphore(max_concurrent_calls)

    def add(
        self,
        messages: list[dict[str, str]],
        *,
        user_id: str,
        memory_scope_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """向 Mem0 写入消息并返回事件摘要。

        messages 可能包含用户正文，只传给 Mem0；日志只在 _call 中记录参数数量和键名。
        metadata 会补充 memory_scope_id 以保持范围隔离。

        流程图::

            进入 (messages, user_id, memory_scope_id, metadata)
                │
                ▼
            scoped_metadata = dict(metadata or {})
            scoped_metadata["memory_scope_id"] = memory_scope_id
                │
                ▼
            ┌── 委托 _call ────────────────────────────┐
            │  result = self._call(                    │
            │      "add",                              │
            │      messages,                           │
            │      user_id=user_id,                    │
            │      agent_id=memory_scope_id,           │
            │      metadata=scoped_metadata)           │
            │    ↓                                     │
            │  _call 内部:                             │
            │    ├─ _call_capacity (信号量限流)        │
            │    ├─ _mem0_inline_threadpool()          │
            │    ├─ mem0.Memory.add(...)               │
            │    │    → LLM 抽取事实                   │
            │    │    → embedder 编码                   │
            │    │    → Milvus 写入 + 历史表           │
            │    └─ 异常 → RuntimeError 包装           │
            └───────────────────────┬─────────────────┘
                                │
                                ▼
            events = self._results(result)
            (统一解析 dict / list / {"id","memory"} 单条)
                │
                ▼
            INFO: "memory backend add completed"
            (user_id, memory_scope_id,
             event_count, metadata_keys)
                │
                ▼
            返回 events
        """
        scoped_metadata = dict(metadata or {})
        scoped_metadata["memory_scope_id"] = memory_scope_id
        result = self._call(
            "add",
            messages,
            user_id=user_id,
            agent_id=memory_scope_id,
            metadata=scoped_metadata,
        )
        events = self._results(result)
        logger.bind(
            user_id=user_id,
            memory_scope_id=memory_scope_id,
            event_count=len(events),
            metadata_keys=sorted(str(key) for key in (metadata or {})),
        ).info("memory backend add completed")
        return events

    def search(
        self,
        query: str,
        *,
        user_id: str,
        memory_scope_id: str,
        limit: int,
        threshold: float | None = None,
    ) -> list[dict[str, Any]]:
        """按 query 检索长期记忆。

        query 原文只传给 Mem0，不进入日志；user_id 和 memory_scope_id 用于构造后端
        scope/filter，limit/threshold 控制召回规模。

        流程图::

            进入 (query, user_id, memory_scope_id,
                  limit, threshold=None)
                │
                ▼
            构造参数:
              scope_kwargs = _scope_kwargs(...)
                ├─ mem0 签名支持 user_id + agent_id 顶层参数
                │   → {"user_id", "agent_id", "filters": None}
                └─ 否则 → {"filters": {"user_id", "agent_id"}}
              limit_kwargs = _limit_kwargs(...)
                ├─ mem0 签名含 limit → {"limit": limit}
                ├─ 否则签名含 top_k → {"top_k": limit}
                └─ 其它 → {"limit": limit}
                │
                ▼
            ┌── 委托 _call ────────────────────────────┐
            │  result = self._call(                    │
            │      "search",                           │
            │      query, **scope_kwargs,              │
            │      **limit_kwargs,                     │
            │      threshold=threshold)                │
            │    ↓                                     │
            │  mem0.Memory.search(                     │
            │      query,                              │
            │      user_id=…, agent_id=…, filters=…,   │
            │      limit=…, threshold=…)               │
            │    ├─ embedder.encode(query)             │
            │    ├─ Milvus 向量检索 + user/agent 过滤 │
            │    └─ LLM rerank（可选）                 │
            └───────────────────────┬─────────────────┘
                                │
                                ▼
            items = self._results(result)
            INFO: "memory backend search completed"
            (user_id, memory_scope_id, limit,
             has_threshold, result_count,
             query_length=len(query))
                │
                ▼
            返回 items

        设计点：
        - ``query_length`` 而不是 ``query`` 入日志，避免敏感上下文泄露。
        - 旧版 mem0 签名差异（``limit`` vs ``top_k``）由 ``_limit_kwargs``
          反射适配，不在这里分支。
        - 本方法不 catch RuntimeError，由 :meth:`MemoryService.recall`
          捕获并降级为只返回 L1/L2。
        """
        result = self._call(
            "search",
            query,
            **self._scope_kwargs("search", user_id=user_id, memory_scope_id=memory_scope_id),
            **self._limit_kwargs("search", limit),
            threshold=threshold,
        )
        items = self._results(result)
        logger.bind(
            user_id=user_id,
            memory_scope_id=memory_scope_id,
            limit=limit,
            has_threshold=threshold is not None,
            result_count=len(items),
            query_length=len(query),
        ).info("memory backend search completed")
        return items

    def update(self, memory_id: str, data: str) -> None:
        """更新一条后端记忆内容；data 不会被日志记录。

        ``_update_memory`` 场景的已知竞态（真实 Milvus 上复现）：紧邻
        ``add`` 之后的 ``update`` 偶发 ``Memory with id ... not found`` ——
        Milvus growing segment 的 pk 点查在写入后的短窗口内不可见
        （同一 client 的 filter 查询已可见），秒级自愈。主链路里
        ``_index_l3_event`` 物化本地索引后立刻提交 ``_BackendUpdate``
        规范化 mem0 原文，正落在这个窗口上；放任失败会让本地索引与
        向量库内容分叉、只能等 rebuild 收敛。因此对「not found」做
        有界退避重试，其它错误（真实缺失、网络故障）不重试直接上抛。
        """

        for attempt, delay in enumerate((*self._NOT_FOUND_RETRY_DELAYS, None)):
            try:
                self._call("update", memory_id, data)
                logger.bind(memory_id=memory_id, data_length=len(data), retried=attempt > 0).info(
                    "memory backend update completed"
                )
                return
            except RuntimeError as exc:
                if delay is None or not _is_not_found_valueerror(exc, memory_id):
                    raise
                logger.bind(
                    memory_id=memory_id,
                    retry_count=attempt,
                    delay_seconds=delay,
                    error_type=type(exc.__cause__).__name__,
                ).warning("memory backend update target not visible yet; retrying")
                time.sleep(delay)

    def delete(self, memory_id: str) -> None:
        """删除单条后端记忆；兼容 Mem0 对缺失 ID 的已知错误形态。

        Mem0 v2 对不存在的 memory_id 抛 ``ValueError``（"Memory with id
        … not found"，内部 ``vector_store.get`` 点查为空）；旧版（0.1.x）
        同场景是 ``list[0]`` 取空 list 的 ``IndexError``。两种形态都视为
        「目标不存在」。

        ``ValueError`` 形态还有一个已知竞态（真实 Milvus 上复现）：紧邻
        ``add``/``search`` 之后的 delete 落在 growing segment 读可见性
        窗口内 —— pk 点查暂时看不到刚写入的行，秒级自愈。supersede
        清理路径（同 slot 新记忆落地后删旧记忆）正踩这个时序，放任
        失败会在向量库残留本该删除的记忆。因此对「not found」先做
        短退避重试，仍不见则按「已删除」幂等跳过（delete 的幂等语义
        下二者不可区分，跳过是安全收敛）。

        流程图::

            进入 (memory_id)
                │
                ▼
            ┌── 退避重试循环 ──────────────────────────┐
            │  try: self._call("delete", memory_id)     │
            │  except RuntimeError as exc:              │
            │    ├─ IndexError cause（旧版）            │
            │    │    → INFO skipped; return            │
            │    ├─ not-found ValueError cause          │
            │    │    且还有重试预算                      │
            │    │    → WARNING; sleep; 继续             │
            │    └─ 其它 → raise                        │
            └──────────────────────┬───────────────────┘
                                   ▼
            INFO: "memory backend delete completed"
        """

        for delay in (*self._NOT_FOUND_RETRY_DELAYS, None):
            try:
                self._call("delete", memory_id)
            except RuntimeError as exc:
                if isinstance(exc.__cause__, IndexError):
                    logger.bind(memory_id=memory_id, reason="missing_memory_id").info(
                        "memory backend delete skipped"
                    )
                    return
                if not _is_not_found_valueerror(exc, memory_id):
                    raise
                if delay is None:
                    # 重试预算耗尽仍不可见：与「已被并发删除」不可区分，
                    # 按 delete 幂等语义收敛为跳过。
                    logger.bind(
                        memory_id=memory_id,
                        reason="missing_memory_id_after_retries",
                    ).info("memory backend delete skipped")
                    return
                logger.bind(
                    memory_id=memory_id,
                    delay_seconds=delay,
                    error_type=type(exc.__cause__).__name__,
                ).warning("memory backend delete target not visible yet; retrying")
                time.sleep(delay)
            else:
                logger.bind(memory_id=memory_id).info("memory backend delete completed")
                return

    def delete_many(self, memory_ids: list[str]) -> int:
        """逐条删除后端记忆，返回实际尝试删除的非空 ID 数量。"""

        deleted = 0
        skipped = 0
        for memory_id in memory_ids:
            if not memory_id:
                skipped += 1
                continue
            self.delete(memory_id)
            deleted += 1
        logger.bind(
            attempted_count=len(memory_ids),
            deleted_count=deleted,
            skipped_empty_count=skipped,
        ).info("memory backend delete_many completed")
        return deleted

    def delete_all(self, *, user_id: str, memory_scope_id: str) -> int:
        """删除指定用户和记忆范围内的所有后端记忆。

        流程图::

            进入 (user_id, memory_scope_id)
                │
                ▼
            ┌── 阶段 1: 列举候选 ───────────────────────┐
            │  result = self._call("get_all",          │
            │      **scope_kwargs, limit=10000)        │
            │    ↓                                     │
            │  mem0.Memory.get_all(                    │
            │      user_id=…, agent_id=…,              │
            │      filters=…, limit=10000)             │
            │    ↓                                     │
            │  memory_ids = [str(m["id"])              │
            │                 for m in _results(result)]│
            └───────────────────────┬─────────────────┘
                                │
                                ▼
            ┌── 阶段 2: 逐条删除 ───────────────────────┐
            │  deleted = self.delete_many(memory_ids)  │
            │    for each memory_id:                   │
            │      ├─ 空 ID → 跳过计数                 │
            │      └─ 调 self.delete(memory_id)        │
            │          ├─ 存在 → 删                   │
            │          └─ 缺失 → IndexError 兼容      │
            └───────────────────────┬─────────────────┘
                                │
                                ▼
            INFO: "memory backend delete_all completed"
            (user_id, memory_scope_id,
             candidate_count, deleted_count)
                │
                ▼
            返回 deleted

        设计点：
        - 用 ``get_all + delete_many`` 而不是 mem0 的批量 delete API，
          因为 mem0 Library 没有稳定暴露批量删除，逐条 delete 的语义
          最容易和 ``IndexError`` 兼容路径对齐。
        - ``limit=10000`` 是单页上限：单页删完后继续翻页，直到取空或
          达到安全页数上限，保证 "删除用户全部数据" 不在向量库留残骸。
        """
        page_limit = 10000
        max_pages = 100  # 单调用最多清 100 万条，防止异常数据量下的无限循环
        deleted = 0
        candidate_count = 0
        for _ in range(max_pages):
            result = self._call(
                "get_all",
                **self._scope_kwargs("get_all", user_id=user_id, memory_scope_id=memory_scope_id),
                **self._limit_kwargs("get_all", page_limit),
            )
            memory_ids = [str(memory.get("id", "")) for memory in self._results(result)]
            if not memory_ids:
                break
            candidate_count += len(memory_ids)
            deleted += self.delete_many(memory_ids)
            if len(memory_ids) < page_limit:
                break
        logger.bind(
            user_id=user_id,
            memory_scope_id=memory_scope_id,
            candidate_count=candidate_count,
            deleted_count=deleted,
        ).info("memory backend delete_all completed")
        return deleted

    def health_check(self) -> dict[str, str]:
        """写入并清理探针记忆，验证 Mem0 后端是否可用。"""

        logger.debug("memory backend health check started")
        probe_memory_ids: list[str] = []
        try:
            events = self.add(
                [{"role": "user", "content": "innies-memory library health check"}],
                user_id="innies-memory-health",
                memory_scope_id="innies-memory-health",
                metadata={"source_type": "system_migration", "memory_type": "event"},
            )
            probe_memory_ids = [
                str(event.get("id", ""))
                for event in events
                if self._is_backend_memory_id(str(event.get("id", "")))
            ]
        except Exception as exc:
            logger.bind(error_type=type(exc).__name__).warning(
                "memory backend health check probe add failed"
            )
            return {"status": "not_ready", "detail": str(exc)}
        for memory_id in probe_memory_ids:
            try:
                self.delete(memory_id)
            except Exception as exc:
                logger.bind(memory_id=memory_id, error_type=type(exc).__name__).warning(
                    "memory backend health check probe cleanup failed"
                )
                return {"status": "not_ready", "detail": str(exc)}
        logger.bind(probe_count=len(probe_memory_ids)).debug(
            "memory backend health check completed"
        )
        return {"status": "ready", "detail": "ok"}

    @property
    def memory_client(self) -> Any:
        with self._memory_client_lock:
            if self._memory_client is None:
                self._memory_client = self._build_memory_client()
            return self._memory_client

    def _build_memory_client(self) -> Any:
        history_db_path = self.config.get("history_db_path")
        if isinstance(history_db_path, str) and history_db_path:
            Path(history_db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        if self._memory_factory is not None:
            return self._memory_factory(deepcopy(self.config))
        try:
            from mem0 import Memory
        except ImportError as exc:
            raise RuntimeError("mem0 library is not installed; install mem0ai") from exc
        disable_mem0_telemetry()
        register_mem0_embedder_provider()
        return Memory.from_config(deepcopy(self.config))

    def _call(self, operation: str, *args: Any, **kwargs: Any) -> Any:
        """统一的后端调用入口。

        所有 ``Mem0LibraryMemoryBackend`` 的对外方法（add / search / update
        / delete / delete_many）最终都走本函数，集中处理：

        - **客户端懒构造** ：首次使用时根据 config 构建 mem0 ``Memory``
          实例（``_build_memory_client``）。
        - **并发限流** ：``_call_capacity = BoundedSemaphore(max_concurrent_calls)``
          限制同时进入 mem0 的请求数，避免上游突发打爆 mem0 + Milvus。
        - **线程池内联** ：``_mem0_inline_threadpool()`` 临时把 mem0 的
          ``concurrent.futures`` 替换为同步内联执行器，避免 mem0 内部线程
          与 innies-memory 自己的 L3 executor 抢资源、延长端到端延迟、
          拖死 SIGTERM 退出。
        - **异常包装** ：mem0 抛任何异常都被包装为
          ``RuntimeError("mem0 library <op> failed: <exc>")``，调用方
          （``_BackendUpdate`` / ``_BackendDeleteSync`` / ``_run_backend_writes``
          / ``MemoryService``）能稳定捕获并打点。

        流程图::

            进入 (operation, *args, **kwargs)
                │
                ▼
            绑定 call_log (operation, arg_count, kwarg_keys,
                            max_concurrent_calls)
            INFO: "memory backend call started"
                │
                ▼
            method = getattr(self.memory_client, operation)
            (懒构造：首次访问 memory_client 时
             从 config 构建 mem0 Memory 实例)
                │
                ▼
            ┌── 临界区 ─────────────────────────────────┐
            │  with self._call_capacity,                 │
            │       _mem0_inline_threadpool():           │
            │      result = method(*args, **kwargs)      │
            │    ├─ _call_capacity.acquire()             │
            │    │    超 max_concurrent_calls → 阻塞     │
            │    ├─ mem0.concurrent 临时替换为内联执行器 │
            │    │    (深度计数管理嵌套场景)             │
            │    ├─ method(...)                          │
            │    └─ finally 释放信号量 + 恢复原 concurrent│
            └───────────────────────┬─────────────────┘
                                │
                ├─ 正常返回
                │     call_log.bind(result_summary).info(
                │         "memory backend call completed")
                │     return result
                │
                └─ 异常
                      call_log.bind(error_type).warning(
                          "memory backend call failed")
                      raise RuntimeError(
                          f"mem0 library {operation} failed: {exc}"
                      ) from exc

        设计取舍：
        - ``_call_capacity`` 与 ``max_concurrent_calls`` 同源；
          ``add`` / ``search`` / ``update`` / ``delete`` 共享同一个信号量，
          因为 mem0 + Milvus 是单一资源池，分桶会让某个动作独自打爆。
        - ``_mem0_inline_threadpool`` 走深度计数：允许 ``_call`` 在
          mem0 内部递归调用其他 mem0 API 时不会被中间层恢复原 concurrent
          导致 worker 线程突现。
        """
        call_log = logger.bind(
            operation=operation,
            arg_count=len(args),
            kwarg_keys=sorted(str(key) for key in kwargs),
            max_concurrent_calls=self.max_concurrent_calls,
        )
        call_log.info("memory backend call started")
        try:
            method = getattr(self.memory_client, operation)
            with self._call_capacity, _mem0_inline_threadpool():
                result = method(*args, **kwargs)
            call_log.bind(**self._result_summary(result)).info("memory backend call completed")
            return result
        except Exception as exc:
            call_log.bind(
                error_type=type(exc).__name__,
                error_message=str(exc)[:200],
            ).warning("memory backend call failed")
            raise RuntimeError(f"mem0 library {operation} failed: {exc}") from exc

    @staticmethod
    def _result_summary(result: Any) -> dict[str, Any]:
        """生成后端返回值摘要，避免日志写入记忆正文或完整返回对象。"""

        if isinstance(result, dict):
            raw_results = result.get("results", result.get("memories"))
            if isinstance(raw_results, list):
                return {"result_type": "dict", "result_count": len(raw_results)}
            if {"id", "memory"} <= result.keys():
                return {"result_type": "dict", "result_count": 1}
            return {"result_type": "dict", "result_count": None}
        if isinstance(result, list):
            return {"result_type": "list", "result_count": len(result)}
        return {"result_type": type(result).__name__, "result_count": None}

    def _limit_kwargs(self, operation: str, limit: int) -> dict[str, int]:
        method = getattr(self.memory_client, operation)
        try:
            parameters = signature(method).parameters
        except (TypeError, ValueError):
            return {"limit": limit}
        if "limit" in parameters:
            return {"limit": limit}
        if "top_k" in parameters:
            return {"top_k": limit}
        return {"limit": limit}

    def _scope_kwargs(
        self, operation: str, *, user_id: str, memory_scope_id: str
    ) -> dict[str, Any]:
        method = getattr(self.memory_client, operation)
        try:
            parameters = signature(method).parameters
        except (TypeError, ValueError):
            return {"user_id": user_id, "agent_id": memory_scope_id}
        supports_top_level_scope = "user_id" in parameters and "agent_id" in parameters
        if supports_top_level_scope:
            return {"user_id": user_id, "agent_id": memory_scope_id, "filters": None}
        return {"filters": {"user_id": user_id, "agent_id": memory_scope_id}}

    @staticmethod
    def _results(result: object) -> list[dict[str, Any]]:
        if isinstance(result, dict):
            raw_results = result.get("results", result.get("memories", []))
            if isinstance(raw_results, list):
                return [dict(item) for item in raw_results if isinstance(item, dict)]
            if {"id", "memory"} <= result.keys():
                return [dict(result)]
        if isinstance(result, list):
            return [dict(item) for item in result if isinstance(item, dict)]
        return []

    @staticmethod
    def _is_backend_memory_id(memory_id: str) -> bool:
        return bool(memory_id) and not memory_id.startswith("local-p0:")


class _InlineThreadPoolExecutor:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        _ = args
        _ = kwargs

    def __enter__(self) -> _InlineThreadPoolExecutor:
        return self

    def __exit__(self, *args: Any) -> None:
        _ = args

    def submit(
        self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any
    ) -> concurrent.futures.Future[Any]:
        future: concurrent.futures.Future[Any] = concurrent.futures.Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as exc:
            future.set_exception(exc)
        return future


class _Mem0FuturesProxy:
    def __init__(self, original_futures: Any) -> None:
        self._original_futures = original_futures
        self.ThreadPoolExecutor = _InlineThreadPoolExecutor

    def __getattr__(self, name: str) -> Any:
        return getattr(self._original_futures, name)


class _Mem0ConcurrentProxy:
    def __init__(self, original_concurrent: Any) -> None:
        self._original_concurrent = original_concurrent
        self.futures = _Mem0FuturesProxy(original_concurrent.futures)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._original_concurrent, name)


_mem0_inline_threadpool_lock = RLock()
_mem0_inline_threadpool_depth = 0
_mem0_inline_threadpool_original: Any | None = None


@contextmanager
def _mem0_inline_threadpool() -> Any:
    """在 ``_call`` 期间把 mem0 的全局 ``concurrent`` 替换为内联执行器。

    mem0 内部在 ``add`` 等热路径上会用 ``concurrent.futures.ThreadPoolExecutor``
    起 worker；innies-memory 已经维护了自己的 L3 executor，再额外启线程会：
    - 拉长 mem0 调用的端到端延迟（线程切换 + 队列调度）。
    - 在 mem0 卡顿时让 worker 线程持续占用，导致 SIGTERM 时进程无法及时退出。

    这里通过临时 monkey-patch 让 mem0 的 ``submit`` 调用改为在调用线程里同步
    执行，从而避免上述问题。深度计数用于支持嵌套场景：外层 ``_call`` 结束后
    才真正恢复原始 ``concurrent``，避免相互覆盖。

    注意：mem0 未安装时直接 yield，调用方仍能在没有 mem0 的测试环境跑通。
    """

    global _mem0_inline_threadpool_depth, _mem0_inline_threadpool_original
    try:
        import mem0.memory.main as mem0_main
    except ImportError:
        logger.debug("mem0 inline threadpool skipped: mem0 not installed")
        yield
        return

    with _mem0_inline_threadpool_lock:
        if _mem0_inline_threadpool_depth == 0:
            _mem0_inline_threadpool_original = mem0_main.concurrent
            mem0_main.concurrent = _Mem0ConcurrentProxy(mem0_main.concurrent)
            logger.debug("mem0 inline threadpool activated")
        _mem0_inline_threadpool_depth += 1
    try:
        yield
    finally:
        with _mem0_inline_threadpool_lock:
            _mem0_inline_threadpool_depth -= 1
            if _mem0_inline_threadpool_depth == 0:
                mem0_main.concurrent = _mem0_inline_threadpool_original
                _mem0_inline_threadpool_original = None
                logger.debug("mem0 inline threadpool restored")
