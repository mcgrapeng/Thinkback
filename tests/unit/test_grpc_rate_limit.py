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

    def __init__(self) -> None:
        self.aborted: tuple[grpc.StatusCode, str] | None = None

    def peer(self) -> str:
        return "ipv4:127.0.0.1:5555"

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
