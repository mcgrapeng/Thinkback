"""Request trace id helpers."""

from __future__ import annotations

from contextvars import ContextVar
from uuid import uuid4

_trace_id: ContextVar[str | None] = ContextVar("trace_id", default=None)


def set_trace_id(incoming_trace_id: str | None = None) -> str:
    trace_id = incoming_trace_id or uuid4().hex
    _trace_id.set(trace_id)
    return trace_id


def get_trace_id() -> str | None:
    return _trace_id.get()


def clear_trace_id() -> None:
    _trace_id.set(None)
