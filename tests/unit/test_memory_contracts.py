import pytest
from pydantic import ValidationError

from thinkback.memory.schemas import (
    AppendMemoryRequest,
    DeleteMemoryRequest,
    DeleteScope,
    MemoryMessage,
    MemoryType,
    MessageRole,
    RebuildMemoryRequest,
    RecallIntent,
    RecallMemoryRequest,
    SummaryState,
    UpdateMemoryRequest,
)


def test_append_request_requires_full_user_assistant_round() -> None:
    request = AppendMemoryRequest(
        request_id="req-1",
        user_id="user-1",
        session_id="session-1",
        round_id="round-1",
        messages=[
            {
                "message_id": "m1",
                "role": MessageRole.USER,
                "content": "我最近睡不好",
                "timestamp": "2026-05-04T10:00:00Z",
            },
            {
                "message_id": "m2",
                "role": MessageRole.ASSISTANT,
                "content": "我先陪你把这件事说清楚。",
                "timestamp": "2026-05-04T10:00:03Z",
            },
        ],
        source_timestamp="2026-05-04T10:00:03Z",
    )

    assert request.messages[0].role is MessageRole.USER
    assert request.messages[1].role is MessageRole.ASSISTANT
    assert request.scope_key == "user-1:session-1"


def test_append_request_rejects_isolated_single_message() -> None:
    with pytest.raises(ValidationError, match="complete user -> assistant round"):
        AppendMemoryRequest(
            request_id="req-1",
            user_id="user-1",
            session_id="session-1",
            round_id="round-1",
            messages=[
                {
                    "message_id": "m1",
                    "role": MessageRole.USER,
                    "content": "只有单条消息",
                    "timestamp": "2026-05-04T10:00:00Z",
                }
            ],
            source_timestamp="2026-05-04T10:00:00Z",
        )


def test_append_request_rejects_empty_assistant_content() -> None:
    with pytest.raises(ValidationError, match="non-empty"):
        AppendMemoryRequest(
            request_id="req-1",
            user_id="user-1",
            session_id="session-1",
            round_id="round-1",
            messages=[
                {
                    "message_id": "m1",
                    "role": MessageRole.USER,
                    "content": "我最近睡不好",
                    "timestamp": "2026-05-04T10:00:00Z",
                },
                {
                    "message_id": "m2",
                    "role": MessageRole.ASSISTANT,
                    "content": "   ",
                    "timestamp": "2026-05-04T10:00:03Z",
                },
            ],
            source_timestamp="2026-05-04T10:00:03Z",
        )


def test_append_request_rejects_empty_user_content() -> None:
    with pytest.raises(ValidationError, match="non-empty"):
        AppendMemoryRequest(
            request_id="req-1",
            user_id="user-1",
            session_id="session-1",
            round_id="round-1",
            messages=[
                {
                    "message_id": "m1",
                    "role": MessageRole.USER,
                    "content": "",
                    "timestamp": "2026-05-04T10:00:00Z",
                },
                {
                    "message_id": "m2",
                    "role": MessageRole.ASSISTANT,
                    "content": "我先陪你把这件事说清楚。",
                    "timestamp": "2026-05-04T10:00:03Z",
                },
            ],
            source_timestamp="2026-05-04T10:00:03Z",
        )


def test_documented_enums_are_available() -> None:
    assert MemoryType.PREFERENCE.value == "preference"
    assert SummaryState.DIRTY.value == "dirty"
    assert SummaryState.STALE.value == "stale"
    assert RecallIntent.SENSITIVE.value == "sensitive"
    assert DeleteScope.SESSION.value == "session"


def test_recall_request_has_l3_score_threshold() -> None:
    request = RecallMemoryRequest(
        user_id="user-1",
        session_id="session-1",
        query="用户的狗叫什么？",
    )

    assert request.l3_score_threshold == 0.5


def test_memory_requests_reject_internal_scope_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AppendMemoryRequest(
            request_id="req-1",
            user_id="user-1",
            session_id="session-1",
            memory_scope_id="internal-scope",
            round_id="round-1",
            messages=[
                {
                    "message_id": "m1",
                    "role": MessageRole.USER,
                    "content": "用户问题",
                    "timestamp": "2026-05-04T10:00:00Z",
                },
                {
                    "message_id": "m2",
                    "role": MessageRole.ASSISTANT,
                    "content": "助手回答",
                    "timestamp": "2026-05-04T10:00:03Z",
                },
            ],
            source_timestamp="2026-05-04T10:00:03Z",
        )
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RecallMemoryRequest(
            user_id="user-1",
            session_id="session-1",
            memory_scope_id="internal-scope",
            query="query",
        )


def test_delete_request_rejects_scope_irrelevant_identifiers() -> None:
    with pytest.raises(ValidationError, match="memory deletion must not include session_id"):
        DeleteMemoryRequest(
            request_id="delete-1",
            user_id="user-1",
            scope=DeleteScope.MEMORY,
            operation_id="delete-op-1",
            memory_id="memory-1",
            session_id="session-1",
        )

    with pytest.raises(ValidationError, match="session deletion must not include memory_id"):
        DeleteMemoryRequest(
            request_id="delete-1",
            user_id="user-1",
            scope=DeleteScope.SESSION,
            operation_id="delete-op-1",
            memory_id="memory-1",
            session_id="session-1",
        )

    with pytest.raises(ValidationError, match="all deletion must not include memory_id"):
        DeleteMemoryRequest(
            request_id="delete-1",
            user_id="user-1",
            scope=DeleteScope.ALL,
            operation_id="delete-op-1",
            memory_id="memory-1",
        )

    with pytest.raises(ValidationError, match="all deletion must not include session_id"):
        DeleteMemoryRequest(
            request_id="delete-1",
            user_id="user-1",
            scope=DeleteScope.ALL,
            operation_id="delete-op-1",
            session_id="session-1",
        )


def test_update_memory_request_requires_non_empty_content_and_rejects_internal_scope() -> None:
    request = UpdateMemoryRequest(
        request_id="update-1",
        user_id="user-1",
        operation_id="update-op-1",
        memory_id="memory-1",
        content="  User has a cat named 麻薯  ",
        memory_type=MemoryType.PROFILE,
    )

    assert request.content == "User has a cat named 麻薯"
    assert request.memory_type is MemoryType.PROFILE

    with pytest.raises(ValidationError, match="memory update requires non-empty content"):
        UpdateMemoryRequest(
            request_id="update-1",
            user_id="user-1",
            operation_id="update-op-1",
            memory_id="memory-1",
            content="   ",
        )

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        UpdateMemoryRequest(
            request_id="update-1",
            user_id="user-1",
            operation_id="update-op-1",
            memory_id="memory-1",
            memory_scope_id="thinkback",
            content="User has a cat named 麻薯",
        )


# ---------------------------------------------------------------------------
# N-1: id 字段 max_length=128 约束（与 PG String(128) 对齐）
# ---------------------------------------------------------------------------


def _valid_round_kwargs() -> dict:
    """完整 round payload 的 baseline kwargs（不含将被覆盖的 request_id/user_id/session_id/round_id）。"""
    return {
        "messages": [
            {
                "message_id": "m1",
                "role": MessageRole.USER,
                "content": "用户问题",
                "timestamp": "2026-05-04T10:00:00Z",
            },
            {
                "message_id": "m2",
                "role": MessageRole.ASSISTANT,
                "content": "助手回答",
                "timestamp": "2026-05-04T10:00:03Z",
            },
        ],
        "source_timestamp": "2026-05-04T10:00:03Z",
    }


def _base_append_kwargs() -> dict:
    """完整合法的 AppendMemoryRequest kwargs。"""
    return dict(
        request_id="req-1",
        user_id="user-1",
        session_id="session-1",
        round_id="round-1",
        **_valid_round_kwargs(),
    )


def test_request_id_too_long_raises_validation_error() -> None:
    with pytest.raises(ValidationError, match="at most 128 characters"):
        AppendMemoryRequest(
            request_id="r" * 200,
            user_id="user-1",
            session_id="session-1",
            round_id="round-1",
            **_valid_round_kwargs(),
        )


def test_user_id_too_long_raises_validation_error() -> None:
    with pytest.raises(ValidationError, match="at most 128 characters"):
        AppendMemoryRequest(
            request_id="req-1",
            user_id="u" * 200,
            session_id="session-1",
            round_id="round-1",
            **_valid_round_kwargs(),
        )


def test_session_id_too_long_raises_validation_error() -> None:
    with pytest.raises(ValidationError, match="at most 128 characters"):
        AppendMemoryRequest(
            request_id="req-1",
            user_id="user-1",
            session_id="s" * 200,
            round_id="round-1",
            **_valid_round_kwargs(),
        )


def test_round_id_too_long_raises_validation_error() -> None:
    with pytest.raises(ValidationError, match="at most 128 characters"):
        AppendMemoryRequest(
            request_id="req-1",
            user_id="user-1",
            session_id="session-1",
            round_id="r" * 200,
            **_valid_round_kwargs(),
        )


def test_operation_id_too_long_raises_validation_error() -> None:
    with pytest.raises(ValidationError, match="at most 128 characters"):
        UpdateMemoryRequest(
            request_id="update-1",
            user_id="user-1",
            operation_id="o" * 200,
            memory_id="memory-1",
            content="non-empty",
        )

    with pytest.raises(ValidationError, match="at most 128 characters"):
        DeleteMemoryRequest(
            request_id="delete-1",
            user_id="user-1",
            scope=DeleteScope.ALL,
            operation_id="o" * 200,
        )

    with pytest.raises(ValidationError, match="at most 128 characters"):
        RebuildMemoryRequest(
            request_id="rebuild-1",
            user_id="user-1",
            operation_id="o" * 200,
        )


def test_memory_id_too_long_raises_validation_error() -> None:
    with pytest.raises(ValidationError, match="at most 128 characters"):
        UpdateMemoryRequest(
            request_id="update-1",
            user_id="user-1",
            operation_id="op-1",
            memory_id="m" * 200,
            content="non-empty",
        )

    with pytest.raises(ValidationError, match="at most 128 characters"):
        DeleteMemoryRequest(
            request_id="delete-1",
            user_id="user-1",
            scope=DeleteScope.MEMORY,
            operation_id="op-1",
            memory_id="m" * 200,
        )


def test_message_id_too_long_raises_validation_error() -> None:
    with pytest.raises(ValidationError, match="at most 128 characters"):
        MemoryMessage(
            message_id="m" * 200,
            role=MessageRole.USER,
            content="用户问题",
            timestamp="2026-05-04T10:00:00Z",
        )


def test_id_within_limit_passes() -> None:
    """128 字符正好通过，反向 sanity check。"""
    AppendMemoryRequest(
        request_id="r" * 128,
        user_id="user-1",
        session_id="session-1",
        round_id="round-1",
        **_valid_round_kwargs(),
    )


# ---------------------------------------------------------------------------
# N-8: metadata 64KB 大小限制（防 PG JSONB TOAST/失败）
# ---------------------------------------------------------------------------


def test_metadata_oversized_raises_validation_error() -> None:
    with pytest.raises(ValidationError, match="at most 65536 bytes"):
        AppendMemoryRequest(
            metadata={"blob": "x" * 100_000},
            **_base_append_kwargs(),
        )


def test_metadata_within_limit_passes() -> None:
    request = AppendMemoryRequest(
        metadata={"tag": "profile", "score": 0.9, "tags": ["a", "b", "c"]},
        **_base_append_kwargs(),
    )
    assert request.metadata["tag"] == "profile"


def test_metadata_empty_passes() -> None:
    """空 dict / 默认值不应触发 64KB 校验。"""
    request = AppendMemoryRequest(
        **_base_append_kwargs(),
    )
    assert request.metadata == {}
