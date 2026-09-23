"""领域枚举与状态机。

三层记忆架构的词汇表，被实体、仓储端口、应用层 DTO 共同使用。
本模块是纯领域层最底部的稳定词汇，不依赖任何其他模块。
"""

from enum import StrEnum


class MessageRole(StrEnum):
    """消息角色枚举，标识一轮对话中的参与者"""

    USER = "user"  # 用户消息
    ASSISTANT = "assistant"  # 模型输出
    SYSTEM = "system"  # 系统级指令


class MemoryType(StrEnum):
    """记忆分类。L3 后端可用此标签来分组或聚合相关记忆"""

    PROFILE = "profile"  # 用户身份、背景信息
    PREFERENCE = "preference"  # 喜好、偏好、风格
    EVENT = "event"  # 已发生的事件、操作记录
    PLAN = "plan"  # 计划、目标、待办
    CONSTRAINT = "constraint"  # 限制、禁忌、边界（系统级元数据）


class SourceType(StrEnum):
    """记忆来源类型。表明记忆如何被创建"""

    CHAT_ROUND = "chat_round"  # 来自一轮对话的自动提取
    SESSION_REBUILD = "session_rebuild"  # 重建时补缺的历史回合
    MANUAL_FIX = "manual_fix"  # 人工修正或编辑的记忆
    IMPORT = "import"  # 从外部导入（迁移、同步）
    SYSTEM_MIGRATION = "system_migration"  # 系统级操作（删除墓碑等）


class MemoryStatus(StrEnum):
    """L3 本地索引记忆状态机。决定召回时是否返回该记忆"""

    ACTIVE = "ACTIVE"  # 有效，可被召回
    DELETED = "DELETED"  # 已删除，不可召回（保留作为删除屏障）
    SUPPRESSED = "SUPPRESSED"  # 被禁用，暂不使用（保留扩展用）
    SUPERSEDED = "SUPERSEDED"  # 被新版本取代，重建时清理


class DataClassification(StrEnum):
    """数据敏感等级。控制 L3 后端是否接收或存储"""

    NORMAL = "normal"  # 常规数据，完全存储
    PERSONAL = "personal"  # 个人身份相关，脱敏存储
    SENSITIVE = "sensitive"  # 敏感内容（密钥、医疗等），不存储
    RESTRICTED = "restricted"  # 系统级元数据，不存储


class SummaryState(StrEnum):
    """L2 摘要状态机。影响召回时返回的 L2 质量"""

    ACTIVE = "active"  # 最新且可信，优先返回
    STALE = "stale"  # 陈旧但有效，返回但标记降级
    DIRTY = "dirty"  # 删除或重建中，跳过不返回
    REBUILDING = "rebuilding"  # 正在重建中，跳过不返回


class TaskStatus(StrEnum):
    """后台异步任务状态机。追踪长期操作的进度"""

    PENDING = "pending"  # 等待执行
    RUNNING = "running"  # 正在执行中
    COMPLETED = "completed"  # 成功完成
    FAILED = "failed"  # 执行失败（可重试）
    DEAD_LETTER = "dead_letter"  # 多次失败，需人工介入


class OperationType(StrEnum):
    """操作类型。后台任务所涉及的核心操作"""

    WRITE_ROUND = "write_round"  # 追加对话回合并提取 L3
    UPDATE_MEMORY = "update_memory"  # 手工编辑单条长期记忆
    DELETE_MEMORY = "delete_memory"  # 删除单条记忆
    DELETE_SESSION = "delete_session"  # 删除整个会话的记忆
    DELETE_ALL = "delete_all"  # 删除用户全部记忆
    REBUILD_L2 = "rebuild_l2"  # 重建 L2 摘要
    REBUILD_L3 = "rebuild_l3"  # 重建 L3 索引


class RecallIntent(StrEnum):
    """召回意图。决定召回安全性级别和失败处理"""

    CHAT = "chat"  # 常规对话，接受降级召回
    SENSITIVE = "sensitive"  # 隐私相关，fail-closed 策略，任何错误都拒绝


class DeleteScope(StrEnum):
    """删除范围。决定删除操作的作用域"""

    MEMORY = "memory"  # 单条记忆
    SESSION = "session"  # 单个会话
    ALL = "all"  # 用户全局
