"""P0 memory service workflows aligned with the three-layer architecture.

本模块是应用层编排器：领域策略（安全门禁 / 槽位抽取 / 召回去重 / 摘要）
已下沉到 ``thinkback.domain``，存储与 mem0 适配在
``thinkback.memory.repositories`` / ``thinkback.memory.backends``。
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress
from contextvars import copy_context
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from threading import RLock
from typing import Any, Literal

from loguru import logger

from thinkback.domain.errors import TaskStaleWriteError
from thinkback.domain.ports import HistorySource, MemoryBackend, MemoryRepository
from thinkback.domain.recall_policy import dedupe_and_clip
from thinkback.domain.safety import (
    messages_are_restricted_or_unsafe,
    text_is_restricted_or_unsafe,
)
from thinkback.domain.slots import (
    canonical_memory_text,
    canonical_memory_text_from_source,
    conflict_partition,
    conflict_partition_from_metadata,
    context_terms,
    extract_birthday,
    extract_communication_preference,
    extract_current_location,
    extract_current_nickname,
    extract_current_work_status,
    extract_favorite_consumable,
    extract_pet_name,
    extract_sleep_reminder_preference,
    is_backend_managed_memory_id,
    is_local_index_memory_id,
    is_same_source_local_p0_memory,
    memory_conflict_slot,
    memory_context_allowed,
    memory_supported_by_source,
    p0_canonical_memories_from_source,
    p0_canonical_memory_for_source_slot,
    query_conflict_slot,
    query_is_broad_memory_request,
    round_order,
    should_extract_to_l3,
    source_cursor,
    source_cursor_from_refs,
    source_has_any,
)
from thinkback.domain.summarization import SummaryComposer
from thinkback.infra.structured_logging import LOGGER_MEMORY
from thinkback.memory.decay import MemoryDecaySweeper
from thinkback.memory.l2_refresh import L2BackgroundRefresher
from thinkback.memory.l3 import L3WriteExecutor
from thinkback.memory.repositories import (
    InMemoryMemoryRepository,
    JournalEntry,
    SqlAlchemyMemoryRepository,
    TaskEntry,
    request_fingerprint,
    source_ref_key,
)
from thinkback.memory.schemas import (
    AdminMemoryItem,
    AppendMemoryRequest,
    AppendMemoryResponse,
    DataClassification,
    DeleteMemoryRequest,
    DeleteMemoryResponse,
    DeleteScope,
    GetMemoryResponse,
    ListMemoriesResponse,
    ManagedMemoryItem,
    MemoryItem,
    MemoryStatus,
    MemoryType,
    MessageRole,
    OperationType,
    RebuildMemoryRequest,
    RebuildMemoryResponse,
    RecallIntent,
    RecallMemoryRequest,
    RecallMemoryResponse,
    SourceType,
    SummaryState,
    TaskResponse,
    TaskStatus,
    UpdateMemoryRequest,
    UpdateMemoryResponse,
)


class _BackendDeleteSync:
    """H-3: sync 模式下 backend.delete 的延迟执行包装。

    把 ``self.backend.delete(memory_id)`` 从 _index_mutation_lock 持锁期
    推迟到锁外执行。持锁期间只收集 ``_BackendDeleteSync`` 列表，锁外由
    ``delete()`` 调度器统一执行 ``callback()``，这样 mem0 网络 I/O 不会
    串行化其他同 user L3 写。
    """

    __slots__ = ("memory_id", "backend")

    def __init__(self, memory_id: str, backend: Any) -> None:
        self.memory_id = memory_id
        self.backend = backend

    def __call__(self) -> None:
        self.backend.delete(self.memory_id)


class _BackendUpdate:
    """H-3: 锁外执行的 backend.update 延迟回调。

    与 ``_BackendDeleteSync`` 对称：锁内只 append 到 pending list，
    锁外由 ``_run_backend_writes`` 调用 ``__call__`` 执行 mem0 网络写。

    N-6（W-1 修订）: 必须走 ``self.backend.update(...)`` 而不是
    ``self.backend._call("update", ...)``。``Mem0LibraryMemoryBackend.update``
    内部同样经 ``_call`` 把 mem0 异常包装为统一的
    ``RuntimeError("mem0 library update failed: ...")``（异常语义不变），
    并额外承担「紧邻 add 的 pk 点查可见性窗口」退避重试 —— 直接调
    ``_call`` 会绕过该重试，让本地索引与向量库内容分叉（真实链路
    复现过，见 DEBUG_REPORT W-1）。
    """

    __slots__ = ("memory_id", "data", "backend")

    def __init__(self, memory_id: str, data: str, backend: Any) -> None:
        self.memory_id = memory_id
        self.data = data
        self.backend = backend

    def __call__(self) -> None:
        self.backend.update(self.memory_id, self.data)


def _memory_valid_sort_key(memory: Any) -> float:
    """``valid_at`` 排序键：None（遗留行）视为最老，退化为 epoch 0。"""

    valid_at = getattr(memory, "valid_at", None)
    if valid_at is None:
        return 0.0
    return float(valid_at.timestamp())


class _RebuildDeleteCutoffs:
    """单次 rebuild 的删除截止时间快照。

    delete-all 截止对整个 user+scope 只算一次；session 截止按 session_id
    惰性记忆。替代旧路径"每个回合各做一次 list_memories 全量扫描"的
    O(rounds × memories) 行为 —— 大用户 rebuild 会阻塞仓储事件循环，
    把其他用户的 DB 操作拖到 30s 超时。
    """

    def __init__(self, service: MemoryService, user_id: str, memory_scope_id: str) -> None:
        self._service = service
        self._user_id = user_id
        self._memory_scope_id = memory_scope_id
        self._delete_all_cutoff = service._delete_all_cutoff(user_id, memory_scope_id)
        self._session_cutoffs: dict[str, datetime | None] = {}

    def allows(self, entry: JournalEntry) -> bool:
        """回合是否晚于所有删除墓碑截止（晚于 = 未被删除屏障拦截）。"""
        if self._delete_all_cutoff is not None and not self._service._round_is_after_timestamp(
            entry.source_timestamp, self._delete_all_cutoff
        ):
            return False
        if entry.session_id not in self._session_cutoffs:
            self._session_cutoffs[entry.session_id] = self._service._session_delete_cutoff(
                self._user_id, self._memory_scope_id, entry.session_id
            )
        session_cutoff = self._session_cutoffs[entry.session_id]
        return session_cutoff is None or self._service._round_is_after_timestamp(
            entry.source_timestamp, session_cutoff
        )


class MemoryService:
    """三层记忆服务核心编排器

    架构概览（write -> recall -> delete/rebuild）：

    写入流程：append(request)
      1. 幂等检查：round_id 是否已处理过
      2. 内容安全检查：是否包含敏感内容
      3. L1 更新：存储原始回合到 L1 短期缓存
      4. L3 写入：调用后端提取长期记忆
         - 同步模式：立即阻塞执行
         - 异步模式：提交到后台线程池，返回 deferred 状态
      5. 本地索引：记录 L3 后端返回的记忆 ID 和反链
      6. L2 缓存失效：摘要可能过期

    召回流程：recall(request)
      1. 读取 L1：最近 N 轮对话
      2. 读取 L2：会话摘要（若有效则返回，若陈旧则标记降级）
      3. 读取 L3：通过后端语义检索+本地索引去重
      4. 去重+裁剪：基于 token_budget 做 LRU 去重和截断
      5. 返回：按 L1->L2->L3 优先级排序

    删除流程：delete(request)
      1. 删除范围检查：单条/会话/全局
      2. 标记 L1：轮次删除墓碑
      3. 删除 L3：后端异步清理，本地索引标记 DELETED
      4. 添加删除屏障：墓碑记录，用于阻止重建时的漂移
      5. L2 标记 DIRTY：需要重建

    重建流程：rebuild(request)
      1. 历史一致性检查：如果提供了外部版本号
      2. 清理陈旧数据：删除源引用已被删除的 L3 记忆
      3. 重新提取 L3：对覆盖不足的回合
      4. 重建 L2：从最新回合重新摘要
      5. 异步后台完成
    """

    name = "thinkback"
    LONG_TERM_SCOPE_ID = "thinkback"
    # N-3: 单个 task 的最大重试次数。retry_count 达到上限后写入 DEAD_LETTER
    # 并抛出 RuntimeError，阻止客户端无限重试。保守值 5 与上游 SDK 默认对齐。
    MAX_RETRIES = 5

    def __init__(
        self,
        *,
        repository: MemoryRepository,
        backend: MemoryBackend,
        history_source: HistorySource | None = None,
        mem0_infer_facts: bool = True,
        l3_write_mode: Literal["sync", "async"] = "sync",
        l3_executor: ThreadPoolExecutor | None = None,
        l3_executor_workers: int = 2,
        l3_max_pending_tasks: int = 64,
        l3_queue_wait_seconds: float = 0.25,
        l2_summary_composer: SummaryComposer | None = None,
        l2_refresh_interval_rounds: int = 5,
        decay_sweeper: MemoryDecaySweeper | None = None,
    ) -> None:
        """初始化记忆服务的核心依赖与运行边界。

        参数：
          repository: L1/L2/L3 本地索引的存储层（内存或 SQLAlchemy）
          backend: L3 后端实现（向量库、语义检索）
          history_source: 可选的外部历史源，用于重建时的数据回放
          l3_write_mode:
            - "sync": 写入时立即调用后端，返回前完成
            - "async": 写入时提交后台，立即返回 deferred 状态
          l3_executor: 自定义的 ThreadPoolExecutor；为 None 时自动创建
          l3_executor_workers: 后台执行器线程数
          l3_max_pending_tasks: L3 后台写入队列最大容量（限流保护）
          l3_queue_wait_seconds: 队列满时的等待超时
          l2_summary_composer: L2 LLM 综合摘要合成器；None = L2 保持拼接降级（V1 行为）
          l2_refresh_interval_rounds: L2 去抖间隔（每作用域累计 N 个 append 触发刷新）
          decay_sweeper: 遗忘 decay 清扫器；None = 不启用（默认，V1 行为）

        核心不变量：
        - repository 和 backend 作为依赖，必须提供
        - L3 异步写入由 BoundedSemaphore 限流，防止队列爆炸
        - L1/L2 缓存 TTL 保证读的"最终一致性"
        """
        self.repository = repository
        self.backend = backend
        self.history_source = history_source
        # V2.1 瘦身：删除 ttl 读缓存（_active_memory_cache / _summary_cache）。
        # 之前每次写路径都 invalidate，cache lifetime ≈ 0；唯一受益的是"读-读-读"
        # 无穿插写的突发场景，但 mem0 读延迟 ~50ms，cache 收益不抵复杂度。
        # 后续如需热读缓存，加在仓库层（SQLite materialized view 或专用 KV），
        # 不放服务层。
        self.mem0_infer_facts = mem0_infer_facts
        self.l3_write_mode = l3_write_mode
        self.l3_executor_workers = l3_executor_workers
        self.l3_max_pending_tasks = l3_max_pending_tasks
        self.l3_queue_wait_seconds = l3_queue_wait_seconds
        # L3 后台写基础设施（线程池生命周期 / 容量限流 / future 记账）独立成
        # L3WriteExecutor；executor 生命周期规则（caller 注入则不由本服务关闭）
        # 与报错文案保持不变，见 memory/l3.py。
        self._l3 = L3WriteExecutor(
            executor=l3_executor,
            workers=l3_executor_workers,
            max_pending_tasks=l3_max_pending_tasks,
            queue_wait_seconds=l3_queue_wait_seconds,
        )
        self._index_mutation_lock = RLock()
        # Serializes every read-modify-write on a task's status/result so that
        # concurrent cleanup completions, registrations and the synchronous
        # finalization in delete()/rebuild() cannot lose updates. The SQLAlchemy
        # repository rebuilds a fresh TaskEntry per get_task and overwrites the
        # whole row on save_task, so a lock spanning the full get->mutate->save
        # sequence is required (object identity alone is not enough).
        self._task_status_lock = RLock()
        # L2 LLM 综合摘要后台刷新（P0）：composer 为 None 时完全不启用，
        # L2 保持拼接式降级实现；启用时复用 L3 后台线程池。
        # P2#7 遗忘 decay：默认不构造（开关在 dependencies 侧按配置注入）。
        self._decay_sweeper = decay_sweeper
        self._l2_refresher: L2BackgroundRefresher | None = (
            L2BackgroundRefresher(
                repository=repository,
                composer=l2_summary_composer,
                executor=self._l3.executor,
                refresh_interval_rounds=l2_refresh_interval_rounds,
                on_refreshed=lambda _u, _s: None,  # V2.1：cache 已删，no-op
            )
            if l2_summary_composer is not None
            else None
        )

    @staticmethod
    def _metadata_key_summary(metadata: dict[str, Any] | None) -> list[str]:
        """只返回 metadata 键名，避免把用户正文、密钥或大对象写入日志。"""

        return sorted(str(key) for key in metadata or {})[:20]

    @classmethod
    def _long_term_scope_id(cls, _request: Any) -> str:
        return cls.LONG_TERM_SCOPE_ID

    @staticmethod
    def _session_scope_id(request: Any) -> str:
        session_id = getattr(request, "session_id", None)
        if session_id:
            return str(session_id)
        return "__all_sessions__"

    def _check_retry_budget(self, task: TaskEntry) -> None:
        """N-3: 校验 retry budget。

        当 ``task.retry_count`` 已达到或超过 ``MAX_RETRIES``，把 task 标
        ``DEAD_LETTER``（last_error 必填）并抛 ``RuntimeError``，阻断本次
        重试。客户端应在收到 dead_letter 响应后停止重试。
        """

        if task.retry_count >= self.MAX_RETRIES:
            task.status = TaskStatus.DEAD_LETTER
            task.last_error = (
                f"dead_letter: operation {task.task_id} exceeded retry budget "
                f"of {self.MAX_RETRIES} (retry_count={task.retry_count})"
            )
            self.repository.save_task(task)
            raise RuntimeError(
                f"dead_letter: operation {task.task_id} exceeded retry budget of {self.MAX_RETRIES}"
            )

    def _rebuild_summary_scope_ids(self, request: RebuildMemoryRequest) -> list[str]:
        if request.session_id:
            return [request.session_id]
        if self.history_source:
            history_rounds = self.history_source.list_rounds(
                request.user_id,
                self._session_scope_id(request),
                None,
            )
            history_scope_ids = sorted({entry.memory_scope_id for entry in history_rounds})
            if history_scope_ids:
                return history_scope_ids
        return sorted(
            {entry.memory_scope_id for entry in self.repository.list_user_rounds(request.user_id)}
        )

    def _history_scope_id(self, request: RebuildMemoryRequest) -> str:
        if request.session_id:
            return str(request.session_id)
        if self.history_source:
            return self._session_scope_id(request)
        scope_ids = self._rebuild_summary_scope_ids(request)
        if len(scope_ids) == 1:
            return scope_ids[0]
        return self._session_scope_id(request)

    def _user_summary_scope_ids(self, user_id: str) -> set[str]:
        rounds = self.repository.list_user_rounds(user_id)
        return {entry.memory_scope_id for entry in rounds} or {self.LONG_TERM_SCOPE_ID}

    def append(self, request: AppendMemoryRequest) -> AppendMemoryResponse:
        """追加一个完整对话回合，并按安全策略同步或异步写入 L3。

        完整的写入流程（流程图）：

        用户请求 (request)
               |
               v
          幂等检查 (round_id)
          /            \
        新              已存在
        |                |
        |         -> 返回 already_done
        v
     安全检查 (restricted_or_unsafe)
       /    \
      安全   不安全
      |        |
      |     -> 标记 SKIP, 删除源
      |
      v
   检查写入模式
    /          \
  SYNC        ASYNC
   |            |
   |        -> 预留队列
   |        -> 提交后台
   v        -> 立即返回 deferred
L3 写入    (不阻塞)
   |
   v
L1 更新 (缓存回合)
   |
   v
L2 更新 (缓存失效)
   |
   v
返回 AppendMemoryResponse

核心参数（request）：
  - round_id: 回合 ID，用于幂等去重
  - user_id/session_id: 维度隔离
  - messages: [user_msg, assistant_msg]
  - metadata: 用户自定义上下文

返回状态：
  - "completed": L3 写入完成（同步模式或重新提交）
  - "already_done": 幂等重试，返回前次结果
  - (异步模式): 立即返回 "completed"，后台执行

"""
        session_scope_id = self._session_scope_id(request)
        l3_scope_id = self._long_term_scope_id(request)
        LOGGER_MEMORY.started(
            "append",
            user_id=request.user_id,
            session_id=request.session_id,
            round_id=request.round_id,
            message_count=len(request.messages),
            metadata_key_count=len(request.metadata or {}),
        )
        append_log = logger.bind(
            user_id=request.user_id,
            session_id=request.session_id,
            round_id=request.round_id,
            message_count=len(request.messages),
            l3_write_mode=self.l3_write_mode,
            metadata_keys=self._metadata_key_summary(request.metadata),
        )
        append_log.info("memory append started")
        existing = self.repository.get_round(request.round_id)
        task_id = f"memory-extract:{request.round_id}"
        if existing:
            self._assert_round_matches_request(existing, request)
            existing_task = self.repository.get_task(task_id)
            if existing_task is not None and existing_task.status in {
                TaskStatus.PENDING,
                TaskStatus.RUNNING,
                TaskStatus.COMPLETED,
            }:
                LOGGER_MEMORY.completed(
                    "append",
                    "already_done",
                    round_id=request.round_id,
                    task_id=task_id,
                )
                return AppendMemoryResponse(
                    status="already_done", task_id=task_id, round_id=request.round_id
                )

        restricted_or_unsafe = self._is_restricted_or_unsafe(request.messages)
        l3_slot_reserved = False
        if self.l3_write_mode == "async" and not restricted_or_unsafe:
            self._l3.reserve_write_slot()
            l3_slot_reserved = True

        previous_task = self.repository.get_task(task_id)
        was_retrying_failed_task = (
            previous_task is not None and previous_task.status is TaskStatus.FAILED
        )
        task = previous_task or TaskEntry(
            task_id=task_id,
            request_id=request.request_id,
            op_type=OperationType.WRITE_ROUND,
            scope={
                "user_id": request.user_id,
                "session_id": request.session_id,
                "session_scope_id": session_scope_id,
                "long_term_scope_id": l3_scope_id,
            },
            status=TaskStatus.RUNNING,
        )
        task.request_id = request.request_id
        task.status = TaskStatus.RUNNING
        task.last_error = None
        if was_retrying_failed_task:
            task.retry_count += 1
            self._check_retry_budget(task)
        self.repository.save_task(task)
        try:
            entry = existing or self.repository.save_round(request)
            entry.memory_scope_id = session_scope_id
            if restricted_or_unsafe:
                l3_events = [{"event": "SKIP", "reason": "restricted_or_unsafe_memory_content"}]
                self.repository.mark_round_deleted(request.round_id)
                append_pending_backend_writes: list[Any] = []
            else:
                entry.round_state = "pending_append" if self.l3_write_mode == "sync" else "active"
                if self.l3_write_mode == "sync":
                    self.repository.mark_round_pending_append(request.round_id)
                if self.l3_write_mode == "async":
                    append_pending_backend_writes = self._publish_append_round_locally(
                        entry=entry,
                        request=request,
                        session_scope_id=session_scope_id,
                        l3_scope_id=l3_scope_id,
                    )
                    l3_events = [{"event": "DEFERRED", "reason": "l3_background_write"}]
                    self._submit_l3_write(
                        task,
                        messages=[
                            {"role": message.role.value, "content": message.content}
                            for message in request.messages
                        ],
                        user_id=request.user_id,
                        memory_scope_id=l3_scope_id,
                        source_refs=[
                            {"session_id": request.session_id, "round_id": request.round_id}
                        ],
                        request_metadata=request.metadata,
                        slot_reserved=l3_slot_reserved,
                    )
                    l3_slot_reserved = False
                    # 锁外执行 append 后台 publish 阶段收集到的 backend 写
                    # （async 模式下 backend.delete 已在 _l3.executor 上，本列表仅含
                    # _BackendDeleteSync 兜底项）。
                    if append_pending_backend_writes:
                        self._run_backend_writes(
                            append_pending_backend_writes, index_log=append_log
                        )
                    LOGGER_MEMORY.completed(
                        "append",
                        "completed",
                        round_id=request.round_id,
                        task_id=task.task_id,
                        l3_event_count=len(l3_events),
                        mode="async",
                    )
                    return AppendMemoryResponse(
                        status="completed",
                        task_id=task.task_id,
                        round_id=request.round_id,
                        l3_events=l3_events,
                    )
                l3_events = self._run_l3_write(
                    messages=[
                        {"role": message.role.value, "content": message.content}
                        for message in request.messages
                    ],
                    user_id=request.user_id,
                    memory_scope_id=l3_scope_id,
                    source_refs=[{"session_id": request.session_id, "round_id": request.round_id}],
                    request_metadata=request.metadata,
                )
                entry.round_state = "active"
                self.repository.mark_round_active(request.round_id)
                append_pending_backend_writes = self._publish_append_round_locally(
                    entry=entry,
                    request=request,
                    session_scope_id=session_scope_id,
                    l3_scope_id=l3_scope_id,
                )
        except Exception as exc:
            if l3_slot_reserved:
                self._l3.release_write_slot()
                l3_slot_reserved = False
            task.status = TaskStatus.FAILED
            task.last_error = str(exc)
            self.repository.save_task(task)
            append_log.bind(task_id=task.task_id, error_type=type(exc).__name__).warning(
                "memory append failed"
            )
            raise

        task.status = TaskStatus.COMPLETED
        task.result = {
            "round_id": request.round_id,
            "l3_events": [self._redact_event(event) for event in l3_events],
        }
        # S3 (P1a 自动重放): append 终态写走 _mutate_task_with_retry，
        # 跨副本 CAS 冲突时重读快照重放。status=COMPLETED 是幂等转换；
        # conflict 后重读到最新 row_version（哪怕是另一副本已 FAILED），
        # mutate 回调会把 status 改为 COMPLETED 与业务最终态对齐。
        self._mutate_task_with_retry(
            task.task_id,
            lambda current_task: self._finalize_append_task(
                current_task, request.round_id, l3_events
            ),
            log_context={"stage": "append_final"},
        )
        # 锁外执行 append 后台 publish 阶段（_backfill_p0_slots → supersede）收集
        # 的 backend.delete 回调；避免 mem0 网络 I/O 串行化其他同 user L3 写。
        if append_pending_backend_writes:
            self._run_backend_writes(append_pending_backend_writes, index_log=append_log)
        LOGGER_MEMORY.completed(
            "append",
            "completed",
            round_id=request.round_id,
            task_id=task.task_id,
            l3_event_count=len(l3_events),
            mode="sync",
        )
        append_log.bind(
            status="completed",
            task_id=task.task_id,
            l3_event_count=len(l3_events),
            mode="sync",
        ).info("memory append completed")
        return AppendMemoryResponse(
            status="completed",
            task_id=task.task_id,
            round_id=request.round_id,
            l3_events=[self._redact_event(event) for event in l3_events],
        )

    def _publish_append_round_locally(
        self,
        *,
        entry: JournalEntry,
        request: AppendMemoryRequest,
        session_scope_id: str,
        l3_scope_id: str,
    ) -> list[Any]:
        """apppend 后的本地状态发布；返回锁外待执行的 backend 写回调。"""
        self.repository.update_l1(entry)
        self._upsert_default_summary(request.user_id, session_scope_id)
        if self._l2_refresher is not None:
            # 拼接版已同步落库（降级保底）；LLM 综合版按去抖间隔后台刷新。
            self._l2_refresher.maybe_submit(request.user_id, session_scope_id)
        if self._decay_sweeper is not None:
            # P2#7：遗忘清扫（进程级时间门控，默认 1 小时至多一次）。
            self._decay_sweeper.maybe_submit(request.user_id, l3_scope_id)
        pending_backend_writes = self._backfill_p0_slots_from_round_source(
            source_text=" ".join(
                message.content.strip()
                for message in request.messages
                if message.role is MessageRole.USER
                if message.content.strip()
            ),
            user_id=request.user_id,
            memory_scope_id=l3_scope_id,
            source_refs=[{"session_id": request.session_id, "round_id": request.round_id}],
            request_metadata=request.metadata,
        )
        return pending_backend_writes

    # ---- 测试与运维观测垫片：底层状态已移至 L3WriteExecutor ----

    @property
    def _l3_executor(self) -> ThreadPoolExecutor:
        return self._l3.executor

    @property
    def _owns_l3_executor(self) -> bool:
        return self._l3.owns_executor

    @property
    def _l3_executor_shutdown(self) -> bool:
        return self._l3.shutdown_done

    def drain_l3_background_tasks(self, timeout: float | None = None) -> None:
        self._l3.drain(timeout=timeout)
        # L2 刷新是 best-effort：只等待，不向上抛（失败已落日志并降级保留拼接版）。
        if self._l2_refresher is not None:
            self._l2_refresher.drain(timeout=timeout)

    def shutdown_l3_executor(self, *, wait: bool = True) -> None:
        """关闭服务自有的 L3 executor。

        - `_owns_l3_executor=False`（caller 注入）：**不**关闭，避免 double-shutdown
          抛 RuntimeError，并把控制权留给 caller
        - 已关闭：幂等返回

        生产 lifespan 退出路径必须调用此方法，否则 SIGTERM 时进程延迟数十秒。
        注意：调用方应先 `drain_l3_background_tasks` 再 `shutdown_l3_executor`，
        以便 in-flight 工作有机会完成。
        """
        self._l3.shutdown(wait=wait)

    def l3_background_status(self) -> dict[str, Any]:
        pending_write_tasks = self._l3.pending_write_count()
        cleanup_tasks = self._l3.cleanup_count()
        return {
            "write_mode": self.l3_write_mode,
            "executor_workers": self.l3_executor_workers,
            "max_pending_tasks": self.l3_max_pending_tasks,
            "pending_write_tasks": pending_write_tasks,
            "cleanup_tasks": cleanup_tasks,
            "available_capacity": max(0, self.l3_max_pending_tasks - pending_write_tasks),
        }

    @staticmethod
    def _bounded_internal_request_id(*parts: str) -> str:
        raw = ":".join(part for part in parts if part)
        if len(raw) <= 128:
            return raw
        digest = sha256(raw.encode("utf-8")).hexdigest()[:32]
        return f"memory-extract:{digest}"

    def _submit_l3_write(
        self,
        task: TaskEntry,
        *,
        messages: list[dict[str, str]],
        user_id: str,
        memory_scope_id: str,
        source_refs: list[dict[str, str]],
        request_metadata: dict[str, Any],
        slot_reserved: bool = False,
    ) -> None:
        """将 L3 写入提交到后台执行器。

        task 是外层可查询的任务状态；messages 会传给后端提取，但日志只记录数量；
        source_refs 用于把 L3 记忆反查到会话/回合；slot_reserved 表示调用方已经占用
        队列容量，避免重复 acquire。
        """
        submit_log = logger.bind(
            task_id=task.task_id,
            user_id=user_id,
            memory_scope_id=memory_scope_id,
            message_count=len(messages),
            source_ref_count=len(source_refs),
            metadata_keys=self._metadata_key_summary(request_metadata),
            slot_reserved=slot_reserved,
            pending_write_tasks=self._l3.pending_write_count(),
            max_pending_tasks=self.l3_max_pending_tasks,
        )
        submit_log.info("memory l3 background write submitted")
        reserved_here = False
        if not slot_reserved:
            self._l3.reserve_write_slot()
            reserved_here = True
        try:
            context = copy_context()
            future = self._l3.executor.submit(
                context.run,
                self._complete_async_l3_write,
                task.task_id,
                messages,
                user_id,
                memory_scope_id,
                source_refs,
                request_metadata,
            )
        except Exception:
            if reserved_here:
                self._l3.release_write_slot()
            submit_log.warning("memory l3 background write submit failed")
            raise
        self._l3.track_write(
            future, lambda done_future: context.run(self._finish_l3_background_write, done_future)
        )
        submit_log.bind(pending_write_tasks=self._l3.pending_write_count()).info(
            "memory l3 background write queued"
        )

    def _finish_l3_background_write(self, future: Future[None]) -> None:
        self._l3.discard_write(future)
        self._l3.release_write_slot()
        failed = False
        if future.done() and not future.cancelled():
            failed = future.exception() is not None
        logger.bind(cancelled=future.cancelled(), failed=failed).info(
            "memory l3 background write finished"
        )

    def _finish_l3_cleanup(self, future: Future[None], task_id: str | None = None) -> None:
        self._l3.discard_cleanup(future)
        failed = False
        error: BaseException | None = None
        if future.done() and not future.cancelled():
            error = future.exception()
            failed = error is not None
        if task_id is not None:
            if failed:

                def mark_failed(task: TaskEntry) -> None:
                    task.status = TaskStatus.FAILED
                    task.last_error = str(error)

                self._mutate_task_with_retry(task_id, mark_failed)
            else:
                # 计数递减不依赖任务当前状态：delete 主体中途失败（task 已
                # FAILED）时，已提交的后台清理 future 仍要递减
                # pending_cleanup_tasks。否则重试会在残留计数上 +1，
                # 分母永远大于存活 future 数，任务卡 RUNNING 无法收敛。
                def decrement(task: TaskEntry) -> None:
                    pending = max(0, int(task.result.get("pending_cleanup_tasks", 1)) - 1)
                    task.result = {**task.result, "pending_cleanup_tasks": pending}
                    if pending == 0 and task.status is TaskStatus.RUNNING:
                        task.status = TaskStatus.COMPLETED

                self._mutate_task_with_retry(task_id, decrement)
        logger.bind(cancelled=future.cancelled(), failed=failed).info("memory l3 cleanup finished")

    def _mutate_task_with_retry(
        self,
        task_id: Any,
        mutate: Any,
        *,
        attempts: int = 3,
        log_context: dict[str, Any] | None = None,
    ) -> TaskEntry | None:
        """乐观锁下的任务读-改-写：冲突时重读快照重放变更（P1a）。

        进程内并发由 ``_task_status_lock`` 串行化；跨副本并发由仓储层
        row_version CAS 拦截（TaskStaleWriteError），这里以**重读重放**
        收敛——后台计数类 best-effort 变更（pending_cleanup_tasks 增减）
        在冲突时重放是安全的：mutate 必须表达为"基于最新快照的相对变更"。
        重试耗尽放弃并打 warning（不影响主链路）。
        """
        with self._task_status_lock:
            for _attempt in range(attempts):
                task = self.repository.get_task(task_id)
                if task is None:
                    return None
                mutate(task)
                try:
                    return self.repository.save_task(task)
                except TaskStaleWriteError:
                    continue
        logger.bind(
            task_id=task_id,
            attempts=attempts,
            **(log_context or {}),
        ).warning("task optimistic lock contention gave up")
        return None

    @staticmethod
    def _finalize_append_task(
        task: TaskEntry, round_id: str, l3_events: list[dict[str, Any]]
    ) -> None:
        """S3: append 终态 mutate 回调。set 绝对值；与 ``_mutate_task_with_retry``
        配合，跨副本 CAS 冲突时重读快照重放。"""

        task.status = TaskStatus.COMPLETED
        task.result = {
            "round_id": round_id,
            "l3_events": [MemoryService._redact_event(event) for event in l3_events],
        }

    def _register_pending_l3_cleanup(self, task_id: str | None) -> None:
        if task_id is None:
            return

        def mutate(task: TaskEntry) -> None:
            task.status = TaskStatus.RUNNING
            task.result = {
                **task.result,
                "pending_cleanup_tasks": int(task.result.get("pending_cleanup_tasks", 0)) + 1,
            }

        self._mutate_task_with_retry(task_id, mutate)

    def _unregister_pending_l3_cleanup(self, task_id: str | None) -> None:
        if task_id is None:
            return

        def mutate(task: TaskEntry) -> None:
            pending = max(0, int(task.result.get("pending_cleanup_tasks", 0)) - 1)
            task.result = {**task.result, "pending_cleanup_tasks": pending}

        self._mutate_task_with_retry(task_id, mutate)

    def _delete_backend_memory(self, memory_id: str, task_id: str | None = None) -> bool:
        if self.l3_write_mode == "async":
            self._register_pending_l3_cleanup(task_id)
            context = copy_context()
            try:
                future = self._l3.executor.submit(context.run, self.backend.delete, memory_id)
            except Exception:
                self._unregister_pending_l3_cleanup(task_id)
                raise
            self._l3.track_cleanup(
                future,
                lambda done_future: context.run(self._finish_l3_cleanup, done_future, task_id),
            )
            return True
        self.backend.delete(memory_id)
        return False

    def _schedule_backend_delete(
        self, memory_id: str, task_id: str | None = None
    ) -> bool | _BackendDeleteSync:
        """H-3: 调度 backend.delete，最小化 _index_mutation_lock 持锁期。

        async 模式：与 _delete_backend_memory 一致，提交到 _l3_executor，返回 True。
        sync 模式：返回 _BackendDeleteSync 包装的回调，caller 在锁外执行回调。
        这样持锁期间不会执行 mem0 网络 I/O，避免串行化其他同 user L3 写。
        """
        if self.l3_write_mode == "async":
            self._delete_backend_memory(memory_id, task_id=task_id)
            return True
        return _BackendDeleteSync(memory_id=memory_id, backend=self.backend)

    def _delete_backend_memories(self, memory_ids: list[str], task_id: str | None = None) -> bool:
        backend_memory_ids = [memory_id for memory_id in memory_ids if memory_id]
        if not backend_memory_ids:
            return False
        if self.l3_write_mode == "async":
            self._register_pending_l3_cleanup(task_id)
            context = copy_context()
            try:
                future = self._l3.executor.submit(
                    context.run, self.backend.delete_many, backend_memory_ids
                )
            except Exception:
                self._unregister_pending_l3_cleanup(task_id)
                raise
            self._l3.track_cleanup(
                future,
                lambda done_future: context.run(self._finish_l3_cleanup, done_future, task_id),
            )
            return True
        self.backend.delete_many(backend_memory_ids)
        return False

    def _complete_async_l3_write(
        self,
        task_id: str,
        messages: list[dict[str, str]],
        user_id: str,
        memory_scope_id: str,
        source_refs: list[dict[str, str]],
        request_metadata: dict[str, Any],
    ) -> None:
        """后台完成 L3 写入并把可查询任务状态持久化。"""

        async_log = logger.bind(
            task_id=task_id,
            user_id=user_id,
            memory_scope_id=memory_scope_id,
            message_count=len(messages),
            source_ref_count=len(source_refs),
            metadata_keys=self._metadata_key_summary(request_metadata),
        )
        async_log.info("memory l3 async write started")
        try:
            l3_events = self._run_l3_write(
                messages=messages,
                user_id=user_id,
                memory_scope_id=memory_scope_id,
                source_refs=source_refs,
                request_metadata=request_metadata,
            )
        except Exception as exc:
            # 关机竞态下 repository 可能已关闭：终态写丢失不能让回调抛异常
            # （异常会被线程池吞掉），显式记录损失，任务由读路径自愈兜底。
            try:
                with self._task_status_lock:
                    task = self.repository.get_task(task_id)
                    if task is None:
                        async_log.warning("memory l3 async write task missing")
                        return
                    task.status = TaskStatus.FAILED
                    task.last_error = str(exc)
                    self.repository.save_task(task)
            except Exception:  # noqa: BLE001
                async_log.bind(error_type=type(exc).__name__).error(
                    "memory l3 async write failed; task final state not persisted "
                    "(repository unavailable, task stays RUNNING until reclaimed)"
                )
                return
            async_log.bind(error_type=type(exc).__name__).warning("memory l3 async write failed")
            return
        try:
            with self._task_status_lock:
                task = self.repository.get_task(task_id)
                if task is None:
                    async_log.warning("memory l3 async write task missing")
                    return
                task.status = TaskStatus.COMPLETED
                task.last_error = None
                task.result = {
                    "round_id": source_refs[0].get("round_id") if source_refs else None,
                    "l3_events": [self._redact_event(event) for event in l3_events],
                }
                self.repository.save_task(task)
        except Exception:  # noqa: BLE001
            async_log.error(
                "memory l3 async write completed; task final state not persisted "
                "(repository unavailable, task stays RUNNING until reclaimed)"
            )
            return
        async_log.bind(l3_event_count=len(l3_events)).info("memory l3 async write completed")

    def _run_l3_write(
        self,
        *,
        messages: list[dict[str, str]],
        user_id: str,
        memory_scope_id: str,
        source_refs: list[dict[str, str]],
        request_metadata: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """执行一次 L3 写入流程：检查删除遮罩、调用后端、刷新本地缓存。

        流程图::

            进入 (messages, user_id, memory_scope_id,
                  source_refs, request_metadata)
                │
                ▼
            绑定日志上下文
            (user_id, memory_scope_id, message_count,
             source_ref_count, metadata_keys)
                │
                ▼
            INFO: "memory l3 write started"
                │
                ▼
            ┌── 遮罩检查 ──────────────────────────────┐
            │  _source_refs_excluded()                  │
            │  (检查删除墓碑 / 排除集合)               │
            └───────────────────────┬─────────────────┘
                    │ 命中
                    │   └─► INFO "l3 write skipped"
                    │       └─► 返回 [{"event": "SKIP",
                    │                    "reason": "source_ref_excluded"}]
                    │
                    ▼ 未命中
            ┌── 后端抽取 ──────────────────────────────┐
            │  _add_l3_from_messages()                 │
            │    ├─ 组装 l3_metadata (含 source_refs)  │
            │    ├─ 拼接 source_text                   │
            │    ├─ backend.add()                      │
            │    │    → Mem0LibraryMemoryBackend._call │
            │    ├─ 二次遮罩检查（_source_refs_excluded）│
            │    │   → 命中则 backend.delete_many 撤销 │
            │    └─ 对每个 event: _index_l3_event()    │
            │         ├─ 锁内 _index_l3_event_locked   │
            │         │   ├─ upsert MemoryIndexEntry   │
            │         │   └─ 收集 _BackendUpdate/      │
            │         │      _BackendDeleteSync 回调   │
            │         └─ 锁外 _run_backend_writes      │
            └───────────────────────┬─────────────────┘
                                │
                                ▼
            INFO: "memory l3 write completed"
            (l3_event_count)
                │
                ▼
            返回 events 列表

        注意：
        - ``backend.add`` 可能抛出 ``RuntimeError("mem0 library add failed: ...")``，
          由 :meth:`append` 或 :meth:`_complete_async_l3_write` 统一捕获并把
          task 标 ``FAILED``，调用方不会降级——L3 写入失败是必须上报的硬错误。
        - 二次遮罩检查用于竞态防护：另一并发路径可能在 ``backend.add`` 期间
          把同一 source_refs 标进删除集合，此时本地索引要回滚新写入的 mem0 记录。
        """
        run_log = logger.bind(
            user_id=user_id,
            memory_scope_id=memory_scope_id,
            message_count=len(messages),
            source_ref_count=len(source_refs),
            metadata_keys=self._metadata_key_summary(request_metadata),
        )
        run_log.info("memory l3 write started")
        if self._source_refs_excluded(user_id, memory_scope_id, source_refs):
            run_log.bind(reason="source_ref_excluded").info("memory l3 write skipped")
            return [{"event": "SKIP", "reason": "source_ref_excluded"}]
        # S5: 写入前廉价过滤层（slot 命中 / 长消息 / metadata 强制）—— 任一
        # 命中即送 mem0；全不命中则只落 L1/L2，被过滤的轮不会在 Milvus 沉淀
        # 不可召回的"暗数据"，顺带消灭 B-4（白名单外记忆写入 mem0 但永不可召回）。
        from thinkback.infra.config import settings as _settings

        if not should_extract_to_l3(
            messages=messages,
            metadata=request_metadata,
            allowed_slots=_settings.memory_p0_slots_list or None,
        ):
            run_log.bind(reason="pre_extract_gate_failed").info("memory l3 write skipped")
            return [{"event": "SKIP", "reason": "pre_extract_gate_failed"}]
        l3_events = self._add_l3_from_messages(
            messages,
            user_id=user_id,
            memory_scope_id=memory_scope_id,
            source_refs=source_refs,
            request_metadata=request_metadata,
        )
        run_log.bind(l3_event_count=len(l3_events)).info("memory l3 write completed")
        return l3_events

    def recall(self, request: RecallMemoryRequest) -> RecallMemoryResponse:
        """按意图召回 L1/L2/L3 记忆。

        request.query 只用于匹配和后端检索；日志记录 query_length 与召回参数，避免
        将用户查询原文写入日志。

        流程图::

            进入 RecallMemoryRequest
            (user_id, session_id, intent, query,
             l3_limit, token_budget, l3_score_threshold)
                │
                ▼
            解析 session_scope_id / l3_scope_id
            LOGGER_MEMORY.started("recall", …)
            INFO: "memory recall started"
                │
                ▼
            ┌── 意图拦截 ──────────────────────────────┐
            │  intent == RecallIntent.SENSITIVE        │
            │    └─► LOGGER_MEMORY.failed              │
            │        raise ValueError("fail-closed …") │
            └───────────────────────┬─────────────────┘
                                │
                                ▼
            items = []
            degradation_reasons = []
                │
                ▼
            ┌── L1 召回 (本地短期) ─────────────────────┐
            │  for entry in repository.get_l1(...):    │
            │    content = 拼接非受限消息              │
            │    items.append(MemoryItem(              │
            │        layer="L1", content, source))     │
            └───────────────────────┬─────────────────┘
                                │
                                ▼
            ┌── L2 召回 (本地摘要) ─────────────────────┐
            │  summary = _summary(user_id,             │
            │                     session_scope_id)    │
            │  ├─ ACTIVE + text → append(L2)           │
            │  ├─ STALE  + text → append(L2) +         │
            │  │                 degradation +=        │
            │  │                 "l2_stale"            │
            │  ├─ DIRTY → degradation +=               │
            │  │            "l2_dirty_skipped"         │
            │  └─ REBUILDING → degradation +=          │
            │                  "l2_rebuilding"         │
            └───────────────────────┬─────────────────┘
                                │
                                ▼
            must_query_l3 = (intent is RecallIntent.CHAT)
                │
                ├─ False → 跳过 L3
                │
                └─ True:
                       │
                       ▼
            ┌── L3 召回 (后端语义检索) ────────────────┐
            │  active = _active_memories(user_id, …)  │
            │  by_backend_id = {m.backend_memory_id: │
            │                    m for m in active}   │
            │  query_slot = _query_conflict_slot(q)   │
            │  if query_slot:                          │
            │      _backfill_matching_slot_memories(  │
            │          items, query, active)          │
            │                                          │
            │  try:                                     │
            │      backend.search(query, …)            │
            │  except RuntimeError:                     │
            │      degradation += "l3_backend_…        │
            │                   unreachable"           │
            │      backend_items = []                   │
            │                                          │
            │  for item in backend_items:               │
            │      indexed = by_backend_id.get(        │
            │          item.id)                        │
            │      ├─ None → 跳过（不在本地索引里）   │
            │      ├─ context 不允许 → 跳过           │
            │      ├─ query_slot 不匹配 → 跳过        │
            │      └─ 通过 → append(L3)                │
            └───────────────────────┬─────────────────┘
                                │
                                ▼
            ┌── 去重 + 裁剪 ───────────────────────────┐
            │  clipped = _dedupe_and_clip(             │
            │      items, token_budget)                │
            │    ├─ 按 layer 优先级排序                │
            │    ├─ LRU 去重                           │
            │    └─ token 截断                         │
            └───────────────────────┬─────────────────┘
                                │
                                ▼
            LOGGER_MEMORY.completed("recall", "ok", …)
            INFO: "memory recall completed"
            (item_count, degraded, queried_l3)
                │
                ▼
            返回 RecallMemoryResponse
            (status="ok", degraded, degradation_reasons,
             items=clipped_items)

        关键设计：
        - L1/L2 来自本地 repository（in-memory / SQLAlchemy），永远可用。
        - L3 走 mem0 语义检索，**失败时降级为只返回 L1/L2**，
          不抛 502。降级通过 ``degradation_reasons`` 暴露给上游。
        - L2 的 STALE/DIRTY/REBUILDING 状态会同时出现在 items 和
          degradation_reasons 中，让客户端知道"数据可能不准"。
        """
        session_scope_id = self._session_scope_id(request)
        l3_scope_id = self._long_term_scope_id(request)
        LOGGER_MEMORY.started(
            "recall",
            user_id=request.user_id,
            session_id=request.session_id,
            intent=request.intent.value,
            query_length=len(request.query),
            l3_limit=request.l3_limit,
        )
        recall_log = logger.bind(
            user_id=request.user_id,
            session_id=request.session_id,
            intent=request.intent.value,
            query_length=len(request.query),
            l3_limit=request.l3_limit,
            token_budget=request.token_budget,
        )
        recall_log.info("memory recall started")
        if request.intent is RecallIntent.SENSITIVE:
            LOGGER_MEMORY.failed("recall", "INTENT_ERROR", detail="fail_closed_intent")
            recall_log.bind(reason="fail_closed_intent").warning("memory recall rejected")
            raise ValueError(f"fail-closed recall intent: {request.intent.value}")

        items: list[MemoryItem] = []
        degradation_reasons: list[str] = []
        for entry in self.repository.get_l1(request.user_id, session_scope_id, request.session_id):
            content = " ".join(
                message["content"]
                for message in entry.messages
                if not self._text_is_restricted_or_unsafe(message["content"])
            )
            items.append(MemoryItem(layer="L1", content=content, source=entry.round_id))

        summary = self._summary(request.user_id, session_scope_id)
        if summary and summary.summary_state is SummaryState.ACTIVE and summary.summary_text:
            items.append(
                MemoryItem(layer="L2", content=summary.summary_text, source=summary.summary_id)
            )
        elif summary and summary.summary_state is SummaryState.STALE and summary.summary_text:
            degradation_reasons.append("l2_stale")
            items.append(
                MemoryItem(
                    layer="L2",
                    content=summary.summary_text,
                    source=summary.summary_id,
                    metadata={"summary_state": SummaryState.STALE.value},
                )
            )
        elif summary and summary.summary_state is SummaryState.DIRTY:
            degradation_reasons.append("l2_dirty_skipped")
        elif summary and summary.summary_state is SummaryState.REBUILDING:
            degradation_reasons.append("l2_rebuilding")

        must_query_l3 = request.intent is RecallIntent.CHAT
        if must_query_l3:
            active_memories = self._active_memories(request.user_id, l3_scope_id)
            active_memory_by_backend_id = {
                memory.backend_memory_id: memory for memory in active_memories
            }
            query_slot = self._query_conflict_slot(request.query)
            recalled_entries: list[Any] = []
            if query_slot:
                backfilled = self._backfill_matching_slot_memories(
                    items,
                    query=request.query,
                    active_memories=active_memories,
                )
                recalled_entries.extend(backfilled)
            if not self._should_skip_backend_search(query_slot, items):
                # N-7: mem0 网络/服务抖动时 backend.search 可能抛 RuntimeError
                # （由 Mem0LibraryMemoryBackend._call 统一包装）。V1 设计要求
                # 降级为只返回 L1/L2，不允许直接 502 让上游雪崩。这里只识别
                # RuntimeError，其他异常（如 KeyboardInterrupt）继续向上抛。
                try:
                    backend_items = self.backend.search(
                        request.query,
                        user_id=request.user_id,
                        memory_scope_id=l3_scope_id,
                        limit=request.l3_limit,
                        threshold=request.l3_score_threshold,
                    )
                except RuntimeError as exc:
                    recall_log.bind(
                        error_type=type(exc).__name__,
                        error_message=str(exc),
                    ).warning("memory recall degraded: l3 backend unreachable")
                    degradation_reasons.append("l3_backend_unreachable")
                    backend_items = []
                for item in backend_items:
                    memory_id = str(item.get("id", ""))
                    indexed_memory = active_memory_by_backend_id.get(memory_id)
                    if indexed_memory is None:
                        continue
                    # P2#6 双时态兜底：失效时刻已定的记忆不再进入召回
                    # （状态过滤之外的第二道闸，覆盖多副本竞争/遗留行）。
                    if getattr(indexed_memory, "invalid_at", None) is not None:
                        continue
                    if not self._memory_context_allowed(indexed_memory, request.query):
                        continue
                    if (
                        query_slot
                        and self._memory_conflict_slot(indexed_memory.memory_text) != query_slot
                    ):
                        continue
                    items.append(
                        MemoryItem(
                            layer="L3",
                            content=indexed_memory.memory_text,
                            source="mem0",
                            memory_id=indexed_memory.memory_id,
                            score=item.get("score"),
                            metadata=self._recall_l3_metadata(item.get("metadata")),
                            recall_count=getattr(indexed_memory, "recall_count", 0) or 0,
                            last_recalled_at=getattr(indexed_memory, "last_recalled_at", None),
                        )
                    )
                    recalled_entries.append(indexed_memory)
                # P2#7：被召回即强化（艾宾浩斯信号，debounce 限流写放大）。
                self._touch_recalled_entries(recalled_entries)

        clipped_items = self._dedupe_and_clip(items, request.token_budget)
        LOGGER_MEMORY.completed(
            "recall",
            "ok",
            item_count=len(clipped_items),
            degraded=bool(degradation_reasons),
            degradation_reason_count=len(degradation_reasons),
        )
        recall_log.bind(
            item_count=len(clipped_items),
            degraded=bool(degradation_reasons),
            degradation_reasons=degradation_reasons,
            queried_l3=must_query_l3,
        ).info("memory recall completed")
        return RecallMemoryResponse(
            status="ok",
            degraded=bool(degradation_reasons),
            degradation_reasons=degradation_reasons,
            items=clipped_items,
        )

    def delete(self, request: DeleteMemoryRequest) -> DeleteMemoryResponse:
        """删除单条、单会话或用户全部记忆。

        request.scope 决定删除范围；operation_id 用于幂等；memory_id 只在单条删除时
        必填。日志只记录是否提供 memory_id，不记录记忆内容。
        """
        session_scope_id = self._session_scope_id(request)
        l3_scope_id = self._long_term_scope_id(request)
        delete_log = logger.bind(
            user_id=request.user_id,
            session_id=request.session_id,
            scope=request.scope.value,
            operation_id=request.operation_id,
            has_memory_id=request.memory_id is not None,
        )
        delete_log.info("memory delete started")
        task_scope = {
            "user_id": request.user_id,
            "session_id": request.session_id,
            "session_scope_id": session_scope_id,
            "long_term_scope_id": l3_scope_id,
            "delete_scope": request.scope.value,
            "memory_id": request.memory_id,
        }
        self._validate_delete_required_identifiers(request)
        existing_task = self.repository.get_task(f"memory-delete:{request.operation_id}")
        if existing_task:
            self._assert_operation_scope_matches(
                existing_task,
                task_scope,
                operation_name="delete",
            )
            existing_response = self._existing_delete_response(
                existing_task=existing_task,
                delete_log=delete_log,
            )
            if existing_response is not None:
                return existing_response
        task = existing_task or TaskEntry(
            task_id=f"memory-delete:{request.operation_id}",
            request_id=request.request_id,
            op_type={
                DeleteScope.MEMORY: OperationType.DELETE_MEMORY,
                DeleteScope.SESSION: OperationType.DELETE_SESSION,
                DeleteScope.ALL: OperationType.DELETE_ALL,
            }[request.scope],
            scope=task_scope,
            status=TaskStatus.RUNNING,
            operation_id=request.operation_id,
        )
        if existing_task is None:
            task, task_claimed = self.repository.claim_task(task)
            self._assert_operation_scope_matches(
                task,
                task_scope,
                operation_name="delete",
            )
            if not task_claimed:
                existing_response = self._existing_delete_response(
                    existing_task=task,
                    delete_log=delete_log,
                )
                if existing_response is not None:
                    return existing_response
        was_retrying_failed_task = task.status is TaskStatus.FAILED
        task.request_id = request.request_id
        task.op_type = {
            DeleteScope.MEMORY: OperationType.DELETE_MEMORY,
            DeleteScope.SESSION: OperationType.DELETE_SESSION,
            DeleteScope.ALL: OperationType.DELETE_ALL,
        }[request.scope]
        task.scope = task_scope
        task.operation_id = request.operation_id
        task.status = TaskStatus.RUNNING
        task.last_error = None
        if was_retrying_failed_task:
            task.retry_count += 1
            self._check_retry_budget(task)
        self.repository.save_task(task)
        affected_memory_ids: list[str] = []
        pending_cleanup_tasks = 0
        deferred_backend_deletes: list[_BackendDeleteSync] = []

        try:
            with self._index_mutation_lock:
                if request.scope is DeleteScope.MEMORY:
                    (
                        affected_memory_ids,
                        pending_cleanup_tasks,
                        deferred_backend_deletes,
                    ) = self._delete_one_memory(
                        request,
                        retry_failed_task=was_retrying_failed_task,
                    )
                elif request.scope is DeleteScope.SESSION:
                    (
                        affected_memory_ids,
                        pending_cleanup_tasks,
                        deferred_backend_deletes,
                    ) = self._delete_session_memories(
                        request,
                        retry_failed_task=was_retrying_failed_task,
                    )
                else:
                    (
                        affected_memory_ids,
                        pending_cleanup_tasks,
                        deferred_backend_deletes,
                    ) = self._delete_all_memories(
                        request,
                        retry_failed_task=was_retrying_failed_task,
                    )
        except Exception as exc:
            with self._task_status_lock:
                # 重新读取后再写：删除体内已提交的清理回调会并发改写同一任务，
                # 用陈旧快照直接覆盖会丢失计数；FAILED 是操作级终态，必须保留。
                task = self.repository.get_task(task.task_id) or task
                task.status = TaskStatus.FAILED
                task.last_error = str(exc)
                self.repository.save_task(task)
            delete_log.bind(task_id=task.task_id, error_type=type(exc).__name__).warning(
                "memory delete failed"
            )
            raise

        # H-3: 在锁外执行 sync 模式下累积的 backend.delete，避免 mem0 网络 I/O
        # 串行化其他同 user L3 写。失败不吞：让异常向上抛，外层 except 块会
        # 把 task 标 FAILED + last_error 写回，保持与 H-3 修复前 sync 路径
        # "backend 失败 → 抛 RuntimeError → task FAILED" 的契约一致。
        try:
            for deferred in deferred_backend_deletes:
                deferred()
        except Exception as exc:
            with self._task_status_lock:
                captured_exc = exc

                def mark_failed(task_to_update: TaskEntry) -> None:
                    task_to_update.status = TaskStatus.FAILED
                    task_to_update.last_error = str(captured_exc)

                task = self.repository.get_task(task.task_id) or task
                if task is not None:
                    self._mutate_task_with_retry(
                        task.task_id,
                        mark_failed,
                        log_context={"stage": "delete_backend_cleanup_failed"},
                    )
                    task = self.repository.get_task(task.task_id) or task
            delete_log.bind(
                task_id=task.task_id,
                backend_memory_id=(
                    deferred_backend_deletes[0].memory_id if deferred_backend_deletes else None
                ),
                error_type=type(exc).__name__,
            ).warning("memory delete backend cleanup failed")
            raise

        with self._task_status_lock:
            # S3 (P1a 自动重放): 跨副本 CAS 冲突时由
            # ``_mutate_task_with_retry`` 重读快照重放。mutate 内部基于最新
            # 状态决定 status（保留另一副本已 FAILED 的终态，避免把已失败
            # 的任务改回 COMPLETED）。
            current_task = self.repository.get_task(task.task_id)
            if current_task is not None and (
                current_task.status is TaskStatus.FAILED
                or current_task.status is TaskStatus.COMPLETED
            ):
                registered_pending_cleanup_tasks = 0
            else:
                current_task = current_task or task
                registered_pending_cleanup_tasks = int(
                    current_task.result.get("pending_cleanup_tasks", 0)
                )

            def finalize_delete(task_to_update: TaskEntry) -> None:
                # 保留终态：另一副本已 FAILED/COMPLETED 时跳过 status 改写
                if (
                    task_to_update.status is TaskStatus.FAILED
                    or task_to_update.status is TaskStatus.COMPLETED
                ):
                    pending_cleanup_tasks = 0
                elif registered_pending_cleanup_tasks:
                    task_to_update.status = TaskStatus.RUNNING
                    pending_cleanup_tasks = registered_pending_cleanup_tasks
                else:
                    task_to_update.status = TaskStatus.COMPLETED
                    pending_cleanup_tasks = 0
                task_to_update.result = {
                    **task_to_update.result,
                    "affected_count": len(affected_memory_ids),
                    "affected_memory_ids": affected_memory_ids,
                    "summary_state": SummaryState.DIRTY.value,
                    "scope": request.scope.value,
                    "session_id": request.session_id,
                    "pending_cleanup_tasks": pending_cleanup_tasks,
                }

            task = self._mutate_task_with_retry(
                task.task_id,
                finalize_delete,
                log_context={"stage": "delete_final"},
            ) or task
        delete_log.bind(
            status=task.status.value,
            task_id=task.task_id,
            affected_count=len(affected_memory_ids),
        ).info("memory delete completed")
        return DeleteMemoryResponse(
            status=task.status.value,
            task_id=task.task_id,
            affected_memories=len(affected_memory_ids),
            summary_state=SummaryState.DIRTY,
        )

    @staticmethod
    def _existing_delete_response(
        *,
        existing_task: TaskEntry,
        delete_log: Any,
    ) -> DeleteMemoryResponse | None:
        # N-3: 任务已 dead_letter，短路返回 failed 状态，避免客户端无限重试
        if existing_task.status is TaskStatus.DEAD_LETTER:
            delete_log.bind(
                status="failed",
                task_id=existing_task.task_id,
                retry_count=existing_task.retry_count,
            ).warning("memory delete dead_letter short-circuit")
            return DeleteMemoryResponse(
                status="failed",
                task_id=existing_task.task_id,
                affected_memories=0,
                summary_state=SummaryState.DIRTY,
            )
        if existing_task.status in {TaskStatus.PENDING, TaskStatus.RUNNING}:
            delete_log.bind(
                status="running",
                task_id=existing_task.task_id,
                affected_count=int(existing_task.result.get("affected_count", 0)),
            ).info("memory delete already running")
            return DeleteMemoryResponse(
                status="running",
                task_id=existing_task.task_id,
                affected_memories=int(existing_task.result.get("affected_count", 0)),
                summary_state=SummaryState.DIRTY,
            )
        if existing_task.status is TaskStatus.COMPLETED:
            delete_log.bind(
                status="already_done",
                task_id=existing_task.task_id,
                affected_count=int(existing_task.result.get("affected_count", 0)),
            ).info("memory delete completed")
            return DeleteMemoryResponse(
                status="already_done",
                task_id=existing_task.task_id,
                affected_memories=int(existing_task.result.get("affected_count", 0)),
                summary_state=SummaryState.DIRTY,
            )
        return None

    def list_memory_items(
        self, *, user_id: str, include_deleted: bool = False
    ) -> ListMemoriesResponse:
        l3_scope_id = self.LONG_TERM_SCOPE_ID
        memories = (
            self.repository.list_memories(user_id, l3_scope_id)
            if include_deleted
            else self.repository.active_memories(user_id, l3_scope_id)
        )
        return ListMemoriesResponse(
            status="ok",
            items=[
                self._managed_memory_item(memory)
                for memory in memories
                if self._is_manageable_memory(memory)
            ],
        )

    def get_memory_item(self, *, user_id: str, memory_id: str) -> GetMemoryResponse | None:
        memory = self._find_manageable_memory(user_id=user_id, memory_id=memory_id)
        if memory is None:
            return None
        return GetMemoryResponse(status="ok", memory=self._managed_memory_item(memory))

    def update_memory(self, request: UpdateMemoryRequest) -> UpdateMemoryResponse:
        """编辑单条长期记忆，并同步更新后端和本地业务索引。

        V1 管理编辑是同步操作：不提供 CAS/版本号，不写人工治理审计，只保证同一
        operation_id 幂等、相关 L1/L2 fail-safe 置脏、召回和删除继续使用业务 memory_id。

        H-3: 锁仅保护 _update_memory_locked 中的本地索引计算，backend.update
        由 _run_backend_writes 在锁外执行，避免 mem0 网络 I/O 串行化其他
        同 user L3 写。
        """

        with self._index_mutation_lock:
            response, pending_backend_writes = self._update_memory_locked(request)
        # 锁外执行 backend.update 回调（失败仅记日志）
        if pending_backend_writes:
            self._run_backend_writes(pending_backend_writes)
        return response

    def _update_memory_locked(
        self, request: UpdateMemoryRequest
    ) -> tuple[UpdateMemoryResponse, list[Any]]:
        l3_scope_id = self._long_term_scope_id(request)
        content_fingerprint = sha256(request.content.encode("utf-8")).hexdigest()
        task_scope = {
            "user_id": request.user_id,
            "long_term_scope_id": l3_scope_id,
            "memory_id": request.memory_id,
            "content_fingerprint": content_fingerprint,
            "memory_type": request.memory_type.value if request.memory_type else None,
        }
        update_log = logger.bind(
            user_id=request.user_id,
            operation_id=request.operation_id,
            memory_id=request.memory_id,
            content_length=len(request.content),
            has_memory_type=request.memory_type is not None,
        )
        update_log.info("memory update started")
        existing_task = self.repository.get_task(f"memory-update:{request.operation_id}")
        if existing_task:
            self._assert_operation_scope_matches(
                existing_task,
                task_scope,
                operation_name="update",
            )
            existing_response = self._existing_update_response(
                request=request,
                existing_task=existing_task,
                content_fingerprint=content_fingerprint,
            )
            if existing_response is not None:
                return existing_response, []
        if self._text_is_restricted_or_unsafe(request.content):
            raise ValueError("memory update content is restricted_or_unsafe (fail-closed)")
        memory = self._find_manageable_memory(
            user_id=request.user_id,
            memory_id=request.memory_id,
            active_only=True,
        )
        if memory is None:
            raise ValueError("memory not found")
        self._validate_local_p0_memory_update(memory, request.content)

        task = existing_task or TaskEntry(
            task_id=f"memory-update:{request.operation_id}",
            request_id=request.request_id,
            op_type=OperationType.UPDATE_MEMORY,
            scope=task_scope,
            status=TaskStatus.RUNNING,
            operation_id=request.operation_id,
        )
        if existing_task is None:
            task, task_claimed = self.repository.claim_task(task)
            self._assert_operation_scope_matches(
                task,
                task_scope,
                operation_name="update",
            )
            if not task_claimed:
                existing_response = self._existing_update_response(
                    request=request,
                    existing_task=task,
                    content_fingerprint=content_fingerprint,
                )
                if existing_response is not None:
                    return existing_response, []  # H-3: 已完成/重复路径无 pending 写
        was_retrying_failed_task = task.status is TaskStatus.FAILED
        task.request_id = request.request_id
        task.op_type = OperationType.UPDATE_MEMORY
        task.scope = task_scope
        task.operation_id = request.operation_id
        task.status = TaskStatus.RUNNING
        task.last_error = None
        if was_retrying_failed_task:
            task.retry_count += 1
            self._check_retry_budget(task)
        self.repository.save_task(task)

        try:
            updated_memory, pending_backend_writes = self._apply_memory_update(
                memory,
                request=request,
                l3_scope_id=l3_scope_id,
            )
        except Exception as exc:
            task.status = TaskStatus.FAILED
            task.last_error = str(exc)
            self.repository.save_task(task)
            update_log.bind(task_id=task.task_id, error_type=type(exc).__name__).warning(
                "memory update failed"
            )
            raise

        task.status = TaskStatus.COMPLETED
        task.result = {
            "memory_id": updated_memory.memory_id,
            "content_fingerprint": content_fingerprint,
            "memory_type": updated_memory.memory_type,
            "summary_state": SummaryState.DIRTY.value,
        }
        self.repository.save_task(task)
        item = self._managed_memory_item(updated_memory)
        update_log.bind(task_id=task.task_id, status=task.status.value).info(
            "memory update completed"
        )
        return UpdateMemoryResponse(
            status="completed",
            task_id=task.task_id,
            memory=item,
            summary_state=SummaryState.DIRTY,
        ), pending_backend_writes

    def _existing_update_response(
        self,
        *,
        request: UpdateMemoryRequest,
        existing_task: TaskEntry,
        content_fingerprint: str,
    ) -> UpdateMemoryResponse | None:
        # N-3: 任务已 dead_letter，短路返回 failed 状态
        if existing_task.status is TaskStatus.DEAD_LETTER:
            existing_memory = self.get_memory_item(
                user_id=request.user_id, memory_id=request.memory_id
            )
            if existing_memory is None:
                raise ValueError("memory not found")
            return UpdateMemoryResponse(
                status="failed",
                task_id=existing_task.task_id,
                memory=existing_memory.memory,
                summary_state=SummaryState.DIRTY,
            )
        if existing_task.status in {TaskStatus.PENDING, TaskStatus.RUNNING}:
            existing_memory = self.get_memory_item(
                user_id=request.user_id, memory_id=request.memory_id
            )
            if existing_memory is None:
                raise ValueError("memory not found")
            return UpdateMemoryResponse(
                status="running",
                task_id=existing_task.task_id,
                memory=existing_memory.memory,
                summary_state=SummaryState.DIRTY,
            )
        if existing_task.status is TaskStatus.COMPLETED:
            return self._completed_update_response(
                request=request,
                existing_task=existing_task,
                content_fingerprint=content_fingerprint,
            )
        return None

    def _completed_update_response(
        self,
        *,
        request: UpdateMemoryRequest,
        existing_task: TaskEntry,
        content_fingerprint: str,
    ) -> UpdateMemoryResponse:
        memory = self._find_manageable_memory(
            user_id=request.user_id,
            memory_id=request.memory_id,
            active_only=True,
        )
        if memory is None:
            raise ValueError("operation_id conflict: completed update result is no longer current")
        current_fingerprint = sha256(str(memory.memory_text).encode("utf-8")).hexdigest()
        if current_fingerprint != content_fingerprint:
            raise ValueError("operation_id conflict: completed update result is no longer current")
        if "memory_type" in existing_task.result:
            expected_memory_type = existing_task.result["memory_type"]
        else:
            expected_memory_type = existing_task.scope.get("memory_type")
        if memory.memory_type != expected_memory_type:
            raise ValueError("operation_id conflict: completed update result is no longer current")
        return UpdateMemoryResponse(
            status="already_done",
            task_id=existing_task.task_id,
            memory=self._managed_memory_item(memory),
            summary_state=SummaryState.DIRTY,
        )

    def _validate_local_p0_memory_update(self, memory: Any, content: str) -> None:
        backend_memory_id = str(getattr(memory, "backend_memory_id", ""))
        if not backend_memory_id.startswith("local-p0:"):
            return
        existing_slot = self._memory_conflict_slot(str(getattr(memory, "memory_text", "")))
        updated_slot = self._memory_conflict_slot(content)
        if existing_slot is None or updated_slot != existing_slot:
            raise ValueError("local p0 memory updates must stay in same recall slot")

    @staticmethod
    def _validate_delete_required_identifiers(request: DeleteMemoryRequest) -> None:
        if request.scope is DeleteScope.MEMORY and not request.memory_id:
            raise ValueError("memory_id is required for memory deletion")
        if request.scope is DeleteScope.SESSION and not request.session_id:
            raise ValueError("session_id is required for session deletion")

    def rebuild(self, request: RebuildMemoryRequest) -> RebuildMemoryResponse:
        """从回合流水重建 L2 摘要和/或 L3 索引。

        request.history_version 用于外部历史源的一致性校验；rebuild_l2/rebuild_l3 控制
        重建层级，日志只记录开关和任务结果。
        """
        session_scope_id = self._session_scope_id(request)
        history_scope_id = self._history_scope_id(request)
        l3_scope_id = self._long_term_scope_id(request)
        rebuild_log = logger.bind(
            user_id=request.user_id,
            session_id=request.session_id,
            operation_id=request.operation_id,
            rebuild_l2=request.rebuild_l2,
            rebuild_l3=request.rebuild_l3,
            has_history_version=request.history_version is not None,
        )
        rebuild_log.info("memory rebuild started")
        task_scope = {
            "user_id": request.user_id,
            "session_id": request.session_id,
            "session_scope_id": session_scope_id,
            "long_term_scope_id": l3_scope_id,
            "rebuild_l2": request.rebuild_l2,
            "rebuild_l3": request.rebuild_l3,
            "history_version": request.history_version,
        }
        existing_task = self.repository.get_task(f"memory-rebuild:{request.operation_id}")
        if existing_task:
            self._assert_operation_scope_matches(
                existing_task,
                task_scope,
                operation_name="rebuild",
            )
            existing_response = self._existing_rebuild_response(
                request=request,
                existing_task=existing_task,
                rebuild_log=rebuild_log,
            )
            if existing_response is not None:
                return existing_response
        if request.rebuild_l3:
            rebuild_op_type = OperationType.REBUILD_L3
        else:
            rebuild_op_type = OperationType.REBUILD_L2
        task = existing_task or TaskEntry(
            task_id=f"memory-rebuild:{request.operation_id}",
            request_id=request.request_id,
            op_type=rebuild_op_type,
            scope=task_scope,
            status=TaskStatus.RUNNING,
            operation_id=request.operation_id,
            history_version=request.history_version,
        )
        if existing_task is None:
            task, task_claimed = self.repository.claim_task(task)
            self._assert_operation_scope_matches(
                task,
                task_scope,
                operation_name="rebuild",
            )
            if not task_claimed:
                existing_response = self._existing_rebuild_response(
                    request=request,
                    existing_task=task,
                    rebuild_log=rebuild_log,
                )
                if existing_response is not None:
                    return existing_response
        was_retrying_failed_task = task.status is TaskStatus.FAILED
        task.request_id = request.request_id
        task.op_type = rebuild_op_type
        task.scope = task_scope
        task.operation_id = request.operation_id
        task.history_version = request.history_version
        task.status = TaskStatus.RUNNING
        task.last_error = None
        if was_retrying_failed_task:
            task.retry_count += 1
            self._check_retry_budget(task)
        self.repository.save_task(task)
        l3_extract_task_ids: list[str] = []
        pending_cleanup_tasks = 0
        try:
            if request.history_version and self.history_source:
                current_version = self.history_source.current_version(
                    request.user_id,
                    history_scope_id,
                )
                if current_version != request.history_version:
                    raise ValueError(
                        f"history_version mismatch: expected {request.history_version}, got {current_version}"
                    )
            rounds = self._rebuild_rounds(request)
            delete_cutoffs = _RebuildDeleteCutoffs(self, request.user_id, l3_scope_id)
            if request.rebuild_l2:
                deleted_refs = self.repository.excluded_source_refs(
                    request.user_id,
                    l3_scope_id,
                )
                for summary_scope_id in self._rebuild_summary_scope_ids(request):
                    l2_rounds = [
                        entry
                        for entry in self._effective_history_rounds(
                            request.user_id,
                            summary_scope_id,
                            summary_scope_id,
                        )
                        if (entry.session_id, entry.round_id) not in deleted_refs
                        and delete_cutoffs.allows(entry)
                    ]
                    self.repository.upsert_summary_from_rounds(
                        request.user_id,
                        summary_scope_id,
                        l2_rounds,
                    )
            if request.rebuild_l3:
                deleted_refs = self.repository.excluded_source_refs(
                    request.user_id,
                    l3_scope_id,
                )
                stale_memories = [
                    memory
                    for memory in self._rebuild_cleanup_candidates(
                        request,
                        retry_failed_task=was_retrying_failed_task,
                    )
                    if all(
                        (ref.get("session_id"), ref.get("round_id")) in deleted_refs
                        for ref in memory.source_refs
                    )
                ]
                if self._delete_backend_memories(
                    [
                        memory.backend_memory_id
                        for memory in stale_memories
                        if self._is_backend_managed_memory_id(memory.backend_memory_id)
                    ],
                    task_id=task.task_id,
                ):
                    pending_cleanup_tasks += 1
                for memory in stale_memories:
                    self._mark_memory_superseded_for_rebuild_cleanup(
                        memory,
                        operation_id=request.operation_id,
                    )
                for entry in self._uncovered_rebuild_rounds(request, rounds, deleted_refs):
                    messages = [
                        {"role": message["role"], "content": message["content"]}
                        for message in entry.messages
                    ]
                    if self._messages_are_restricted_or_unsafe(messages):
                        continue
                    source_refs = [{"session_id": entry.session_id, "round_id": entry.round_id}]
                    if self.l3_write_mode == "async":
                        extract_task, should_submit = self._ensure_l3_extract_task(
                            request_id=self._bounded_internal_request_id(
                                request.request_id,
                                entry.round_id,
                            ),
                            user_id=request.user_id,
                            memory_scope_id=l3_scope_id,
                            round_id=entry.round_id,
                        )
                        if not should_submit:
                            continue
                        l3_extract_task_ids.append(extract_task.task_id)
                        self._submit_l3_write(
                            extract_task,
                            messages=messages,
                            user_id=request.user_id,
                            memory_scope_id=l3_scope_id,
                            source_refs=source_refs,
                            request_metadata={},
                        )
                        continue
                    self._run_l3_write(
                        messages=messages,
                        user_id=request.user_id,
                        memory_scope_id=l3_scope_id,
                        source_refs=source_refs,
                        request_metadata={},
                    )
        except Exception as exc:
            with self._task_status_lock:
                # 重新读取后再写：重建体内已提交的清理回调会并发改写同一任务，
                # 用陈旧快照直接覆盖会丢失计数；FAILED 是操作级终态，必须保留。
                task = self.repository.get_task(task.task_id) or task
                task.status = TaskStatus.FAILED
                task.last_error = str(exc)
                self.repository.save_task(task)
            rebuild_log.bind(task_id=task.task_id, error_type=type(exc).__name__).warning(
                "memory rebuild failed"
            )
            raise
        with self._task_status_lock:
            current_task = self.repository.get_task(task.task_id)
            if current_task is not None and (
                current_task.status is TaskStatus.FAILED
                or current_task.status is TaskStatus.COMPLETED
            ):
                task = current_task
                pending_cleanup_tasks = 0
            else:
                task = current_task or task
                registered_pending_cleanup_tasks = int(task.result.get("pending_cleanup_tasks", 0))
                if registered_pending_cleanup_tasks:
                    pending_cleanup_tasks = registered_pending_cleanup_tasks
                    task.status = TaskStatus.RUNNING
                else:
                    pending_cleanup_tasks = 0
                    task.status = TaskStatus.COMPLETED
            task.result = {
                **task.result,
                "rebuilt_l2": request.rebuild_l2,
                "rebuilt_l3": request.rebuild_l3,
                "session_id": request.session_id,
                "history_version": request.history_version,
                "l3_replay_status": "deferred" if l3_extract_task_ids else "completed",
                "l3_extract_task_ids": l3_extract_task_ids,
                "pending_cleanup_tasks": pending_cleanup_tasks,
            }
            self.repository.save_task(task)
            task = self.repository.get_task(task.task_id) or task
        rebuild_log.bind(
            status=task.status.value,
            task_id=task.task_id,
            rebuilt_l2=request.rebuild_l2,
            rebuilt_l3=request.rebuild_l3,
        ).info("memory rebuild completed")
        return RebuildMemoryResponse(
            status=task.status.value,
            task_id=task.task_id,
            rebuilt_l2=request.rebuild_l2,
            rebuilt_l3=request.rebuild_l3,
        )

    @staticmethod
    def _existing_rebuild_response(
        *,
        request: RebuildMemoryRequest,
        existing_task: TaskEntry,
        rebuild_log: Any,
    ) -> RebuildMemoryResponse | None:
        # N-3: 任务已 dead_letter，短路返回 failed 状态
        if existing_task.status is TaskStatus.DEAD_LETTER:
            rebuild_log.bind(
                status="failed",
                task_id=existing_task.task_id,
                retry_count=existing_task.retry_count,
            ).warning("memory rebuild dead_letter short-circuit")
            return RebuildMemoryResponse(
                status="failed",
                task_id=existing_task.task_id,
                rebuilt_l2=False,
                rebuilt_l3=False,
            )
        if existing_task.status in {TaskStatus.PENDING, TaskStatus.RUNNING}:
            rebuild_log.bind(
                status="running",
                task_id=existing_task.task_id,
                rebuilt_l2=request.rebuild_l2,
                rebuilt_l3=request.rebuild_l3,
            ).info("memory rebuild already running")
            return RebuildMemoryResponse(
                status="running",
                task_id=existing_task.task_id,
                rebuilt_l2=request.rebuild_l2,
                rebuilt_l3=request.rebuild_l3,
            )
        if existing_task.status is TaskStatus.COMPLETED:
            rebuild_log.bind(
                status="already_done",
                task_id=existing_task.task_id,
                rebuilt_l2=request.rebuild_l2,
                rebuilt_l3=request.rebuild_l3,
            ).info("memory rebuild completed")
            return RebuildMemoryResponse(
                status="already_done",
                task_id=existing_task.task_id,
                rebuilt_l2=request.rebuild_l2,
                rebuilt_l3=request.rebuild_l3,
            )
        return None

    def get_task(self, task_id: str) -> TaskResponse | None:
        task = self.repository.get_task(task_id)
        if not task:
            return None
        # 读路径自愈：关机竞态 / SIGKILL 遗留的幽灵 RUNNING 在被读取时惰性
        # 回收（原子条件更新，健康任务 no-op），不依赖下一次进程启动。
        if task.status is TaskStatus.RUNNING and self._reclaim_stale_running_task(task_id):
            task = self.repository.get_task(task_id)
            if not task:
                return None
        return TaskResponse(
            task_id=task.task_id,
            request_id=task.request_id,
            op_type=task.op_type,
            status=task.status,
            scope=task.scope,
            last_error=task.last_error,
            result=task.result,
        )

    def list_tasks(
        self,
        *,
        statuses: list[str] | None = None,
        older_than_seconds: float | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[TaskResponse]:
        """管理面任务列表（只读聚合，策略在仓储层）。"""

        return [
            TaskResponse(
                task_id=task.task_id,
                request_id=task.request_id,
                op_type=task.op_type,
                status=task.status,
                scope=task.scope,
                last_error=task.last_error,
                result=task.result,
                retry_count=task.retry_count,
            )
            for task in self.repository.list_tasks(
                statuses=statuses,
                older_than_seconds=older_than_seconds,
                limit=limit,
                offset=offset,
            )
        ]

    def overview_stats(self) -> dict[str, Any]:
        """管理面总览聚合：记忆/任务状态计数 + 数据分类 + 来源类型（只读）。"""

        return {
            "memories": self.repository.count_memories_by_status(),
            "tasks": self.repository.count_tasks_by_status(),
            "by_classification": self.repository.count_memories_by_classification(),
            "by_source_type": self.repository.count_memories_by_source_type(),
        }

    def admin_list_memories(
        self,
        *,
        user_id: str | None = None,
        memory_scope_id: str | None = None,
        statuses: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """管理面记忆检索（只读）：条目 + 冲突槽位派生 + 分页总数。"""

        entries, total = self.repository.admin_list_memories(
            user_id=user_id,
            memory_scope_id=memory_scope_id,
            statuses=statuses,
            limit=limit,
            offset=offset,
        )
        items = [
            AdminMemoryItem(
                memory_id=entry.memory_id,
                backend_memory_id=entry.backend_memory_id,
                user_id=entry.user_id,
                memory_scope_id=entry.memory_scope_id,
                memory_text=entry.memory_text,
                memory_status=entry.memory_status,
                memory_type=entry.memory_type,
                data_classification=entry.data_classification,
                source_refs=entry.source_refs,
                valid_at=entry.valid_at,
                invalid_at=entry.invalid_at,
                last_recalled_at=entry.last_recalled_at,
                recall_count=entry.recall_count,
                conflict_slot=memory_conflict_slot(entry.memory_text),
            )
            for entry in entries
        ]
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    def memory_source(self, memory_id: str) -> dict[str, Any] | None:
        """治理台来源反链：记忆条目 + 其 source_refs 指向的 journal 原文回合。"""

        memory = self.repository.get_memory_index(memory_id)
        if memory is None:
            return None
        rounds = self.repository.get_rounds_by_refs(memory.source_refs)
        return {
            "memory": AdminMemoryItem(
                memory_id=memory.memory_id,
                backend_memory_id=memory.backend_memory_id,
                user_id=memory.user_id,
                memory_scope_id=memory.memory_scope_id,
                memory_text=memory.memory_text,
                memory_status=memory.memory_status,
                memory_type=memory.memory_type,
                data_classification=memory.data_classification,
                source_refs=memory.source_refs,
                valid_at=memory.valid_at,
                invalid_at=memory.invalid_at,
                last_recalled_at=memory.last_recalled_at,
                recall_count=memory.recall_count,
                conflict_slot=memory_conflict_slot(memory.memory_text),
            ),
            "rounds": [
                {
                    "round_id": entry.round_id,
                    "session_id": entry.session_id,
                    "source_timestamp": entry.source_timestamp,
                    "messages": entry.messages,
                }
                for entry in rounds
            ],
        }

    def _reclaim_stale_running_task(self, task_id: str) -> bool:
        """读路径惰性回收；失败（DB 抖动等）不阻塞读，按原状态返回。"""

        from thinkback.infra.config import settings

        try:
            return self.repository.reclaim_stale_running_task(
                task_id, max_age_seconds=settings.task_orphan_running_seconds
            )
        except Exception as exc:  # noqa: BLE001
            logger.opt(exception=exc).warning("memory stale running task read-path reclaim failed")
            return False

    def reclaim_orphan_running_tasks(self) -> list[str]:
        """启动恢复：把无人推进的超龄 running 任务回收为 failed。

        进程被硬杀（SIGKILL / OOM / 节点驱逐）时 in-flight 任务永远停在
        running，``GetTask`` 客户端会无限轮询。lifespan 启动阶段调用本
        方法，用 ``task_orphan_running_seconds`` 阈值做集合级原子回收，
        多副本安全（条件更新幂等）。回收失败只记 warning，不阻塞启动。
        """

        from thinkback.infra.config import settings

        try:
            return self.repository.reclaim_stale_running_tasks(
                max_age_seconds=settings.task_orphan_running_seconds
            )
        except Exception as exc:  # noqa: BLE001
            logger.opt(exception=exc).error("memory orphan running task reclaim failed")
            return []

    @staticmethod
    def _assert_operation_scope_matches(
        existing_task: TaskEntry,
        expected_scope: dict[str, Any],
        *,
        operation_name: str,
    ) -> None:
        for key, expected_value in expected_scope.items():
            if existing_task.scope.get(key) != expected_value:
                raise ValueError(
                    f"operation_id conflict: existing {operation_name} task scope does not match"
                )

    def _find_manageable_memory(
        self,
        *,
        user_id: str,
        memory_id: str,
        active_only: bool = False,
    ) -> Any | None:
        l3_scope_id = self.LONG_TERM_SCOPE_ID
        memories = (
            self.repository.active_memories(user_id, l3_scope_id)
            if active_only
            else self.repository.list_memories(user_id, l3_scope_id)
        )
        return next(
            (
                memory
                for memory in memories
                if memory.memory_id == memory_id and self._is_manageable_memory(memory)
            ),
            None,
        )

    @staticmethod
    def _is_manageable_memory(memory: Any) -> bool:
        backend_memory_id = str(getattr(memory, "backend_memory_id", ""))
        if backend_memory_id.startswith(("deleted-source:", "delete-all:", "delete-session:")):
            return False
        metadata = getattr(memory, "metadata", {}) or {}
        if not isinstance(metadata, dict):
            return True
        return not any(
            bool(metadata.get(key))
            for key in (
                "source_ref_tombstone",
                "delete_all_tombstone",
                "session_delete_tombstone",
            )
        )

    def _managed_memory_item(self, memory: Any) -> ManagedMemoryItem:
        memory_type = self._enum_or_none(MemoryType, getattr(memory, "memory_type", None))
        return ManagedMemoryItem(
            memory_id=str(memory.memory_id),
            content=str(memory.memory_text),
            status=MemoryStatus(memory.memory_status),
            source_type=SourceType(
                self._enum_value(
                    SourceType,
                    getattr(memory, "source_type", None),
                    SourceType.CHAT_ROUND.value,
                )
            ),
            data_classification=DataClassification(
                self._enum_value(
                    DataClassification,
                    getattr(memory, "data_classification", None),
                    DataClassification.NORMAL.value,
                )
            ),
            memory_type=MemoryType(memory_type) if memory_type else None,
            backend_categories=list(getattr(memory, "backend_categories", []) or []),
        )

    def _apply_memory_update(
        self,
        memory: Any,
        *,
        request: UpdateMemoryRequest,
        l3_scope_id: str,
    ) -> tuple[Any, list[Any]]:
        # H-3: 锁内只做本地索引计算 + 收集 backend.update 回调；返回的
        # pending_backend_writes 由调用方在 _index_mutation_lock 锁外执行。
        # update_memory() 在外层持有 _index_mutation_lock 包裹整个
        # _update_memory_locked 流程，这里不再二次 acquire——避免 RLock
        # 嵌套导致 outer lock 在 run_backend_writes 期间仍处于 held 状态。
        current = self._find_manageable_memory(
            user_id=request.user_id,
            memory_id=memory.memory_id,
            active_only=True,
        )
        if current is None:
            raise ValueError("memory not found")
        affected_source_refs = list(current.source_refs)
        affected_source_refs.extend(
            source_ref
            for conflicting in self._conflicting_active_memories(
                user_id=request.user_id,
                memory_scope_id=l3_scope_id,
                backend_memory_id=current.backend_memory_id,
                memory_text=request.content,
            )
            for source_ref in conflicting.source_refs
        )
        previous_backend_memory_id = str(current.backend_memory_id)
        previous_source_refs = list(current.source_refs)
        previous_memory_text = str(current.memory_text)
        previous_source_type = str(current.source_type)
        previous_data_classification = str(current.data_classification)
        previous_memory_type = current.memory_type
        previous_backend_categories = list(current.backend_categories)
        previous_metadata = dict(getattr(current, "metadata", {}) or {})
        previous_valid_at = getattr(current, "valid_at", None)
        metadata = dict(getattr(current, "metadata", {}) or {})
        metadata.pop("source_text", None)
        metadata.update(
            {
                "manual_update": True,
                "manual_update_operation_id": request.operation_id,
                "previous_source_type": current.source_type,
            }
        )
        backend_updated = False
        index_updated = False
        superseded_memory_ids: list[str] = []
        updated = None
        pending_backend_writes: list[Any] = []
        try:
            if self._is_backend_managed_memory_id(current.backend_memory_id):
                pending_backend_writes.append(
                    _BackendUpdate(
                        memory_id=current.backend_memory_id,
                        data=request.content,
                        backend=self.backend,
                    )
                )
                backend_updated = True
            # Bug #3 修复：manual update 必须刷新 valid_at。
            # _backfill_matching_slot_memories 用 _memory_valid_sort_key 选
            # newest；不刷 valid_at 会让用户编辑被跨 session 后续自然提到的
            # 旧事实覆盖。
            update_valid_at = datetime.now(UTC)
            updated = self.repository.update_memory_index(
                current.memory_id,
                backend_memory_id=current.backend_memory_id,
                source_refs=current.source_refs,
                memory_text=request.content,
                source_type=SourceType.MANUAL_FIX.value,
                data_classification=current.data_classification,
                memory_type=request.memory_type.value
                if request.memory_type
                else current.memory_type,
                backend_categories=current.backend_categories,
                metadata=metadata,
                valid_at=update_valid_at,
            )
            if updated is None:
                raise RuntimeError("memory index update failed")
            index_updated = True
            superseded_memory_ids, supersede_pending = self._supersede_conflicting_memories(
                user_id=request.user_id,
                memory_scope_id=l3_scope_id,
                backend_memory_id=current.backend_memory_id,
                memory_text=request.content,
                l3_metadata={"source_refs": current.source_refs},
            )
            pending_backend_writes.extend(supersede_pending)
        except Exception:
            # 失败回滚：先在锁内恢复本地索引，再收集锁外回滚 backend.update。
            rollback_writes: list[Any] = []
            if index_updated:
                with suppress(Exception):
                    self.repository.update_memory_index(
                        current.memory_id,
                        backend_memory_id=previous_backend_memory_id,
                        source_refs=previous_source_refs,
                        memory_text=previous_memory_text,
                        source_type=previous_source_type,
                        data_classification=previous_data_classification,
                        memory_type=previous_memory_type,
                        backend_categories=previous_backend_categories,
                        metadata=previous_metadata,
                        valid_at=previous_valid_at,
                    )
            if backend_updated:
                rollback_writes.append(
                    _BackendUpdate(
                        memory_id=previous_backend_memory_id,
                        data=previous_memory_text,
                        backend=self.backend,
                    )
                )
            for superseded_id in superseded_memory_ids:
                with suppress(Exception):
                    self.repository.mark_memory_active(superseded_id)
            # 即便失败也要让上层感知 rollback 失败（虽然本地索引已恢复）
            if rollback_writes:
                self._run_backend_writes(rollback_writes)
            raise
        self._mark_source_refs_dirty(request.user_id, affected_source_refs)
        return updated, pending_backend_writes

    def _conflicting_active_memories(
        self,
        *,
        user_id: str,
        memory_scope_id: str,
        backend_memory_id: str,
        memory_text: str,
    ) -> list[Any]:
        slot = self._memory_conflict_slot(memory_text)
        if slot is None:
            return []
        return [
            memory
            for memory in self.repository.active_memories(user_id, memory_scope_id)
            if memory.backend_memory_id != backend_memory_id
            and self._memory_conflict_slot(memory.memory_text) == slot
        ]

    def _mark_source_refs_dirty(self, user_id: str, source_refs: list[dict[str, str]]) -> None:
        session_scope_ids = {
            str(source_ref["session_id"])
            for source_ref in source_refs
            if source_ref.get("session_id")
        }
        for session_scope_id in session_scope_ids:
            self.repository.clear_l1(user_id, session_scope_id, session_scope_id)
            self.repository.mark_summary(user_id, session_scope_id, SummaryState.DIRTY)

    @staticmethod
    def _enum_or_none(enum_type: type[StrEnum], value: object) -> str | None:
        if isinstance(value, StrEnum):
            return str(value.value)
        if isinstance(value, str):
            valid_values = {str(member.value) for member in enum_type}
            if value in valid_values:
                return value
        return None

    def _ensure_l3_extract_task(
        self,
        *,
        request_id: str,
        user_id: str,
        memory_scope_id: str = LONG_TERM_SCOPE_ID,
        round_id: str,
    ) -> tuple[TaskEntry, bool]:
        task_id = f"memory-extract:{round_id}"
        existing = self.repository.get_task(task_id)
        was_retrying_failed_task = existing is not None and existing.status is TaskStatus.FAILED
        if existing is not None and existing.status in {
            TaskStatus.PENDING,
            TaskStatus.RUNNING,
            TaskStatus.COMPLETED,
        }:
            return existing, False
        task = existing or TaskEntry(
            task_id=task_id,
            request_id=self._bounded_internal_request_id(request_id),
            op_type=OperationType.WRITE_ROUND,
            scope={"user_id": user_id, "memory_scope_id": memory_scope_id},
            status=TaskStatus.RUNNING,
        )
        task.request_id = self._bounded_internal_request_id(request_id)
        task.status = TaskStatus.RUNNING
        task.last_error = None
        if was_retrying_failed_task:
            task.retry_count += 1
            self._check_retry_budget(task)
        self.repository.save_task(task)
        return task, True

    def _delete_one_memory(
        self,
        request: DeleteMemoryRequest,
        *,
        retry_failed_task: bool = False,
    ) -> tuple[list[str], int, list[Any]]:
        l3_scope_id = self._long_term_scope_id(request)
        if not request.memory_id:
            raise ValueError("memory_id is required for memory deletion")
        # N-4: 客户端 delete(memory_id=...) 必须能识别 SUPERSEDED 状态。
        # rebuild 标 SUPERSEDED 后客户端可能再次发 delete 清理残留——这时
        # memory 不在 active_memories 里但仍在 list_memories 里。retry
        # 路径已经用 list_memories；非 retry 路径之前用 active_memories
        # 会漏掉 SUPERSEDED，导致 delete 静默返回 affected=0 且 backend
        # 残留未清。统一用 list_memories 然后过滤 DELETED（已删的不再处理）。
        if retry_failed_task:
            memory_candidates = self.repository.list_memories(request.user_id, l3_scope_id)
        else:
            memory_candidates = [
                memory
                for memory in self.repository.list_memories(request.user_id, l3_scope_id)
                if memory.memory_status is not MemoryStatus.DELETED
            ]
        memory = next(
            (memory for memory in memory_candidates if memory.memory_id == request.memory_id), None
        )
        if not memory:
            return [], 0, []

        task_id = f"memory-delete:{request.operation_id}"
        pending_cleanup_tasks = 0
        # H-3: 同步模式下 backend.delete 是 mem0 网络 I/O；为了最小化持锁期，
        # 把 sync backend.delete 推迟到锁外执行。async 模式下 _delete_backend_memory
        # 内部已提交到 _l3_executor，锁内只做 bookkeeping，立即返回 True。
        deferred_backend_deletes: list[Any] = []
        if self._is_backend_managed_memory_id(memory.backend_memory_id):
            deferred = self._schedule_backend_delete(memory.backend_memory_id, task_id=task_id)
            if isinstance(deferred, _BackendDeleteSync):
                deferred_backend_deletes.append(deferred)
            elif deferred is True:
                pending_cleanup_tasks += 1
        self._mark_memory_deleted_for_operation(
            memory,
            operation_id=request.operation_id,
            delete_scope=request.scope.value,
        )
        self._mark_superseded_slot_sources_deleted(
            user_id=request.user_id,
            memory_scope_id=l3_scope_id,
            slot=self._memory_conflict_slot(memory.memory_text),
            deleted_memory_id=memory.memory_id,
        )
        affected_session_scope_ids = {
            str(source_ref["session_id"])
            for source_ref in memory.source_refs
            if source_ref.get("session_id")
        } or {self._session_scope_id(request)}
        for source_ref in memory.source_refs:
            source_session_id = source_ref.get("session_id")
            self.repository.clear_l1(
                request.user_id,
                str(source_session_id) if source_session_id else self._session_scope_id(request),
                source_session_id,
            )
        for session_scope_id in affected_session_scope_ids:
            self.repository.mark_summary(request.user_id, session_scope_id, SummaryState.DIRTY)
        return [memory.memory_id], pending_cleanup_tasks, deferred_backend_deletes

    def _delete_session_memories(
        self,
        request: DeleteMemoryRequest,
        *,
        retry_failed_task: bool = False,
    ) -> tuple[list[str], int, list[Any]]:
        session_scope_id = self._session_scope_id(request)
        l3_scope_id = self._long_term_scope_id(request)
        if not request.session_id:
            raise ValueError("session_id is required for session deletion")
        affected: list[str] = []
        pending_cleanup_tasks = 0
        deferred_backend_deletes: list[Any] = []
        session_cutoff = self._delete_tombstone_cutoff(
            user_id=request.user_id,
            memory_scope_id=l3_scope_id,
            tombstone_key="session_delete_tombstone",
            operation_id=request.operation_id,
        )
        memory_candidates = (
            [
                memory
                for memory in self.repository.list_memories(request.user_id, l3_scope_id)
                if memory.memory_status is MemoryStatus.DELETED
                and not self._is_local_index_memory_id(memory.backend_memory_id)
                and self._memory_deleted_by_operation(memory, request.operation_id)
            ]
            if retry_failed_task and session_cutoff is not None
            else self.repository.active_memories(request.user_id, l3_scope_id)
        )
        for memory in memory_candidates:
            if any(ref.get("session_id") == request.session_id for ref in memory.source_refs):
                remaining_refs = [
                    ref for ref in memory.source_refs if ref.get("session_id") != request.session_id
                ]
                if remaining_refs:
                    removed_refs = [
                        ref
                        for ref in memory.source_refs
                        if ref.get("session_id") == request.session_id
                    ]
                    self.repository.update_memory_source_refs(memory.memory_id, remaining_refs)
                    self._add_deleted_source_ref_tombstone(
                        user_id=request.user_id,
                        memory_scope_id=l3_scope_id,
                        source_refs=removed_refs,
                        memory_text=memory.memory_text,
                    )
                else:
                    if self._is_backend_managed_memory_id(memory.backend_memory_id):  # noqa: SIM102
                        deferred = self._schedule_backend_delete(
                            memory.backend_memory_id,
                            task_id=f"memory-delete:{request.operation_id}",
                        )
                        if isinstance(deferred, _BackendDeleteSync):
                            deferred_backend_deletes.append(deferred)
                        elif deferred is True:
                            pending_cleanup_tasks += 1
                    self._mark_memory_deleted_for_operation(
                        memory,
                        operation_id=request.operation_id,
                        delete_scope=request.scope.value,
                    )
                affected.append(memory.memory_id)
        self.repository.clear_l1(request.user_id, session_scope_id, request.session_id)
        self.repository.mark_rounds_deleted(
            request.user_id,
            session_scope_id,
            request.session_id,
        )
        delete_cutoff = self._add_session_delete_tombstone(
            user_id=request.user_id,
            memory_scope_id=l3_scope_id,
            session_id=request.session_id,
            operation_id=request.operation_id,
        )
        self._add_deleted_source_ref_tombstones_for_history(
            user_id=request.user_id,
            memory_scope_id=l3_scope_id,
            session_id=request.session_id,
            deleted_before=delete_cutoff,
        )
        self.repository.mark_summary(request.user_id, session_scope_id, SummaryState.DIRTY)
        return affected, pending_cleanup_tasks, deferred_backend_deletes

    def _add_deleted_source_ref_tombstone(
        self,
        *,
        user_id: str,
        memory_scope_id: str,
        source_refs: list[dict[str, str]],
        memory_text: str,
    ) -> None:
        if not source_refs:
            return
        raw = "|".join(
            (
                user_id,
                memory_scope_id,
                *(
                    f"{source_ref.get('session_id', '')}:{source_ref.get('round_id', '')}"
                    for source_ref in source_refs
                ),
            )
        )
        digest = sha256(raw.encode("utf-8")).hexdigest()[:32]
        backend_memory_id = f"deleted-source:{digest}"
        if any(
            memory.backend_memory_id == backend_memory_id
            for memory in self.repository.list_memories(user_id, memory_scope_id)
        ):
            return
        tombstone = self.repository.add_memory_index(
            backend_memory_id=backend_memory_id,
            user_id=user_id,
            memory_scope_id=memory_scope_id,
            source_refs=source_refs,
            memory_text=memory_text,
            metadata={"source_ref_tombstone": True},
        )
        self.repository.mark_memory_deleted(tombstone.memory_id)

    def _delete_tombstone_cutoff(
        self,
        *,
        user_id: str,
        memory_scope_id: str,
        tombstone_key: str,
        operation_id: str,
    ) -> datetime | None:
        cutoffs: list[datetime] = []
        for memory in self.repository.list_memories(user_id, memory_scope_id):
            if memory.memory_status is not MemoryStatus.DELETED:
                continue
            if not memory.metadata.get(tombstone_key):
                continue
            if memory.metadata.get("operation_id") != operation_id:
                continue
            deleted_before = self._parse_iso_datetime(memory.metadata.get("deleted_before"))
            if deleted_before is not None:
                cutoffs.append(deleted_before)
        if not cutoffs:
            return None
        return max(cutoffs)

    @staticmethod
    def _memory_deleted_by_operation(memory: Any, operation_id: str) -> bool:
        return bool(memory.metadata.get("delete_operation_id") == operation_id)

    def _mark_memory_deleted_for_operation(
        self,
        memory: Any,
        *,
        operation_id: str,
        delete_scope: str,
    ) -> None:
        metadata = dict(getattr(memory, "metadata", {}) or {})
        metadata.update({"delete_operation_id": operation_id, "delete_scope": delete_scope})
        self.repository.update_memory_index(
            memory.memory_id,
            backend_memory_id=memory.backend_memory_id,
            source_refs=memory.source_refs,
            memory_text=memory.memory_text,
            source_type=memory.source_type,
            data_classification=memory.data_classification,
            memory_type=memory.memory_type,
            backend_categories=memory.backend_categories,
            metadata=metadata,
        )
        self.repository.mark_memory_deleted(memory.memory_id)

    def _mark_memory_superseded_for_rebuild_cleanup(
        self,
        memory: Any,
        *,
        operation_id: str,
    ) -> None:
        metadata = dict(getattr(memory, "metadata", {}) or {})
        metadata.update({"rebuild_cleanup_operation_id": operation_id})
        self.repository.update_memory_index(
            memory.memory_id,
            backend_memory_id=memory.backend_memory_id,
            source_refs=memory.source_refs,
            memory_text=memory.memory_text,
            source_type=memory.source_type,
            data_classification=memory.data_classification,
            memory_type=memory.memory_type,
            backend_categories=memory.backend_categories,
            metadata=metadata,
        )
        self.repository.mark_memory_superseded(memory.memory_id)
        # N-2: 本函数是 rebuild cleanup 路径上"标 SUPERSEDED"的统一入口。
        # backend 残留清理由 service.py rebuild() 流程通过 _delete_backend_memories
        # (bulk) 集中处理——本函数纯做本地索引更新，不再触发 backend.delete，
        # 避免 per-memory delete 与 bulk delete 重复导致
        # pending_cleanup_tasks 计数翻倍 (issue: test_async_rebuild_*)。
        # N-4 配套：客户端 delete(memory_id=...) 现在能识别 SUPERSEDED 状态，
        # 如果 rebuild 标了 SUPERSEDED 但 backend 残留（异常路径），客户端
        # 显式 delete 仍会清 backend。

    def _add_delete_all_tombstone(
        self,
        *,
        user_id: str,
        memory_scope_id: str,
        operation_id: str,
    ) -> datetime:
        existing_cutoff = self._delete_tombstone_cutoff(
            user_id=user_id,
            memory_scope_id=memory_scope_id,
            tombstone_key="delete_all_tombstone",
            operation_id=operation_id,
        )
        if existing_cutoff is not None:
            return existing_cutoff
        deleted_before = datetime.now(UTC)
        raw = "|".join((user_id, memory_scope_id, "delete_all", deleted_before.isoformat()))
        digest = sha256(raw.encode("utf-8")).hexdigest()[:32]
        tombstone = self.repository.add_memory_index(
            backend_memory_id=f"delete-all:{digest}",
            user_id=user_id,
            memory_scope_id=memory_scope_id,
            source_refs=[],
            memory_text="delete_all tombstone",
            source_type=SourceType.SYSTEM_MIGRATION.value,
            data_classification=DataClassification.RESTRICTED.value,
            memory_type=MemoryType.CONSTRAINT.value,
            metadata={
                "delete_all_tombstone": True,
                "deleted_before": deleted_before.isoformat(),
                "operation_id": operation_id,
            },
        )
        self.repository.mark_memory_deleted(tombstone.memory_id)
        return deleted_before

    def _add_session_delete_tombstone(
        self,
        *,
        user_id: str,
        memory_scope_id: str,
        session_id: str,
        operation_id: str,
    ) -> datetime:
        existing_cutoff = self._delete_tombstone_cutoff(
            user_id=user_id,
            memory_scope_id=memory_scope_id,
            tombstone_key="session_delete_tombstone",
            operation_id=operation_id,
        )
        if existing_cutoff is not None:
            return existing_cutoff
        deleted_before = datetime.now(UTC)
        raw = "|".join(
            (user_id, memory_scope_id, session_id, "delete_session", deleted_before.isoformat())
        )
        digest = sha256(raw.encode("utf-8")).hexdigest()[:32]
        tombstone = self.repository.add_memory_index(
            backend_memory_id=f"delete-session:{digest}",
            user_id=user_id,
            memory_scope_id=memory_scope_id,
            source_refs=[],
            memory_text="delete_session tombstone",
            source_type=SourceType.SYSTEM_MIGRATION.value,
            data_classification=DataClassification.RESTRICTED.value,
            memory_type=MemoryType.CONSTRAINT.value,
            metadata={
                "session_delete_tombstone": True,
                "deleted_session_id": session_id,
                "deleted_before": deleted_before.isoformat(),
                "operation_id": operation_id,
            },
        )
        self.repository.mark_memory_deleted(tombstone.memory_id)
        return deleted_before

    def _delete_all_memories(
        self,
        request: DeleteMemoryRequest,
        *,
        retry_failed_task: bool = False,
    ) -> tuple[list[str], int, list[Any]]:
        l3_scope_id = self._long_term_scope_id(request)
        delete_all_cutoff = self._delete_tombstone_cutoff(
            user_id=request.user_id,
            memory_scope_id=l3_scope_id,
            tombstone_key="delete_all_tombstone",
            operation_id=request.operation_id,
        )
        active_memories = (
            [
                memory
                for memory in self.repository.list_memories(request.user_id, l3_scope_id)
                if memory.memory_status is MemoryStatus.DELETED
                and not self._is_local_index_memory_id(memory.backend_memory_id)
                and self._memory_deleted_by_operation(memory, request.operation_id)
            ]
            if retry_failed_task and delete_all_cutoff is not None
            else self.repository.active_memories(request.user_id, l3_scope_id)
        )
        pending_cleanup_tasks = 0
        deferred_backend_deletes: list[Any] = []
        for memory in active_memories:
            if self._is_backend_managed_memory_id(memory.backend_memory_id):  # noqa: SIM102
                deferred = self._schedule_backend_delete(
                    memory.backend_memory_id,
                    task_id=f"memory-delete:{request.operation_id}",
                )
                if isinstance(deferred, _BackendDeleteSync):
                    deferred_backend_deletes.append(deferred)
                elif deferred is True:
                    pending_cleanup_tasks += 1
        for memory in active_memories:
            self._mark_memory_deleted_for_operation(
                memory,
                operation_id=request.operation_id,
                delete_scope=request.scope.value,
            )
        delete_cutoff = self._add_delete_all_tombstone(
            user_id=request.user_id,
            memory_scope_id=l3_scope_id,
            operation_id=request.operation_id,
        )
        for summary_scope_id in self._user_summary_scope_ids(request.user_id):
            self.repository.clear_l1(request.user_id, summary_scope_id)
            self.repository.mark_rounds_deleted(request.user_id, summary_scope_id)
            self.repository.mark_summary(request.user_id, summary_scope_id, SummaryState.DIRTY)
        self._add_deleted_source_ref_tombstones_for_history(
            user_id=request.user_id,
            memory_scope_id=l3_scope_id,
            session_id=None,
            deleted_before=delete_cutoff,
        )
        return (
            [memory.memory_id for memory in active_memories],
            pending_cleanup_tasks,
            deferred_backend_deletes,
        )

    def _add_deleted_source_ref_tombstones_for_history(
        self,
        *,
        user_id: str,
        memory_scope_id: str,
        session_id: str | None,
        deleted_before: datetime | None = None,
    ) -> None:
        if self.history_source is None:
            return
        history_scope_id = session_id or "__all_sessions__"
        try:
            history_rounds = list(
                self.history_source.list_rounds(user_id, history_scope_id, session_id)
            )
        except Exception as exc:
            logger.bind(
                user_id=user_id,
                memory_scope_id=memory_scope_id,
                session_id=session_id,
                error_type=type(exc).__name__,
            ).warning("memory delete history tombstone skipped")
            return
        for entry in history_rounds:
            if deleted_before is not None and self._round_is_after_timestamp(
                entry.source_timestamp,
                deleted_before,
            ):
                continue
            self._add_deleted_source_ref_tombstone(
                user_id=user_id,
                memory_scope_id=memory_scope_id,
                source_refs=[{"session_id": entry.session_id, "round_id": entry.round_id}],
                memory_text="deleted source ref tombstone",
            )

    def _active_memories(self, user_id: str, memory_scope_id: str) -> list[Any]:
        """读 L3 active 索引。直读仓库，V2.1 起不再缓存（之前的 cache 在每次写
        路径都被 invalidate，cache lifetime ≈ 0，收益不抵复杂度）。"""
        return list(self.repository.active_memories(user_id, memory_scope_id))

    def _summary(self, user_id: str, memory_scope_id: str) -> Any | None:
        """读 L2 摘要。直读仓库（同上，V2.1 起不再缓存）。"""
        return self.repository.get_summary(user_id, memory_scope_id)

    def _dedupe_and_clip(self, items: list[MemoryItem], token_budget: int) -> list[MemoryItem]:
        """召回去重与预算裁剪（策略在 ``domain.recall_policy``）。"""

        return dedupe_and_clip(items, token_budget)

    @staticmethod
    def _recall_l3_metadata(raw_metadata: Any) -> dict[str, Any]:
        safe_metadata: dict[str, Any] = {"memory_as_data": True}
        if not isinstance(raw_metadata, dict):
            return safe_metadata
        for key in ("memory_type", "data_classification"):
            value = raw_metadata.get(key)
            if isinstance(value, str) and value:
                safe_metadata[key] = value
        return safe_metadata

    def _touch_recalled_entries(self, entries: list[Any]) -> None:
        """P2#7 召回强化：对"距上次记录超过窗口"的命中记忆批量落触达。

        debounce 窗口（1 小时）限制热路径写放大——同一记忆在窗口内多次
        被召回只写一次；entries 来自召回装配期持有的对象，无需回查。
        best-effort：失败只打 warning，不影响召回返回。
        """

        if not entries:
            return
        window = 3600.0
        cutoff = datetime.now(UTC) - timedelta(seconds=window)
        stale_ids = []
        for entry in entries:
            last_recalled: datetime | None = getattr(entry, "last_recalled_at", None)
            if last_recalled is None or last_recalled < cutoff:
                stale_ids.append(entry.memory_id)
        if not stale_ids:
            return
        try:
            self.repository.touch_memory_recalled(stale_ids)
        except Exception as exc:  # noqa: BLE001
            logger.bind(error_type=type(exc).__name__, count=len(stale_ids)).warning(
                "memory recall touch failed"
            )

    def _backfill_matching_slot_memories(
        self,
        items: list[MemoryItem],
        *,
        query: str,
        active_memories: list[Any],
    ) -> list[Any]:
        query_slot = self._query_conflict_slot(query)
        if query_slot is None:
            return []
        existing_l3_ids = {item.memory_id for item in items if item.layer == "L3"}
        # P2#6 新事实优先：同槽位可能存在多条 ACTIVE（异步竞争遗留、
        # 非本副本写入的旧事实等），只回填 valid_at 最新的一条 ——
        # 否则"搬家/换工作"后的旧事实会与新事实并列甚至挤占预算。
        candidates = []
        for memory in active_memories:
            if memory.memory_id in existing_l3_ids:
                continue
            if getattr(memory, "invalid_at", None) is not None:
                continue
            if not self._memory_context_allowed(memory, query):
                continue
            if self._memory_conflict_slot(memory.memory_text) != query_slot:
                continue
            candidates.append(memory)
        if not candidates:
            return []
        newest = max(candidates, key=_memory_valid_sort_key)
        items.append(
            MemoryItem(
                layer="L3",
                content=newest.memory_text,
                source="business_index",
                memory_id=newest.memory_id,
                metadata={"memory_as_data": True, "slot_backfill": query_slot},
                recall_count=getattr(newest, "recall_count", 0) or 0,
                last_recalled_at=getattr(newest, "last_recalled_at", None),
            )
        )
        return [newest]

    @staticmethod
    def _should_skip_backend_search(
        query_slot: str | None, items: list[MemoryItem]
    ) -> bool:
        """是否跳过 mem0 backend.search？

        真值表（query_slot 是否命中 P0 槽位，items 中是否已含 L3 命中）：

        +----------+-----------+--------------------------------+
        |query_slot | has L3?   | 决策                            |
        +==========+==========+================================+
        | True     | True      | skip（slot 匹配已精确收敛）     |
        +----------+-----------+--------------------------------+
        | True     | False     | skip（slot 已知无业务匹配）     |
        +----------+-----------+--------------------------------+
        | False    | True      | search（语义召回继续补全）     |
        +----------+-----------+--------------------------------+
        | False    | False     | search（必须靠语义召回）       |
        +----------+-----------+--------------------------------+

        关键设计：query_slot 命中时**故意不**回退到 backend.search —— 已知
        slot 的 active business index 没匹配意味着 slot 上没东西，再去 mem0
        走语义检索反而引入"同主题无关事实"污染（参见
        test_recall_does_not_search_backend_when_known_slot_has_no_active_business_index_match）。
        """
        _ = items  # 当前决策只依赖 query_slot；items 参数为可读性保留
        return query_slot is not None

    @staticmethod
    def _query_conflict_slot(query: str) -> str | None:
        """查询侧槽位识别（引擎在 ``domain.slots``）。"""

        return query_conflict_slot(query)

    @classmethod
    def _memory_context_allowed(cls, memory: Any, query: str) -> bool:
        """槽位/宽泛查询放行；其余要求上下文词相交（``domain.slots``）。"""

        return memory_context_allowed(memory, query)

    @staticmethod
    def _query_is_broad_memory_request(query: str) -> bool:
        return query_is_broad_memory_request(query)

    @staticmethod
    def _context_terms(text: str) -> set[str]:
        return context_terms(text)

    def _add_l3_from_messages(
        self,
        messages: list[dict[str, str]],
        *,
        user_id: str,
        memory_scope_id: str,
        source_refs: list[dict[str, str]],
        request_metadata: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """调用 L3 后端提取长期记忆，并把返回事件交给本地索引。

        messages 可能包含用户原文，只传给后端和支持性校验；日志只记录数量、范围和
        metadata 键名，避免泄露正文。

        流程图::

            进入 (messages, user_id, memory_scope_id,
                  source_refs, request_metadata)
                │
                ▼
            INFO: "memory l3 add started"
                │
                ▼
            ┌── 准备 metadata ───────────────────────────┐
            │  l3_metadata = _l3_metadata(              │
            │      source_refs, request_metadata)       │
            │  source_text = 拼接非空消息 content       │
            │  l3_metadata["source_text"] = source_text │
            └───────────────────────┬─────────────────┘
                                │
                                ▼
            ┌── 调用后端 ──────────────────────────────┐
            │  events = backend.add(                    │
            │      messages, user_id,                   │
            │      memory_scope_id, metadata)           │
            │    ↓                                       │
            │  Mem0LibraryMemoryBackend._call("add",…)  │
            │    ├─ _call_capacity (BoundedSemaphore)   │
            │    └─ _mem0_inline_threadpool()           │
            │         (monkey-patch 掉 mem0 内部线程池) │
            └───────────────────────┬─────────────────┘
                                │
                                ▼
            ┌── 二次遮罩回滚 ───────────────────────────┐
            │  if _source_refs_excluded(...):           │
            │      backend.delete_many(event ids)       │
            │      return [{"event": "SKIP", …}]        │
            └───────────────────────┬─────────────────┘
                                │ 未命中
                                ▼
            ┌── 本地索引循环 ───────────────────────────┐
            │  for event in events:                     │
            │      _index_l3_event(event, …)            │
            │        ├─ 锁内 _index_l3_event_locked     │
            │        │   (upsert MemoryIndexEntry +     │
            │        │    收集 backend 回调)            │
            │        └─ 锁外 _run_backend_writes        │
            └───────────────────────┬─────────────────┘
                                │
                                ▼
            INFO: "memory l3 add completed"
            (backend_event_count)
                │
                ▼
            返回 [dict(event) for event in events]

        注意：
        - 二次遮罩回滚是"先写后审"语义：mem0 已经落库，但只要 source_refs
          在 add 期间被另一并发路径标进删除集合，就把刚写的事件撤销。
          抵消动作是同步的 backend.delete_many，确保 L3 召回不会出现
          "被删除 source 的事实"。
        - ``backend.add`` 抛 ``RuntimeError("mem0 library add failed: …")``
          时本方法直接向上抛，由 :meth:`_run_l3_write` 的调用方统一处理。
        """

        add_log = logger.bind(
            user_id=user_id,
            memory_scope_id=memory_scope_id,
            message_count=len(messages),
            source_ref_count=len(source_refs),
            metadata_keys=self._metadata_key_summary(request_metadata),
        )
        add_log.info("memory l3 add started")
        l3_metadata = self._l3_metadata(source_refs, request_metadata)
        source_text = " ".join(
            message.get("content", "").strip()
            for message in messages
            if message.get("content", "").strip()
        )
        if source_text:
            l3_metadata["source_text"] = source_text
        events = self.backend.add(
            messages,
            user_id=user_id,
            memory_scope_id=memory_scope_id,
            metadata=l3_metadata,
            infer=self.mem0_infer_facts,
        )
        if self._source_refs_excluded(user_id, memory_scope_id, source_refs):
            self.backend.delete_many(
                [
                    str(event.get("id", ""))
                    for event in events
                    if self._is_backend_managed_memory_id(str(event.get("id", "")))
                ]
            )
            add_log.bind(reason="source_ref_excluded", backend_event_count=len(events)).info(
                "memory l3 add skipped"
            )
            return [{"event": "SKIP", "reason": "source_ref_excluded"}]
        for event in events:
            self._index_l3_event(
                event,
                user_id=user_id,
                memory_scope_id=memory_scope_id,
                source_refs=source_refs,
                l3_metadata=l3_metadata,
            )
        add_log.bind(backend_event_count=len(events)).info("memory l3 add completed")
        return [dict(event) for event in events]

    def _backfill_p0_slots_from_round_source(
        self,
        *,
        source_text: str,
        user_id: str,
        memory_scope_id: str,
        source_refs: list[dict[str, str]],
        request_metadata: dict[str, Any],
    ) -> list[Any]:
        """从原始回合中补齐 P0 关键槽位，降低后端抽取遗漏造成的召回风险。

        返回值：锁外待执行的 backend 写回调（_BackendDeleteSync 列表）。
        由调用方在持锁路径外通过 ``_run_backend_writes`` 执行。
        """

        backfill_log = logger.bind(
            user_id=user_id,
            memory_scope_id=memory_scope_id,
            source_length=len(source_text),
            source_ref_count=len(source_refs),
            metadata_keys=self._metadata_key_summary(request_metadata),
        )
        backfill_log.info("memory p0 slot backfill started")
        l3_metadata = self._l3_metadata(
            source_refs,
            {**request_metadata, "source_text": source_text},
        )
        indexed_count = 0
        skipped_count = 0
        pending_backend_writes: list[Any] = []
        for memory_text in self._p0_canonical_memories_from_source(source_text):
            slot = self._memory_conflict_slot(memory_text)
            if not self._memory_supported_by_source(memory_text, l3_metadata):
                skipped_count += 1
                continue
            backend_id = self._local_p0_backend_memory_id(
                user_id=user_id,
                memory_scope_id=memory_scope_id,
                slot=slot,
                source_refs=source_refs,
            )
            with self._index_mutation_lock:
                existing_same_source = [
                    memory
                    for memory in self.repository.active_memories(user_id, memory_scope_id)
                    if self._memory_conflict_slot(memory.memory_text)
                    == self._memory_conflict_slot(memory_text)
                    and self._conflict_partition(memory)
                    == self._conflict_partition_from_metadata(l3_metadata)
                    and any(
                        source_ref_key(ref) in {source_ref_key(raw_ref) for raw_ref in source_refs}
                        for ref in memory.source_refs
                    )
                ]
                if any(memory.memory_text == memory_text for memory in existing_same_source):
                    skipped_count += 1
                    continue
                if self._is_older_than_active_conflicting_memory(
                    user_id=user_id,
                    memory_scope_id=memory_scope_id,
                    backend_memory_id=backend_id,
                    memory_text=memory_text,
                    l3_metadata=l3_metadata,
                ):
                    skipped_count += 1
                    continue
                _, supersede_pending = self._supersede_conflicting_memories(
                    user_id=user_id,
                    memory_scope_id=memory_scope_id,
                    backend_memory_id=backend_id,
                    memory_text=memory_text,
                    l3_metadata=l3_metadata,
                )
                pending_backend_writes.extend(supersede_pending)
                self.repository.add_memory_index(
                    backend_memory_id=backend_id,
                    user_id=user_id,
                    memory_scope_id=memory_scope_id,
                    source_refs=source_refs,
                    memory_text=memory_text,
                    source_type=str(l3_metadata["source_type"]),
                    data_classification=str(l3_metadata["data_classification"]),
                    memory_type=str(l3_metadata["memory_type"]),
                    backend_categories=self._list_metadata_value(
                        l3_metadata.get("backend_categories")
                    ),
                    metadata={**l3_metadata, "p0_source_backfill": True},
                )
                indexed_count += 1
                logger.bind(
                    user_id=user_id,
                    memory_scope_id=memory_scope_id,
                    backend_memory_id=backend_id,
                    slot=slot,
                ).info("memory p0 slot backfill indexed")
        backfill_log.bind(indexed_count=indexed_count, skipped_count=skipped_count).info(
            "memory p0 slot backfill completed"
        )
        return pending_backend_writes

    @staticmethod
    def _local_p0_backend_memory_id(
        *,
        user_id: str,
        memory_scope_id: str,
        slot: str | None,
        source_refs: list[dict[str, str]],
    ) -> str:
        raw = "|".join(
            (
                user_id,
                memory_scope_id,
                slot or "unknown",
                *(
                    f"{source_ref.get('session_id', '')}:{source_ref.get('round_id', '')}"
                    for source_ref in source_refs
                ),
            )
        )
        digest = sha256(raw.encode("utf-8")).hexdigest()[:32]
        return f"local-p0:{slot or 'unknown'}:{digest}"

    def _index_l3_event(
        self,
        event: dict[str, Any],
        *,
        user_id: str,
        memory_scope_id: str = LONG_TERM_SCOPE_ID,
        source_refs: list[dict[str, str]],
        l3_metadata: dict[str, Any],
    ) -> None:
        """把后端事件同步到本地 L3 索引。

        event.memory 只用于规范化和冲突判断；日志记录 backend_id、事件类型和 slot，
        不记录 memory 文本本身。

        流程图::

            进入 (event, user_id, memory_scope_id,
                  source_refs, l3_metadata)
                │
                ▼
            解析 backend_id / original_memory_text
            memory_text = _canonical_memory_text_from_source(...)
                │
                ▼
            绑定 index_log (含 backend_memory_id / slot 等)
            INFO: "memory l3 event indexing started"
                │
                ▼
            ┌── 过滤 ─────────────────────────────────┐
            │  not backend_id                          │
            │    or event.event ∉ {ADD, UPDATE}        │
            │    └─► INFO "unsupported_event"          │
            │        └─► return                       │
            └───────────────────────┬─────────────────┘
                                │
                                ▼
            ┌── H-3: 锁内 ────────────────────────────┐
            │  with self._index_mutation_lock:        │
            │      pending = _index_l3_event_locked(  │
            │          …, index_log=index_log)        │
            │    ├─ 二次遮罩 → _BackendDeleteSync 收集│
            │    ├─ P0 白名单 gate                   │
            │    ├─ 已存在 → update_memory_index      │
            │    │   (canonical ≠ original 时收集     │
            │    │    _BackendUpdate)                 │
            │    ├─ 同源 P0 → materialized 路径       │
            │    ├─ 冲突老于活跃 → _BackendDeleteSync │
            │    └─ 新增 → add_memory_index           │
            └───────────────────────┬─────────────────┘
                                │
                                ▼
            ┌── 锁外 ─────────────────────────────────┐
            │  _run_backend_writes(                   │
            │      pending, index_log=index_log)      │
            │    for write_op in pending:             │
            │        try: write_op()                  │
            │        except: WARNING + 继续            │
            │  (mem0 网络 I/O 不再持锁)                │
            └──────────────────────────────────────────┘

        关键不变量：
        - 锁内不做任何 mem0 网络调用，只做本地 MemoryIndexEntry 的 upsert
          和对 mem0 的延迟写回调收集。
        - 锁外批量执行 mem0 调用，单条失败仅记日志——业务路径不能因为
          mem0 抖动而把本地索引回滚。
        """

        backend_id = str(event.get("id", ""))
        original_memory_text = str(event.get("memory", ""))
        memory_text = self._canonical_memory_text_from_source(original_memory_text, l3_metadata)
        index_log = logger.bind(
            user_id=user_id,
            memory_scope_id=memory_scope_id,
            backend_memory_id=backend_id,
            backend_event=event.get("event"),
            source_ref_count=len(source_refs),
            slot=self._memory_conflict_slot(memory_text),
        )
        index_log.info("memory l3 event indexing started")
        if not backend_id or event.get("event") not in {"ADD", "UPDATE"}:
            index_log.bind(reason="unsupported_event").info("memory l3 event indexing skipped")
            return
        # H-3: 锁内只做本地索引计算 + 收集 backend.update/delete 回调，
        # 锁外由 _run_backend_writes 统一执行，避免 mem0 网络 I/O 串行化
        # 其他同 user L3 写。
        with self._index_mutation_lock:
            pending_backend_writes = self._index_l3_event_locked(
                backend_id=backend_id,
                original_memory_text=original_memory_text,
                memory_text=memory_text,
                user_id=user_id,
                memory_scope_id=memory_scope_id,
                source_refs=source_refs,
                l3_metadata=l3_metadata,
                event=event,
                index_log=index_log,
            )
        self._run_backend_writes(pending_backend_writes, index_log=index_log)

    def _index_l3_event_locked(
        self,
        *,
        backend_id: str,
        original_memory_text: str,
        memory_text: str,
        user_id: str,
        memory_scope_id: str,
        source_refs: list[dict[str, str]],
        l3_metadata: dict[str, Any],
        event: dict[str, Any],
        index_log: Any,
    ) -> list[Any]:
        """H-3: 锁内只做本地索引计算 + 收集 backend.update/delete 回调。

        返回值：锁外待执行的 backend 写回调列表（lambda 或 _BackendDeleteSync）。
        锁释放后由 ``_index_l3_event`` 调用 ``_run_backend_writes`` 执行。

        流程图（在 ``_index_mutation_lock`` 内执行）::

            进入 (backend_id, memory_text, original_memory_text, …)
                │
                ▼
            pending_backend_writes = []
                │
                ▼
            ┌── 二次遮罩 ──────────────────────────────┐
            │  _source_refs_excluded(...)               │
            │  ├─ 命中 → append(_BackendDeleteSync)    │
            │  │       INFO "source_ref_excluded"      │
            │  │       return pending                  │
            │  └─ 未命中 → 继续                       │
            └───────────────────────┬─────────────────┘
                                │
                                ▼
            解析 event_metadata / backend_categories /
            index_metadata = {**l3_metadata, **event_metadata}
                │
                ▼
            existing = get_active_memory_by_backend_id(...)
            is_p0_supported = _memory_supported_by_source(...)
                │
                ├─► not is_p0_supported
                │       INFO "unsupported_by_source"
                │       return pending    # 跳过本地索引，mem0 仍保留供 L3 召回
                │
                ▼
            ┌── 已存在 (existing != None) ─────────────┐
            │  update_memory_index(existing.memory_id) │
            │  if memory_text ≠ original_memory_text: │
            │      append(_BackendUpdate(...))         │
            │  INFO "index_action=updated"             │
            │  return pending                          │
            └──────────────────────────────────────────┘
                                │
                                ▼ 不存在
            ┌── 同源 P0 探测 ──────────────────────────┐
            │  same = _same_source_local_p0_memory(…)  │
            │  ├─ 命中 → update_memory_index(          │
            │  │         same.memory_id,              │
            │  │         backend_memory_id=backend_id,│
            │  │         p0_source_backfill=True,     │
            │  │         backend_materialized=True)   │
            │  │     if canonical ≠ original:         │
            │  │         append(_BackendUpdate)       │
            │  │     INFO "index_action=               │
            │  │          materialized_local_p0"       │
            │  │     return pending                   │
            │  └─ 未命中 → 继续                       │
            └───────────────────────┬─────────────────┘
                                │
                                ▼
            ┌── 冲突版本检查 ──────────────────────────┐
            │  if _is_older_than_active_conflicting…:  │
            │      append(_BackendDeleteSync)         │
            │      INFO "older_than_active_conflict"   │
            │      return pending                     │
            └───────────────────────┬─────────────────┘
                                │
                                ▼
            _, supersede_pending = _supersede_conflicting_memories(...)
            extend(supersede_pending)
            add_memory_index(backend_id, …, index_metadata)
            if memory_text ≠ original_memory_text:
                append(_BackendUpdate(...))
            INFO "index_action=created"
                │
                ▼
            return pending

        关键约束：
        - **禁止**在本函数内调用 ``self.backend.*`` 直接做 mem0 写。
          所有 mem0 副作用通过 ``pending_backend_writes`` 收集到锁外执行。
        - P0 白名单 gate 失败时 mem0 已经成功 add，本地不索引但 mem0 记录保留
          ——避免误删导致 L3 召回丢事实（之前线上 bug）。
        """
        pending_backend_writes: list[Any] = []
        if self._source_refs_excluded(user_id, memory_scope_id, source_refs):
            pending_backend_writes.append(
                _BackendDeleteSync(memory_id=backend_id, backend=self.backend)
            )
            index_log.bind(reason="source_ref_excluded").info("memory l3 event indexing skipped")
            return pending_backend_writes

        raw_event_metadata = event.get("metadata")
        event_metadata: dict[str, Any] = (
            dict(raw_event_metadata) if isinstance(raw_event_metadata, dict) else {}
        )
        backend_categories = self._list_metadata_value(
            event_metadata.get("categories")
            or event_metadata.get("backend_categories")
            or l3_metadata.get("backend_categories")
        )
        index_metadata = {**dict(l3_metadata), **event_metadata}

        existing = self.repository.get_active_memory_by_backend_id(
            user_id,
            memory_scope_id,
            backend_id,
        )
        is_p0_supported = self._memory_supported_by_source(memory_text, l3_metadata)
        # P0 白名单 gate（MEMORY_P0_SLOTS 配置）：
        # - 留空（默认）= ``is_p0_supported`` 永远 True = 全部进本地 MemoryRecord
        # - 非空 = slot 不在白名单的 memory 被跳过本地索引，但保留在 mem0 供 /recall
        # 不删 mem0 那条记录——之前误删导致 /recall 的 L3 召回也丢事实。
        if not is_p0_supported:
            index_log.bind(reason="unsupported_by_source").info("memory l3 event indexing skipped")
            return pending_backend_writes
        if existing:
            self.repository.update_memory_index(
                existing.memory_id,
                source_refs=source_refs,
                memory_text=memory_text,
                source_type=str(l3_metadata["source_type"]),
                data_classification=str(l3_metadata["data_classification"]),
                memory_type=str(l3_metadata["memory_type"]),
                backend_categories=backend_categories,
                metadata=index_metadata,
            )
            if memory_text != original_memory_text:
                pending_backend_writes.append(
                    _BackendUpdate(memory_id=backend_id, data=memory_text, backend=self.backend)
                )
            index_log.bind(index_action="updated").info("memory l3 event indexed")
            return pending_backend_writes

        if is_p0_supported:
            same_source_local = self._same_source_local_p0_memory(
                user_id=user_id,
                memory_scope_id=memory_scope_id,
                memory_text=memory_text,
                l3_metadata=l3_metadata,
            )
            if same_source_local is not None:
                self.repository.update_memory_index(
                    same_source_local.memory_id,
                    backend_memory_id=backend_id,
                    source_refs=source_refs,
                    memory_text=memory_text,
                    source_type=str(l3_metadata["source_type"]),
                    data_classification=str(l3_metadata["data_classification"]),
                    memory_type=str(l3_metadata["memory_type"]),
                    backend_categories=backend_categories,
                    metadata={
                        **index_metadata,
                        "p0_source_backfill": True,
                        "backend_materialized": True,
                    },
                )
                if memory_text != original_memory_text:
                    pending_backend_writes.append(
                        _BackendUpdate(memory_id=backend_id, data=memory_text, backend=self.backend)
                    )
                index_log.bind(index_action="materialized_local_p0").info("memory l3 event indexed")
                return pending_backend_writes

            if self._is_older_than_active_conflicting_memory(
                user_id=user_id,
                memory_scope_id=memory_scope_id,
                backend_memory_id=backend_id,
                memory_text=memory_text,
                l3_metadata=l3_metadata,
            ):
                pending_backend_writes.append(
                    _BackendDeleteSync(memory_id=backend_id, backend=self.backend)
                )
                index_log.bind(reason="older_than_active_conflict").info(
                    "memory l3 event indexing skipped"
                )
                return pending_backend_writes
            _, supersede_pending = self._supersede_conflicting_memories(
                user_id=user_id,
                memory_scope_id=memory_scope_id,
                backend_memory_id=backend_id,
                memory_text=memory_text,
                l3_metadata=l3_metadata,
            )
            pending_backend_writes.extend(supersede_pending)
        self.repository.add_memory_index(
            backend_memory_id=backend_id,
            user_id=user_id,
            memory_scope_id=memory_scope_id,
            source_refs=source_refs,
            memory_text=memory_text,
            source_type=str(l3_metadata["source_type"]),
            data_classification=str(l3_metadata["data_classification"]),
            memory_type=str(l3_metadata["memory_type"]),
            backend_categories=backend_categories,
            metadata=index_metadata,
        )
        if memory_text != original_memory_text:
            pending_backend_writes.append(
                _BackendUpdate(memory_id=backend_id, data=memory_text, backend=self.backend)
            )
        index_log.bind(index_action="created").info("memory l3 event indexed")
        return pending_backend_writes

    def _run_backend_writes(self, pending_writes: list[Any], *, index_log: Any = None) -> None:
        """H-3: 在锁外执行 backend.update/delete 回调。失败仅记日志，不抛出。

        调用方已经做完本地索引更新，mem0 残留记录由后续 rebuild / cleanup
        流程回收；不抛出是为了不让 mem0 网络抖动击穿业务路径。

        流程图::

            进入 (pending_writes, index_log)
                │
                ▼
            for write_op in pending_writes:
                │
                ├─ try: write_op()
                │         │
                │         ├─ _BackendUpdate()
                │         │     → backend.update(…)
                │         │     → Mem0LibraryMemoryBackend.update
                │         │        ├─ not-found 可见性窗口退避重试
                │         │        └─ _call → _call_capacity
                │         │             + _mem0_inline_threadpool()
                │         │
                │         └─ _BackendDeleteSync()
                │               → backend.delete(…)
                │               → 同上（含窗口重试 + 幂等跳过）
                │
                └─ except Exception as exc:
                       │
                       ├─ index_log 存在
                       │     index_log.bind(
                       │         backend_memory_id, error_type
                       │     ).warning(
                       │         "memory backend write after index failed"
                       │     )
                       │
                       └─ 否则 → 顶层 logger.bind(...).warning(...)

            注意：所有异常**只记日志不抛出**。
            业务路径已经把本地索引落盘，mem0 残留记录会被后续
            rebuild / cleanup 异步回收。

        设计取舍：
        - 顺序执行而非并发：单条 L3 写只会产生 0~1 个 pending callback，
          并发收益小，但能让 mem0 失败时的日志按写入顺序可读。
        - 不区分 update/delete 的错误：两类副作用都是"修补已落地的本地索引"，
          失败都让 rebuild 兜底即可。
        """
        for write_op in pending_writes:
            try:
                write_op()
            except Exception as exc:
                if index_log is not None:
                    index_log.bind(
                        backend_memory_id=getattr(write_op, "memory_id", None),
                        error_type=type(exc).__name__,
                    ).warning("memory backend write after index failed")
                else:
                    logger.bind(
                        backend_memory_id=getattr(write_op, "memory_id", None),
                        error_type=type(exc).__name__,
                    ).warning("memory backend write after index failed")

    def _same_source_local_p0_memory(
        self,
        *,
        user_id: str,
        memory_scope_id: str,
        memory_text: str,
        l3_metadata: dict[str, Any],
    ) -> Any | None:
        return next(
            (
                memory
                for memory in self.repository.active_memories(user_id, memory_scope_id)
                if self._is_same_source_local_p0_memory(
                    memory,
                    memory_text=memory_text,
                    l3_metadata=l3_metadata,
                )
            ),
            None,
        )

    def _supersede_conflicting_memories(
        self,
        *,
        user_id: str,
        memory_scope_id: str,
        backend_memory_id: str,
        memory_text: str,
        l3_metadata: dict[str, Any] | None = None,
    ) -> tuple[list[str], list[Any]]:
        new_slot = self._memory_conflict_slot(memory_text)
        if new_slot is None:
            return [], []
        new_partition = self._conflict_partition_from_metadata(l3_metadata or {})
        superseded_ids: list[str] = []
        pending_backend_writes: list[Any] = []
        for memory in self.repository.active_memories(user_id, memory_scope_id):
            if memory.backend_memory_id == backend_memory_id:
                continue
            if self._memory_conflict_slot(memory.memory_text) != new_slot:
                continue
            if self._conflict_partition(memory) != new_partition:
                continue
            superseded = self.repository.mark_memory_superseded(memory.memory_id)
            if superseded is None:
                continue
            superseded_ids.append(memory.memory_id)
            if self._is_backend_managed_memory_id(memory.backend_memory_id):
                deferred = self._schedule_backend_delete(memory.backend_memory_id)
                if isinstance(deferred, _BackendDeleteSync):
                    pending_backend_writes.append(deferred)
                # async 模式：_schedule_backend_delete 已提交到 _l3.executor，
                # 返回 True，caller 不需要跟踪。
        return superseded_ids, pending_backend_writes

    @staticmethod
    def _is_backend_managed_memory_id(memory_id: str) -> bool:
        return is_backend_managed_memory_id(memory_id)

    @staticmethod
    def _is_local_index_memory_id(memory_id: str) -> bool:
        return is_local_index_memory_id(memory_id)

    def _is_older_than_active_conflicting_memory(
        self,
        *,
        user_id: str,
        memory_scope_id: str,
        backend_memory_id: str,
        memory_text: str,
        l3_metadata: dict[str, Any] | None = None,
    ) -> bool:
        new_slot = self._memory_conflict_slot(memory_text)
        if new_slot is None:
            return False
        new_partition = self._conflict_partition_from_metadata(l3_metadata or {})
        new_cursor = self._source_cursor(user_id, l3_metadata or {})
        if new_cursor is None:
            return False
        for memory in self.repository.active_memories(user_id, memory_scope_id):
            if memory.backend_memory_id == backend_memory_id:
                continue
            if self._memory_conflict_slot(memory.memory_text) != new_slot:
                continue
            if self._conflict_partition(memory) != new_partition:
                continue
            existing_cursor = self._source_cursor(user_id, memory.metadata)
            if existing_cursor is None:
                continue
            if new_cursor < existing_cursor:
                return True
            if new_cursor == existing_cursor and not self._is_same_source_local_p0_memory(
                memory,
                memory_text=memory_text,
                l3_metadata=l3_metadata or {},
            ):
                return True
        return False

    @staticmethod
    def _is_same_source_local_p0_memory(
        memory: Any,
        *,
        memory_text: str,
        l3_metadata: dict[str, Any],
    ) -> bool:
        return is_same_source_local_p0_memory(
            memory, memory_text=memory_text, l3_metadata=l3_metadata
        )

    def _source_cursor(
        self, user_id: str, metadata: dict[str, Any]
    ) -> tuple[tuple[int, float, str], str, str] | None:
        return source_cursor(self.repository, user_id, metadata)

    def _source_cursor_from_refs(
        self, user_id: str, raw_refs: list[dict[str, str]]
    ) -> tuple[tuple[int, float, str], str, str] | None:
        return source_cursor_from_refs(self.repository, user_id, raw_refs)

    def _round_order(self, user_id: str, session_id: str, round_id: str) -> tuple[int, float, str]:
        return round_order(self.repository, user_id, session_id, round_id)

    def _mark_superseded_slot_sources_deleted(
        self,
        *,
        user_id: str,
        memory_scope_id: str,
        slot: str | None,
        deleted_memory_id: str,
    ) -> None:
        if slot is None:
            return
        for memory in self.repository.list_memories(user_id, memory_scope_id):
            if memory.memory_id == deleted_memory_id:
                continue
            if self._memory_conflict_slot(memory.memory_text) != slot:
                continue
            if memory.memory_status.name != "SUPERSEDED":
                continue
            self.repository.mark_memory_deleted(memory.memory_id)

    @staticmethod
    def _conflict_partition_from_metadata(_metadata: dict[str, Any]) -> tuple[()]:
        return conflict_partition_from_metadata(_metadata)

    @staticmethod
    def _conflict_partition(_memory: Any) -> tuple[()]:
        return conflict_partition(_memory)

    @staticmethod
    def _memory_conflict_slot(memory_text: str) -> str | None:
        return memory_conflict_slot(memory_text)

    @classmethod
    def _memory_supported_by_source(cls, memory_text: str, l3_metadata: dict[str, Any]) -> bool:
        """mem0 抽取准入校验（策略在 ``domain.slots``）。"""

        # hot path 上 import 一次 settings（lru_cache 单例），避免重复解析 .env
        from thinkback.infra.config import settings

        return memory_supported_by_source(
            memory_text, l3_metadata, allowed_slots=settings.memory_p0_slots_list
        )

    @staticmethod
    def _source_has_any(source_text: str, markers: tuple[str, ...]) -> bool:
        return source_has_any(source_text, markers)

    @classmethod
    def _p0_canonical_memories_from_source(cls, source_text: str) -> list[str]:
        return p0_canonical_memories_from_source(source_text)

    @classmethod
    def _canonical_memory_text(cls, memory_text: str) -> str:
        return canonical_memory_text(memory_text)

    @classmethod
    def _canonical_memory_text_from_source(
        cls, memory_text: str, l3_metadata: dict[str, Any]
    ) -> str:
        return canonical_memory_text_from_source(memory_text, l3_metadata)

    @classmethod
    def _p0_canonical_memory_for_source_slot(cls, memory_text: str, source_text: str) -> str | None:
        return p0_canonical_memory_for_source_slot(memory_text, source_text)

    @staticmethod
    def _extract_current_nickname(memory_text: str) -> str | None:
        return extract_current_nickname(memory_text)

    @staticmethod
    def _extract_pet_name(memory_text: str) -> tuple[str, str] | None:
        return extract_pet_name(memory_text)

    @staticmethod
    def _extract_current_location(memory_text: str) -> str | None:
        return extract_current_location(memory_text)

    @staticmethod
    def _extract_current_work_status(memory_text: str) -> str | None:
        return extract_current_work_status(memory_text)

    @staticmethod
    def _extract_communication_preference(memory_text: str) -> str | None:
        return extract_communication_preference(memory_text)

    @staticmethod
    def _extract_birthday(memory_text: str) -> str | None:
        return extract_birthday(memory_text)

    @staticmethod
    def _extract_favorite_consumable(memory_text: str) -> tuple[str, str] | None:
        return extract_favorite_consumable(memory_text)

    @staticmethod
    def _extract_sleep_reminder_preference(memory_text: str) -> str | None:
        return extract_sleep_reminder_preference(memory_text)

    def _l3_metadata(
        self, source_refs: list[dict[str, str]], request_metadata: dict[str, Any]
    ) -> dict[str, Any]:
        metadata = {
            "source_refs": source_refs,
            "source_type": self._enum_value(
                SourceType,
                request_metadata.get("source_type"),
                SourceType.CHAT_ROUND.value,
            ),
            "data_classification": self._enum_value(
                DataClassification,
                request_metadata.get("data_classification"),
                self._classify_data(request_metadata),
            ),
            "memory_type": self._enum_value(
                MemoryType,
                request_metadata.get("memory_type"),
                MemoryType.PROFILE.value,
            ),
        }
        backend_categories = self._list_metadata_value(request_metadata.get("backend_categories"))
        if backend_categories:
            metadata["backend_categories"] = backend_categories
        if request_metadata.get("source_text"):
            metadata["source_text"] = str(request_metadata["source_text"])
        return metadata

    @staticmethod
    def _enum_value(enum_type: type[StrEnum], value: object, default: str) -> str:
        if isinstance(value, str):
            valid_values = {str(member.value) for member in enum_type}
            if value in valid_values:
                return value
        return default

    @staticmethod
    def _list_metadata_value(value: object) -> list[str]:
        if isinstance(value, list):
            return [str(item) for item in value]
        if isinstance(value, str) and value:
            return [value]
        return []

    @staticmethod
    def _classify_data(request_metadata: dict[str, Any]) -> str:
        if request_metadata.get("data_classification"):
            return str(request_metadata["data_classification"])
        return str(DataClassification.PERSONAL.value)

    def _is_restricted_or_unsafe(self, messages: list[Any]) -> bool:
        return messages_are_restricted_or_unsafe(
            [
                {
                    "role": getattr(message.role, "value", str(message.role)),
                    "content": message.content,
                }
                for message in messages
            ]
        )

    def _messages_are_restricted_or_unsafe(self, messages: list[dict[str, str]]) -> bool:
        """内容安全门禁（词表在 ``domain.safety``）。"""

        return messages_are_restricted_or_unsafe(messages)

    def _upsert_default_summary(self, user_id: str, memory_scope_id: str) -> None:
        """写入拼接占位摘要（P0：已有 llm 综合摘要时由仓储层跳过覆盖）。

        拼接版只是"尚无 LLM 综合摘要时"的同步占位；已有 llm 版时不做
        拼接覆盖——最新轮次由 L1 承载，L2 综合版允许滞后到下一次去抖
        刷新。保留判断在仓储层完成（SQL upsert 本就先 select，零额外查询）。
        """
        deleted_refs = self.repository.excluded_source_refs(user_id, memory_scope_id)
        rounds = [
            entry
            for entry in self.repository.list_rounds(user_id, memory_scope_id)
            if (entry.session_id, entry.round_id) not in deleted_refs
        ]
        self.repository.upsert_summary_from_rounds(
            user_id, memory_scope_id, rounds, preserve_llm=True
        )

    @staticmethod
    def _text_is_restricted_or_unsafe(content: str) -> bool:
        """受限/不安全文本判定（词表在 ``domain.safety``）。"""

        return text_is_restricted_or_unsafe(content)

    @staticmethod
    def _redact_event(event: dict[str, Any]) -> dict[str, Any]:
        redacted = {key: value for key, value in event.items() if key not in {"memory", "content"}}
        if "id" in redacted:
            redacted["backend_memory_id"] = redacted.pop("id")
        return redacted

    def _assert_round_matches_request(
        self, existing: JournalEntry, request: AppendMemoryRequest
    ) -> None:
        if (
            existing.user_id != request.user_id
            or existing.memory_scope_id != self._session_scope_id(request)
            or existing.session_id != request.session_id
            or existing.round_fingerprint != request_fingerprint(request)
        ):
            raise ValueError(
                "round_id conflict: existing round scope or fingerprint does not match"
            )

    def _source_refs_excluded(
        self,
        user_id: str,
        memory_scope_id: str,
        source_refs: list[dict[str, str]],
    ) -> bool:
        excluded_refs = self.repository.excluded_source_refs(user_id, memory_scope_id)
        return any(source_ref_key(source_ref) in excluded_refs for source_ref in source_refs)

    @staticmethod
    def _parse_iso_datetime(value: Any) -> datetime | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    @staticmethod
    def _round_is_after_timestamp(source_timestamp: datetime, cutoff: datetime) -> bool:
        if source_timestamp.tzinfo is None:
            source_timestamp = source_timestamp.replace(tzinfo=UTC)
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=UTC)
        return bool(source_timestamp > cutoff)

    def _delete_all_cutoff(self, user_id: str, memory_scope_id: str) -> datetime | None:
        cutoffs: list[datetime] = []
        for memory in self.repository.list_memories(user_id, memory_scope_id):
            if memory.memory_status is not MemoryStatus.DELETED:
                continue
            if not memory.metadata.get("delete_all_tombstone"):
                continue
            deleted_before = self._parse_iso_datetime(memory.metadata.get("deleted_before"))
            if deleted_before is not None:
                cutoffs.append(deleted_before)
        if not cutoffs:
            return None
        return max(cutoffs)

    def _round_is_after_delete_all_cutoff(
        self,
        entry: JournalEntry,
        *,
        user_id: str,
        memory_scope_id: str,
    ) -> bool:
        cutoff = self._delete_all_cutoff(user_id, memory_scope_id)
        if cutoff is None:
            return True
        return self._round_is_after_timestamp(entry.source_timestamp, cutoff)

    def _session_delete_cutoff(
        self, user_id: str, memory_scope_id: str, session_id: str
    ) -> datetime | None:
        cutoffs: list[datetime] = []
        for memory in self.repository.list_memories(user_id, memory_scope_id):
            if memory.memory_status is not MemoryStatus.DELETED:
                continue
            if not memory.metadata.get("session_delete_tombstone"):
                continue
            if memory.metadata.get("deleted_session_id") != session_id:
                continue
            deleted_before = self._parse_iso_datetime(memory.metadata.get("deleted_before"))
            if deleted_before is not None:
                cutoffs.append(deleted_before)
        if not cutoffs:
            return None
        return max(cutoffs)

    def _round_is_after_session_delete_cutoff(
        self,
        entry: JournalEntry,
        *,
        user_id: str,
        memory_scope_id: str,
    ) -> bool:
        cutoff = self._session_delete_cutoff(user_id, memory_scope_id, entry.session_id)
        if cutoff is None:
            return True
        return self._round_is_after_timestamp(entry.source_timestamp, cutoff)

    def _rebuild_rounds(self, request: RebuildMemoryRequest) -> list[JournalEntry]:
        l3_scope_id = self._long_term_scope_id(request)
        excluded_refs = self.repository.excluded_source_refs(request.user_id, l3_scope_id)
        delete_cutoffs = _RebuildDeleteCutoffs(self, request.user_id, l3_scope_id)
        rounds: list[JournalEntry] = []
        for summary_scope_id in self._rebuild_summary_scope_ids(request):
            rounds.extend(
                entry
                for entry in self._effective_history_rounds(
                    request.user_id,
                    summary_scope_id,
                    summary_scope_id,
                )
                if (entry.session_id, entry.round_id) not in excluded_refs
                and delete_cutoffs.allows(entry)
            )
        return rounds

    def _uncovered_rebuild_rounds(
        self,
        request: RebuildMemoryRequest,
        rounds: list[JournalEntry],
        deleted_refs: set[tuple[str | None, str | None]],
    ) -> list[JournalEntry]:
        processed_refs = self._processed_l3_source_refs(request, deleted_refs)
        active_memories = self.repository.active_memories(
            request.user_id,
            self._long_term_scope_id(request),
        )
        return [
            entry
            for entry in rounds
            if not self._rebuild_round_is_l3_covered(
                entry,
                active_memories=active_memories,
                deleted_refs=deleted_refs,
                processed_refs=processed_refs,
            )
        ]

    def _rebuild_round_is_l3_covered(
        self,
        entry: JournalEntry,
        *,
        active_memories: list[Any],
        deleted_refs: set[tuple[str | None, str | None]],
        processed_refs: set[tuple[str | None, str | None]],
    ) -> bool:
        ref_key = (entry.session_id, entry.round_id)
        if ref_key in deleted_refs:
            return True
        source_slots = self._source_l3_slots(entry)
        if not source_slots:
            return ref_key in processed_refs
        return all(
            self._rebuild_slot_is_covered_by_active_memory(entry, slot, active_memories)
            for slot in source_slots
        )

    def _source_l3_slots(self, entry: JournalEntry) -> set[str]:
        source_text = " ".join(
            str(message.get("content", "")).strip()
            for message in entry.messages
            if message.get("role") == MessageRole.USER.value
            and str(message.get("content", "")).strip()
        )
        slots = {
            self._memory_conflict_slot(memory_text)
            for memory_text in self._p0_canonical_memories_from_source(source_text)
        }
        return {slot for slot in slots if slot is not None}

    def _rebuild_slot_is_covered_by_active_memory(
        self,
        entry: JournalEntry,
        slot: str,
        active_memories: list[Any],
    ) -> bool:
        entry_cursor = (
            self._round_order(entry.user_id, entry.session_id, entry.round_id),
            entry.session_id,
            entry.round_id,
        )
        entry_ref = (entry.session_id, entry.round_id)
        for memory in active_memories:
            if self._memory_conflict_slot(memory.memory_text) != slot:
                continue
            if entry_ref in {source_ref_key(ref) for ref in memory.source_refs}:
                return True
            memory_cursor = self._source_cursor_from_refs(entry.user_id, memory.source_refs)
            if memory_cursor is not None and memory_cursor >= entry_cursor:
                return True
        return False

    def _processed_l3_source_refs(
        self,
        request: RebuildMemoryRequest,
        deleted_refs: set[tuple[str | None, str | None]],
    ) -> set[tuple[str | None, str | None]]:
        processed_refs: set[tuple[str | None, str | None]] = set()
        l3_scope_id = self._long_term_scope_id(request)
        for memory in self.repository.list_memories(request.user_id, l3_scope_id):
            if memory.memory_status is not MemoryStatus.ACTIVE:
                continue
            for source_ref in memory.source_refs:
                ref_key = source_ref_key(source_ref)
                if ref_key in deleted_refs:
                    continue
                if (
                    request.session_id is not None
                    and source_ref.get("session_id") != request.session_id
                ):
                    continue
                processed_refs.add(ref_key)
        return processed_refs

    def _effective_history_rounds(
        self, user_id: str, memory_scope_id: str, session_id: str | None
    ) -> list[JournalEntry]:
        if self.history_source:
            return list(self.history_source.list_rounds(user_id, memory_scope_id, session_id))
        return list(self.repository.list_rounds(user_id, memory_scope_id, session_id))

    def _rebuild_cleanup_candidates(
        self,
        request: RebuildMemoryRequest,
        *,
        retry_failed_task: bool,
    ) -> list[Any]:
        if not retry_failed_task:
            return self._l3_memories_in_rebuild_scope(request)
        l3_scope_id = self._long_term_scope_id(request)
        memories = [
            memory
            for memory in self.repository.list_memories(request.user_id, l3_scope_id)
            if memory.memory_status is MemoryStatus.SUPERSEDED
            and memory.metadata.get("rebuild_cleanup_operation_id") == request.operation_id
            and self._is_backend_managed_memory_id(memory.backend_memory_id)
        ]
        if request.session_id is None:
            return memories
        return [
            memory
            for memory in memories
            if any(ref.get("session_id") == request.session_id for ref in memory.source_refs)
        ]

    def _l3_memories_in_rebuild_scope(self, request: RebuildMemoryRequest) -> list[Any]:
        active_memories = self.repository.active_memories(
            request.user_id,
            self._long_term_scope_id(request),
        )
        if request.session_id is None:
            return list(active_memories)
        return [
            memory
            for memory in active_memories
            if any(ref.get("session_id") == request.session_id for ref in memory.source_refs)
        ]


__all__ = [
    "InMemoryMemoryRepository",
    "MemoryService",
    "SqlAlchemyMemoryRepository",
]
