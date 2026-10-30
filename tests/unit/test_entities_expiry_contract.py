"""过期判定的唯一口径：``domain/entities.memory_is_expired``。

召回、列表、槽位回填三处都问它（service.py 里三个调用点），口径不一致的
代价是「已过期的事实还在上下文里」。它是纯函数 + 判定分支，改坏时
必须红：``<=`` 写成 ``<``（到期瞬间多活一秒）、naive 时间按本地时区读
（差 8 小时）、``now`` 参数被忽略（无法复现）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from thinkback.domain.entities import memory_is_expired

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _index(expires_at: datetime | None) -> Any:
    """最小可用的索引行：只要有 expires_at 属性即可。"""

    class Row:
        pass

    row = Row()
    row.expires_at = expires_at
    return row


def test_a_memory_without_an_expiry_never_expires() -> None:
    assert memory_is_expired(_index(None), now=NOW) is False


def test_a_memory_expiring_in_the_future_is_not_expired() -> None:
    assert memory_is_expired(_index(NOW + timedelta(seconds=1)), now=NOW) is False


def test_a_memory_expiring_in_the_past_is_expired() -> None:
    assert memory_is_expired(_index(NOW - timedelta(seconds=1)), now=NOW) is True


def test_a_memory_expiring_exactly_now_is_expired() -> None:
    """到期瞬间即过期（``<=`` 不是 ``<``）：多活一秒就是泄漏。"""

    assert memory_is_expired(_index(NOW), now=NOW) is True


def test_a_naive_expiry_is_read_as_utc() -> None:
    """asyncpg / SQLite 可能丢掉 tzinfo —— naive 必须按 UTC 读，不能按本地时区。"""

    naive = datetime(2026, 1, 1, 12, 0)
    assert memory_is_expired(_index(naive), now=NOW) is True
    assert memory_is_expired(_index(naive + timedelta(hours=1)), now=NOW) is False


def test_an_aware_expiry_is_not_shifted() -> None:
    aware = NOW + timedelta(hours=8)
    assert memory_is_expired(_index(aware), now=NOW) is False


def test_without_a_now_the_current_time_decides() -> None:
    """不传 now 时用当前时刻 —— 传了 now 却被忽略的话，过期用例会假红。"""

    assert memory_is_expired(_index(NOW - timedelta(days=365))) is True
    assert memory_is_expired(_index(datetime.now(UTC) + timedelta(days=365))) is False


def test_an_entry_without_the_attribute_is_not_expired() -> None:
    """TTL 字段缺失的行按「未过期」处理 —— 不是所有调用方都传 TTL。"""

    assert memory_is_expired(object(), now=NOW) is False
