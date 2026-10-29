"""数据库迁移的契约：启动期迁移必须真跑，失败必须挡住启动。

迁移是部署路径上唯一一段「坏了整个服务起不来」的代码：

- ``alembic.ini`` 找不到（镜像打包漏了）→ 必须明确报错，不能带空配置往下跑；
- ``alembic upgrade head`` 失败 → 必须往上抛，让 lifespan 中断启动。
  吞掉异常等于让服务带着没对齐的结构接流量，写出一半的脏数据。

advisory lock 那层的**接线**由 ``tests/unit/test_ops_boundaries.py`` 静态钉住；
它的**行为**（锁在迁移之前拿、迁移失败也要解锁、引擎一定要释放）在这里用
假引擎钉住 —— 不需要真 PG：advisory lock 的语义就是「拿不到就等」，用假
引擎把调用顺序记下来就能钉死顺序与清理。

这层以前是 0 覆盖的：它存在的唯一理由是「多 Pod 同时启动跑 DDL 会撞
already exists」，而这条最需要保障的路径恰恰没人测 —— 写错了也照样绿。
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest

from thinkback.infra.database import migration


def test_build_alembic_config_fails_when_alembic_ini_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """镜像里漏了 alembic.ini → 明确报错，不是一句看不懂的 alembic 异常。"""
    monkeypatch.setattr(migration, "_PROJECT_ROOT", tmp_path)

    with pytest.raises(FileNotFoundError, match="alembic.ini not found"):
        migration._build_alembic_config()


def test_build_alembic_config_points_at_the_project_alembic_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """script_location 必须指向仓库里的 alembic 目录，别指向别处。"""
    (tmp_path / "alembic.ini").write_text("[alembic]\n", encoding="utf-8")
    monkeypatch.setattr(migration, "_PROJECT_ROOT", tmp_path)

    cfg = migration._build_alembic_config()

    assert cfg.get_main_option("script_location") == str(tmp_path / "alembic")


def test_run_migrations_upgrades_to_head(monkeypatch: pytest.MonkeyPatch) -> None:
    """迁移必须升到 head。"""
    calls: list[tuple[Any, str]] = []

    monkeypatch.setattr(migration, "_build_alembic_config", lambda: "cfg")
    monkeypatch.setattr(migration.command, "upgrade", lambda cfg, rev: calls.append((cfg, rev)))

    migration.run_migrations()

    assert calls == [("cfg", "head")]


def test_run_migrations_reraises_when_alembic_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """迁移失败必须往上抛：吞掉等于带着没对齐的结构接流量。"""

    def _boom(cfg: Any, rev: str) -> None:
        _ = (cfg, rev)
        raise RuntimeError('relation "ins_memory" already exists')

    monkeypatch.setattr(migration, "_build_alembic_config", lambda: "cfg")
    monkeypatch.setattr(migration.command, "upgrade", _boom)

    with pytest.raises(RuntimeError, match="already exists"):
        migration.run_migrations()


@pytest.mark.asyncio
async def test_run_migrations_async_does_not_run_alembic_on_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """alembic 是同步 API，env.py 内部会 asyncio.run —— 放在事件循环里跑会直接炸。

    所以必须丢到工作线程（没有事件循环）里跑。
    """
    seen: dict[str, Any] = {}

    def _fake_run_migrations() -> None:
        seen["thread"] = threading.current_thread()
        seen["has_loop"] = None
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            seen["has_loop"] = False
        else:
            seen["has_loop"] = True

    monkeypatch.setattr(migration, "run_migrations", _fake_run_migrations)

    await migration.run_migrations_async()

    assert seen["has_loop"] is False, "迁移跑在事件循环里：alembic 的 asyncio.run 会抛"
    assert seen["thread"] is not threading.main_thread()


@pytest.mark.asyncio
async def test_run_migrations_serialized_async_goes_through_the_advisory_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """lifespan 用的必须是带锁的那条：与迁移 Job 并发 DDL 会撞「already exists」。"""
    calls: list[str] = []

    monkeypatch.setattr(
        migration, "run_migrations_with_advisory_lock", lambda: calls.append("locked")
    )

    await migration.run_migrations_serialized_async()

    assert calls == ["locked"]


# ---------------------------------------------------------------------------
# advisory lock 的行为契约（假引擎，不需要真 PG）
# ---------------------------------------------------------------------------


class _FakeConn:
    """只记录被执行的 SQL —— advisory lock 的语义全在调用顺序里。"""

    def __init__(self, calls: list[tuple[str, dict[str, Any]]]) -> None:
        self._calls = calls

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> None:
        self._calls.append((str(statement), dict(params or {})))


class _FakeConnect:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *_exc_info: object) -> bool:
        return False


class _FakeEngine:
    def __init__(self, calls: list[tuple[str, dict[str, Any]]]) -> None:
        self._calls = calls
        self.disposed = False

    def connect(self) -> _FakeConnect:
        return _FakeConnect(_FakeConn(self._calls))

    async def dispose(self) -> None:
        self.disposed = True


def _install_fake_engine(
    monkeypatch: pytest.MonkeyPatch, engine: _FakeEngine
) -> list[tuple[str, dict[str, Any]]]:
    """把 ``create_async_engine`` 换成假引擎，返回记录 SQL 的列表。"""
    import sqlalchemy.ext.asyncio as sa_asyncio

    calls: list[tuple[str, dict[str, Any]]] = []
    engine._calls = calls
    monkeypatch.setattr(sa_asyncio, "create_async_engine", lambda *_a, **_k: engine)
    return calls


def test_advisory_lock_is_held_while_the_migrations_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """锁必须在迁移**之前**拿、迁移**之后**才放。

    顺序反了等于没加锁：迁移跑在锁外，两个 Pod 照样并发 DDL。
    """
    engine = _FakeEngine([])
    calls = _install_fake_engine(monkeypatch, engine)
    events: list[str] = []

    def _fake_run_migrations() -> None:
        events.append("migrate")
        # 迁移执行的那一刻，锁必须已经拿在手里。
        events.append(f"locked={len(calls)}")

    monkeypatch.setattr(migration, "run_migrations", _fake_run_migrations)

    migration.run_migrations_with_advisory_lock()

    assert events == ["migrate", "locked=1"], "迁移执行时锁还没拿到"
    assert "pg_advisory_lock" in calls[0][0]
    assert "pg_advisory_unlock" in calls[-1][0]
    assert len(calls) == 2


def test_advisory_lock_is_released_when_the_migrations_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """迁移失败也必须解锁：锁不放 → 下一个 Pod 永远拿不到 → 启动全卡死。"""
    engine = _FakeEngine([])
    calls = _install_fake_engine(monkeypatch, engine)

    def _boom() -> None:
        raise RuntimeError('relation "ins_memory" already exists')

    monkeypatch.setattr(migration, "run_migrations", _boom)

    with pytest.raises(RuntimeError, match="already exists"):
        migration.run_migrations_with_advisory_lock()

    assert any("pg_advisory_unlock" in sql for sql, _ in calls), "迁移失败后没有解锁"
    assert engine.disposed is True, "迁移失败后连接引擎没有释放"


def test_lock_engine_is_disposed_after_a_successful_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """锁连接跑在独立引擎上：不释放就等于每次启动泄漏一个连接池。"""
    engine = _FakeEngine([])
    _install_fake_engine(monkeypatch, engine)
    monkeypatch.setattr(migration, "run_migrations", lambda: None)

    migration.run_migrations_with_advisory_lock()

    assert engine.disposed is True


def test_advisory_lock_uses_one_fixed_key_for_lock_and_unlock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """加锁与解锁必须是同一把 key —— 键不一致等于没解，锁会一直挂着。"""
    engine = _FakeEngine([])
    calls = _install_fake_engine(monkeypatch, engine)
    monkeypatch.setattr(migration, "run_migrations", lambda: None)

    migration.run_migrations_with_advisory_lock()

    lock_params = calls[0][1]
    unlock_params = calls[1][1]
    assert lock_params["lock_id"] == unlock_params["lock_id"]
    assert lock_params["lock_id"] == migration._MIGRATION_LOCK_ID


def test_advisory_lock_covers_every_migration_entrypoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """三条迁移入口都必须经过锁：直跑的那条（Job/脚本）最容易漏。"""
    engine = _FakeEngine([])
    calls = _install_fake_engine(monkeypatch, engine)
    monkeypatch.setattr(migration, "run_migrations", lambda: None)

    migration.run_migrations_with_advisory_lock()

    assert sum("pg_advisory_lock" in sql for sql, _ in calls) == 1
