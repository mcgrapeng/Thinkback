"""L1 短期回合的进程内缓存（mixin）。

缓存最近 N 轮对话，用于快速召回，避免每次都走数据库查询。
按 (user_id, memory_scope_id, session_id) 多维索引。

短期记忆的持久化仍在 PostgreSQL（ins_summary_round_journal）；
本缓存只是进程内读加速，缓存丢失不影响正确性。
"""

from __future__ import annotations

from threading import Lock

from innies_memory.domain.entities import JournalEntry
from innies_memory.domain.keys import scope_key

# L1 短期记忆窗口：保留最近 N 个完整轮次。
L1_CACHE_LIMIT = 10


class L1CacheMixin:
    """L1 短期回合的内存缓存层

    缓存最近 N 轮对话，用于快速召回，避免每次都走数据库查询。
    按 (user_id, memory_scope_id, session_id) 多维索引。

    N-5: 所有访问受 _l1_lock 保护，避免多线程并发 update_l1/clear_l1 出现
    读-改-写竞态丢数据。子类需要保证 _l1_lock 字段已设置（dataclass 子类
    走 field(default_factory=Lock)，显式 __init__ 子类走 L1CacheMixin.__init__）。
    """

    l1_cache: dict[str, list[JournalEntry]]
    _l1_lock: Lock

    def init_l1_lock(self) -> None:
        """N-5: 给子类一个统一的入口确保 _l1_lock 已初始化。

        dataclass 子类（如 InMemoryMemoryRepository）的 __init__ 由装饰器生成，
        不会调用这里的 __init__，所以 _l1_lock 必须作为 dataclass 字段提供。
        这个方法仅在显式 __init__ 子类（如 SqlAlchemyMemoryRepository）中
        在 self.l1_cache 赋值之后调用一次。
        """
        self._l1_lock = Lock()

    def update_l1(self, entry: JournalEntry, limit: int = 10) -> None:
        """更新 L1 缓存，保留最近 N 条回合

        工作流：
        1. 构造缓存键 "{user_id}:{memory_scope_id}:{session_id}"
        2. 移除重复的 round_id（避免重复计算）
        3. 追加新回合
        4. 只保留最近 limit 条（其余自动淘汰）
        """
        key = f"{scope_key(entry.user_id, entry.memory_scope_id)}:{entry.session_id}"
        with self._l1_lock:
            existing = [
                cached_entry
                for cached_entry in self.l1_cache.get(key, [])
                if cached_entry.round_id != entry.round_id
            ]
            existing.append(entry)
            self.l1_cache[key] = existing[-limit:]

    def clear_l1(self, user_id: str, memory_scope_id: str, session_id: str | None = None) -> None:
        """清空 L1 缓存。删除时调用，确保不返回已删除的旧数据

        参数：
          session_id: 为 None 时清空整个 memory_scope_id 下的所有会话；
                      为值时只清空该会话
        """
        prefix = scope_key(user_id, memory_scope_id)
        with self._l1_lock:
            if session_id:
                self.l1_cache.pop(f"{prefix}:{session_id}", None)
                return
            for key in list(self.l1_cache):
                if key.startswith(f"{prefix}:"):
                    self.l1_cache.pop(key, None)

    def get_l1(self, user_id: str, memory_scope_id: str, session_id: str) -> list[JournalEntry]:
        """读取 L1 缓存。从缓存中查询最近 N 轮回合

        返回格式：[JournalEntry, ...]（按时间序列，可为空）
        """
        with self._l1_lock:
            return list(
                self.l1_cache.get(f"{scope_key(user_id, memory_scope_id)}:{session_id}", [])
            )

    def close(self, timeout: float = 5.0) -> None:
        """释放底层资源（事件循环、worker 线程、连接池等）。

        ``InMemoryMemoryRepository`` 没有 worker 线程，默认 no-op；
        ``SqlAlchemyMemoryRepository`` 覆写该方法停掉事件循环并 join 线程。
        """
        _ = timeout
        return
