import pytest
from pydantic import ValidationError

from memory.schemas import (
    AppendMemoryRequest,
    DeleteScope,
    MemoryType,
    MessageRole,
    RecallIntent,
    SummaryState,
)


def test_append_request_requires_full_user_assistant_round() -> None:
    request = AppendMemoryRequest(
        request_id="req-1",
        user_id="user-1",
        character_id="char-1",
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
    assert request.scope_key == "user-1:char-1"


def test_append_request_rejects_isolated_single_message() -> None:
    with pytest.raises(ValidationError, match="complete user -> assistant round"):
        AppendMemoryRequest(
            request_id="req-1",
            user_id="user-1",
            character_id="char-1",
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


def test_documented_enums_are_available() -> None:
    assert MemoryType.PREFERENCE.value == "preference"
    assert MemoryType.RELATIONSHIP.value == "relationship"
    assert SummaryState.DIRTY.value == "dirty"
    assert SummaryState.STALE.value == "stale"
    assert RecallIntent.RELATIONSHIP_CONTINUITY.value == "relationship_continuity"
    assert RecallIntent.DELETE_CONFIRMATION.value == "delete_confirmation"
    assert DeleteScope.SESSION.value == "session"
