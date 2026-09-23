"""持久化无关的领域实体。

- JournalEntry: 对话回合日志（L1），记录完整消息内容
- SummaryEntry: 会话摘要（L2），递进式摘要文本
- MemoryIndexEntry: 长期记忆索引（L3），记录后端 ID 和反链信息
- TaskEntry: 后台异步任务，追踪删除/重建等长期操作

实体只依赖领域枚举，仓储实现负责与数据库行互转。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from thinkback.domain.enums import (
    DataClassification,
    MemoryStatus,
    OperationType,
    SourceType,
    SummaryState,
    TaskStatus,
)


@dataclass
class JournalEntry:
    """对话流水账簿条目（L1 短期记忆）

    L1 是对话的完整记录，存储消息原文，用于后续分析和故障排查。
    每一条 JournalEntry 对应一轮完整的 user-assistant 对话。

    属性：
      journal_id: 流水账编号（唯一），用于幂等去重
      user_id: 用户维度，用于隔离
      memory_scope_id: 通常是 session_id，标记所属会话
      session_id: 会话 ID，同上
      round_id: 调用方提供的回合 ID，用于链路追踪
      messages: 完整消息列表 [user_msg, assistant_msg]
      source_timestamp: 回合发生的时间戳
      round_fingerprint: 消息内容的摘要指纹，用于冲突检测
      round_state: 状态机 [active|pending_append|deleted_tombstone]
      round_index: 可选的序号，用于排序或版本化
    """

    journal_id: str
    user_id: str
    memory_scope_id: str
    session_id: str
    round_id: str
    messages: list[dict[str, Any]]
    source_timestamp: datetime
    round_fingerprint: str
    round_index: int | None = None
    round_state: str = "active"


@dataclass
class SummaryEntry:
    """会话摘要条目（L2 中期记忆）

    L2 是周期性编译的摘要文本，包含最近 N 轮的用户关键信息。
    通过摘要削减调用 L3 的频率，加快召回速度。

    属性：
      summary_id: 摘要编号（唯一）
      user_id: 用户维度
      memory_scope_id: 会话维度
      summary_text: 摘要正文（纯文本）
      summary_cursor_round: 最后处理过的回合 ID
      latest_source_round_id: 最新源回合 ID（用于版本号）
      latest_source_timestamp: 最新源回合的时间戳
      summary_state: 状态机 [ACTIVE|STALE|DIRTY|REBUILDING]
      summary_kind: 生成方式 [concat(拼接降级)|llm(LLM 综合摘要)]
    """

    summary_id: str
    user_id: str
    memory_scope_id: str
    summary_text: str
    summary_cursor_round: str | None
    latest_source_round_id: str | None
    latest_source_timestamp: datetime | None
    summary_state: SummaryState = SummaryState.ACTIVE
    summary_kind: str = "concat"


@dataclass
class MemoryIndexEntry:
    """长期记忆索引条目（L3）

    L3 是 L3 后端（向量库）中存储的实际记忆的本地代理。
    此条目记录了指向后端记忆的反链信息（source_refs）和元数据。

    属性：
      memory_id: 本地业务记忆 ID（唯一）
      backend_memory_id: 后端（如 mem0）中的记忆 ID
      user_id: 用户维度
      memory_scope_id: 范围维度（通常是 "thinkback"）
      source_refs: 来源引用列表 [{session_id, round_id}, ...]
      memory_text: 记忆原文（用于本地召回和验证）
      memory_status: 状态机 [ACTIVE|DELETED|SUPERSEDED]
      source_type: 来源类型
      data_classification: 敏感等级
      memory_type: 记忆分类 [profile|preference|event|...]
      backend_categories: 后端自定义分类标签列表
      metadata: 任意附加元数据（不含密钥）
    """

    memory_id: str
    backend_memory_id: str
    user_id: str
    memory_scope_id: str
    source_refs: list[dict[str, str]]
    memory_text: str
    memory_status: MemoryStatus = MemoryStatus.ACTIVE
    source_type: str = SourceType.CHAT_ROUND.value
    data_classification: str = DataClassification.NORMAL.value
    memory_type: str | None = None
    backend_categories: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    # P2#6 轻量双时态：事实生效/失效时间（None=未知/未设置，不影响既有语义）。
    valid_at: datetime | None = None
    invalid_at: datetime | None = None
    # P2#7 遗忘信号：最近召回时刻与累计召回次数（None=从未被召回过）。
    last_recalled_at: datetime | None = None
    recall_count: int = 0


@dataclass
class TaskEntry:
    """后台异步任务条目

    任务表用于追踪长期操作的进度，支持重试和断点续传。

    属性：
      task_id: 任务 ID（唯一）
      request_id: 创建该任务的调用方请求 ID
      op_type: 操作类型 [WRITE_ROUND|DELETE_*|REBUILD_*]
      scope: 任务作用域摘要（user_id, session_id 等）
      status: 状态机 [PENDING|RUNNING|COMPLETED|FAILED]
      operation_id: 操作级幂等 ID（删除/重建时使用）
      history_version: 外部历史版本号（重建时的一致性校验）
      retry_count: 重试次数计数
      last_error: 最后一次失败的错误信息
      result: 任务完成后的结果摘要（JSON）
      row_version: 乐观锁版本（P1a）：save_task 按 WHERE row_version=? CAS 写入，
        冲突抛 TaskStaleWriteError；每次成功写 +1
    """

    task_id: str
    request_id: str
    op_type: OperationType
    scope: dict[str, Any]
    status: TaskStatus
    operation_id: str | None = None
    history_version: str | None = None
    retry_count: int = 0
    last_error: str | None = None
    result: dict[str, Any] = field(default_factory=dict)
    row_version: int = 0
