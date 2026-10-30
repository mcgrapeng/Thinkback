"""配置校验契约：写错的配置要么启动失败，要么**静默关掉一道闸**。

最危险的不是报错的那几条，是「看起来配了、其实没生效」的那几条：
限流速率配成 0 = 等于关掉限流但看不出是配置写的；孤儿任务回收阈值配成
30 秒 = 把正在跑的健康任务当成孤儿回收掉。这些都必须在启动时炸。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from thinkback.infra.config import Settings

BASE: dict[str, object] = {"memory_llm_base_url": "http://llm.invalid"}


def _settings(**kwargs: object) -> Settings:
    return Settings(**{**BASE, **kwargs})  # type: ignore[arg-type]


# ── 并发与线程池：配成 0 会让写路径直接不可用 ─────────────────────────


@pytest.mark.parametrize(
    "field",
    [
        "memory_l3_executor_workers",
        "memory_backend_max_concurrent_calls",
        "memory_api_worker_limit",
    ],
)
def test_a_worker_count_below_one_is_rejected(field: str) -> None:
    with pytest.raises(ValidationError, match=">= 1"):
        _settings(**{field: 0})


@pytest.mark.parametrize(
    "field",
    [
        "memory_l3_executor_workers",
        "memory_backend_max_concurrent_calls",
        "memory_api_worker_limit",
    ],
)
def test_a_worker_count_of_one_is_accepted(field: str) -> None:
    assert getattr(_settings(**{field: 1}), field) == 1


def test_a_negative_api_wait_is_rejected() -> None:
    """等待时间允许 0（直接报 busy），不允许负数。"""

    with pytest.raises(ValidationError, match=">= 0"):
        _settings(memory_api_worker_wait_seconds=-1)


def test_a_zero_api_wait_is_accepted() -> None:
    assert _settings(memory_api_worker_wait_seconds=0).memory_api_worker_wait_seconds == 0


# ── 限流：配成 0 等于关掉限流 ─────────────────────────────────────────


def test_a_zero_rate_limit_is_rejected() -> None:
    """0 速率会让第一请求就被拒 —— 与「关掉限流」不是一回事。"""

    with pytest.raises(ValidationError, match=">= 1"):
        _settings(memory_rate_limit_per_minute=0)


def test_a_zero_burst_is_rejected() -> None:
    with pytest.raises(ValidationError, match=">= 1"):
        _settings(memory_rate_limit_burst=0)


def test_a_rate_limit_of_one_is_accepted() -> None:
    settings = _settings(memory_rate_limit_per_minute=1, memory_rate_limit_burst=1)
    assert settings.memory_rate_limit_per_minute == 1
    assert settings.memory_rate_limit_burst == 1


# ── 孤儿任务回收：阈值太小会误杀健康任务 ─────────────────────────────


def test_an_orphan_threshold_below_sixty_seconds_is_rejected() -> None:
    with pytest.raises(ValidationError, match=">= 60"):
        _settings(task_orphan_running_seconds=30)


def test_an_orphan_threshold_of_exactly_sixty_seconds_is_accepted() -> None:
    assert _settings(task_orphan_running_seconds=60).task_orphan_running_seconds == 60


# ── 日志级别：大小写不敏感，但不认识的名字必须拒 ─────────────────────


@pytest.mark.parametrize("value", ["debug", "INFO", "Warning", "error", "CRITICAL"])
def test_the_log_level_is_normalized_to_upper_case(value: str) -> None:
    assert _settings(log_level=value).log_level == value.upper()


def test_an_unknown_log_level_is_rejected() -> None:
    with pytest.raises(ValidationError, match="Invalid log level"):
        _settings(log_level="VERBOSE")


# ── P0 槽位列表：逗号分隔的解析 ──────────────────────────────────────


def test_an_empty_p0_slot_list_means_the_gate_is_off() -> None:
    assert _settings(memory_p0_slots="").memory_p0_slots_list == []


def test_the_p0_slot_list_is_split_and_trimmed() -> None:
    settings = _settings(memory_p0_slots="current_location, favorite:food , ")
    assert settings.memory_p0_slots_list == ["current_location", "favorite:food"]
