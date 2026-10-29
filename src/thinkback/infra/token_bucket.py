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
