"""令牌桶 —— HTTP 面与 gRPC 面共用的速率上限原语。

放在 infra 而不是某个面里：HTTP 的限流中间件（``api/ratelimit.py``）与 gRPC
的限流器（``rpc/ratelimit.py``）是同一个服务的两条面，限额口径必须一致 ——
各写一份，迟早漂成「换个协议就绕开限流」。

算法：桶里最多 ``burst`` 个令牌，按 ``rate_per_second`` 匀速回填；取令牌
成功则扣一个，不足则返回需要等待的秒数。
"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class TokenBucket:
    tokens: float
    last_refill: float
    rate_per_second: float
    burst: int

    def consume(self, n: int = 1) -> tuple[bool, float]:
        """尝试消费 n 个 token,返回 (是否成功, 需等待秒数)。"""
        now = time.time()
        elapsed = now - self.last_refill
        self.tokens = min(self.burst, self.tokens + elapsed * self.rate_per_second)
        self.last_refill = now

        if self.tokens >= n:
            self.tokens -= n
            return True, 0.0
        wait = (n - self.tokens) / self.rate_per_second
        return False, wait


class BucketRegistry:
    """按 key 分桶的令牌桶集合，**桶数有上限**。

    分桶键是**调用方可控**的（``namespace`` + ``user_id``）。不封顶的话，
    这张表就是一个能无限长大的内存洞：拿随机 user_id 打几次就能把它撑爆。

    - 桶数有硬上限（``max_buckets``），超了淘汰最久没用上的那个；
    - 每次消费更新该桶的 ``last_used``，淘汰按它排序。

    「限额变了不重置余额」的旧规则也在这条路径上：改配置不该给每个桶发一次
    免费额度。
    """

    def __init__(
        self,
        rate_limit_per_minute: int,
        burst: int,
        *,
        max_buckets: int = 10_000,
    ) -> None:
        self.rate_limit_per_minute = rate_limit_per_minute
        self.burst = burst
        self.max_buckets = max_buckets
        self._buckets: dict[str, TokenBucket] = {}
        self._last_used: dict[str, float] = {}

    def _get_bucket(self, key: str) -> TokenBucket:
        bucket = self._buckets.get(key)
        if bucket is None:
            if len(self._buckets) >= self.max_buckets:
                self._evict_oldest()
            bucket = TokenBucket(
                tokens=float(self.burst),
                last_refill=time.time(),
                rate_per_second=self.rate_limit_per_minute / 60.0,
                burst=self.burst,
            )
            self._buckets[key] = bucket
        else:
            bucket.burst = self.burst
            bucket.rate_per_second = self.rate_limit_per_minute / 60.0
            bucket.tokens = min(bucket.tokens, float(self.burst))
        self._last_used[key] = time.time()
        return bucket

    def _evict_oldest(self) -> None:
        """淘汰最久没用上的桶。

        淘汰掉的桶回到「新桶」状态（满额度）—— 攻击者轮换 key 拿到的是一把
        新桶，这由上面那层「按对端 IP 的天花板」兜底。
        """

        if not self._last_used:
            return
        oldest = min(self._last_used, key=lambda key: self._last_used[key])
        self._last_used.pop(oldest, None)
        self._buckets.pop(oldest, None)

    def consume(self, key: str) -> tuple[bool, float]:
        """消费一个令牌，返回 (是否成功, 需等待秒数)。"""

        return self._get_bucket(key).consume(1)

    def remaining(self, key: str) -> float:
        """桶里还剩多少令牌（响应头用；桶不存在就是 0）。"""

        bucket = self._buckets.get(key)
        return 0.0 if bucket is None else bucket.tokens

    def bucket_count(self) -> int:
        """当前活着的桶数（测试钩子）。"""

        return len(self._buckets)
