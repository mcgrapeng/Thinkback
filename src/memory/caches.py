"""进程内 TTL 读缓存（dict 子类）。

服务在短期记忆读路径上维护两个缓存（活跃记忆索引 / L2 摘要），
TTL + 条目上限双约束，防止按用户维度无界增长。缓存只是读加速：
短期记忆的持久化始终在 PostgreSQL，缓存丢失不影响正确性。

实现为 ``dict`` 子类，键为 ``(user_id, memory_scope_id)``，
值为 ``(cached_at_monotonic, value)``；``len`` / ``in`` / ``get``
语义与普通 dict 一致，便于观测与测试。
"""

from __future__ import annotations

from time import monotonic
from typing import Any


class BoundedTTLCache(dict):  # type: ignore[type-arg]
    """带 TTL 与容量上限的读缓存。

    参数：
      ttl_seconds: 条目存活时间；过期后读取方应视为未命中。
      max_entries: 条目数硬上限；写入满时先淘汰过期项，再淘汰最旧项。
    """

    def __init__(self, *, ttl_seconds: float, max_entries: int) -> None:
        super().__init__()
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be > 0")
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries

    def put(self, cache_key: tuple[str, str], value: Any, now: float | None = None) -> None:
        """写入条目；容量满时先清理过期项，再按最旧淘汰。"""

        current = monotonic() if now is None else now
        if cache_key not in self and len(self) >= self.max_entries:
            expired_keys = [
                key
                for key, (cached_at, _) in self.items()
                if current - cached_at > self.ttl_seconds
            ]
            for key in expired_keys:
                self.pop(key, None)
            while self and len(self) >= self.max_entries:
                oldest_key = min(self, key=lambda key: self[key][0])
                self.pop(oldest_key, None)
        self[cache_key] = (current, value)

    def get_if_fresh(self, cache_key: tuple[str, str], now: float | None = None) -> Any | None:
        """读取未过期条目；过期或缺失返回 None。"""

        current = monotonic() if now is None else now
        cached = self.get(cache_key)
        if cached is None:
            return None
        cached_at, value = cached
        if current - cached_at > self.ttl_seconds:
            return None
        return value
