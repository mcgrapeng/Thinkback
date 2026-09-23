"""仓储包：领域端口 ``MemoryRepository`` 的两种实现。

兼容层：旧 ``thinkback.memory.repositories`` 模块的公开名全部
re-export；领域实体 / 键 / 协议已下沉到 ``thinkback.domain``。
"""

from thinkback.domain.entities import (
    JournalEntry,
    MemoryIndexEntry,
    SummaryEntry,
    TaskEntry,
)
from thinkback.domain.keys import (
    fingerprint,
    request_fingerprint,
    round_sort_key,
    scope_key,
    source_ref_key,
)
from thinkback.domain.ports import MemoryRepository
from thinkback.domain.summarization import summarize_rounds
from thinkback.memory.repositories._l1_cache import L1CacheMixin
from thinkback.memory.repositories.in_memory import InMemoryMemoryRepository
from thinkback.memory.repositories.sqlalchemy import SqlAlchemyMemoryRepository

__all__ = [
    "InMemoryMemoryRepository",
    "JournalEntry",
    "L1CacheMixin",
    "MemoryIndexEntry",
    "MemoryRepository",
    "SqlAlchemyMemoryRepository",
    "SummaryEntry",
    "TaskEntry",
    "fingerprint",
    "request_fingerprint",
    "round_sort_key",
    "scope_key",
    "source_ref_key",
    "summarize_rounds",
]
