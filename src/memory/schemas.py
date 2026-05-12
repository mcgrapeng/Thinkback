"""Memory API schemas and architecture enums."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class MemoryType(StrEnum):
    PROFILE = "profile"
    PREFERENCE = "preference"
    EVENT = "event"
    PLAN = "plan"
    RELATIONSHIP = "relationship"
    CONSTRAINT = "constraint"


class SourceType(StrEnum):
    CHAT_ROUND = "chat_round"
    SESSION_REBUILD = "session_rebuild"
    MANUAL_FIX = "manual_fix"
    IMPORT = "import"
    SYSTEM_MIGRATION = "system_migration"


class FactSubject(StrEnum):
    USER = "user"
    CHARACTER = "character"
    RELATIONSHIP = "relationship"
    STORY_WORLD = "story_world"
    THIRD_PARTY = "third_party"


class ContextType(StrEnum):
    REAL_USER = "real_user"
    ROLEPLAY = "roleplay"
    FICTIONAL_SETTING = "fictional_setting"
    MIXED = "mixed"
    UNCERTAIN = "uncertain"


class RoleplayMode(StrEnum):
    OFF = "off"
    ON = "on"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class MemoryStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DELETED = "DELETED"
    SUPPRESSED = "SUPPRESSED"
    SUPERSEDED = "SUPERSEDED"


class DataClassification(StrEnum):
    NORMAL = "normal"
    PERSONAL = "personal"
    SENSITIVE = "sensitive"
    RESTRICTED = "restricted"


class SummaryState(StrEnum):
    ACTIVE = "active"
    STALE = "stale"
    DIRTY = "dirty"
    REBUILDING = "rebuilding"


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


class OperationType(StrEnum):
    WRITE_ROUND = "write_round"
    DELETE_MEMORY = "delete_memory"
    DELETE_SESSION = "delete_session"
    DELETE_ALL = "delete_all"
    REBUILD = "rebuild"
    REBUILD_L2 = "rebuild_l2"
    REBUILD_L3 = "rebuild_l3"


class RecallIntent(StrEnum):
    CHAT = "chat"
    MEMORY_QUERY = "memory_query"
    PERSONAL_INFO = "personal_info"
    PREFERENCE = "preference"
    RELATIONSHIP_CONTINUITY = "relationship_continuity"
    DELETE_CONFIRMATION = "delete_confirmation"
    PRIVACY = "privacy"
    SENSITIVE = "sensitive"


class DeleteScope(StrEnum):
    MEMORY = "memory"
    SESSION = "session"
    ALL = "all"


class MemoryMessage(BaseModel):
    model_config = ConfigDict(populate_by_name=True, use_enum_values=False)

    message_id: str
    role: MessageRole
    content: str = Field(validation_alias=AliasChoices("content", "normalized_content"))
    timestamp: datetime

    @field_validator("content", mode="before")
    @classmethod
    def accept_content_alias(cls, value: Any) -> Any:
        return value

    @field_validator("content")
    @classmethod
    def strip_content(cls, value: str) -> str:
        return value.strip()


class AppendMemoryRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, use_enum_values=False)

    request_id: str
    user_id: str
    character_id: str
    session_id: str
    round_id: str
    messages: list[MemoryMessage]
    source_timestamp: datetime
    round_index: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def scope_key(self) -> str:
        return f"{self.user_id}:{self.character_id}"

    @model_validator(mode="after")
    def validate_complete_round(self) -> AppendMemoryRequest:
        if len(self.messages) != 2:
            raise ValueError("append requires a complete user -> assistant round")
        if self.messages[0].role is not MessageRole.USER or self.messages[1].role is not MessageRole.ASSISTANT:
            raise ValueError("append requires messages ordered as a complete user -> assistant round")
        if not any(message.content for message in self.messages):
            raise ValueError("append requires non-empty round content")
        return self


class RecallMemoryRequest(BaseModel):
    user_id: str
    character_id: str
    session_id: str
    query: str
    intent: RecallIntent = RecallIntent.CHAT
    l3_limit: int = Field(default=5, ge=0, le=50)
    l3_score_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    token_budget: int = Field(default=1200, ge=100, le=12000)


class DeleteMemoryRequest(BaseModel):
    request_id: str
    user_id: str
    character_id: str
    scope: DeleteScope
    operation_id: str
    memory_id: str | None = None
    session_id: str | None = None


class RebuildMemoryRequest(BaseModel):
    request_id: str
    user_id: str
    character_id: str
    operation_id: str
    session_id: str | None = None
    history_version: str | None = None
    rebuild_l2: bool = True
    rebuild_l3: bool = True

    @model_validator(mode="after")
    def validate_rebuild_layer_selection(self) -> RebuildMemoryRequest:
        if not self.rebuild_l2 and not self.rebuild_l3:
            raise ValueError("rebuild requires at least one layer")
        return self


class MemoryItem(BaseModel):
    layer: str
    content: str
    source: str
    memory_id: str | None = None
    score: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AppendMemoryResponse(BaseModel):
    status: str
    task_id: str
    round_id: str
    l3_events: list[dict[str, Any]] = Field(default_factory=list)


class RecallMemoryResponse(BaseModel):
    status: str
    degraded: bool = False
    degradation_reasons: list[str] = Field(default_factory=list)
    items: list[MemoryItem] = Field(default_factory=list)


class DeleteMemoryResponse(BaseModel):
    status: str
    task_id: str
    affected_memories: int = 0
    summary_state: SummaryState | None = None


class RebuildMemoryResponse(BaseModel):
    status: str
    task_id: str
    rebuilt_l2: bool
    rebuilt_l3: bool


class TaskResponse(BaseModel):
    task_id: str
    request_id: str
    op_type: OperationType
    status: TaskStatus
    scope: dict[str, Any]
    last_error: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)
