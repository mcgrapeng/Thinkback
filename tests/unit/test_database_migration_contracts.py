"""数据库迁移的契约：启动期迁移必须真跑，失败必须挡住启动。

迁移是部署路径上唯一一段「坏了整个服务起不来」的代码：

- ``alembic.ini`` 找不到（镜像打包漏了）→ 必须明确报错，不能带空配置往下跑；
- ``alembic upgrade head`` 失败 → 必须往上抛，让 lifespan 中断启动。
  吞掉异常等于让服务带着没对齐的结构接流量，写出一半的脏数据。

带 advisory lock 的那层需要真 PG，单测覆盖不到；它的「必须走同一把锁」的
接线由 ``tests/unit/test_ops_boundaries.py`` 静态钉住。
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
