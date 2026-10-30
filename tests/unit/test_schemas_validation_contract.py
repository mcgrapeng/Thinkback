"""入参校验契约：schemas.py 是服务面对**不可信调用方**的第一道闸。

gRPC / curl / SDK 都不经过前端表单，服务端必须自己挡住：空 ID、超长 ID、
非法 namespace、超大 metadata、越界的预算与阈值。Web 表单能挡的不算数
—— 这里的用例必须只靠服务端校验就红。

每条用例钉一个具体结论：把校验放宽（`<=` 写成 `<`、`>=` 写成 `>`、
默认 namespace 回落成空串）时必须红。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from thinkback.memory.schemas import (
    METADATA_MAX_BYTES,
    AppendEventRequest,
    AppendMemoryRequest,
    DeleteMemoryRequest,
    DeleteScope,
    MemoryMessage,
    MessageRole,
    RebuildMemoryRequest,
    RecallMemoryRequest,
    SourceEvent,
    UpdateMemoryRequest,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _message(**kwargs: Any) -> MemoryMessage:
    payload: dict[str, Any] = {
        "message_id": "m1",
        "role": MessageRole.USER,
        "content": "hello",
        "timestamp": NOW,
    }
    payload.update(kwargs)
    return MemoryMessage(**payload)


def _event(**kwargs: Any) -> SourceEvent:
    payload: dict[str, Any] = {
        "event_id": "e1",
        "source": "chat_round",
        "role": MessageRole.USER,
        "payload": "fact",
        "occurred_at": NOW,
    }
    payload.update(kwargs)
    return SourceEvent(**payload)


def _append(**kwargs: Any) -> AppendMemoryRequest:
    payload: dict[str, Any] = {
        "request_id": "req-1",
        "user_id": "user-1",
        "session_id": "s1",
        "round_id": "r1",
        "messages": [_message()],
        "source_timestamp": NOW,
    }
    payload.update(kwargs)
    return AppendMemoryRequest(**payload)


# ── namespace：作用域隔离靠它 ─────────────────────────────────────────


def test_a_namespace_with_a_colon_is_rejected() -> None:
    """namespace 会和 user_id 拼成作用域键 —— 允许 ':' 就是允许跨作用域碰撞。"""

    with pytest.raises(ValidationError, match="namespace must match"):
        _append(namespace="a:b")


def test_a_namespace_longer_than_sixty_four_characters_is_rejected() -> None:
    with pytest.raises(ValidationError, match="namespace must match"):
        _append(namespace="n" * 65)


def test_a_namespace_of_exactly_sixty_four_characters_is_accepted() -> None:
    assert _append(namespace="n" * 64).namespace == "n" * 64


def test_an_empty_namespace_is_rejected() -> None:
    with pytest.raises(ValidationError, match="namespace must match"):
        _append(namespace="")


@pytest.mark.parametrize(
    "model,payload",
    [
        (RecallMemoryRequest, {"user_id": "u", "session_id": "s", "query": "q"}),
        (
            DeleteMemoryRequest,
            {
                "request_id": "r",
                "user_id": "u",
                "scope": DeleteScope.ALL,
                "operation_id": "op",
            },
        ),
        (
            UpdateMemoryRequest,
            {
                "request_id": "r",
                "user_id": "u",
                "operation_id": "op",
                "memory_id": "m",
                "content": "x",
            },
        ),
        (
            RebuildMemoryRequest,
            {"request_id": "r", "user_id": "u", "operation_id": "op"},
        ),
    ],
)
def test_every_request_model_rejects_an_illegal_namespace(
    model: type, payload: dict[str, Any]
) -> None:
    """每个模型都要自己校验 namespace —— 漏一个就是一个作用域逃逸口。"""

    with pytest.raises(ValidationError, match="namespace must match"):
        model(**{**payload, "namespace": "bad namespace"})


# ── 必填 ID：空串会让「幂等键」退化成同一个键 ────────────────────────


@pytest.mark.parametrize("field", ["user_id", "session_id", "round_id", "request_id"])
def test_an_append_with_a_blank_id_is_rejected(field: str) -> None:
    with pytest.raises(ValidationError, match="must be non-empty"):
        _append(**{field: "   "})


@pytest.mark.parametrize("field", ["user_id", "session_id", "request_id"])
def test_an_event_append_with_a_blank_id_is_rejected(field: str) -> None:
    payload: dict[str, Any] = {
        "request_id": "req-1",
        "user_id": "user-1",
        "session_id": "s1",
        "event": _event(),
    }
    with pytest.raises(ValidationError, match="must be non-empty"):
        AppendEventRequest(**{**payload, field: "   "})


@pytest.mark.parametrize("field", ["user_id", "session_id", "request_id"])
def test_an_event_append_with_an_empty_id_is_rejected(field: str) -> None:
    """空串先被 IdStr 的 min_length 挡住 —— 两条路都得堵。"""

    payload: dict[str, Any] = {
        "request_id": "req-1",
        "user_id": "user-1",
        "session_id": "s1",
        "event": _event(),
    }
    with pytest.raises(ValidationError):
        AppendEventRequest(**{**payload, field: ""})


def test_a_too_long_id_is_rejected_before_it_reaches_the_database() -> None:
    """schema 端先拒超长 id，否则 PG 的 String(128) 直接报 DataError 500。"""

    with pytest.raises(ValidationError):
        _append(user_id="u" * 129)


def test_a_recall_with_a_blank_user_id_is_rejected() -> None:
    with pytest.raises(ValidationError, match="must be non-empty"):
        RecallMemoryRequest(user_id=" ", session_id="s", query="q")


def test_a_recall_allows_an_empty_query() -> None:
    """query 允许空 —— 允许空查询的召回（按时间/强度）不是非法请求。"""

    assert RecallMemoryRequest(user_id="u", session_id="s", query="").query == ""


# ── 事件：写入契约的原子单位 ─────────────────────────────────────────


@pytest.mark.parametrize("field", ["event_id", "source", "payload"])
def test_an_event_with_a_blank_field_is_rejected(field: str) -> None:
    with pytest.raises(ValidationError):
        _event(**{field: "  "})


def test_an_event_payload_is_stripped() -> None:
    assert _event(payload="  fact  ").payload == "fact"


def test_an_event_with_a_non_dict_provenance_is_left_to_pydantic() -> None:
    """provenance 不是 dict 时不该在这里炸 —— 让 pydantic 报类型错。"""

    with pytest.raises(ValidationError):
        _event(provenance=["not", "a", "dict"])


def test_an_event_with_an_oversized_provenance_is_rejected() -> None:
    with pytest.raises(ValidationError, match="metadata must be at most"):
        _event(provenance={"blob": "x" * (METADATA_MAX_BYTES + 1)})


# ── metadata：JSONB 静默 TOAST 的前哨 ───────────────────────────────


def test_an_oversized_metadata_is_rejected() -> None:
    with pytest.raises(ValidationError, match="metadata must be at most"):
        _append(metadata={"blob": "x" * (METADATA_MAX_BYTES + 1)})


def test_a_metadata_at_the_size_limit_is_accepted() -> None:
    """边界值必须放行 —— 把 `>` 写成 `>=` 会把合法请求拒掉。"""

    filler = "x" * (METADATA_MAX_BYTES - 20)
    request = _append(metadata={"blob": filler})
    assert request.metadata["blob"] == filler


def test_an_event_append_with_an_oversized_metadata_is_rejected() -> None:
    payload: dict[str, Any] = {
        "request_id": "req-1",
        "user_id": "user-1",
        "session_id": "s1",
        "event": _event(),
    }
    with pytest.raises(ValidationError, match="metadata must be at most"):
        AppendEventRequest(**payload, metadata={"blob": "x" * (METADATA_MAX_BYTES + 1)})


# ── 写入轮次的完整性 ─────────────────────────────────────────────────


def test_an_append_without_messages_is_rejected() -> None:
    with pytest.raises(ValidationError, match="at least one message"):
        _append(messages=[])


def test_an_append_with_a_blank_message_content_is_rejected() -> None:
    """正文被 strip 成空串的 message 必须拒 —— 否则写进 L1 的是一句空话。"""

    with pytest.raises(ValidationError, match="non-empty content"):
        _append(messages=[_message(content="   ")])


def test_a_message_content_is_stripped() -> None:
    assert _message(content="  hi  ").content == "hi"


def test_a_message_accepts_the_normalized_content_alias() -> None:
    """SDK 发的字段名是 normalized_content —— 别名断了就是静默丢正文。"""

    message = MemoryMessage(
        message_id="m1",
        role=MessageRole.USER,
        normalized_content="aliased",
        timestamp=NOW,
    )
    assert message.content == "aliased"


def test_an_append_scope_key_joins_user_and_session() -> None:
    assert _append(user_id="u1", session_id="s1").scope_key == "u1:s1"


def test_an_event_append_becomes_a_single_message_round() -> None:
    """一条事件 = 一轮一条消息：event_id 同时是 round_id 与幂等锚点。"""

    request = AppendEventRequest(
        request_id="req-1",
        user_id="user-1",
        session_id="s1",
        event=_event(event_id="e-9", role=MessageRole.USER, payload="fact"),
        round_index=3,
    )
    internal = request.to_internal()
    assert internal.round_id == "e-9"
    assert internal.round_index == 3
    assert internal.source_timestamp == NOW
    assert len(internal.messages) == 1
    assert internal.messages[0].content == "fact"


def test_the_declared_memory_type_travels_through_the_metadata() -> None:
    """类型走 metadata：内部解析「先请求字段后 metadata」，两者必须等价。"""

    request = AppendEventRequest(
        request_id="req-1",
        user_id="user-1",
        session_id="s1",
        event=_event(),
        memory_type="state",
    )
    assert request.to_internal().metadata["memory_type"] == "state"


# ── 删除：范围与标识符必须互斥 ───────────────────────────────────────


def test_a_memory_delete_without_a_memory_id_is_rejected() -> None:
    with pytest.raises(ValidationError, match="memory_id is required"):
        DeleteMemoryRequest(
            request_id="r", user_id="u", scope=DeleteScope.MEMORY, operation_id="op"
        )


def test_a_memory_delete_with_a_session_id_is_rejected() -> None:
    with pytest.raises(ValidationError, match="must not include session_id"):
        DeleteMemoryRequest(
            request_id="r",
            user_id="u",
            scope=DeleteScope.MEMORY,
            operation_id="op",
            memory_id="m",
            session_id="s",
        )


def test_a_session_delete_without_a_session_id_is_rejected() -> None:
    with pytest.raises(ValidationError, match="session_id is required"):
        DeleteMemoryRequest(
            request_id="r", user_id="u", scope=DeleteScope.SESSION, operation_id="op"
        )


def test_a_session_delete_with_a_memory_id_is_rejected() -> None:
    with pytest.raises(ValidationError, match="must not include memory_id"):
        DeleteMemoryRequest(
            request_id="r",
            user_id="u",
            scope=DeleteScope.SESSION,
            operation_id="op",
            session_id="s",
            memory_id="m",
        )


def test_an_all_delete_with_identifiers_is_rejected() -> None:
    with pytest.raises(ValidationError, match="all deletion must not include memory_id"):
        DeleteMemoryRequest(
            request_id="r",
            user_id="u",
            scope=DeleteScope.ALL,
            operation_id="op",
            memory_id="m",
        )
    with pytest.raises(ValidationError, match="all deletion must not include session_id"):
        DeleteMemoryRequest(
            request_id="r",
            user_id="u",
            scope=DeleteScope.ALL,
            operation_id="op",
            session_id="s",
        )


# ── 编辑：正文不能是空白 ─────────────────────────────────────────────


def test_an_update_with_a_blank_content_is_rejected() -> None:
    with pytest.raises(ValidationError, match="non-empty content"):
        UpdateMemoryRequest(
            request_id="r", user_id="u", operation_id="op", memory_id="m", content="   "
        )


def test_an_update_strips_its_content() -> None:
    request = UpdateMemoryRequest(
        request_id="r", user_id="u", operation_id="op", memory_id="m", content="  x  "
    )
    assert request.content == "x"


# ── 召回的预算与阈值：越界必须在入口被拒 ─────────────────────────────


@pytest.mark.parametrize(
    "field,value",
    [
        ("l3_limit", -1),
        ("l3_limit", 51),
        ("l3_score_threshold", -0.1),
        ("l3_score_threshold", 1.1),
        ("token_budget", 99),
        ("token_budget", 12001),
    ],
)
def test_a_recall_with_an_out_of_range_budget_is_rejected(field: str, value: float) -> None:
    payload: dict[str, Any] = {"user_id": "u", "session_id": "s", "query": "q"}
    with pytest.raises(ValidationError):
        RecallMemoryRequest(**payload, **{field: value})


@pytest.mark.parametrize(
    "field,value",
    [("l3_limit", 0), ("l3_limit", 50), ("l3_score_threshold", 0.0), ("l3_score_threshold", 1.0)],
)
def test_a_recall_keeps_the_boundary_values(field: str, value: float) -> None:
    payload: dict[str, Any] = {"user_id": "u", "session_id": "s", "query": "q"}
    assert getattr(RecallMemoryRequest(**payload, **{field: value}), field) == value
