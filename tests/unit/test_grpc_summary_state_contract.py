"""gRPC 响应里的 ``summary_state``：**有值要带出去，没值不能带键**。

``Delete`` / ``Suppress`` / ``Erase`` / ``UpdateMemory`` 四个响应都带一个可选的
``summary_state``（删除/编辑后 L2 摘要的状态）。服务端把它翻译成 proto 枚举：

```python
if resp.summary_state is not None:
    kw["summary_state"] = _SUMMARY_STATE_TO_PB[resp.summary_state]
```

两条路都要验证：

- **有值** → proto 响应里必须带上，且值对应；
- **没值**（``None``）→ proto 响应里**不能有这个字段**。

第二条不是「无所谓」：proto3 里没设置的可选字段读出来是默认值（枚举 0），
而 ``SUMMARY_STATE_ACTIVE`` 恰好就是 0 —— 所以「没值却带了字段」和
「没值且没字段」在客户端读起来**完全一样**，但服务端多发了一个键。
反过来「有值却漏了」客户端会以为是 ACTIVE（摘要新鲜），
而实际可能是 DIRTY（摘要已脏，要重算）—— **语义反了，客户端会据此
决定用不用旧摘要**。

这些分支此前从未被走到：单测里的响应**总是**带 ``summary_state``，
``None`` 那条路一次都没进过。
"""

from __future__ import annotations

from typing import Any

import pytest

from thinkback.domain.enums import SummaryState
from thinkback.rpc import memory_pb2 as pb
from thinkback.rpc import servicer


class _Stub:
    """替换 ``MemoryServicer`` 里的服务替身，返回固定的响应。"""

    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls: list[Any] = []

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401
        def _call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, args, kwargs))
            return self.response

        return _call


def _servicer(response: Any) -> tuple[Any, _Stub]:
    stub = _Stub(response)
    instance = servicer.MemoryServicer(stub)  # type: ignore[arg-type]
    return instance, stub


def _delete_request() -> pb.DeleteMemoryRequest:
    return pb.DeleteRequest(
        request_id="r1",
        user_id="u1",
        namespace="thinkback",
        memory_id="m1",
        operation_id="op1",
        scope=pb.DELETE_SCOPE_MEMORY,
    )


def _suppress_request() -> pb.SuppressMemoryRequest:
    return pb.SuppressRequest(
        request_id="r1",
        user_id="u1",
        namespace="thinkback",
        memory_id="m1",
        operation_id="op1",
        scope=pb.DELETE_SCOPE_MEMORY,
    )


def _erase_request() -> pb.EraseMemoryRequest:
    return pb.EraseRequest(
        request_id="r1",
        user_id="u1",
        namespace="thinkback",
        operation_id="op1",
        scope=pb.DELETE_SCOPE_ALL,
    )


def _update_request() -> pb.UpdateMemoryRequest:
    return pb.UpdateMemoryRequest(
        request_id="r1",
        user_id="u1",
        namespace="thinkback",
        memory_id="m1",
        operation_id="op1",
        content="新的内容",
    )


def test_delete_carries_the_summary_state_when_present() -> None:
    """有 summary_state → proto 响应里带上，且值对应。"""
    from thinkback.memory.schemas import DeleteMemoryResponse

    response = DeleteMemoryResponse(
        status="completed",
        task_id="t1",
        affected_memories=1,
        summary_state=SummaryState.DIRTY,
    )
    instance, _stub = _servicer(response)
    result = instance.Delete(_delete_request(), _Ctx())
    assert result.summary_state == pb.SUMMARY_STATE_DIRTY


def test_delete_omits_the_summary_state_when_absent() -> None:
    """没 summary_state → proto 响应里**不能有这个字段**。

    服务端多发一个键，客户端读出来是默认值 0（= ACTIVE），
    与「真的没这个状态」混同 —— 语义被抹平。
    """
    from thinkback.memory.schemas import DeleteMemoryResponse

    response = DeleteMemoryResponse(status="completed", task_id="t1", affected_memories=1)
    assert response.summary_state is None

    instance, _stub = _servicer(response)
    result = instance.Delete(_delete_request(), _Ctx())
    assert not result.HasField("summary_state"), "没状态时却发了一个 summary_state 字段"


def test_suppress_carries_the_summary_state_when_present() -> None:
    """Suppress 路径同样要带。"""
    from thinkback.memory.schemas import DeleteMemoryResponse

    response = DeleteMemoryResponse(
        status="completed",
        task_id="t1",
        affected_memories=1,
        summary_state=SummaryState.ACTIVE,
    )
    instance, _stub = _servicer(response)
    result = instance.Suppress(_suppress_request(), _Ctx())
    assert result.summary_state == pb.SUMMARY_STATE_ACTIVE


def test_suppress_omits_the_summary_state_when_absent() -> None:
    """Suppress 路径的「没值就不带」。"""
    from thinkback.memory.schemas import DeleteMemoryResponse

    response = DeleteMemoryResponse(status="completed", task_id="t1", affected_memories=1)
    instance, _stub = _servicer(response)
    result = instance.Suppress(_suppress_request(), _Ctx())
    assert not result.HasField("summary_state")


def test_erase_carries_the_summary_state_when_present() -> None:
    """Erase 路径同样要带。"""
    from thinkback.memory.schemas import DeleteMemoryResponse

    response = DeleteMemoryResponse(
        status="completed",
        task_id="t1",
        affected_memories=2,
        summary_state=SummaryState.DIRTY,
    )
    instance, _stub = _servicer(response)
    result = instance.Erase(_erase_request(), _Ctx())
    assert result.summary_state == pb.SUMMARY_STATE_DIRTY


def test_erase_omits_the_summary_state_when_absent() -> None:
    """Erase 路径的「没值就不带」。"""
    from thinkback.memory.schemas import DeleteMemoryResponse

    response = DeleteMemoryResponse(status="completed", task_id="t1", affected_memories=2)
    instance, _stub = _servicer(response)
    result = instance.Erase(_erase_request(), _Ctx())
    assert not result.HasField("summary_state")


def test_update_carries_the_summary_state_when_present() -> None:
    """UpdateMemory 路径同样要带。"""
    from thinkback.memory.schemas import UpdateMemoryResponse

    response = UpdateMemoryResponse(
        status="completed",
        task_id="t1",
        memory=_managed_item(),
        summary_state=SummaryState.DIRTY,
    )
    instance, _stub = _servicer(response)
    result = instance.UpdateMemory(_update_request(), _Ctx())
    assert result.summary_state == pb.SUMMARY_STATE_DIRTY


def test_update_omits_the_summary_state_when_absent() -> None:
    """UpdateMemory 路径的「没值就不带」。"""
    from thinkback.memory.schemas import UpdateMemoryResponse

    response = UpdateMemoryResponse(status="completed", task_id="t1", memory=_managed_item())
    instance, _stub = _servicer(response)
    result = instance.UpdateMemory(_update_request(), _Ctx())
    assert not result.HasField("summary_state")


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (SummaryState.ACTIVE, pb.SUMMARY_STATE_ACTIVE),
        (SummaryState.STALE, pb.SUMMARY_STATE_STALE),
        (SummaryState.DIRTY, pb.SUMMARY_STATE_DIRTY),
        (SummaryState.REBUILDING, pb.SUMMARY_STATE_REBUILDING),
    ],
)
def test_every_summary_state_maps_to_its_proto_value(state: SummaryState, expected: int) -> None:
    """四个状态都要映到正确的 proto 值（不是「有值就行」）。"""
    from thinkback.memory.schemas import DeleteMemoryResponse

    response = DeleteMemoryResponse(
        status="completed",
        task_id="t1",
        affected_memories=1,
        summary_state=state,
    )
    instance, _stub = _servicer(response)
    result = instance.Delete(_delete_request(), _Ctx())
    assert result.summary_state == expected


def _managed_item() -> Any:
    from thinkback.domain.enums import DataClassification, MemoryStatus, SourceType
    from thinkback.memory.schemas import ManagedMemoryItem

    return ManagedMemoryItem(
        memory_id="m1",
        content="内容",
        status=MemoryStatus.ACTIVE,
        source_type=SourceType.CHAT_ROUND,
        data_classification=DataClassification.PERSONAL,
        backend_categories=[],
    )


class _Ctx:
    """最小 gRPC 上下文替身。"""

    def invocation_metadata(self) -> tuple[tuple[str, str], ...]:
        return ()

    def peer(self) -> str:
        return "ipv4:127.0.0.1:1234"

    def abort(self, code: int, details: str) -> None:
        raise RuntimeError(f"grpc abort: {code} {details}")
