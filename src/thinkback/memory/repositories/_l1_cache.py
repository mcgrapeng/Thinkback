"""L1 短期回合的进程内缓存（mixin）。

缓存最近 N 轮对话，用于快速召回，避免每次都走数据库查询。
按 (user_id, namespace, memory_scope_id, session_id) 多维索引。

短期记忆的持久化仍在 PostgreSQL（ins_summary_round_journal）；
本缓存只是进程内读加速，缓存丢失不影响正确性。
"""

from __future__ import annotations

import time
from threading import Lock

from thinkback.domain.entities import JournalEntry
from thinkback.domain.keys import DEFAULT_NAMESPACE, namespace_scope_key

# L1 短期记忆窗口：保留最近 N 个完整轮次。
L1_CACHE_LIMIT = 10

# 缓存条目的最长存活时间（秒）。
# 原实现只在缓存**为空**时回源 PG：一旦热了就永远不再读库，而 clear_l1 只在
# delete/rebuild 路径调用 → 多副本下（k8s replicas=2）另一个 Pod 追加的轮次
# 本 Pod 永远看不到，同一用户换个 Pod 就换一份记忆（已实测复现）。
# 加 TTL 后，缓存最多陈旧这么久就会被判为 miss 并回源 PG。
L1_CACHE_TTL_SECONDS = 30.0

# 缓存**键**的数量上界。值有上界（L1_CACHE_LIMIT 条/键），键没有：每个
# (user, namespace, scope, session) 组合一个键，会话只增不减 → 长跑服务里
# 键空间无界增长（每个键还带着最多 10 条轮次原文）。TTL 是惰性的：只在
# 命中时判断，不再访问的键永远不过期。所以必须显式淘汰。
#
# ponytail: 固定 1000 个键（≈1000×10 条轮次原文）。按并发会话数调；
# 调大前先确认内存余量，调小会让热点会话互相挤掉缓存。
L1_CACHE_MAX_KEYS = 1000


class L1CacheMixin:
    """L1 短期回合的内存缓存层

    缓存最近 N 轮对话，用于快速召回，避免每次都走数据库查询。
    按 (user_id, namespace, memory_scope_id, session_id) 多维索引。

    N-5: 所有访问受 _l1_lock 保护，避免多线程并发 update_l1/clear_l1 出现
    读-改-写竞态丢数据。子类需要保证 _l1_lock 字段已设置（dataclass 子类
    走 field(default_factory=Lock)，显式 __init__ 子类走 L1CacheMixin.__init__）。
    """

    l1_cache: dict[str, list[JournalEntry]]
    _l1_lock: Lock
    #: 关掉后整层旁路：每次都回源 PG（读穿透）。多副本部署必须这么配 ——
    #: 进程内缓存的失效只在本副本，别的副本追加的轮次本副本看不见；TTL 只是
    #: 把陈旧窗口从「永久」压到 30 秒，不是根治。
    l1_cache_enabled: bool = True

    def _l1_timestamps(self) -> dict[str, float]:
        """每个缓存键的写入时刻（monotonic）。惰性建表，免改两个 adapter 的构造。"""
        stamps = getattr(self, "_l1_cache_at", None)
        if stamps is None:
            stamps = {}
            self._l1_cache_at = stamps
        return stamps

    def init_l1_lock(self) -> None:
        """N-5: 给子类一个统一的入口确保 _l1_lock 已初始化。

        dataclass 子类（如 InMemoryMemoryRepository）的 __init__ 由装饰器生成，
        不会调用这里的 __init__，所以 _l1_lock 必须作为 dataclass 字段提供。
        这个方法仅在显式 __init__ 子类（如 SqlAlchemyMemoryRepository）中
        在 self.l1_cache 赋值之后调用一次。
        """
        self._l1_lock = Lock()

    @staticmethod
    def _l1_cache_key(user_id: str, memory_scope_id: str, session_id: str, namespace: str) -> str:
        """缓存键：必须含 namespace，否则同 session_id 的两个 namespace 互相命中。"""
        return f"{namespace_scope_key(user_id, namespace, memory_scope_id)}:{session_id}"

    def update_l1(self, entry: JournalEntry, limit: int = 10) -> None:
        """更新 L1 缓存，保留最近 N 条回合

        工作流：
        1. 构造缓存键 "{user_id}:{namespace}:{scope}:{session_id}"
        2. 移除重复的 round_id（避免重复计算）
        3. 追加新回合
        4. 只保留最近 limit 条（其余自动淘汰）
        """
        if not self.l1_cache_enabled:
            return
        key = self._l1_cache_key(
            entry.user_id, entry.memory_scope_id, entry.session_id, entry.namespace
        )
        with self._l1_lock:
            existing = [
                cached_entry
                for cached_entry in self.l1_cache.get(key, [])
                if cached_entry.round_id != entry.round_id
            ]
            existing.append(entry)
            self.l1_cache[key] = existing[-limit:]
            self._l1_timestamps()[key] = time.monotonic()
            self._evict_oldest_keys()

    def _evict_oldest_keys(self) -> None:
        """键数超上界时淘汰最早的键（调用方已持锁）。

        dict 保持插入顺序，最早插入的键排在最前；同一个键重复写入不会改变
        它的插入位置，所以这是 FIFO 而不是 LRU —— 对「防止无界增长」这个
        目的够用，不必为真 LRU 再引入 OrderedDict 与 move_to_end 的维护。
        """
        stamps = self._l1_timestamps()
        while len(self.l1_cache) > L1_CACHE_MAX_KEYS:
            oldest = next(iter(self.l1_cache))
            del self.l1_cache[oldest]
            # 时间戳簿记可能已经缺失（脏状态：clear_l1 的整 scope 路径会留下
            # 「有缓存条目但没有时间戳」的条目）。淘汰跑在每次 append 上，
            # 不能因为簿记脏就打断写入。
            stamps.pop(oldest, None)

    def clear_l1(
        self,
        user_id: str,
        memory_scope_id: str,
        session_id: str | None = None,
        *,
        namespace: str = DEFAULT_NAMESPACE,
    ) -> None:
        """清空 L1 缓存。删除时调用，确保不返回已删除的旧数据

        参数：
          session_id: 为 None 时清空整个 (namespace, memory_scope_id) 下的所有会话；
                      为值时只清空该会话
          namespace: 命名空间维度；不同 namespace 的同名 session 互不影响
        """
        prefix = namespace_scope_key(user_id, namespace, memory_scope_id)
        with self._l1_lock:
            stamps = self._l1_timestamps()
            if session_id:
                key = f"{prefix}:{session_id}"
                self.l1_cache.pop(key, None)
                stamps.pop(key, None)
                return
            for key in list(self.l1_cache):
                if key.startswith(f"{prefix}:"):
                    self.l1_cache.pop(key, None)
                    stamps.pop(key, None)

    def get_l1(
        self,
        user_id: str,
        memory_scope_id: str,
        session_id: str,
        *,
        namespace: str = DEFAULT_NAMESPACE,
    ) -> list[JournalEntry]:
        """读取 L1 缓存。从缓存中查询最近 N 轮回合

        返回格式：[JournalEntry, ...]（按时间序列，可为空）

        缓存关掉时永远返回空：调用方据此回源 PG（读穿透）。
        """
        if not self.l1_cache_enabled:
            return []
        key = self._l1_cache_key(user_id, memory_scope_id, session_id, namespace)
        with self._l1_lock:
            entries = self.l1_cache.get(key)
            if not entries:
                return []
            stamped_at = self._l1_timestamps().get(key)
            if stamped_at is None or (time.monotonic() - stamped_at) > L1_CACHE_TTL_SECONDS:
                # 超过 TTL：判为 miss，交给调用方回源 PG（并重新填充缓存）。
                self.l1_cache.pop(key, None)
                self._l1_timestamps().pop(key, None)
                return []
            return list(entries)

    def close(self, timeout: float = 5.0) -> None:
        """释放底层资源（事件循环、worker 线程、连接池等）。

        ``InMemoryMemoryRepository`` 没有 worker 线程，默认 no-op；
        ``SqlAlchemyMemoryRepository`` 覆写该方法停掉事件循环并 join 线程。
        """
        _ = timeout
        return
