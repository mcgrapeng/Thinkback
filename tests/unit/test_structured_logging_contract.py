"""结构化日志「空 reason / 空 unit」的格式契约。

告警规则按 ``[PREFIX] {action} {verb}`` 匹配，日志里多一个括号或后缀，
规则就静默失配 —— 事故当天才发现告警从来没响过。

这一层盯的是**可选字段为空时不得留下残渣**：``reason`` 没给就不能出现
``()``，``unit`` 没给就不能多出后缀。
"""

from __future__ import annotations

from collections.abc import Generator

import pytest
from loguru import logger

from thinkback.infra.structured_logging import StructuredLogger


@pytest.fixture()
def sink() -> Generator[list[str], None, None]:
    records: list[str] = []
    handler_id = logger.add(records.append, level="DEBUG", format="{level}|{message}|{extra}")
    try:
        yield records
    finally:
        logger.remove(handler_id)


def _only(records: list[str]) -> str:
    assert len(records) == 1, records
    return records[0]


def test_decision_without_reason_has_no_empty_parentheses(sink: list[str]) -> None:
    """没给 reason → 日志里不能出现空的 ``()``。

    ``[MEMORY] gate allowed ()`` 会被按 ``allowed (原因)`` 的规则匹配到，
    抽取出来的原因是空串 —— 看板上是「allowed 但没有理由」。
    """
    StructuredLogger("MEMORY").decision("gate", allowed=True)
    line = _only(sink)
    assert "[MEMORY] gate allowed" in line
    assert "()" not in line


def test_decision_with_reason_keeps_the_reason(sink: list[str]) -> None:
    StructuredLogger("MEMORY").decision("gate", allowed=False, reason="policy")
    line = _only(sink)
    assert "[MEMORY] gate denied (policy)" in line


def test_denied_decision_logs_at_warning_level(sink: list[str]) -> None:
    StructuredLogger("MEMORY").decision("gate", allowed=False)
    assert _only(sink).startswith("WARNING|")


def test_allowed_decision_logs_at_info_level(sink: list[str]) -> None:
    StructuredLogger("MEMORY").decision("gate", allowed=True)
    assert _only(sink).startswith("INFO|")


def test_metric_without_unit_has_no_trailing_suffix(sink: list[str]) -> None:
    """没给 unit → 数值后面不能多出任何东西。

    多出的后缀会让「按单位聚合」的看板把这条算进别的单位里。
    """
    StructuredLogger("MEMORY").metric("latency", 12)
    line = _only(sink)
    assert "[MEMORY] metric latency=12" in line
    assert not line.rstrip().endswith("12 ")


def test_metric_with_unit_appends_the_unit(sink: list[str]) -> None:
    StructuredLogger("MEMORY").metric("latency", 12, "ms")
    assert "[MEMORY] metric latency=12ms" in _only(sink)


def test_metric_logs_at_debug_level(sink: list[str]) -> None:
    """指标默认 debug —— 生产环境不该被指标流淹没。"""
    StructuredLogger("MEMORY").metric("latency", 12)
    assert _only(sink).startswith("DEBUG|")
