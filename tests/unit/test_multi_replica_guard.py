"""多副本前置检查与 L1 读穿透。

两件事要钉住：
- 宣称多副本但前置没齐时，服务在启动期硬失败（不是静默跑起来双写）；
- L1 缓存关掉后走读穿透：读不到缓存，每次回源 PG（多副本下别的副本追加的
  轮次才看得见）。
"""

from __future__ import annotations

import pytest

from thinkback.domain.entities import JournalEntry
from thinkback.infra.config import Settings
from thinkback.infra.replica_guard import (
    assert_multi_replica_ready,
    check_multi_replica,
)
from thinkback.memory.repositories.in_memory import InMemoryMemoryRepository


def _entry(round_id: str = "r1") -> JournalEntry:
    from datetime import UTC, datetime

    return JournalEntry(
        journal_id=f"j-{round_id}",
        round_id=round_id,
        user_id="u1",
        memory_scope_id="thinkback",
        namespace="thinkback",
        session_id="s1",
        messages=[],
        source_timestamp=datetime.now(UTC),
        round_fingerprint="fp",
    )


def test_single_replica_never_fails_startup() -> None:
    """默认单副本：不做任何检查。"""

    assert_multi_replica_ready(Settings(environment="development"))


def test_multi_replica_without_prerequisites_refuses_to_start() -> None:
    settings = Settings(
        environment="development",
        memory_multi_replica=True,
        memory_l1_cache_enabled=True,
        memory_history_db_shared=False,
    )

    with pytest.raises(RuntimeError, match="多副本部署的前置未齐"):
        assert_multi_replica_ready(settings)


def test_multi_replica_report_lists_every_prerequisite() -> None:
    checks = check_multi_replica(Settings(environment="development"))

    names = [item.name for item in checks]
    assert len(names) == 5
    assert "幂等与限流入 PG" in names
    assert "L1 缓存改读穿透" in names


def test_l1_read_through_is_satisfied_when_cache_disabled() -> None:
    settings = Settings(
        environment="development",
        memory_multi_replica=True,
        memory_l1_cache_enabled=False,
        memory_history_db_shared=True,
    )

    checks = {item.name: item for item in check_multi_replica(settings)}
    assert checks["L1 缓存改读穿透"].satisfied is True
    assert checks["引擎侧历史外置"].satisfied is True
    # 幂等与限流仍未落 PG：这一项没过，多副本依然不能开。
    assert checks["幂等与限流入 PG"].satisfied is False


def test_l1_cache_disabled_always_reads_through() -> None:
    """缓存关掉后 get_l1 永远返回空：调用方据此回源 PG。"""

    repository = InMemoryMemoryRepository(l1_cache_enabled=False)
    repository.update_l1(_entry())

    assert repository.get_l1("u1", "thinkback", "s1") == []
    assert repository.l1_cache == {}


def test_l1_cache_enabled_still_serves_from_cache() -> None:
    repository = InMemoryMemoryRepository()
    repository.update_l1(_entry())

    entries = repository.get_l1("u1", "thinkback", "s1")

    assert len(entries) == 1
