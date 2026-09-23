from __future__ import annotations

import pytest
from loguru import logger

from thinkback.api import dependencies
from thinkback.infra import logging as logging_infra
from thinkback.infra import readiness
from thinkback.memory.backends import FakeMemoryBackend
from thinkback.memory.repositories import InMemoryMemoryRepository


def _append_payload() -> dict[str, object]:
    return {
        "request_id": "req-r1",
        "user_id": "user-1",
        "session_id": "session-1",
        "round_id": "round-1",
        "messages": [
            {
                "message_id": "m1",
                "role": "user",
                "content": "我不喜欢被催睡觉",
                "timestamp": "2026-05-04T10:00:00Z",
            },
            {
                "message_id": "m2",
                "role": "assistant",
                "content": "我会记住这个边界。",
                "timestamp": "2026-05-04T10:00:03Z",
            },
        ],
        "source_timestamp": "2026-05-04T10:00:03Z",
    }


def test_patch_log_record_injects_active_trace_id() -> None:
    logging_infra.set_trace_id("trace-123")
    try:
        record = {"extra": {}}

        logging_infra.patch_log_record(record)

        assert record["trace_id"] == "trace-123"
        assert "trace_id" not in record["extra"]
    finally:
        logging_infra.clear_trace_id()


def test_patch_log_record_defaults_trace_id_to_dash() -> None:
    logging_infra.clear_trace_id()
    record = {"extra": {}}

    logging_infra.patch_log_record(record)

    assert record["trace_id"] == "-"
    assert "trace_id" not in record["extra"]


def test_configure_logging_registers_trace_id_patcher(monkeypatch) -> None:
    calls: dict[str, object] = {}

    def fake_remove() -> None:
        calls["remove"] = True

    def fake_configure(**kwargs) -> None:
        calls["configure"] = kwargs

    def fake_add(*args, **kwargs) -> int:
        calls["add"] = kwargs
        return 1

    monkeypatch.setattr(logging_infra.logger, "remove", fake_remove)
    monkeypatch.setattr(logging_infra.logger, "configure", fake_configure)
    monkeypatch.setattr(logging_infra.logger, "add", fake_add)
    logging_infra.configure_logging.cache_clear()

    try:
        logging_infra.configure_logging()
    finally:
        logging_infra.configure_logging.cache_clear()

    assert calls["remove"] is True
    assert calls["configure"]["patcher"] is logging_infra.patch_log_record
    assert "trace_id" in str(calls["add"]["format"])
    assert "{extra[trace_id]}" not in str(calls["add"]["format"])
    assert calls["add"]["level"] == logging_infra.settings.log_level


def test_configured_format_renders_trace_id_once(monkeypatch) -> None:
    calls: dict[str, object] = {}

    def fake_remove() -> None:
        calls["remove"] = True

    def fake_configure(**kwargs) -> None:
        calls["configure"] = kwargs

    def fake_add(*args, **kwargs) -> int:
        calls["add"] = kwargs
        return 1

    with monkeypatch.context() as patch_context:
        patch_context.setattr(logging_infra.logger, "remove", fake_remove)
        patch_context.setattr(logging_infra.logger, "configure", fake_configure)
        patch_context.setattr(logging_infra.logger, "add", fake_add)
        logging_infra.configure_logging.cache_clear()
        try:
            logging_infra.configure_logging()
        finally:
            logging_infra.configure_logging.cache_clear()

    sink: list[str] = []
    handler_id = logger.add(sink.append, level="INFO", format=str(calls["add"]["format"]))
    logging_infra.logger.configure(patcher=logging_infra.patch_log_record)
    logging_infra.set_trace_id("trace-format")
    try:
        logger.bind(user_id="user-format").info("format check")
    finally:
        logging_infra.clear_trace_id()
        logger.remove(handler_id)
        logging_infra.logger.configure(patcher=logging_infra.patch_log_record)

    rendered = "".join(sink)
    assert rendered.count("trace_id=trace-format") == 1
    assert "'trace_id': 'trace-format'" not in rendered


@pytest.mark.asyncio
async def test_collect_readiness_logs_english_messages_and_trace_id(monkeypatch) -> None:
    sink: list[str] = []
    handler_id = logger.add(sink.append, level="INFO", format="{message} {extra}")
    logging_infra.logger.configure(patcher=logging_infra.patch_log_record)
    logging_infra.set_trace_id("trace-readiness")

    async def ready_check() -> dict[str, str]:
        return {"status": "ready", "detail": "ok"}

    monkeypatch.setattr(readiness, "check_database", ready_check)
    monkeypatch.setattr(readiness, "check_milvus", ready_check)
    monkeypatch.setattr(readiness, "check_mem0_library", ready_check)

    try:
        payload = await readiness.collect_readiness(timeout_seconds=0.01)
    finally:
        logging_infra.clear_trace_id()
        logger.remove(handler_id)
        logging_infra.logger.configure(patcher=logging_infra.patch_log_record)

    assert payload["status"] == "ready"
    logs = "\n".join(sink)
    assert "readiness aggregation started" not in logs
    assert "readiness aggregation completed" not in logs
    assert "readiness dependency check started" not in logs
    assert "readiness dependency check completed" not in logs


@pytest.mark.asyncio
async def test_collect_readiness_logs_warning_when_not_ready(monkeypatch) -> None:
    sink: list[str] = []
    handler_id = logger.add(sink.append, level="INFO", format="{level} {message} {extra}")
    logging_infra.logger.configure(patcher=logging_infra.patch_log_record)
    logging_infra.set_trace_id("trace-readiness-warning")

    async def ready_check() -> dict[str, str]:
        return {"status": "ready", "detail": "ok"}

    async def not_ready_check() -> dict[str, str]:
        return {"status": "not_ready", "detail": "unavailable"}

    monkeypatch.setattr(readiness, "check_database", ready_check)
    monkeypatch.setattr(readiness, "check_milvus", not_ready_check)
    monkeypatch.setattr(readiness, "check_mem0_library", ready_check)

    try:
        payload = await readiness.collect_readiness(timeout_seconds=0.01)
    finally:
        logging_infra.clear_trace_id()
        logger.remove(handler_id)
        logging_infra.logger.configure(patcher=logging_infra.patch_log_record)

    assert payload["status"] == "not_ready"
    logs = "\n".join(sink)
    assert "WARNING readiness aggregation completed" in logs
    assert "milvus" in logs


def test_memory_append_route_preserves_trace_id_through_worker_threads(client, monkeypatch) -> None:
    sink: list[str] = []
    handler_id = logger.add(
        sink.append, level="INFO", format="{message} trace_id={trace_id} {extra}"
    )
    logging_infra.logger.configure(patcher=logging_infra.patch_log_record)
    monkeypatch.setattr(dependencies, "_memory_service", None)
    monkeypatch.setattr(dependencies, "SqlAlchemyMemoryRepository", InMemoryMemoryRepository)
    monkeypatch.setattr(
        dependencies, "get_memory_backend", lambda _settings=None: FakeMemoryBackend()
    )

    try:
        response = client.post(
            "/memory/append",
            json=_append_payload(),
            headers={"X-Request-Id": "trace-api"},
        )
        service = dependencies.get_memory_service()
        service.drain_l3_background_tasks(timeout=1.0)
    finally:
        logger.remove(handler_id)
        logging_infra.logger.configure(patcher=logging_infra.patch_log_record)

    assert response.status_code == 200
    assert response.headers["X-Trace-Id"] == "trace-api"
    logs = "\n".join(sink)
    lines = list(sink)
    assert "HTTP request completed" not in logs
    assert "memory append request received" not in logs
    assert "memory service initialized" in logs
    assert "memory append started" in logs
    assert "trace-api" in logs
    is_async = "memory l3 async write started" in logs
    is_sync = "memory l3 write started" in logs
    assert is_async or is_sync
    if is_async:
        assert any(
            "memory l3 background write finished" in line and "trace-api" in line for line in lines
        )
