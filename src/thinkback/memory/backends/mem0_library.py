"""Mem0 Library + Milvus 生产适配器。

所有业务读写都必须经过 ``MemoryBackend`` 协议；本模块是唯一允许
接触 mem0 / Milvus SDK 的位置（readiness 的只读探针除外）。
"""

from __future__ import annotations

import concurrent.futures
import sqlite3
import time
from collections.abc import Callable
from contextlib import contextmanager, suppress
from copy import deepcopy
from inspect import signature
from pathlib import Path
from threading import BoundedSemaphore, Lock, RLock
from typing import Any

from loguru import logger

from thinkback.domain.ports import MEMORY_LIST_PAGE_LIMIT

# 全局默认抽取指令住在 domain/prompts.py：它是领域策略，后端只负责传参。
#
# P1b（mem0 v2 升级）：v2 的 custom_instructions 以「## Custom Instructions」
# 段拼进 user prompt（system prompt 由 v2 自带的 ADDITIVE_EXTRACTION_PROMPT
# 承担，质量规范远比 0.1.x 丰富）。v2 的写入参数 ``prompt`` 是**覆盖**配置级
# 默认值的（``custom_instr = prompt or self.custom_instructions``），所以全局
# 默认与类型指令由 Thinkback 拼好再传（ADR-0014），配置级留空。
from thinkback.domain.prompts import DEFAULT_EXTRACTION_INSTRUCTIONS
from thinkback.infra.milvus_collection import (
    DEFAULT_ANALYZER_TYPE,
    MilvusMirror,
    delete_mirror_rows,
    mirror_rows,
)

#: 单条 DELETE 的占位符上限。SQLite 对一条语句的变量数有上限（老版本 999），
#: ALL 范围要一次清几千个 memory_id，必须分块。
_SQLITE_MAX_VARIABLES = 500

THINKBACK_OPENAI_EMBEDDER_CLASS = (
    "thinkback.memory.embeddings.OpenAICompatibleEmbeddingNoDimensions"
)

# 只读操作：不需要 LLM 抽取，只要 embedding + 向量检索。
# 它们用独立的并发预算，避免被慢速写路径（LLM 抽取）饿死。
_READ_OPERATIONS = frozenset({"search", "get_all", "get"})

#: 写锁分片数。mem0 写入按 (user, scope) 串行化（mem0 不是线程安全的），
#: 分片数固定 = 锁表不随用户数增长；分片冲突只会多一次串行，不会出错。
_WRITE_LOCK_STRIPES = 64

#: 引擎侧历史清理遇到 SQLite 锁时的重试次数与退避基数（秒）。
#: mem0 自己开着一条连接写同一个 history.db，回滚日志模式下我们的 DELETE
#: 可能撞上「升级为排他锁」的瞬时冲突。写事务通常只持锁几毫秒，重试即可。
_PURGE_LOCK_RETRIES = 3
_PURGE_LOCK_BACKOFF_SECONDS = 0.2

#: 兼容旧引用名：默认指令现在住在 domain/prompts.py。
THINKBACK_CUSTOM_INSTRUCTIONS = DEFAULT_EXTRACTION_INSTRUCTIONS


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
    """Build the Mem0 Library config used by thinkback's L3 backend.

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
        #
        # 留空：抽取指令由服务层按「全局默认 + 类型指令」拼好后随每次写入传入
        # （ADR-0014）。引擎的写入参数 prompt 是覆盖配置级默认值的，两者同时
        # 存在只会让人误以为配置级也在起作用。
        "custom_instructions": "",
    }


def register_mem0_embedder_provider() -> None:
    """Register thinkback's OpenAI-compatible embedder with Mem0."""

    try:
        from mem0.utils.factory import EmbedderFactory
    except ImportError as exc:
        raise RuntimeError("mem0 library is not installed; install mem0ai") from exc
    EmbedderFactory.provider_to_class["openai"] = THINKBACK_OPENAI_EMBEDDER_CLASS


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
        collection_analyzer: str = DEFAULT_ANALYZER_TYPE,
        mirror: MilvusMirror | None = None,
    ) -> None:
        """初始化 Mem0 Library 后端适配器。

        config 是传给 Mem0 的完整配置，会被 deepcopy 保存以避免外部修改；memory_client
        允许测试或上层注入现成客户端；memory_factory 用于延迟构造真实客户端；
        max_concurrent_calls 限制进入 Mem0 的并发，保护本进程和后端资源。

        collection_analyzer 是 Thinkback 预建集合时给 ``text`` 字段定的分析器
        （ADR-0007）；mirror 是迁移窗口里的次集合，给了才双写 + 双读回落。
        """
        if max_concurrent_calls < 1:
            raise ValueError("max_concurrent_calls must be >= 1")
        self.config = deepcopy(config)
        self._memory_client = memory_client
        self._memory_factory = memory_factory
        self._memory_client_lock = RLock()
        self.max_concurrent_calls = max_concurrent_calls
        self.collection_analyzer = collection_analyzer
        self.mirror = mirror
        self._mirror_client: Any | None = None
        self._mirror_memory: Any | None = None
        self._primary_client: Any | None = None
        # 读、写两把独立的并发预算。
        # 写路径要打 LLM（抽取，慢），读路径只要 embedding + 向量检索（快）。
        # 共用一把信号量时，写积压会把读饿死：实测 4 路并发写入时 add 耗时
        # 76s，排在后面的 search 等了 69s，召回直接 503。召回在助手的
        # 关键路径上，不能被后台写入队列拖死，所以读有自己的一份额度。
        self._call_capacity = BoundedSemaphore(max_concurrent_calls)
        self._read_capacity = BoundedSemaphore(max_concurrent_calls)
        # 写操作的串行化锁（按 user+scope 分片）。
        # mem0 的 Memory 实例不是线程安全的：entity_store 是实例级懒加载属性，
        # 实体链接是 linked_memory_ids 上的读-改-写 —— 同一用户的两次并发写
        # 会互相覆盖，丢掉一条记忆 id，而且不报错（mem0 自己也有一张同类的
        # 异步写入 bug：mem0ai/mem0#4892）。分片而不是一把大锁：不同用户
        # 之间不互相阻塞。固定片数 = 锁表不会随用户数增长。
        self._write_locks = tuple(Lock() for _ in range(_WRITE_LOCK_STRIPES))
        # 集合一致性级别只需提升一次（进程级；提到 Strong 后才记完成）。
        self._consistency_ensured = False

    def add(
        self,
        messages: list[dict[str, str]],
        *,
        user_id: str,
        memory_scope_id: str,
        metadata: dict[str, Any] | None = None,
        infer: bool = True,
        expiration_date: str | None = None,
        prompt: str | None = None,
    ) -> list[dict[str, Any]]:
        """向 Mem0 写入消息并返回事件摘要。

        messages 可能包含用户正文，只传给 Mem0；日志只在 _call 中记录参数数量和键名。
        metadata 会补充 memory_scope_id 以保持范围隔离。

        ``infer`` 控制 mem0 是否调用 LLM 做事实抽取：
        - True (默认)：调用 LLM 抽取事实 → 需要 chat/completions endpoint
        - False：直接把 raw message 存为记忆（thinkback 的 P0 槽位逻辑
                 负责结构化抽取，mem0 退化为纯向量存储）

        流程图::

            进入 (messages, user_id, memory_scope_id, metadata, infer)
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
            │      metadata=scoped_metadata,           │
            │      infer=infer)                        │
            │    ↓                                     │
            │  _call 内部:                             │
            │    ├─ _call_capacity (信号量限流)        │
            │    ├─ _mem0_inline_threadpool()          │
            │    ├─ mem0.Memory.add(...)               │
            │    │    → (infer=True) LLM 抽取事实      │
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
        # 过期交给引擎（Mem0 的 expiration_date）：过期记忆在检索时自动隐藏。
        expiration_kwargs: dict[str, Any] = (
            {"expiration_date": expiration_date} if expiration_date else {}
        )
        # 抽取指令：调用方给的是「全局默认 + 类型指令」拼好的版本（ADR-0014）。
        # 没给就退回全局默认 —— 配置级默认是空的，不能让写入裸奔。
        #
        # 写操作按 (user, scope) 串行：mem0 的 Memory 不是线程安全的
        # （entity_store 是实例级懒加载属性，实体链接是 linked_memory_ids 上的
        # 读-改-写），同一用户的两次并发写会互相覆盖、丢掉一条记忆 id。
        # 按用户分片而不是一把大锁：不同用户之间不互相阻塞。
        with self._write_lock_for(f"{user_id}\x00{memory_scope_id}"):
            result = self._call(
                "add",
                messages,
                user_id=user_id,
                agent_id=memory_scope_id,
                metadata=scoped_metadata,
                infer=infer,
                prompt=prompt or DEFAULT_EXTRACTION_INSTRUCTIONS,
                **expiration_kwargs,
            )
        events = self._results(result)
        # 迁移窗口内双写：新集合是主集合，旧集合挂在镜像位上跟着写，
        # 回滚只要改集合名，不用补数据。
        self._mirror_write([str(event.get("id", "")) for event in events if event.get("id")])
        # 集合由 mem0 首次写入时创建 —— 写完第一笔再把一致性级别提到 Strong
        # （Bounded 下写后立刻查可能查不到刚写的向量）。
        self._ensure_collection_consistency()
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
        explain: bool = False,
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
        search_kwargs: dict[str, Any] = {
            **self._scope_kwargs("search", user_id=user_id, memory_scope_id=memory_scope_id),
            **self._limit_kwargs("search", limit),
            "threshold": threshold,
        }
        # explain 只在需要分数分解时传：默认路径不改变调用形状。
        if explain:
            search_kwargs["explain"] = True
        result = self._call("search", query, **search_kwargs)
        items = self._results(result)
        # 迁移窗口内的双读：主集合（新集合）命中不足时回落旧集合。
        items = self._merge_mirror_search(
            query,
            items,
            user_id=user_id,
            memory_scope_id=memory_scope_id,
            limit=limit,
            threshold=threshold,
            explain=explain,
        )
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
                # update 也走 mem0 的实体链接（同样是读-改-写），要串行。
                # 拿不到 user_id，用固定分片：update/delete 是低频路径，
                # 共享一把锁的代价可以接受。
                with self._write_lock_for(""):
                    self._call("update", memory_id, data)
                logger.bind(memory_id=memory_id, data_length=len(data), retried=attempt > 0).info(
                    "memory backend update completed"
                )
                # 迁移窗口内双写：旧集合里的同一行也要换成新内容，
                # 否则回滚会拿到改口前的旧事实。
                self._mirror_write([memory_id])
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
                # delete 也会改 mem0 的实体链接（读-改-写），与 update 一样串行。
                with self._write_lock_for(""):
                    self._call("delete", memory_id)
            except RuntimeError as exc:
                if isinstance(exc.__cause__, IndexError):
                    logger.bind(memory_id=memory_id, reason="missing_memory_id").info(
                        "memory backend delete skipped"
                    )
                    self._mirror_delete([memory_id])
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
                    self._mirror_delete([memory_id])
                    return
                logger.bind(
                    memory_id=memory_id,
                    delay_seconds=delay,
                    error_type=type(exc.__cause__).__name__,
                ).warning("memory backend delete target not visible yet; retrying")
                time.sleep(delay)
            else:
                logger.bind(memory_id=memory_id).info("memory backend delete completed")
                self._mirror_delete([memory_id])
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

    def get_all(
        self, *, user_id: str, memory_scope_id: str, limit: int = 200
    ) -> list[dict[str, Any]]:
        """列出该 user+scope 的后端记忆（含 metadata），用于重试对账。

        迁移窗口内还要带上镜像集合里的记录：主集合与镜像集合是同一份逻辑
        数据的两个副本，擦除复核与 ALL 范围删除都要按并集算，否则镜像里
        残留的记录既查不出来也删不掉。镜像列不出来时异常照旧往上抛 ——
        擦除复核会把它当成「未能证明已删」而失败重试。

        列出时带上**已过期**的记录：mem0 的 ``get_all`` 默认隐藏过期行，
        而删除与擦除复核必须看得见它们 —— 看不见就列不出来、删不掉，
        擦除却报「已完成」。记忆活得越久越擦不掉，与保留策略的方向正好相反。
        """
        result = self._call(
            "get_all",
            **self._scope_kwargs("get_all", user_id=user_id, memory_scope_id=memory_scope_id),
            **self._limit_kwargs("get_all", min(limit, MEMORY_LIST_PAGE_LIMIT)),
            **self._show_expired_kwargs("get_all"),
        )
        items = self._results(result)
        if self.mirror is None:
            return items
        known = {str(item.get("id", "")) for item in items}
        for memory_id in self._mirror_scope_ids(
            user_id=user_id, memory_scope_id=memory_scope_id, limit=limit
        ):
            if memory_id and memory_id not in known:
                known.add(memory_id)
                items.append({"id": memory_id, "metadata": {}})
        return items

    def get(self, memory_id: str) -> dict[str, Any] | None:
        """按 ID 取一条后端记忆；不存在返回 ``None``。

        擦除复核靠它逐个点查：``get_all`` 一次只列一页，用户记录超过一页时
        「目标还在不在」从列表里判断不出来（列表满了 ≠ 目标还在）。
        """
        result = self._call("get", memory_id)
        if isinstance(result, dict):
            return result or None
        return None

    def delete_all(self, *, user_id: str, memory_scope_id: str) -> list[str]:
        """删除指定用户和记忆范围内的所有后端记忆，返回被删除的记忆 ID。

        返回 ID 是必须的：mem0 的 delete 会把事实原文写进 history 表，
        调用方要按这些 ID 把引擎侧原文清掉。只清本地索引里有的那批会漏掉
        「抽取后索引失败」的暗数据 —— 原文留在引擎侧，擦除却报已完成。


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
        page_limit = MEMORY_LIST_PAGE_LIMIT
        max_pages = 100  # 单调用最多清 100 万条，防止异常数据量下的无限循环
        deleted_ids: list[str] = []
        candidate_count = 0
        for _ in range(max_pages):
            result = self._call(
                "get_all",
                **self._scope_kwargs("get_all", user_id=user_id, memory_scope_id=memory_scope_id),
                **self._limit_kwargs("get_all", page_limit),
                **self._show_expired_kwargs("get_all"),
            )
            memory_ids = [str(memory.get("id", "")) for memory in self._results(result)]
            if not memory_ids:
                break
            candidate_count += len(memory_ids)
            self.delete_many(memory_ids)
            deleted_ids.extend(memory_id for memory_id in memory_ids if memory_id)
            if len(memory_ids) < page_limit:
                break
        logger.bind(
            user_id=user_id,
            memory_scope_id=memory_scope_id,
            candidate_count=candidate_count,
            deleted_count=len(deleted_ids),
        ).info("memory backend delete_all completed")
        return deleted_ids

    def _ensure_collection_consistency(self) -> None:
        """把 Milvus 集合的一致性级别提到 Strong（提到后才算完成）。

        Bounded 一致性下，写入后立刻用同一连接查可能查不到刚写的向量 ——
        对「刚说过的话马上问」的记忆服务是硬伤。

        只有真正提到 Strong 才记为已完成：集合尚未创建（"skipped"）或改级失败
        （"failed"）时保持未完成，下一次写入再试 —— 否则集合会永远停在 Bounded，
        写后立刻读不到刚写的向量。
        """
        if self._consistency_ensured:
            return
        collection_name = (
            self.config.get("vector_store", {}).get("config", {}).get("collection_name")
        )
        if not isinstance(collection_name, str) or not collection_name:
            return
        from thinkback.infra.milvus import ensure_milvus_collection_consistency

        desired_level = "Strong"
        result = ensure_milvus_collection_consistency(
            milvus_url=str(self.config.get("vector_store", {}).get("config", {}).get("url", "")),
            milvus_database=str(
                self.config.get("vector_store", {}).get("config", {}).get("db_name", "")
            ),
            collection_name=collection_name,
            level=desired_level,
            milvus_token=str(
                self.config.get("vector_store", {}).get("config", {}).get("token", "")
            ),
        )
        if result.lower() != desired_level.lower():
            return
        self._consistency_ensured = True

    def purge_history(
        self,
        *,
        user_id: str,
        memory_ids: list[str],
        namespace: str,
        message_texts: list[str],
        purge_all_messages: bool,
    ) -> dict[str, int]:
        """物理擦除引擎侧残留：事实抽取历史 + 原始消息。

        Mem0 2.2.1 没有公开的 history 清理接口，但它把两类原文落在本地
        SQLite（``history_db_path``）：

        - ``history`` 表：``old_memory`` / ``new_memory`` 存事实文本，
          按 ``memory_id`` 索引；
        - ``messages`` 表：按 ``session_scope``（user + agent）存最近 10 条
          **原始消息**，内容就是对话原文。

        这里对同一份 SQLite 文件做定向 DELETE —— 不改引擎行为、不改表结构，
        只删指定记忆与指定作用域的行。

        原始消息这一层没有 session 维度（Mem0 的会话标识只有 user/agent/
        run），所以会话级擦除按**消息正文**精确删除：只删被擦除轮次的那几
        条原文，不会连别的会话一起删。ALL 范围才清空整个作用域的原始消息。

        已知边界：``history.db`` 是进程本地文件，多副本部署时每个副本各有一份，
        一次擦除只清到当前副本（与本适配器里记录的既有边界一致）。

        锁冲突：mem0 自己开着一条连接写同一个文件，本方法再开一条。回滚日志
        模式下，事务先读后写时若期间有别的连接写过，会立刻拿到
        ``database is locked``（busy_timeout 对这种「升级为排他锁」的场景
        不起作用）。写事务通常只持锁几毫秒，所以这里做有限重试 —— 擦除
        不该因为一次瞬时锁就失败，而向量已经删掉了。
        """
        counts = {"history": 0, "messages": 0, "supported": False}
        history_db_path = self.config.get("history_db_path")
        if not isinstance(history_db_path, str) or not history_db_path:
            return counts
        counts["supported"] = True
        last_error: sqlite3.OperationalError | None = None
        for attempt in range(_PURGE_LOCK_RETRIES):
            try:
                return self._purge_history_once(
                    history_db_path=history_db_path,
                    user_id=user_id,
                    memory_ids=memory_ids,
                    namespace=namespace,
                    message_texts=message_texts,
                    purge_all_messages=purge_all_messages,
                )
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc) and "busy" not in str(exc):
                    raise
                last_error = exc
                logger.bind(attempt=attempt + 1, error=str(exc)).warning(
                    "memory backend history purge hit sqlite lock, retrying"
                )
                time.sleep(_PURGE_LOCK_BACKOFF_SECONDS * (attempt + 1))
        raise RuntimeError(
            f"mem0 history purge failed after {_PURGE_LOCK_RETRIES} attempts: {last_error}"
        )

    def _purge_history_once(
        self,
        *,
        history_db_path: str,
        user_id: str,
        memory_ids: list[str],
        namespace: str,
        message_texts: list[str],
        purge_all_messages: bool,
    ) -> dict[str, int]:
        """执行一次定向 DELETE；锁冲突由调用方重试。"""
        counts = {"history": 0, "messages": 0, "supported": True}
        connection = sqlite3.connect(history_db_path, timeout=10.0)
        try:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            with connection:
                if memory_ids:
                    if "history" not in tables:
                        # 表结构漂移（mem0 改名/换表）→ 物理擦除无法被证明。
                        # 契约要求「表结构不匹配时 fail-visible 并让操作失败，
                        # 不允许静默跳过」（插件化设计 §7）：静默跳过后调用方仍会
                        # 把擦除标成 completed，而原文还躺在引擎历史里 ——
                        # 「物理擦除」这一档对外承诺就是假的。
                        raise RuntimeError(
                            "mem0 history table is missing; the engine schema changed, "
                            "so physical erase cannot be proven. Refusing to report "
                            "it as purged."
                        )
                    # 分块删：SQLite 的占位符有上限，ALL 范围一次要清几千个
                    # memory_id，一条 IN 拼不下（too many SQL variables）。
                    deleted_rows = 0
                    for start in range(0, len(memory_ids), _SQLITE_MAX_VARIABLES):
                        chunk = memory_ids[start : start + _SQLITE_MAX_VARIABLES]
                        placeholders = ",".join("?" for _ in chunk)
                        try:
                            cursor = connection.execute(
                                f"DELETE FROM history WHERE memory_id IN ({placeholders})",
                                chunk,
                            )
                        except sqlite3.Error as exc:
                            # 列名漂移（表还在、列没了）：同样 fail-visible。
                            raise RuntimeError(
                                f"mem0 history schema is incompatible: {exc}"
                            ) from exc
                        deleted_rows += max(cursor.rowcount, 0)
                    counts["history"] = deleted_rows
                if "messages" in tables:
                    scope_key = self._mem0_session_scope(user_id, namespace)
                    if purge_all_messages:
                        cursor = connection.execute(
                            "DELETE FROM messages WHERE session_scope = ?",
                            (scope_key,),
                        )
                        counts["messages"] += max(cursor.rowcount, 0)
                    elif message_texts:
                        # 按正文删：Mem0 的消息表没有 session 维度，按 scope 删
                        # 会连别的会话一起删。只有被擦除轮次自己的原文该走。
                        deleted_messages = 0
                        for start in range(0, len(message_texts), _SQLITE_MAX_VARIABLES):
                            chunk = message_texts[start : start + _SQLITE_MAX_VARIABLES]
                            placeholders = ",".join("?" for _ in chunk)
                            cursor = connection.execute(
                                "DELETE FROM messages WHERE session_scope = ? "
                                f"AND content IN ({placeholders})",
                                (scope_key, *chunk),
                            )
                            deleted_messages += max(cursor.rowcount, 0)
                        counts["messages"] += deleted_messages
        finally:
            connection.close()
        logger.bind(
            user_id=user_id,
            namespace=namespace,
            message_text_count=len(message_texts),
            purge_all_messages=purge_all_messages,
            **counts,
        ).info("memory backend history purge completed")
        return counts

    @staticmethod
    def _mem0_session_scope(user_id: str, agent_id: str) -> str:
        """复刻 mem0 的 session scope 拼法（``mem0.memory.main._build_session_scope``）。

        键按字母序：agent_id → run_id → user_id（我们不传 run_id）。
        """
        values = {"agent_id": agent_id, "user_id": user_id}

        def escape(value: str) -> str:
            return value.replace("%", "%25").replace("&", "%26").replace("=", "%3D")

        parts = [
            f"{key}={escape(str(values[key]))}"
            for key in ("agent_id", "run_id", "user_id")
            if values.get(key)
        ]
        return "&".join(parts)

    def health_check(self) -> dict[str, str]:
        """只读探活：查询向量库，不写入任何数据。

        原实现真的 ``add`` 一条探针记忆再 ``delete``。k8s 探针默认 10s 一次，
        于是每次探活 = 一次 LLM + 一次 embedding + 一次 Milvus 写 + 一次删：
        贵，而且在**被探测的链路**上凭空制造写流量（写失败还会让探针报不健康，
        明明后端是好的）。改成只读查询（``get_all``）—— 验证 mem0 客户端与
        向量库连通性，不碰任何数据。
        """

        logger.debug("memory backend health check started")
        try:
            self._call(
                "get_all",
                **self._scope_kwargs(
                    "get_all",
                    user_id="thinkback-health",
                    memory_scope_id="thinkback-health",
                ),
                **self._limit_kwargs("get_all", 1),
            )
        except Exception as exc:
            logger.bind(error_type=type(exc).__name__).warning(
                "memory backend health check query failed"
            )
            return {"status": "not_ready", "detail": str(exc)}
        logger.debug("memory backend health check completed")
        return {"status": "ready", "detail": "ok"}

    @property
    def memory_client(self) -> Any:
        with self._memory_client_lock:
            if self._memory_client is None:
                self._memory_client = self._build_memory_client()
            return self._memory_client

    def warmup(self) -> None:
        """启动期预热：把 mem0 客户端提前构造出来。

        客户端是懒构造的，而 ``Memory.from_config`` 要 3-4 秒（建 embedder、
        向量库与 LLM 客户端）。放在启动期做，首个用户请求就不用等这 4 秒。
        预热失败不阻塞启动：真正用到时还会再构造一次。
        """
        started = time.monotonic()
        try:
            _ = self.memory_client
        except Exception as exc:  # noqa: BLE001 - 预热失败不该挡住服务启动
            logger.bind(error_type=type(exc).__name__).warning("memory backend warmup failed")
            return
        logger.bind(
            elapsed_ms=round((time.monotonic() - started) * 1000, 1),
        ).info("memory backend warmup completed")

    def _build_memory_client(self) -> Any:
        # S7: mem0 v2 把 "事实抽取明细"（add/update/delete 的审计链路）
        # 落本地 SQLite (history_db_path)。多副本部署下每个副本的 history.db
        # 独立持有 —— 副本 A 写入的事实抽取明细只在 A 可见，跨副本审计需
        # 走 LLM trace / 上游调用方日志（这次落实 S7 文档化，不改 mem0 行为）。
        # Pod 重建后 history 归零；属于"已知边界"而非"bug"。
        history_db_path = self.config.get("history_db_path")
        if isinstance(history_db_path, str) and history_db_path:
            Path(history_db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        if self._memory_factory is not None:
            return self._memory_factory(deepcopy(self.config))
        self._ensure_collection_schema()
        return self._build_client_for(self.config)

    def _build_client_for(self, config: dict[str, Any]) -> Any:
        """按给定 config 造一个 mem0 Memory（镜像集合复用同一条构造路径）。"""

        try:
            from mem0 import Memory
        except ImportError as exc:
            raise RuntimeError("mem0 library is not installed; install mem0ai") from exc
        disable_mem0_telemetry()
        register_mem0_embedder_provider()
        return Memory.from_config(deepcopy(config))

    def _ensure_collection_schema(self) -> None:
        """建 mem0 客户端之前先把集合按 Thinkback 的 schema 预建好（ADR-0007）。

        mem0 自建集合时 ``text`` 字段不带 analyzer_params，中文整句被当成一个词，
        BM25 对中文查询恒为 0；Milvus 又不允许原地改分析器，所以集合必须由我们
        先建好，mem0 发现它已存在就直接接管读写。

        失败不阻断：mem0 会自己建集合（分析器退化成默认），此时写与读都还能用，
        只是中文 BM25 失效。降级要看得见，所以记 warning 而不是静默。
        """

        store = self.config.get("vector_store", {}).get("config", {})
        collection_name = store.get("collection_name")
        if not isinstance(collection_name, str) or not collection_name:
            return
        from thinkback.infra.milvus_collection import STATE_DRIFTED, ensure_collection

        targets = [
            (
                collection_name,
                str(store.get("url", "")),
                str(store.get("db_name", "")),
                str(store.get("token", "")),
            )
        ]
        if self.mirror is not None:
            targets.append(
                (
                    self.mirror.collection,
                    self.mirror.url,
                    self.mirror.database,
                    self.mirror.token,
                )
            )
        dim = int(store.get("embedding_model_dims", 0) or 0)
        for name, url, database, token in targets:
            state = ensure_collection(
                milvus_url=url,
                milvus_database=database,
                collection_name=name,
                dim=dim,
                milvus_token=token,
                analyzer_type=self.collection_analyzer,
            )
            if state.state == STATE_DRIFTED:
                logger.bind(
                    collection=name,
                    analyzer=state.analyzer,
                    detail=state.detail,
                ).warning(
                    "milvus collection schema drifted from thinkback; "
                    "chinese bm25 is off until the collection is rebuilt"
                )

    # ── 迁移窗口的双写 / 双读（ADR-0007）───────────────────────────────

    def _primary_collection(self) -> str:
        store = self.config.get("vector_store", {}).get("config", {})
        return str(store.get("collection_name", ""))

    def _mirror_handle(self) -> Any:
        """镜像集合的 Milvus 客户端（懒构造）。"""

        if self.mirror is None:
            raise RuntimeError("mirror collection is not configured")
        if self._mirror_client is None:
            self._mirror_client = self.mirror.client()
        return self._mirror_client

    def _primary_handle(self) -> Any:
        """主集合的 Milvus 客户端（懒构造）。

        镜像回落要核对主集合里这条还在不在，主集合的 mem0 客户端不暴露这层
        能力，所以自己握一个客户端 —— 从 infra 拿，记忆层不直接碰 Milvus SDK。
        """

        if self._primary_client is None:
            from thinkback.infra.milvus_collection import connect

            store = self.config.get("vector_store", {}).get("config", {})
            self._primary_client = connect(
                url=str(store.get("url", "")),
                database=str(store.get("db_name", "")),
                token=str(store.get("token", "")),
            )
        return self._primary_client

    def _mirror_scope_ids(self, *, user_id: str, memory_scope_id: str, limit: int) -> list[str]:
        """镜像集合里属于该作用域的记忆 ID。

        列不出来就往上抛：擦除复核把这个异常当成「未能证明已删」，任务失败
        重试，而不是当作「镜像里没有数据」。
        """

        if self.mirror is None:
            return []
        from thinkback.infra.milvus_collection import scope_ids

        return scope_ids(
            client=self._mirror_handle(),
            collection=self.mirror.collection,
            user_id=user_id,
            agent_id=memory_scope_id,
            limit=limit,
        )

    def _primary_present_ids(self, memory_ids: list[str]) -> set[str]:
        """主集合里还在的记忆 ID。

        镜像回落只能补主集合有的东西：主集合里已经删掉的（擦除已完成），
        镜像里若还留着就说明镜像删除失败，直接补进来等于把已擦除的事实复活。
        """

        if not memory_ids:
            return set()
        from thinkback.infra.milvus_collection import present_ids

        return present_ids(
            client=self._primary_handle(),
            collection=self._primary_collection(),
            memory_ids=memory_ids,
        )

    def _mirror_write(self, memory_ids: list[str]) -> None:
        """双写：把刚写的行搬到次集合。

        窗口内回滚只需把集合名改回旧集合名，所以旧集合必须跟得上新集合的写入。
        搬不到（有时是刚写入的行在 growing segment 里还不可见）就重试几次；
        整体失败只告警 —— 主集合已经写成功，不能因为镜像失败把写入搞挂。
        """

        if self.mirror is None or not memory_ids:
            return
        for attempt, delay in enumerate((*self._NOT_FOUND_RETRY_DELAYS, None)):
            try:
                moved = mirror_rows(
                    client=self._mirror_handle(),
                    source=self._primary_collection(),
                    target=self.mirror.collection,
                    ids=memory_ids,
                )
            except Exception as exc:  # noqa: BLE001 - 镜像失败不阻断主写入
                logger.bind(error_type=type(exc).__name__).warning("memory mirror write failed")
                return
            if moved >= len(memory_ids):
                logger.bind(moved=moved).info("memory mirror write completed")
                return
            if delay is None:
                logger.bind(expected=len(memory_ids), moved=moved).warning(
                    "memory mirror write incomplete"
                )
                return
            logger.bind(moved=moved, expected=len(memory_ids), attempt=attempt).warning(
                "memory mirror write target not visible yet; retrying"
            )
            time.sleep(delay)

    def _mirror_delete(self, memory_ids: list[str]) -> None:
        """双写窗口内的删除同步：旧集合必须跟着删，否则回滚会把已擦除事实带回来。

        失败必须往上抛：吞掉异常等于宣称「已擦除」，而旧集合里还留着原文，
        回滚时它会重新出现在召回里。擦除要么做完，要么失败重试（ADR-0001）。
        """

        if self.mirror is None or not memory_ids:
            return
        try:
            delete_mirror_rows(
                client=self._mirror_handle(),
                target=self.mirror.collection,
                ids=memory_ids,
            )
            logger.bind(deleted=len(memory_ids)).info("memory mirror delete completed")
        except Exception as exc:
            logger.bind(error_type=type(exc).__name__).warning("memory mirror delete failed")
            raise

    def _mirror_memory_client(self) -> Any:
        """镜像集合上的 mem0 客户端（懒构造）。

        双读回落必须用同一个引擎打分：自己写一套向量检索会把两边的分数放到
        不同量纲上（ADR-0003），合并排序就没有依据了。
        """

        if self.mirror is None:
            raise RuntimeError("mirror collection is not configured")
        if self._mirror_memory is None:
            with self._memory_client_lock:
                if self._mirror_memory is None:
                    config = deepcopy(self.config)
                    config.setdefault("vector_store", {}).setdefault("config", {})[
                        "collection_name"
                    ] = self.mirror.collection
                    self._mirror_memory = self._build_client_for(config)
        return self._mirror_memory

    def _merge_mirror_search(
        self,
        query: str,
        items: list[dict[str, Any]],
        *,
        user_id: str,
        memory_scope_id: str,
        limit: int,
        threshold: float | None,
        explain: bool,
    ) -> list[dict[str, Any]]:
        """主集合命中不足时回落旧集合（迁移窗口内的双读）。

        回落只补位、不改序：主集合的结果在前，旧集合的结果按 id 去重后追加。
        窗口结束后清掉镜像配置，这段逻辑就不再参与。
        """

        if self.mirror is None or len(items) >= limit:
            return items
        search_kwargs: dict[str, Any] = {
            **self._scope_kwargs("search", user_id=user_id, memory_scope_id=memory_scope_id),
            **self._limit_kwargs("search", limit),
            "threshold": threshold,
        }
        if explain:
            search_kwargs["explain"] = True
        try:
            client = self._mirror_memory_client()
            result = self._invoke(client, "search", query, **search_kwargs)
        except Exception as exc:  # noqa: BLE001 - 回落失败不影响主集合结果
            logger.bind(error_type=type(exc).__name__).warning("memory mirror search failed")
            return items
        extra = self._results(result)
        if not extra:
            return items
        seen = {str(item.get("id", "")) for item in items}
        candidates = [
            str(item.get("id", "")) for item in extra if str(item.get("id", "")) not in seen
        ]
        if not candidates:
            return items
        # 只补主集合里还在的：主集合删掉的（擦除已完成）若镜像里还留着，
        # 补进来等于把已擦除的事实复活。核对失败就不回落 —— 主集合的结果
        # 仍然可用，缺几条回落比放进一条已擦除的事实安全。
        try:
            alive = self._primary_present_ids(candidates)
        except Exception as exc:  # noqa: BLE001 - 核对失败只放弃回落
            logger.bind(error_type=type(exc).__name__).warning(
                "memory mirror presence check failed; fallback skipped"
            )
            return items
        merged = list(items)
        skipped = 0
        for item in extra:
            key = str(item.get("id", ""))
            if key in seen:
                continue
            if key not in alive:
                skipped += 1
                continue
            seen.add(key)
            merged.append(item)
        logger.bind(
            primary_count=len(items),
            mirror_count=len(extra),
            merged_count=len(merged),
            skipped_absent_in_primary=skipped,
        ).info("memory mirror search merged")
        return merged[:limit]

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
          与 thinkback 自己的 L3 executor 抢资源、延长端到端延迟、
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
        return self._invoke(self.memory_client, operation, *args, **kwargs)

    def _invoke(self, client: Any, operation: str, *args: Any, **kwargs: Any) -> Any:
        """在指定客户端上执行一次后端调用。

        镜像集合的读走这里：主集合与镜像集合用同一套并发预算与线程池内联策略，
        打分也出自同一个引擎，跨集合的分数才在同一个量纲上（ADR-0003）。
        """

        call_log = logger.bind(
            operation=operation,
            arg_count=len(args),
            kwarg_keys=sorted(str(key) for key in kwargs),
            max_concurrent_calls=self.max_concurrent_calls,
        )
        call_log.info("memory backend call started")
        try:
            method = getattr(client, operation)
            with self._capacity_for(operation), _mem0_inline_threadpool():
                result = method(*args, **kwargs)
            call_log.bind(**self._result_summary(result)).info("memory backend call completed")
            return result
        except Exception as exc:
            call_log.bind(
                error_type=type(exc).__name__,
                error_message=str(exc)[:200],
            ).warning("memory backend call failed")
            raise RuntimeError(f"mem0 library {operation} failed: {exc}") from exc

    def _capacity_for(self, operation: str) -> BoundedSemaphore:
        """按操作类型取并发预算：读用自己的额度，不被写积压堵住。"""
        if operation in _READ_OPERATIONS:
            return self._read_capacity
        return self._call_capacity

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

    def _write_lock_for(self, key: str) -> Lock:
        """取写锁：同一 key 的写操作串行，不同 key 尽量并行。"""
        return self._write_locks[hash(key) % _WRITE_LOCK_STRIPES]

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

    def _show_expired_kwargs(self, operation: str) -> dict[str, Any]:
        """列出时带上已过期记录（引擎支持该参数时）。

        mem0 的 ``get_all`` 默认隐藏过期行。删除与擦除复核必须看得见它们：
        看不见就列不出来、删不掉，擦除却报「已完成」—— 记忆活得越久越擦不掉，
        与保留策略的方向正好相反。引擎版本不支持该参数时不传。
        """
        method = getattr(self.memory_client, operation)
        try:
            parameters = signature(method).parameters
        except (TypeError, ValueError):
            return {}
        return {"show_expired": True} if "show_expired" in parameters else {}

    @staticmethod
    def _results(result: object) -> list[dict[str, Any]]:
        if isinstance(result, dict):
            # 默认值不能给 []：给了它，raw_results 永远是 list，下面那条
            # 「单条 {"id","memory"}」的分支就永远走不到（死分支）。这里与
            # _result_summary 取值方式保持一致。
            raw_results = result.get("results", result.get("memories"))
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
    起 worker；thinkback 已经维护了自己的 L3 executor，再额外启线程会：
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
