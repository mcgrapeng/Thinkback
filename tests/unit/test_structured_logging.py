"""结构化日志助手的契约测试。

``infra/structured_logging.py`` 是生产日志的**词汇表**：告警规则、看板聚合、
排障脚本都按 ``[PREFIX] {action} {verb}`` 这个格式匹配。它此前只有 63% 的
行覆盖 —— 恰恰是 ``skipped`` / ``failed`` / ``degraded`` / ``decision`` /
``metric`` / ``timed_block`` 这些**事故时才会有人去读**的动词没被钉住。

格式变了不会让任何功能用例变红，但会让生产上的告警规则静默失效：
规则按 ``[MEMORY] append failed`` 匹配，日志却打成了 ``[MEMORY] append error``，
事故当天才发现告警从来没响过。这里把每个动词的级别与格式都钉死。
"""

from __future__ import annotations

from collections.abc import Generator

import pytest
from loguru import logger

from thinkback.infra.structured_logging import (
    LOGGER_EMBED,
    LOGGER_MEMORY,
    LOGGER_QUERY,
    LOGGER_STORE,
    StructuredLogger,
)


@pytest.fixture()
def sink() -> Generator[list[str], None, None]:
    """收集所有级别的日志行（``{level}|{message}|{extra}``）。"""
    records: list[str] = []
    handler_id = logger.add(records.append, level="DEBUG", format="{level}|{message}|{extra}")
    try:
        yield records
    finally:
        logger.remove(handler_id)


def _only(records: list[str]) -> str:
    assert len(records) == 1, records
    return records[0]


def _message(records: list[str]) -> str:
    """日志正文（``{level}|{message}|{extra}`` 里的中间那段）。

    用它做**精确**断言而不是子串包含：默认参数被改掉时（``reason=""``
    变成 ``reason="XXXX"``），子串包含照样通过 —— 门禁就看不见了。
    """

    parts = _only(records).split("|")
    assert len(parts) >= 3, records
    return parts[1]


def test_started_logs_the_action_at_info(sink: list[str]) -> None:
    StructuredLogger("MEMORY").started("append", user_id="user-1")
    line = _only(sink)
    assert line.startswith("INFO|")
    assert "[MEMORY] append started" in line


def test_completed_logs_the_status(sink: list[str]) -> None:
    StructuredLogger("MEMORY").completed("append", "ok")
    assert "[MEMORY] append completed (ok)" in _only(sink)


def test_completed_defaults_to_ok_status(sink: list[str]) -> None:
    StructuredLogger("MEMORY").completed("append")
    assert "[MEMORY] append completed (ok)" in _only(sink)


def test_skipped_logs_a_warning_with_the_reason(sink: list[str]) -> None:
    StructuredLogger("MEMORY").skipped("erase", "no matching memory")
    line = _only(sink)
    assert line.startswith("WARNING|")
    assert "[MEMORY] erase skipped (no matching memory)" in line


def test_failed_logs_an_error_with_the_error_type(sink: list[str]) -> None:
    StructuredLogger("MEMORY").failed("erase", "TimeoutError")
    line = _only(sink)
    assert line.startswith("ERROR|")
    assert "[MEMORY] erase failed (TimeoutError)" in line


def test_failed_appends_the_detail_when_present(sink: list[str]) -> None:
    StructuredLogger("MEMORY").failed("erase", "TimeoutError", detail="milvus unreachable")
    assert "[MEMORY] erase failed (TimeoutError: milvus unreachable)" in _only(sink)


def test_failed_omits_the_separator_without_detail(sink: list[str]) -> None:
    """没有 detail 时不能留下悬空的冒号 —— 告警规则按精确文本匹配。"""
    StructuredLogger("MEMORY").failed("erase", "TimeoutError")
    assert _message(sink) == "[MEMORY] erase failed (TimeoutError)"


def test_degraded_logs_a_warning_with_the_reason(sink: list[str]) -> None:
    StructuredLogger("MEMORY").degraded("recall", "L3 backend unavailable")
    line = _only(sink)
    assert line.startswith("WARNING|")
    assert "[MEMORY] recall degraded (L3 backend unavailable)" in line


def test_decision_allowed_logs_at_info(sink: list[str]) -> None:
    StructuredLogger("MEMORY").decision("append", True, "within policy")
    line = _only(sink)
    assert line.startswith("INFO|")
    assert "[MEMORY] append allowed (within policy)" in line


def test_decision_denied_logs_at_warning(sink: list[str]) -> None:
    """被拒绝是运营信号，必须是 warning —— 悄悄放行才该是 info。"""
    StructuredLogger("MEMORY").decision("append", False, "restricted data")
    line = _only(sink)
    assert line.startswith("WARNING|")
    assert "[MEMORY] append denied (restricted data)" in line


def test_decision_without_reason_omits_the_parentheses(sink: list[str]) -> None:
    """默认 reason 是空串 —— 精确断言，改默认值会被看见。"""
    StructuredLogger("MEMORY").decision("append", True)
    assert _message(sink) == "[MEMORY] append allowed"


def test_metric_logs_at_debug_with_value_and_unit(sink: list[str]) -> None:
    """指标默认 debug —— 生产 info 流不该被指标淹没。"""
    StructuredLogger("MEMORY").metric("recall_latency", 12.5, "ms")
    line = _only(sink)
    assert line.startswith("DEBUG|")
    assert "[MEMORY] metric recall_latency=12.5ms" in line


def test_metric_without_unit_omits_the_unit_suffix(sink: list[str]) -> None:
    """默认 unit 是空串 —— 精确断言，改默认值会被看见。"""
    StructuredLogger("MEMORY").metric("recall_count", 3)
    assert _message(sink) == "[MEMORY] metric recall_count=3"


def test_timed_block_reports_the_duration(sink: list[str]) -> None:
    with StructuredLogger("MEMORY").timed_block("recall"):
        pass
    line = _only(sink)
    assert "[MEMORY] recall completed (ok)" in line
    assert "duration_ms" in line


def test_timed_block_marks_slow_calls(sink: list[str]) -> None:
    """超阈值的调用标 ``slow``，看板据此统计慢调用。"""
    with StructuredLogger("MEMORY").timed_block("recall", warn_threshold_ms=-1.0):
        pass
    assert "[MEMORY] recall completed (slow)" in _only(sink)


def test_timed_block_still_logs_when_the_body_raises(sink: list[str]) -> None:
    """块里抛异常时也要留下耗时 —— 异常路径恰恰最需要这条日志。"""
    with pytest.raises(RuntimeError), StructuredLogger("MEMORY").timed_block("recall"):
        raise RuntimeError("boom")
    assert "[MEMORY] recall completed" in _only(sink)


def test_operation_id_is_bound_into_the_context(sink: list[str]) -> None:
    StructuredLogger("MEMORY", operation_id="op-1").started("append", user_id="user-1")
    line = _only(sink)
    assert "'operation_id': 'op-1'" in line
    assert "'user_id': 'user-1'" in line


def test_operation_id_is_absent_when_not_configured(sink: list[str]) -> None:
    StructuredLogger("MEMORY").started("append")
    assert "operation_id" not in _only(sink)


def test_predefined_loggers_carry_their_prefixes() -> None:
    assert LOGGER_MEMORY.prefix == "[MEMORY]"
    assert LOGGER_EMBED.prefix == "[EMBED]"
    assert LOGGER_STORE.prefix == "[STORE]"
    assert LOGGER_QUERY.prefix == "[QUERY]"
