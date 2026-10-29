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

from typing import Any

import grpc

from thinkback.domain.keys import DEFAULT_NAMESPACE
from thinkback.infra.token_bucket import BucketRegistry

# 按对端 IP 的天花板 = 每调用方限额的多少倍。与 HTTP 面同一个口径：
# 分桶键是调用方给的，轮换 key 就换一把新桶，天花板按 IP 分桶换不掉。
_PEER_CEILING_FACTOR = 20


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


def peer_address(context: grpc.ServicerContext) -> str:
    """对端**地址**（不含端口）。

    ``context.peer()`` 给的是 ``ipv4:<ip>:<port>``，端口是客户端临时端口 ——
    每条 TCP 连接都不同。带端口分桶等于每条连接一把新桶，限额永远咬不到。
    所以只取地址部分。
    """

    try:
        peer = context.peer() or ""
    except Exception:  # noqa: BLE001 - 拿不到 peer 就退化成统一桶
        return "unknown"
    if not peer:
        return "unknown"
    address, separator, _port = peer.rpartition(":")
    # "ipv4:1.2.3.4:5678" → rpartition 拿到 ("ipv4:1.2.3.4", ":", "5678")；
    # 没有端口时（如 unix socket）rpartition 返回 ("", "", peer)。
    return address if separator and address else peer


class GrpcRateLimiter:
    """按调用方分桶的令牌桶，超限直接 abort。"""

    def __init__(
        self,
        rate_limit_per_minute: int,
        burst: int,
        *,
        max_buckets: int = 10_000,
    ) -> None:
        self.rate_limit_per_minute = rate_limit_per_minute
        self.burst = burst
        self.buckets = BucketRegistry(rate_limit_per_minute, burst, max_buckets=max_buckets)
        self.peer_buckets = BucketRegistry(
            rate_limit_per_minute * _PEER_CEILING_FACTOR,
            burst * _PEER_CEILING_FACTOR,
            max_buckets=max_buckets,
        )

    @classmethod
    def from_settings(cls) -> GrpcRateLimiter:
        """限额取自配置 —— 与 HTTP 面读的是同一份设置。"""

        from thinkback.infra.config import settings

        return cls(
            rate_limit_per_minute=settings.memory_rate_limit_per_minute,
            burst=settings.memory_rate_limit_burst,
        )

    def check(self, request: Any, context: grpc.ServicerContext) -> None:
        """超限就 abort（抛 ``grpc.RpcError``）。

        必须在 servicer 的 try 块**之外**调用：``context.abort`` 抛出的
        RpcError 会被外层 ``except Exception`` 吞掉，再被映射成 INTERNAL ——
        限额拒绝会变成「服务端内部错误」。
        """

        client_id = caller_identity(request) or peer_address(context)
        # 先扣每调用方的桶：超了就是超了，不该再去动共享的天花板。
        ok, wait = self.buckets.consume(client_id)
        if ok:
            # 再扣按对端地址的天花板：轮换分桶键（换 user_id）绕不开这一层。
            ok, wait = self.peer_buckets.consume(peer_address(context))
        if not ok:
            retry_after = max(1, int(wait) + 1)
            context.abort(
                grpc.StatusCode.RESOURCE_EXHAUSTED,
                f"Rate limit exceeded. Retry after {retry_after}s",
            )
