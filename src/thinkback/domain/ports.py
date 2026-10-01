"""端口协议（hexagonal ports）。

应用层编排只依赖这里的三个协议，不依赖任何具体实现：
- ``MemoryRepository``: L1/L2/L3 本地索引与任务存储端口
- ``MemoryBackend``: L3 长期记忆后端（mem0 + Milvus）端口
- ``HistorySource``: 外部会话历史回放端口（rebuild 用）

实现位于 ``thinkback.infra`` 与 ``thinkback.memory.backends``；
测试用 ``InMemoryMemoryRepository`` / ``FakeMemoryBackend`` 替换。
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from thinkback.domain.entities import (
    AdminAuditEntry,
    JournalEntry,
    MemoryIndexEntry,
    SummaryEntry,
    TaskEntry,
)
from thinkback.domain.enums import SummaryState

if TYPE_CHECKING:
    from thinkback.memory.schemas import AppendMemoryRequest


class MemoryRepository(Protocol):
    def get_round(self, round_id: str) -> JournalEntry | None: ...

    def save_round(self, request: AppendMemoryRequest) -> JournalEntry: ...

    def update_l1(self, entry: JournalEntry, limit: int = 10) -> None: ...

    def clear_l1(
        self, user_id: str, memory_scope_id: str, session_id: str | None = None
    ) -> None: ...

    def get_l1(self, user_id: str, memory_scope_id: str, session_id: str) -> list[JournalEntry]: ...

    def upsert_summary_from_journal(self, user_id: str, memory_scope_id: str) -> SummaryEntry: ...

    def upsert_summary_from_rounds(
        self,
        user_id: str,
        memory_scope_id: str,
        rounds: list[JournalEntry],
        *,
        summary_text: str | None = None,
        summary_kind: str = "concat",
        preserve_llm: bool = False,
    ) -> SummaryEntry:
        """``preserve_llm=True`` 时，若当前摘要已是 llm 版则跳过 concat 覆盖。"""
        ...

    def get_summary(self, user_id: str, memory_scope_id: str) -> SummaryEntry | None: ...

    def mark_summary(self, user_id: str, memory_scope_id: str, state: SummaryState) -> None: ...

    def list_rounds(
        self, user_id: str, memory_scope_id: str, session_id: str | None = None
    ) -> list[JournalEntry]: ...

    def list_user_rounds(self, user_id: str) -> list[JournalEntry]: ...

    def mark_rounds_deleted(
        self, user_id: str, memory_scope_id: str, session_id: str | None = None
    ) -> int: ...

    def mark_round_deleted(self, round_id: str) -> bool: ...

    def mark_round_active(self, round_id: str) -> bool: ...

    def mark_round_pending_append(self, round_id: str) -> bool: ...

    def add_memory_index(
        self,
        *,
        backend_memory_id: str,
        user_id: str,
        memory_scope_id: str = "thinkback",
        source_refs: list[dict[str, str]],
        memory_text: str,
        source_type: str = "chat_round",
        data_classification: str = "normal",
        memory_type: str | None = None,
        backend_categories: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        valid_at: datetime | None = None,
    ) -> MemoryIndexEntry: ...

    def active_memories(self, user_id: str, memory_scope_id: str) -> list[MemoryIndexEntry]: ...

    def list_memories(self, user_id: str, memory_scope_id: str) -> list[MemoryIndexEntry]: ...

    def get_active_memory_by_backend_id(
        self, user_id: str, memory_scope_id: str, backend_memory_id: str
    ) -> MemoryIndexEntry | None: ...

    def update_memory_index(
        self,
        memory_id: str,
        *,
        backend_memory_id: str | None = None,
        source_refs: list[dict[str, str]],
        memory_text: str,
        source_type: str = "chat_round",
        data_classification: str = "normal",
        memory_type: str | None = None,
        backend_categories: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        valid_at: datetime | None = None,
    ) -> MemoryIndexEntry | None: ...

    def mark_memory_deleted(self, memory_id: str) -> MemoryIndexEntry | None: ...

    def mark_memory_suppressed(self, memory_id: str) -> MemoryIndexEntry | None:
        """P2#7 decay：置 SUPPRESSED（不可达而非删除）并幂等落失效时刻。"""
        ...

    def touch_memory_recalled(self, memory_ids: list[str]) -> int:
        """P2#7：批量记录召回触达（last_recalled_at=now，recall_count+1）。"""
        ...

    def mark_memory_superseded(self, memory_id: str) -> MemoryIndexEntry | None: ...

    def mark_memory_active(self, memory_id: str) -> MemoryIndexEntry | None: ...

    def update_memory_source_refs(
        self, memory_id: str, source_refs: list[dict[str, str]]
    ) -> MemoryIndexEntry | None: ...

    def deleted_source_refs(
        self, user_id: str, memory_scope_id: str
    ) -> set[tuple[str | None, str | None]]: ...

    def excluded_source_refs(
        self, user_id: str, memory_scope_id: str
    ) -> set[tuple[str | None, str | None]]: ...

    def save_task(self, task: TaskEntry) -> TaskEntry: ...

    def claim_task(self, task: TaskEntry) -> tuple[TaskEntry, bool]: ...

    def get_task(self, task_id: str) -> TaskEntry | None: ...

    def reclaim_stale_running_tasks(self, *, max_age_seconds: float) -> list[str]: ...

    def reclaim_stale_running_task(self, task_id: str, *, max_age_seconds: float) -> bool: ...

    def list_tasks(
        self,
        *,
        statuses: list[str] | None = None,
        older_than_seconds: float | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[TaskEntry]:
        """管理面任务列表：按状态/年龄过滤，updated_at 倒序分页。"""
        ...

    def count_tasks_by_status(self) -> dict[str, int]:
        """管理面聚合：各状态任务计数。"""
        ...

    def count_memories_by_status(self) -> dict[str, int]:
        """管理面聚合：各状态本地索引记忆计数。"""
        ...

    def count_memories_by_classification(self) -> dict[str, int]:
        """管理面聚合：按数据分类（normal/personal/sensitive/restricted）计数。"""
        ...

    def count_memories_by_source_type(self) -> dict[str, int]:
        """管理面聚合：按 source_type（chat_round/manual_fix/...）计数。"""
        ...

    def admin_list_memories(
        self,
        *,
        user_id: str | None = None,
        memory_scope_id: str | None = None,
        statuses: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[MemoryIndexEntry], int]:
        """管理面记忆检索：过滤 + 分页，返回 (条目, 匹配总数)。"""
        ...

    def get_memory_index(self, memory_id: str) -> MemoryIndexEntry | None:
        """按本地记忆 ID 直查索引条目（治理台详情用）。"""
        ...

    def get_rounds_by_refs(self, refs: list[dict[str, str]]) -> list[JournalEntry]:
        """按 source_refs 批量取 journal 原文回合（缺失的静默跳过）。"""
        ...

    def save_admin_audit(self, entry: AdminAuditEntry) -> None:
        """治理台审计留痕（只追加）。"""

    def list_admin_audit(
        self,
        *,
        action: str | None = None,
        operator: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AdminAuditEntry]:
        """审计查询：created_at 倒序分页。"""
        ...

    def close(self, timeout: float = 5.0) -> None: ...


class MemoryBackend(Protocol):
    """L3 长期记忆后端统一接口协议

    定义 ``MemoryService`` 与后端实现之间的契约。所有实现必须：
    - 在 add/search/delete/update/delete_all 等入口保证 user_id + memory_scope_id
      维度的隔离，防止跨用户或跨范围污染。
    - 对异常做"统一包装"（mem0 实现已封装为 ``RuntimeError``），让
      ``_BackendDeleteSync`` / ``_BackendUpdate`` 等锁外回调能稳定捕获。

    ``_call`` 是显式声明的统一异常包装入口：服务层的锁外回调
    （``_BackendUpdate``）必须经由它触发，才能拿到一致的
    ``RuntimeError("mem0 library <op> failed: ...")`` 包装语义。
    """

    def _call(self, operation: str, *args: Any, **kwargs: Any) -> Any:
        """统一的后端调用入口：懒构造客户端 + 并发限流 + 异常包装。"""
        ...

    def add(
        self,
        messages: list[dict[str, str]],
        *,
        user_id: str,
        memory_scope_id: str,
        metadata: dict[str, Any] | None = None,
        infer: bool = True,
    ) -> list[dict[str, Any]]:
        """写入若干条消息并返回抽取出的事件摘要列表。

        ``infer=False`` 跳过 LLM 事实抽取：raw message 落地为一条记忆，
        适用于 LLM endpoint 不可用 / 故意退化的场景。

        返回列表的元素通常形如 ``{"id": ..., "memory": ..., "event": "ADD|UPDATE"}``。
        """
        ...

    def search(
        self,
        query: str,
        *,
        user_id: str,
        memory_scope_id: str,
        limit: int,
        threshold: float | None = None,
    ) -> list[dict[str, Any]]:
        """语义检索：按 query 在 user+scope 范围内找最相关的 limit 条记忆。"""
        ...

    def update(self, memory_id: str, data: str) -> None:
        """更新一条后端记忆内容；data 不会被日志记录。"""
        ...

    def delete(self, memory_id: str) -> None:
        """删除单条后端记忆；缺失 ID 必须视为幂等成功。"""
        ...

    def delete_many(self, memory_ids: list[str]) -> int:
        """批量删除，返回实际尝试删除的条数（不含空 ID）。"""
        ...

    def delete_all(self, *, user_id: str, memory_scope_id: str) -> int:
        """删除指定 user+scope 范围内所有后端记忆，返回被删除条数。"""
        ...


class HistorySource(Protocol):
    """外部会话历史回放源（rebuild 输入）。

    用于从外部系统（而非本地 journal）读取回合历史，重建 L2/L3。
    """

    def current_version(self, user_id: str, session_id: str) -> str: ...

    def list_rounds(
        self,
        user_id: str,
        memory_scope_id: str,
        session_id: str | None = None,
    ) -> list[JournalEntry]: ...
