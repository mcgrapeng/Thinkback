from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import (
    ClassVar as _ClassVar,
    Iterable as _Iterable,
    Mapping as _Mapping,
    Optional as _Optional,
    Union as _Union,
)

DESCRIPTOR: _descriptor.FileDescriptor

class MessageRole(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    MESSAGE_ROLE_USER: _ClassVar[MessageRole]
    MESSAGE_ROLE_ASSISTANT: _ClassVar[MessageRole]
    MESSAGE_ROLE_SYSTEM: _ClassVar[MessageRole]

class MemoryType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    MEMORY_TYPE_PROFILE: _ClassVar[MemoryType]
    MEMORY_TYPE_PREFERENCE: _ClassVar[MemoryType]
    MEMORY_TYPE_EVENT: _ClassVar[MemoryType]
    MEMORY_TYPE_PLAN: _ClassVar[MemoryType]
    MEMORY_TYPE_CONSTRAINT: _ClassVar[MemoryType]

class MemoryStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    MEMORY_STATUS_ACTIVE: _ClassVar[MemoryStatus]
    MEMORY_STATUS_DELETED: _ClassVar[MemoryStatus]
    MEMORY_STATUS_SUPPRESSED: _ClassVar[MemoryStatus]
    MEMORY_STATUS_SUPERSEDED: _ClassVar[MemoryStatus]

class SourceType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SOURCE_TYPE_CHAT_ROUND: _ClassVar[SourceType]
    SOURCE_TYPE_SESSION_REBUILD: _ClassVar[SourceType]
    SOURCE_TYPE_MANUAL_FIX: _ClassVar[SourceType]
    SOURCE_TYPE_IMPORT: _ClassVar[SourceType]
    SOURCE_TYPE_SYSTEM_MIGRATION: _ClassVar[SourceType]

class DataClassification(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    DATA_CLASSIFICATION_NORMAL: _ClassVar[DataClassification]
    DATA_CLASSIFICATION_PERSONAL: _ClassVar[DataClassification]
    DATA_CLASSIFICATION_SENSITIVE: _ClassVar[DataClassification]
    DATA_CLASSIFICATION_RESTRICTED: _ClassVar[DataClassification]

class SummaryState(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SUMMARY_STATE_ACTIVE: _ClassVar[SummaryState]
    SUMMARY_STATE_STALE: _ClassVar[SummaryState]
    SUMMARY_STATE_DIRTY: _ClassVar[SummaryState]
    SUMMARY_STATE_REBUILDING: _ClassVar[SummaryState]

class TaskStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    TASK_STATUS_PENDING: _ClassVar[TaskStatus]
    TASK_STATUS_RUNNING: _ClassVar[TaskStatus]
    TASK_STATUS_COMPLETED: _ClassVar[TaskStatus]
    TASK_STATUS_FAILED: _ClassVar[TaskStatus]
    TASK_STATUS_DEAD_LETTER: _ClassVar[TaskStatus]

class OperationType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    OPERATION_TYPE_WRITE_ROUND: _ClassVar[OperationType]
    OPERATION_TYPE_UPDATE_MEMORY: _ClassVar[OperationType]
    OPERATION_TYPE_DELETE_MEMORY: _ClassVar[OperationType]
    OPERATION_TYPE_DELETE_SESSION: _ClassVar[OperationType]
    OPERATION_TYPE_DELETE_ALL: _ClassVar[OperationType]
    OPERATION_TYPE_REBUILD_L2: _ClassVar[OperationType]
    OPERATION_TYPE_REBUILD_L3: _ClassVar[OperationType]

class RecallIntent(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    RECALL_INTENT_CHAT: _ClassVar[RecallIntent]
    RECALL_INTENT_SENSITIVE: _ClassVar[RecallIntent]

class DeleteScope(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    DELETE_SCOPE_MEMORY: _ClassVar[DeleteScope]
    DELETE_SCOPE_SESSION: _ClassVar[DeleteScope]
    DELETE_SCOPE_ALL: _ClassVar[DeleteScope]

MESSAGE_ROLE_USER: MessageRole
MESSAGE_ROLE_ASSISTANT: MessageRole
MESSAGE_ROLE_SYSTEM: MessageRole
MEMORY_TYPE_PROFILE: MemoryType
MEMORY_TYPE_PREFERENCE: MemoryType
MEMORY_TYPE_EVENT: MemoryType
MEMORY_TYPE_PLAN: MemoryType
MEMORY_TYPE_CONSTRAINT: MemoryType
MEMORY_STATUS_ACTIVE: MemoryStatus
MEMORY_STATUS_DELETED: MemoryStatus
MEMORY_STATUS_SUPPRESSED: MemoryStatus
MEMORY_STATUS_SUPERSEDED: MemoryStatus
SOURCE_TYPE_CHAT_ROUND: SourceType
SOURCE_TYPE_SESSION_REBUILD: SourceType
SOURCE_TYPE_MANUAL_FIX: SourceType
SOURCE_TYPE_IMPORT: SourceType
SOURCE_TYPE_SYSTEM_MIGRATION: SourceType
DATA_CLASSIFICATION_NORMAL: DataClassification
DATA_CLASSIFICATION_PERSONAL: DataClassification
DATA_CLASSIFICATION_SENSITIVE: DataClassification
DATA_CLASSIFICATION_RESTRICTED: DataClassification
SUMMARY_STATE_ACTIVE: SummaryState
SUMMARY_STATE_STALE: SummaryState
SUMMARY_STATE_DIRTY: SummaryState
SUMMARY_STATE_REBUILDING: SummaryState
TASK_STATUS_PENDING: TaskStatus
TASK_STATUS_RUNNING: TaskStatus
TASK_STATUS_COMPLETED: TaskStatus
TASK_STATUS_FAILED: TaskStatus
TASK_STATUS_DEAD_LETTER: TaskStatus
OPERATION_TYPE_WRITE_ROUND: OperationType
OPERATION_TYPE_UPDATE_MEMORY: OperationType
OPERATION_TYPE_DELETE_MEMORY: OperationType
OPERATION_TYPE_DELETE_SESSION: OperationType
OPERATION_TYPE_DELETE_ALL: OperationType
OPERATION_TYPE_REBUILD_L2: OperationType
OPERATION_TYPE_REBUILD_L3: OperationType
RECALL_INTENT_CHAT: RecallIntent
RECALL_INTENT_SENSITIVE: RecallIntent
DELETE_SCOPE_MEMORY: DeleteScope
DELETE_SCOPE_SESSION: DeleteScope
DELETE_SCOPE_ALL: DeleteScope

class MemoryMessage(_message.Message):
    __slots__ = ("message_id", "role", "content", "timestamp")
    MESSAGE_ID_FIELD_NUMBER: _ClassVar[int]
    ROLE_FIELD_NUMBER: _ClassVar[int]
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    message_id: str
    role: MessageRole
    content: str
    timestamp: str
    def __init__(
        self,
        message_id: _Optional[str] = ...,
        role: _Optional[_Union[MessageRole, str]] = ...,
        content: _Optional[str] = ...,
        timestamp: _Optional[str] = ...,
    ) -> None: ...

class AppendRequest(_message.Message):
    __slots__ = (
        "request_id",
        "user_id",
        "session_id",
        "round_id",
        "messages",
        "source_timestamp",
        "round_index",
        "metadata",
    )
    class MetadataEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...

    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    USER_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    ROUND_ID_FIELD_NUMBER: _ClassVar[int]
    MESSAGES_FIELD_NUMBER: _ClassVar[int]
    SOURCE_TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    ROUND_INDEX_FIELD_NUMBER: _ClassVar[int]
    METADATA_FIELD_NUMBER: _ClassVar[int]
    request_id: str
    user_id: str
    session_id: str
    round_id: str
    messages: _containers.RepeatedCompositeFieldContainer[MemoryMessage]
    source_timestamp: str
    round_index: int
    metadata: _containers.ScalarMap[str, str]
    def __init__(
        self,
        request_id: _Optional[str] = ...,
        user_id: _Optional[str] = ...,
        session_id: _Optional[str] = ...,
        round_id: _Optional[str] = ...,
        messages: _Optional[_Iterable[_Union[MemoryMessage, _Mapping]]] = ...,
        source_timestamp: _Optional[str] = ...,
        round_index: _Optional[int] = ...,
        metadata: _Optional[_Mapping[str, str]] = ...,
    ) -> None: ...

class AppendResponse(_message.Message):
    __slots__ = ("status", "task_id", "round_id", "l3_events")
    STATUS_FIELD_NUMBER: _ClassVar[int]
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    ROUND_ID_FIELD_NUMBER: _ClassVar[int]
    L3_EVENTS_FIELD_NUMBER: _ClassVar[int]
    status: str
    task_id: str
    round_id: str
    l3_events: _containers.RepeatedScalarFieldContainer[str]
    def __init__(
        self,
        status: _Optional[str] = ...,
        task_id: _Optional[str] = ...,
        round_id: _Optional[str] = ...,
        l3_events: _Optional[_Iterable[str]] = ...,
    ) -> None: ...

class RecallRequest(_message.Message):
    __slots__ = (
        "user_id",
        "session_id",
        "query",
        "intent",
        "l3_limit",
        "l3_score_threshold",
        "token_budget",
    )
    USER_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    QUERY_FIELD_NUMBER: _ClassVar[int]
    INTENT_FIELD_NUMBER: _ClassVar[int]
    L3_LIMIT_FIELD_NUMBER: _ClassVar[int]
    L3_SCORE_THRESHOLD_FIELD_NUMBER: _ClassVar[int]
    TOKEN_BUDGET_FIELD_NUMBER: _ClassVar[int]
    user_id: str
    session_id: str
    query: str
    intent: RecallIntent
    l3_limit: int
    l3_score_threshold: float
    token_budget: int
    def __init__(
        self,
        user_id: _Optional[str] = ...,
        session_id: _Optional[str] = ...,
        query: _Optional[str] = ...,
        intent: _Optional[_Union[RecallIntent, str]] = ...,
        l3_limit: _Optional[int] = ...,
        l3_score_threshold: _Optional[float] = ...,
        token_budget: _Optional[int] = ...,
    ) -> None: ...

class MemoryItem(_message.Message):
    __slots__ = ("layer", "content", "source", "memory_id", "score", "metadata")
    class MetadataEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...

    LAYER_FIELD_NUMBER: _ClassVar[int]
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    MEMORY_ID_FIELD_NUMBER: _ClassVar[int]
    SCORE_FIELD_NUMBER: _ClassVar[int]
    METADATA_FIELD_NUMBER: _ClassVar[int]
    layer: str
    content: str
    source: str
    memory_id: str
    score: float
    metadata: _containers.ScalarMap[str, str]
    def __init__(
        self,
        layer: _Optional[str] = ...,
        content: _Optional[str] = ...,
        source: _Optional[str] = ...,
        memory_id: _Optional[str] = ...,
        score: _Optional[float] = ...,
        metadata: _Optional[_Mapping[str, str]] = ...,
    ) -> None: ...

class RecallResponse(_message.Message):
    __slots__ = ("status", "degraded", "degradation_reasons", "items")
    STATUS_FIELD_NUMBER: _ClassVar[int]
    DEGRADED_FIELD_NUMBER: _ClassVar[int]
    DEGRADATION_REASONS_FIELD_NUMBER: _ClassVar[int]
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    status: str
    degraded: bool
    degradation_reasons: _containers.RepeatedScalarFieldContainer[str]
    items: _containers.RepeatedCompositeFieldContainer[MemoryItem]
    def __init__(
        self,
        status: _Optional[str] = ...,
        degraded: bool = ...,
        degradation_reasons: _Optional[_Iterable[str]] = ...,
        items: _Optional[_Iterable[_Union[MemoryItem, _Mapping]]] = ...,
    ) -> None: ...

class DeleteRequest(_message.Message):
    __slots__ = ("request_id", "user_id", "scope", "operation_id", "memory_id", "session_id")
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    USER_ID_FIELD_NUMBER: _ClassVar[int]
    SCOPE_FIELD_NUMBER: _ClassVar[int]
    OPERATION_ID_FIELD_NUMBER: _ClassVar[int]
    MEMORY_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    request_id: str
    user_id: str
    scope: DeleteScope
    operation_id: str
    memory_id: str
    session_id: str
    def __init__(
        self,
        request_id: _Optional[str] = ...,
        user_id: _Optional[str] = ...,
        scope: _Optional[_Union[DeleteScope, str]] = ...,
        operation_id: _Optional[str] = ...,
        memory_id: _Optional[str] = ...,
        session_id: _Optional[str] = ...,
    ) -> None: ...

class DeleteResponse(_message.Message):
    __slots__ = ("status", "task_id", "affected_memories", "summary_state")
    STATUS_FIELD_NUMBER: _ClassVar[int]
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    AFFECTED_MEMORIES_FIELD_NUMBER: _ClassVar[int]
    SUMMARY_STATE_FIELD_NUMBER: _ClassVar[int]
    status: str
    task_id: str
    affected_memories: int
    summary_state: SummaryState
    def __init__(
        self,
        status: _Optional[str] = ...,
        task_id: _Optional[str] = ...,
        affected_memories: _Optional[int] = ...,
        summary_state: _Optional[_Union[SummaryState, str]] = ...,
    ) -> None: ...

class ListMemoriesRequest(_message.Message):
    __slots__ = ("user_id", "include_deleted")
    USER_ID_FIELD_NUMBER: _ClassVar[int]
    INCLUDE_DELETED_FIELD_NUMBER: _ClassVar[int]
    user_id: str
    include_deleted: bool
    def __init__(self, user_id: _Optional[str] = ..., include_deleted: bool = ...) -> None: ...

class ManagedMemoryItem(_message.Message):
    __slots__ = (
        "memory_id",
        "content",
        "status",
        "source_type",
        "data_classification",
        "memory_type",
        "backend_categories",
    )
    MEMORY_ID_FIELD_NUMBER: _ClassVar[int]
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    SOURCE_TYPE_FIELD_NUMBER: _ClassVar[int]
    DATA_CLASSIFICATION_FIELD_NUMBER: _ClassVar[int]
    MEMORY_TYPE_FIELD_NUMBER: _ClassVar[int]
    BACKEND_CATEGORIES_FIELD_NUMBER: _ClassVar[int]
    memory_id: str
    content: str
    status: MemoryStatus
    source_type: SourceType
    data_classification: DataClassification
    memory_type: MemoryType
    backend_categories: _containers.RepeatedScalarFieldContainer[str]
    def __init__(
        self,
        memory_id: _Optional[str] = ...,
        content: _Optional[str] = ...,
        status: _Optional[_Union[MemoryStatus, str]] = ...,
        source_type: _Optional[_Union[SourceType, str]] = ...,
        data_classification: _Optional[_Union[DataClassification, str]] = ...,
        memory_type: _Optional[_Union[MemoryType, str]] = ...,
        backend_categories: _Optional[_Iterable[str]] = ...,
    ) -> None: ...

class ListMemoriesResponse(_message.Message):
    __slots__ = ("status", "items")
    STATUS_FIELD_NUMBER: _ClassVar[int]
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    status: str
    items: _containers.RepeatedCompositeFieldContainer[ManagedMemoryItem]
    def __init__(
        self,
        status: _Optional[str] = ...,
        items: _Optional[_Iterable[_Union[ManagedMemoryItem, _Mapping]]] = ...,
    ) -> None: ...

class GetMemoryRequest(_message.Message):
    __slots__ = ("memory_id", "user_id")
    MEMORY_ID_FIELD_NUMBER: _ClassVar[int]
    USER_ID_FIELD_NUMBER: _ClassVar[int]
    memory_id: str
    user_id: str
    def __init__(self, memory_id: _Optional[str] = ..., user_id: _Optional[str] = ...) -> None: ...

class GetMemoryResponse(_message.Message):
    __slots__ = ("status", "memory")
    STATUS_FIELD_NUMBER: _ClassVar[int]
    MEMORY_FIELD_NUMBER: _ClassVar[int]
    status: str
    memory: ManagedMemoryItem
    def __init__(
        self,
        status: _Optional[str] = ...,
        memory: _Optional[_Union[ManagedMemoryItem, _Mapping]] = ...,
    ) -> None: ...

class UpdateMemoryRequest(_message.Message):
    __slots__ = ("request_id", "user_id", "operation_id", "memory_id", "content", "memory_type")
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    USER_ID_FIELD_NUMBER: _ClassVar[int]
    OPERATION_ID_FIELD_NUMBER: _ClassVar[int]
    MEMORY_ID_FIELD_NUMBER: _ClassVar[int]
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    MEMORY_TYPE_FIELD_NUMBER: _ClassVar[int]
    request_id: str
    user_id: str
    operation_id: str
    memory_id: str
    content: str
    memory_type: MemoryType
    def __init__(
        self,
        request_id: _Optional[str] = ...,
        user_id: _Optional[str] = ...,
        operation_id: _Optional[str] = ...,
        memory_id: _Optional[str] = ...,
        content: _Optional[str] = ...,
        memory_type: _Optional[_Union[MemoryType, str]] = ...,
    ) -> None: ...

class UpdateMemoryResponse(_message.Message):
    __slots__ = ("status", "task_id", "memory", "summary_state")
    STATUS_FIELD_NUMBER: _ClassVar[int]
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    MEMORY_FIELD_NUMBER: _ClassVar[int]
    SUMMARY_STATE_FIELD_NUMBER: _ClassVar[int]
    status: str
    task_id: str
    memory: ManagedMemoryItem
    summary_state: SummaryState
    def __init__(
        self,
        status: _Optional[str] = ...,
        task_id: _Optional[str] = ...,
        memory: _Optional[_Union[ManagedMemoryItem, _Mapping]] = ...,
        summary_state: _Optional[_Union[SummaryState, str]] = ...,
    ) -> None: ...

class RebuildRequest(_message.Message):
    __slots__ = (
        "request_id",
        "user_id",
        "operation_id",
        "session_id",
        "history_version",
        "rebuild_l2",
        "rebuild_l3",
    )
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    USER_ID_FIELD_NUMBER: _ClassVar[int]
    OPERATION_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    HISTORY_VERSION_FIELD_NUMBER: _ClassVar[int]
    REBUILD_L2_FIELD_NUMBER: _ClassVar[int]
    REBUILD_L3_FIELD_NUMBER: _ClassVar[int]
    request_id: str
    user_id: str
    operation_id: str
    session_id: str
    history_version: str
    rebuild_l2: bool
    rebuild_l3: bool
    def __init__(
        self,
        request_id: _Optional[str] = ...,
        user_id: _Optional[str] = ...,
        operation_id: _Optional[str] = ...,
        session_id: _Optional[str] = ...,
        history_version: _Optional[str] = ...,
        rebuild_l2: bool = ...,
        rebuild_l3: bool = ...,
    ) -> None: ...

class RebuildResponse(_message.Message):
    __slots__ = ("status", "task_id", "rebuilt_l2", "rebuilt_l3")
    STATUS_FIELD_NUMBER: _ClassVar[int]
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    REBUILT_L2_FIELD_NUMBER: _ClassVar[int]
    REBUILT_L3_FIELD_NUMBER: _ClassVar[int]
    status: str
    task_id: str
    rebuilt_l2: bool
    rebuilt_l3: bool
    def __init__(
        self,
        status: _Optional[str] = ...,
        task_id: _Optional[str] = ...,
        rebuilt_l2: bool = ...,
        rebuilt_l3: bool = ...,
    ) -> None: ...

class GetTaskRequest(_message.Message):
    __slots__ = ("task_id",)
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    task_id: str
    def __init__(self, task_id: _Optional[str] = ...) -> None: ...

class TaskResponse(_message.Message):
    __slots__ = ("task_id", "request_id", "op_type", "status", "scope", "last_error", "result")
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    OP_TYPE_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    SCOPE_FIELD_NUMBER: _ClassVar[int]
    LAST_ERROR_FIELD_NUMBER: _ClassVar[int]
    RESULT_FIELD_NUMBER: _ClassVar[int]
    task_id: str
    request_id: str
    op_type: OperationType
    status: TaskStatus
    scope: str
    last_error: str
    result: str
    def __init__(
        self,
        task_id: _Optional[str] = ...,
        request_id: _Optional[str] = ...,
        op_type: _Optional[_Union[OperationType, str]] = ...,
        status: _Optional[_Union[TaskStatus, str]] = ...,
        scope: _Optional[str] = ...,
        last_error: _Optional[str] = ...,
        result: _Optional[str] = ...,
    ) -> None: ...

class L3BackgroundStatusRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class L3BackgroundStatusResponse(_message.Message):
    __slots__ = (
        "write_mode",
        "executor_workers",
        "max_pending_tasks",
        "pending_write_tasks",
        "cleanup_tasks",
        "available_capacity",
    )
    WRITE_MODE_FIELD_NUMBER: _ClassVar[int]
    EXECUTOR_WORKERS_FIELD_NUMBER: _ClassVar[int]
    MAX_PENDING_TASKS_FIELD_NUMBER: _ClassVar[int]
    PENDING_WRITE_TASKS_FIELD_NUMBER: _ClassVar[int]
    CLEANUP_TASKS_FIELD_NUMBER: _ClassVar[int]
    AVAILABLE_CAPACITY_FIELD_NUMBER: _ClassVar[int]
    write_mode: str
    executor_workers: int
    max_pending_tasks: int
    pending_write_tasks: int
    cleanup_tasks: int
    available_capacity: int
    def __init__(
        self,
        write_mode: _Optional[str] = ...,
        executor_workers: _Optional[int] = ...,
        max_pending_tasks: _Optional[int] = ...,
        pending_write_tasks: _Optional[int] = ...,
        cleanup_tasks: _Optional[int] = ...,
        available_capacity: _Optional[int] = ...,
    ) -> None: ...
