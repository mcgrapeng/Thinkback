"""gRPC 面限流的**精确值**契约。

gRPC 面不走 HTTP 的限流中间件 —— 同一个服务、同一份写入成本，两条面给的
限额不一样，gRPC 面就成了绕开限流的口子。所以这里的用例钉的是：

- 调用方标识的口径（namespace + user_id，与 HTTP 面一致）；
- 超限时 abort 的状态码与「等多少秒」；
- 分桶：不同调用方各有一份额度，取不到调用方标识时退回对端地址。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import grpc
import pytest

from thinkback.domain.keys import DEFAULT_NAMESPACE
from thinkback.infra import token_bucket as token_bucket_module
from thinkback.rpc.ratelimit import GrpcRateLimiter, caller_identity, peer_address


class _Clock:
    """可冻结的时钟。"""

    def __init__(self, now: float) -> None:
        self.now = now

    def time(self) -> float:
        return self.now


class _FakeContext:
    """够用的 ``grpc.ServicerContext`` 替身：peer + abort。"""

    def __init__(self, peer: str = "ipv4:127.0.0.1:5555") -> None:
        self.aborted: tuple[grpc.StatusCode, str] | None = None
        self._peer = peer

    def peer(self) -> str:
        return self._peer

    def abort(self, code: grpc.StatusCode, details: str) -> None:
        self.aborted = (code, details)
        raise grpc.RpcError(details)


class _BrokenContext:
    """``peer()`` 抛异常的替身。"""

    def peer(self) -> str:
        raise RuntimeError("no peer")


def _ctx(peer: str = "ipv4:127.0.0.1:5555") -> grpc.ServicerContext:
    """把替身当成 ``grpc.ServicerContext`` 用（类型上没有别的办法）。"""
    return cast(grpc.ServicerContext, _FakeContext(peer))


def _frozen_clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    clock = _Clock(1_000.0)
    monkeypatch.setattr(token_bucket_module, "time", clock)
    return clock


# ---------------------------------------------------------------------------
# 调用方标识
# ---------------------------------------------------------------------------


def test_the_caller_identity_is_the_namespace_and_the_user() -> None:
    """标识是「namespace:user_id」—— 与 HTTP 面同一口径。

    少了 namespace 这一半，两个命名空间的同一个 user_id 会被算成同一个人，
    于是共用一把桶、互相挤兑。
    """
    request = SimpleNamespace(user_id="u1", namespace="ns")

    assert caller_identity(request) == "ns:u1"


def test_a_missing_namespace_falls_back_to_the_default() -> None:
    """空 namespace 归一到默认值 —— proto3 标量没有 presence，空串 = 没传。"""
    request = SimpleNamespace(user_id="u1", namespace="")

    assert caller_identity(request) == f"{DEFAULT_NAMESPACE}:u1"


def test_a_whitespace_only_user_id_has_no_identity() -> None:
    """只有空格的 user_id 也算「没有调用方」—— 退回按对端地址分桶。

    空白串归一错了 → 同一个调用方被算成两个（一个按空格串、一个按对端地址），
    等于凭空多出一份额度。
    """
    request = SimpleNamespace(user_id="   ", namespace="ns")

    assert caller_identity(request) == ""


# ---------------------------------------------------------------------------
# 对端地址
# ---------------------------------------------------------------------------


def test_the_peer_address_keeps_the_address_and_drops_the_port() -> None:
    """对端地址只取**最后一个冒号之前**的部分（端口是临时的，每条连接都不同）。"""
    assert peer_address(cast(grpc.ServicerContext, _FakeContext("ipv4:1.2.3.4:5678"))) == (
        "ipv4:1.2.3.4"
    )
    # 没有端口时按同样的规则取 —— 这条把「地址」两个字取窄了，
    # 但对端地址本来就是「最后一个冒号之前的那一段」。
    assert peer_address(cast(grpc.ServicerContext, _FakeContext("ipv4:1.2.3.4"))) == "ipv4"
    # 完全没有冒号 → 整段就是地址
    assert peer_address(cast(grpc.ServicerContext, _FakeContext("/tmp/grpc.sock"))) == (
        "/tmp/grpc.sock"
    )


def test_an_unusable_peer_falls_back_to_one_shared_bucket() -> None:
    """拿不到对端地址时退化成统一的桶 —— 不能因为取不到就放行。"""
    assert peer_address(cast(grpc.ServicerContext, _FakeContext(""))) == "unknown"
    assert peer_address(cast(grpc.ServicerContext, _BrokenContext())) == "unknown"


# ---------------------------------------------------------------------------
# 限流器
# ---------------------------------------------------------------------------


def test_the_limiter_aborts_with_the_exact_retry_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    """超限要 abort，状态码与「等多少秒」都是契约。

    RESOURCE_EXHAUSTED 与 HTTP 面的 429 TB-1007 是对齐的；重试秒数是调用方
    唯一的退避依据 —— 算少 → 立刻重试 → 更凶；算多 → 白等。
    """
    _frozen_clock(monkeypatch)
    limiter = GrpcRateLimiter(rate_limit_per_minute=1, burst=1)
    context = _FakeContext()
    request = SimpleNamespace(user_id="u1", namespace="ns")

    limiter.check(request, cast(grpc.ServicerContext, context))  # 第一个请求通过

    with pytest.raises(grpc.RpcError):
        limiter.check(request, cast(grpc.ServicerContext, context))

    assert context.aborted is not None
    code, details = context.aborted
    assert code == grpc.StatusCode.RESOURCE_EXHAUSTED
    # 每分钟 1 个 → 等一个令牌要 60 秒；取整 + 1 → 61
    assert details == "Rate limit exceeded. Retry after 61s"


def test_two_callers_have_separate_grpc_buckets(monkeypatch: pytest.MonkeyPatch) -> None:
    """两个调用方各有一份额度 —— gRPC 面不能把所有人算成一个桶。"""
    _frozen_clock(monkeypatch)
    limiter = GrpcRateLimiter(rate_limit_per_minute=1, burst=1)
    context = _ctx()

    limiter.check(SimpleNamespace(user_id="u1", namespace="ns"), context)
    with pytest.raises(grpc.RpcError):
        limiter.check(SimpleNamespace(user_id="u1", namespace="ns"), context)
    # 另一个调用方不受影响
    limiter.check(SimpleNamespace(user_id="u2", namespace="ns"), context)


def test_a_call_without_a_user_id_falls_back_to_the_peer_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取不到 user_id 的请求（查任务、看后台状态）退回按对端地址分桶。

    不同对端地址 → 各有一份额度（不能退化成全局一把桶）。
    """
    _frozen_clock(monkeypatch)
    limiter = GrpcRateLimiter(rate_limit_per_minute=1, burst=1)

    limiter.check(SimpleNamespace(user_id="", namespace="ns"), _ctx("ipv4:1.2.3.4:1111"))
    limiter.check(SimpleNamespace(user_id="", namespace="ns"), _ctx("ipv4:5.6.7.8:2222"))


def test_the_grpc_peer_ceiling_is_per_address(monkeypatch: pytest.MonkeyPatch) -> None:
    """按对端地址的天花板也是**每个地址一份**。

    每调用方限额 1/分钟、突发 1；天花板是它的 20 倍 → 同一个对端地址打满
    20 个之后，换一个对端地址的请求仍然过得去（不能连坐）。
    """
    _frozen_clock(monkeypatch)
    limiter = GrpcRateLimiter(rate_limit_per_minute=1, burst=1)

    for index in range(20):
        limiter.check(
            SimpleNamespace(user_id=f"u{index}", namespace="ns"), _ctx("ipv4:1.2.3.4:1111")
        )
    # 另一个对端地址 → 另一份天花板
    limiter.check(SimpleNamespace(user_id="other", namespace="ns"), _ctx("ipv4:5.6.7.8:2222"))


def test_the_limiter_keeps_its_own_bucket_cap() -> None:
    """限流器自己的桶表也有上限 —— 轮换 key 不能把内存撑爆。"""
    limiter = GrpcRateLimiter(rate_limit_per_minute=60, burst=5, max_buckets=7)

    assert limiter.buckets.max_buckets == 7
    assert limiter.peer_buckets.max_buckets == 7


def test_the_default_bucket_cap_of_the_limiter_is_ten_thousand() -> None:
    """默认上限就是一万把桶。"""
    limiter = GrpcRateLimiter(rate_limit_per_minute=60, burst=5)

    assert limiter.buckets.max_buckets == 10_000


def test_the_limiter_refills_at_the_configured_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    """gRPC 面的回填速度与 HTTP 面同一口径：每分钟配额 ÷ 60。"""
    _frozen_clock(monkeypatch)
    limiter = GrpcRateLimiter(rate_limit_per_minute=60, burst=1)
    context = _FakeContext()
    request = SimpleNamespace(user_id="u1", namespace="ns")

    limiter.check(request, cast(grpc.ServicerContext, context))  # 新桶是满的
    with pytest.raises(grpc.RpcError):
        limiter.check(request, cast(grpc.ServicerContext, context))

    assert context.aborted is not None
    # 每分钟 60 个 → 每秒 1 个 → 等一个令牌要等 1 秒；取整 + 1 → 2
    assert context.aborted[1] == "Rate limit exceeded. Retry after 2s"
