"""gRPC 传输层转换的契约（rpc/converters.py 进变异范围后的补齐）。

proto3 的标量字段没有 presence：分不清「客户端没传」与「客户端传了 0」。
converters 用约定补齐 —— 0 一律按「没传」处理，越界值直接报错，**不静默夹取**
（静默夹取会让调用方以为自己拿到了 50 条，实际拿到的是被悄悄改过的值）。

这些转换此前只有端到端 gRPC 用例覆盖，且只走了「全传、都在范围内」这一条
路径：阈值边界、越界报错、可选字段的 presence 语义全都没被钉住。
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from thinkback.memory.schemas import (
    DataClassification,
    ListMemoriesResponse,
    ManagedMemoryItem,
    MemoryStatus,
    MemoryType,
    OperationType,
    RebuildMemoryResponse,
    SourceType,
    TaskResponse,
    TaskStatus,
)
from thinkback.rpc import memory_pb2 as pb
from thinkback.rpc.converters import (
    _enum_value,
    list_response_to_pb,
    rebuild_response_to_pb,
    resolve_l3_limit,
    resolve_l3_score_threshold,
    resolve_token_budget,
    task_response_to_pb,
    to_append_event_request,
    to_delete_request,
    to_rebuild_request,
    to_recall_request,
    to_update_request,
)

# ---------------------------------------------------------------------------
# 阈值：0 = 没传，越界 = 报错（不夹取）
# ---------------------------------------------------------------------------


def test_l3_limit_zero_means_not_passed() -> None:
    """proto3 的 0 是「没传」的约定 —— 取默认值 5，而不是 0。"""
    assert resolve_l3_limit(0) == 5


def test_l3_limit_keeps_an_explicit_value() -> None:
    assert resolve_l3_limit(1) == 1
    assert resolve_l3_limit(50) == 50


def test_l3_limit_rejects_out_of_range_instead_of_clamping() -> None:
    """越界必须报错：夹取会让调用方拿到一个自己没要过的值。"""
    with pytest.raises(ValueError, match="l3_limit"):
        resolve_l3_limit(51)
    with pytest.raises(ValueError, match="l3_limit"):
        resolve_l3_limit(-1)


def test_l3_score_threshold_zero_means_not_passed() -> None:
    """0.0 是「没传」—— 取 0.5，而不是「不过滤」。"""
    assert resolve_l3_score_threshold(0.0) == 0.5


def test_l3_score_threshold_keeps_an_explicit_value() -> None:
    assert resolve_l3_score_threshold(0.5) == 0.5
    assert resolve_l3_score_threshold(1.0) == 1.0


def test_l3_score_threshold_rejects_out_of_range() -> None:
    with pytest.raises(ValueError, match="l3_score_threshold"):
        resolve_l3_score_threshold(1.5)
    with pytest.raises(ValueError, match="l3_score_threshold"):
        resolve_l3_score_threshold(-0.1)


def test_token_budget_zero_means_not_passed() -> None:
    assert resolve_token_budget(0) == 1200


def test_token_budget_keeps_the_boundary_values() -> None:
    """100 与 12000 是闭区间端点 —— 端点必须放行。"""
    assert resolve_token_budget(100) == 100
    assert resolve_token_budget(12000) == 12000


def test_token_budget_rejects_out_of_range() -> None:
    with pytest.raises(ValueError, match="token_budget"):
        resolve_token_budget(99)
    with pytest.raises(ValueError, match="token_budget"):
        resolve_token_budget(12001)


def test_an_unknown_proto_enum_becomes_a_value_error() -> None:
    """未知枚举要转成 ValueError。

    直接查字典抛的是 KeyError —— 它既不是 ValueError 也不是 RuntimeError，
    一路落到「未知异常 → INTERNAL」：客户端看到 INTERNAL 会当成服务端故障去
    重试，实际是它自己发错了枚举值。报错文案要带上字段名才知道错在哪。
    """
    with pytest.raises(ValueError, match=r"unknown role enum value"):
        _enum_value({1: "user"}, 99, "role")


def test_an_unknown_role_names_the_field_in_the_error() -> None:
    """事件里的未知 role —— 报错文案要指得出是哪个字段错了。"""
    with pytest.raises(ValueError, match=r"unknown role enum value"):
        to_append_event_request(
            pb.AppendEventRequest(
                request_id="r1",
                user_id="u1",
                session_id="s1",
                event=pb.SourceEvent(
                    event_id="e1",
                    source="chat_round",
                    role=99,  # type: ignore[arg-type]
                    payload="hello",
                    occurred_at="2026-01-01T00:00:00+00:00",
                ),
            )
        )


def test_an_unknown_delete_scope_names_the_field_in_the_error() -> None:
    with pytest.raises(ValueError, match=r"unknown delete scope enum value"):
        to_delete_request(
            pb.DeleteRequest(
                request_id="r1",
                user_id="u1",
                scope=99,
                operation_id="op1",  # type: ignore[arg-type]
            )
        )


# ---------------------------------------------------------------------------
# 请求转换：字段不许丢
# ---------------------------------------------------------------------------


def test_recall_request_carries_the_resolved_thresholds() -> None:
    """召回请求里的三个阈值必须过一遍 resolver —— gRPC 与 HTTP 要同口径。"""
    req = to_recall_request(
        pb.RecallRequest(
            user_id="u1",
            session_id="s1",
            query="hello",
            l3_limit=0,
            l3_score_threshold=0.0,
            token_budget=0,
        )
    )

    assert req.l3_limit == 5
    assert req.l3_score_threshold == 0.5
    assert req.token_budget == 1200


def test_recall_request_keeps_an_explicit_threshold() -> None:
    """显式给的阈值要原样传下去 —— 不能被默认值覆盖。"""
    req = to_recall_request(
        pb.RecallRequest(
            user_id="u1",
            session_id="s1",
            query="hello",
            l3_limit=3,
            l3_score_threshold=0.75,
            token_budget=2000,
        )
    )

    assert req.l3_limit == 3
    assert req.l3_score_threshold == 0.75
    assert req.token_budget == 2000


def test_an_empty_namespace_falls_back_to_the_default() -> None:
    """proto3 空串按「没传」处理 —— 落到默认作用域，不落空串。"""
    req = to_recall_request(pb.RecallRequest(user_id="u1", session_id="s1", query="q"))
    assert req.namespace == "thinkback"

    scoped = to_recall_request(
        pb.RecallRequest(user_id="u1", session_id="s1", query="q", namespace="proj")
    )
    assert scoped.namespace == "proj"


def test_append_event_request_carries_the_optional_fields() -> None:
    """可选字段在位就要带过去：subject_ref / memory_type / round_index。"""
    req = to_append_event_request(
        pb.AppendEventRequest(
            request_id="r1",
            user_id="u1",
            session_id="s1",
            namespace="proj",
            memory_type="state",
            round_index=7,
            metadata={"k": "v"},
            event=pb.SourceEvent(
                event_id="e1",
                source="tool_result",
                role=pb.MESSAGE_ROLE_USER,
                payload="hello",
                occurred_at="2026-01-01T00:00:00+00:00",
                subject_ref="u1",
                provenance={"trace": "abc"},
            ),
        )
    )

    assert req.namespace == "proj"
    assert req.memory_type == "state"
    assert req.round_index == 7
    assert req.event.subject_ref == "u1"
    assert req.event.role == "user"
    assert req.event.event_id == "e1"
    assert req.event.source == "tool_result"
    assert req.event.provenance == {"trace": "abc"}
    assert req.metadata == {"k": "v"}


def test_append_event_request_leaves_absent_optional_fields_empty() -> None:
    """没传的 optional 字段要是 None —— 不是空串、不是 0。"""
    req = to_append_event_request(
        pb.AppendEventRequest(
            request_id="r1",
            user_id="u1",
            session_id="s1",
            event=pb.SourceEvent(
                event_id="e1",
                source="chat_round",
                role=pb.MESSAGE_ROLE_USER,
                payload="hello",
                occurred_at="2026-01-01T00:00:00+00:00",
            ),
        )
    )

    assert req.event.subject_ref is None
    assert req.memory_type is None
    assert req.round_index is None
    assert req.namespace == "thinkback"


def test_delete_request_maps_the_scope_and_keeps_the_ids() -> None:
    """删除范围是枚举 —— 映射错了就删错东西。"""
    single = to_delete_request(
        pb.DeleteRequest(
            request_id="r1",
            user_id="u1",
            scope=pb.DELETE_SCOPE_MEMORY,
            operation_id="op1",
            memory_id="m1",
            namespace="proj",
        )
    )
    assert single.scope == "memory"
    assert single.memory_id == "m1"
    assert single.session_id is None
    assert single.operation_id == "op1"
    assert single.namespace == "proj"

    session = to_delete_request(
        pb.DeleteRequest(
            request_id="r2",
            user_id="u1",
            scope=pb.DELETE_SCOPE_SESSION,
            operation_id="op2",
            session_id="s1",
        )
    )
    assert session.scope == "session"
    assert session.session_id == "s1"


def test_delete_request_leaves_absent_ids_empty() -> None:
    req = to_delete_request(
        pb.DeleteRequest(
            request_id="r1", user_id="u1", scope=pb.DELETE_SCOPE_ALL, operation_id="op1"
        )
    )

    assert req.memory_id is None
    assert req.session_id is None


def test_update_request_maps_the_memory_type_enum() -> None:
    """内置类型走枚举，自定义类型走 memory_type_name —— 两条路都得带过去。"""
    typed = to_update_request(
        pb.UpdateMemoryRequest(
            request_id="r1",
            user_id="u1",
            operation_id="op1",
            memory_id="m1",
            content="c",
            memory_type=pb.MEMORY_TYPE_PREFERENCE,
            namespace="proj",
        )
    )
    assert typed.memory_type == MemoryType.PREFERENCE
    assert typed.memory_type_name is None
    assert typed.namespace == "proj"

    named = to_update_request(
        pb.UpdateMemoryRequest(
            request_id="r1",
            user_id="u1",
            operation_id="op1",
            memory_id="m1",
            content="c",
            memory_type_name="state",
        )
    )
    assert named.memory_type is None
    assert named.memory_type_name == "state"


def test_rebuild_request_keeps_the_optional_fields() -> None:
    req = to_rebuild_request(
        pb.RebuildRequest(
            request_id="r1",
            user_id="u1",
            operation_id="op1",
            session_id="s1",
            history_version="3",
            rebuild_l2=False,
            rebuild_l3=False,
            namespace="proj",
        )
    )

    assert req.session_id == "s1"
    assert req.history_version == "3"
    assert req.rebuild_l2 is False
    assert req.rebuild_l3 is False
    assert req.namespace == "proj"

    empty = to_rebuild_request(pb.RebuildRequest(request_id="r1", user_id="u1", operation_id="op2"))
    assert empty.session_id is None
    assert empty.history_version is None


# ---------------------------------------------------------------------------
# 响应转换
# ---------------------------------------------------------------------------


def test_task_response_carries_the_last_error() -> None:
    """任务失败时的错误文案要带回去 —— 治理台靠它显示失败原因。"""
    failed = task_response_to_pb(
        TaskResponse(
            task_id="t1",
            request_id="r1",
            op_type=OperationType.WRITE_ROUND,
            status=TaskStatus.FAILED,
            scope={"user_id": "u1"},
            last_error="backend down",
            result={},
        )
    )
    assert failed.last_error == "backend down"

    ok = task_response_to_pb(
        TaskResponse(
            task_id="t2",
            request_id="r2",
            op_type=OperationType.WRITE_ROUND,
            status=TaskStatus.COMPLETED,
            scope={"user_id": "u1"},
            result={},
        )
    )
    assert not ok.HasField("last_error")


def test_task_response_json_encodes_the_scope_and_the_result() -> None:
    """scope / result 在 proto 里是字符串 —— 编码错了治理台就解不出来。"""
    resp = task_response_to_pb(
        TaskResponse(
            task_id="t1",
            request_id="r1",
            op_type=OperationType.WRITE_ROUND,
            status=TaskStatus.COMPLETED,
            scope={"user_id": "u1"},
            result={"affected_count": 3},
            retry_count=2,
        )
    )

    assert json.loads(resp.scope) == {"user_id": "u1"}
    assert json.loads(resp.result) == {"affected_count": 3}
    assert resp.retry_count == 2


def test_task_response_encodes_values_json_cannot_represent() -> None:
    """scope / result 里可能有非 JSON 原生类型（时间、对象）。

    编码时给了 ``default=str`` 就是为这个准备的 —— 去掉它就变成 TypeError，
    整个任务查询接口跟着 500。
    """
    resp = task_response_to_pb(
        TaskResponse(
            task_id="t1",
            request_id="r1",
            op_type=OperationType.WRITE_ROUND,
            status=TaskStatus.COMPLETED,
            scope={"user_id": "u1", "at": datetime(2026, 1, 1)},
            result={"items": [datetime(2026, 1, 1)]},
        )
    )

    assert json.loads(resp.scope)["user_id"] == "u1"
    assert "2026-01-01" in json.loads(resp.scope)["at"]
    assert "2026-01-01" in json.loads(resp.result)["items"][0]


def test_rebuild_response_reports_which_layers_were_rebuilt() -> None:
    """rebuilt_l2 / rebuilt_l3 是「这次触发了哪一层」—— 丢了就看不出重建范围。"""
    both = rebuild_response_to_pb(
        RebuildMemoryResponse(
            status="completed",
            task_id="t1",
            rebuilt_l2=True,
            rebuilt_l3=True,
        )
    )

    assert both.status == "completed"
    assert both.task_id == "t1"
    assert both.rebuilt_l2 is True
    assert both.rebuilt_l3 is True

    neither = rebuild_response_to_pb(
        RebuildMemoryResponse(
            status="completed",
            task_id="t2",
            rebuilt_l2=False,
            rebuilt_l3=False,
        )
    )
    assert neither.rebuilt_l2 is False
    assert neither.rebuilt_l3 is False


def test_a_managed_item_carries_its_memory_type() -> None:
    """列表项的 memory_type 要带过去 —— 调用方按它筛选。"""
    resp = list_response_to_pb(
        ListMemoriesResponse(
            status="ok",
            items=[
                ManagedMemoryItem(
                    memory_id="m1",
                    content="c",
                    status=MemoryStatus.ACTIVE,
                    source_type=SourceType.CHAT_ROUND,
                    data_classification=DataClassification.NORMAL,
                    memory_type=MemoryType.PREFERENCE,
                )
            ],
        )
    )

    assert len(resp.items) == 1
    assert resp.items[0].memory_id == "m1"
    assert resp.items[0].memory_type == pb.MEMORY_TYPE_PREFERENCE
