"""gRPC 面的速率上限 —— 与 HTTP 面同一套限额、同一把令牌桶。

为什么 gRPC 面也要限：HTTP 的 ``/v2/memory/*`` 走限流中间件，gRPC 面不走 ——
同一个服务、同一份写入成本（一次写入会触发 L1/L2/L3 沉淀，可能产生 LLM
调用），两条面给的限额不一样，gRPC 面就成了绕开限流的口子。

分桶键与 HTTP 面一致：「谁在用」（``namespace`` + ``user_id``），取不到才
退回对端 IP。服务化接入时所有调用方都从同一个出口 IP 出来，按 IP 分桶
等于全服务共享一份额度 —— 一个多租户调用方会被别的租户挤到超限。

超限的语义与 HTTP 面对齐：``RESOURCE_EXHAUSTED``（HTTP 是 429 TB-1007）。
"""

from __future__ import annotations

import time
from threading import Lock
from typing import Any

import grpc

from thinkback.domain.keys import DEFAULT_NAMESPACE
from thinkback.infra.token_bucket import TokenBucket


def caller_identity(request: Any) -> str:
    """gRPC 请求里的调用方标识（``namespace`` + ``user_id``）。

    与 HTTP 面的 ``api/idempotency.py::_caller_identity`` 同一口径：空
    namespace 归一到默认值（proto3 标量没有 presence，空串 = 没传）。
    取不到 user_id 的请求（查任务、看后台状态）返回空串，由调用方退回对端 IP。
    """

    user_id = getattr(request, "user_id", "") or ""
    if not user_id.strip():
        return ""
    namespace = getattr(request, "namespace", "") or DEFAULT_NAMESPACE
    return f"{namespace}:{user_id}"


class GrpcRateLimiter:
    """按调用方分桶的令牌桶，超限直接 abort。"""

    def __init__(self, rate_limit_per_minute: int, burst: int) -> None:
        self.rate_limit_per_minute = rate_limit_per_minute
        self.burst = burst
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = Lock()

    @classmethod
    def from_settings(cls) -> GrpcRateLimiter:
        """限额取自配置 —— 与 HTTP 面读的是同一份设置。"""

        from thinkback.infra.config import settings

        return cls(
            rate_limit_per_minute=settings.memory_rate_limit_per_minute,
            burst=settings.memory_rate_limit_burst,
        )

    def _bucket(self, client_id: str) -> TokenBucket:
        with self._lock:
            bucket = self._buckets.get(client_id)
            if bucket is None:
                bucket = TokenBucket(
                    tokens=float(self.burst),
                    last_refill=time.time(),
                    rate_per_second=self.rate_limit_per_minute / 60.0,
                    burst=self.burst,
                )
                self._buckets[client_id] = bucket
            return bucket

    def check(self, request: Any, context: grpc.ServicerContext) -> None:
        """超限就 abort（抛 ``grpc.RpcError``）。

        必须在 servicer 的 try 块**之外**调用：``context.abort`` 抛出的
        RpcError 会被外层 ``except Exception`` 吞掉，再被映射成 INTERNAL ——
        限额拒绝会变成「服务端内部错误」。
        """

        client_id = caller_identity(request) or _peer(context)
        ok, wait = self._bucket(client_id).consume(1)
        if not ok:
            retry_after = max(1, int(wait) + 1)
            context.abort(
                grpc.StatusCode.RESOURCE_EXHAUSTED,
                f"Rate limit exceeded. Retry after {retry_after}s",
            )


def _peer(context: grpc.ServicerContext) -> str:
    """对端标识（取不到调用方身份时的兜底分桶键）。"""

    try:
        return context.peer() or "unknown"
    except Exception:  # noqa: BLE001 - 拿不到 peer 就退化成统一桶
        return "unknown"
