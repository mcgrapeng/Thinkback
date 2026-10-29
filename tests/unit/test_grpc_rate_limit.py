"""gRPC 面的限流：与 HTTP 面同一套限额、同一把分桶口径。

HTTP 的 ``/v2/memory/*`` 走限流中间件，gRPC 面不走的话，换个协议就绕开了
限流 —— 同一个服务、同一份写入成本（一次写入会触发 L1/L2/L3 沉淀，可能
产生 LLM 调用），两条面给的限额必须一样。

分桶口径也必须一样：按「谁在用」（namespace + user_id）分，不按 IP 分 ——
服务化接入时所有调用方都从同一个出口 IP 出来，按 IP 分桶等于全服务共享
一份额度。
"""

from __future__ import annotations

from typing import cast

import grpc
import pytest

from thinkback.domain.keys import DEFAULT_NAMESPACE
from thinkback.memory.backends import FakeMemoryBackend
from thinkback.memory.repositories import InMemoryMemoryRepository
from thinkback.memory.service import MemoryService
from thinkback.rpc import memory_pb2 as pb
from thinkback.rpc.ratelimit import GrpcRateLimiter, caller_identity
from thinkback.rpc.servicer import MemoryServicer


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


def _servicer(per_minute: int = 1, burst: int = 2) -> MemoryServicer:
    service = MemoryService(
        repository=InMemoryMemoryRepository(),
        backend=FakeMemoryBackend(),
        l3_write_mode="sync",
    )
    return MemoryServicer(service, limiter=GrpcRateLimiter(per_minute, burst))


def test_the_caller_identity_is_namespace_plus_user_id() -> None:
    """与 HTTP 面的 ``_caller_identity`` 同一口径：谁在用，不是从哪出来的。"""
    assert caller_identity(pb.RecallRequest(user_id="u1", namespace="ns")) == "ns:u1"


def test_an_empty_namespace_falls_back_to_the_default_scope() -> None:
    """proto3 标量没有 presence：空串 = 没传 → 归一到默认作用域。

    归一错了就是两把桶：同一个人被当成两个调用方，各拿一份额度。
    """
    assert caller_identity(pb.RecallRequest(user_id="u1")) == f"{DEFAULT_NAMESPACE}:u1"


def test_a_request_without_a_user_id_has_no_identity() -> None:
    """查任务、看后台状态这类请求没有 user_id → 退回对端 IP 分桶。"""
    assert caller_identity(pb.GetTaskRequest(task_id="t1")) == ""


def test_exceeding_the_limit_is_refused_with_resource_exhausted() -> None:
    limiter = GrpcRateLimiter(rate_limit_per_minute=1, burst=2)
    context = _FakeContext()
    request = pb.RecallRequest(user_id="u1", namespace="ns")

    limiter.check(request, context)
    limiter.check(request, context)
    with pytest.raises(grpc.RpcError):
        limiter.check(request, context)

    assert context.aborted is not None
    assert context.aborted[0] == grpc.StatusCode.RESOURCE_EXHAUSTED


def test_different_callers_do_not_share_a_bucket() -> None:
    """同 IP 下不同租户各用自己的额度 —— 否则一个租户把别人挤到超限。"""
    limiter = GrpcRateLimiter(rate_limit_per_minute=1, burst=1)
    context = _FakeContext()

    limiter.check(pb.RecallRequest(user_id="u1", namespace="ns"), context)
    with pytest.raises(grpc.RpcError):
        limiter.check(pb.RecallRequest(user_id="u1", namespace="ns"), context)

    limiter.check(pb.RecallRequest(user_id="u2", namespace="ns"), context)


def test_requests_without_identity_share_the_peer_bucket() -> None:
    """取不到调用方身份 → 按对端 IP。与 HTTP 面取不到身份时的行为一致。"""
    limiter = GrpcRateLimiter(rate_limit_per_minute=1, burst=1)
    context = _FakeContext()
    request = pb.GetTaskRequest(task_id="t1")

    limiter.check(request, context)
    with pytest.raises(grpc.RpcError):
        limiter.check(request, context)


def test_the_limit_comes_from_the_same_settings_as_the_http_plane() -> None:
    """两个面读同一份配置 —— 限额漂了就成了「换个协议就绕开」。"""
    from thinkback.infra.config import settings

    limiter = GrpcRateLimiter.from_settings()

    assert limiter.rate_limit_per_minute == settings.memory_rate_limit_per_minute
    assert limiter.burst == settings.memory_rate_limit_burst


def test_a_rate_limit_refusal_is_not_turned_into_an_internal_error() -> None:
    """abort 必须落在 try 之外。

    落在 try 里，``context.abort`` 抛出的 RpcError 会被外层
    ``except Exception`` 吞掉、再被映射成 INTERNAL —— 调用方看不出这是
    「退避一下再来」，只会当成服务端故障。
    """
    servicer = _servicer(per_minute=1, burst=1)
    context = _FakeContext()
    request = pb.RecallRequest(user_id="u1", namespace="ns", session_id="s1", query="hi")

    ctx = cast(grpc.ServicerContext, context)
    servicer.Recall(request, ctx)
    with pytest.raises(grpc.RpcError):
        servicer.Recall(request, ctx)

    assert context.aborted is not None
    assert context.aborted[0] == grpc.StatusCode.RESOURCE_EXHAUSTED


def test_every_rpc_method_goes_through_the_rate_limiter() -> None:
    """每个对外的 RPC 方法都必须先过限流。

    漏掉一个就是一条绕过限流的路，而且**静默**：别的方法有闸门，这个没有，
    看上去一切正常。上一轮就漏过一个（AppendEvent）—— 手工往每个方法里塞
    一行，靠的是「记得别漏」，所以这里把它钉成断言。
    """
    import ast
    import inspect
    import textwrap

    source = textwrap.dedent(inspect.getsource(MemoryServicer))
    tree = ast.parse(source)
    klass = next(node for node in tree.body if isinstance(node, ast.ClassDef))

    missing: list[str] = []
    for node in klass.body:
        if not isinstance(node, ast.FunctionDef) or node.name.startswith("_"):
            continue
        checks = [
            child
            for child in ast.walk(node)
            if isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr == "check"
        ]
        if len(checks) != 1:
            missing.append(f"{node.name}({len(checks)})")

    assert not missing, f"没有过限流（或过了多次）的 RPC 方法: {missing}"


def test_different_peer_ports_share_one_bucket() -> None:
    """``context.peer()`` 带的是客户端临时端口 —— 每条连接都不同。

    带端口分桶等于每条连接一把新桶，限额永远咬不到：限流形同虚设。
    """
    limiter = GrpcRateLimiter(rate_limit_per_minute=1, burst=1)

    limiter.check(pb.GetTaskRequest(task_id="t1"), _FakeContext("ipv4:10.0.0.1:5555"))
    with pytest.raises(grpc.RpcError):
        limiter.check(pb.GetTaskRequest(task_id="t1"), _FakeContext("ipv4:10.0.0.1:6666"))


def test_rotating_the_caller_key_cannot_escape_the_peer_ceiling() -> None:
    """轮换 user_id 换的是「每调用方」那把桶 —— 按对端地址的天花板换不掉。

    分桶键是调用方给的，不压一层按 IP 的总闸，限额就只是个摆设：换个
    user_id 就是一把满额的新桶。
    """
    limiter = GrpcRateLimiter(rate_limit_per_minute=1, burst=1)
    context = _FakeContext(peer="ipv4:10.0.0.1:5555")

    # 天花板是每调用方限额的 20 倍 → 这里能过 20 次
    for index in range(20):
        limiter.check(pb.RecallRequest(user_id=f"u{index}", namespace="ns"), context)

    with pytest.raises(grpc.RpcError):
        limiter.check(pb.RecallRequest(user_id="u999", namespace="ns"), context)


def test_the_bucket_map_is_bounded() -> None:
    """分桶键是调用方可控的 —— 不封顶就是一张能无限长大的表。

    拿随机 user_id 打几次就能把它撑爆。
    """
    limiter = GrpcRateLimiter(rate_limit_per_minute=600, burst=60, max_buckets=10)
    context = _FakeContext()

    for index in range(100):
        limiter.check(pb.RecallRequest(user_id=f"u{index}", namespace="ns"), context)

    assert limiter.buckets.bucket_count() <= 10
