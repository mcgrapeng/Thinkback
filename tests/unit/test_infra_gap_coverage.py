"""infra 杂项的行覆盖缺口收口（milvus.py 导入守卫 / token_bucket 容量边界 / config URL 拼装）。

replica_guard.py:90-91 不在此文件补测：`check_multi_replica` 里「幂等与限流入 PG」
前置硬编码 satisfied=False，`assert_multi_replica_ready` 在多副本模式下必然进入
报错分支，`not missing` 的成功分支当前不可达（防御性死代码）。
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from thinkback.infra.config import Settings
from thinkback.infra.milvus import ensure_milvus_collection_consistency, ensure_milvus_database
from thinkback.infra.token_bucket import BucketRegistry


def test_ensure_milvus_database_without_pymilvus_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "pymilvus", None)

    with pytest.raises(RuntimeError, match="pymilvus is not installed; install pymilvus"):
        ensure_milvus_database(milvus_url="http://m:19530", milvus_database="thinkback")


def test_consistency_without_pymilvus_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "pymilvus", None)

    with pytest.raises(RuntimeError, match="pymilvus is not installed; install pymilvus"):
        ensure_milvus_collection_consistency(
            milvus_url="http://m:19530",
            milvus_database="thinkback",
            collection_name="c",
        )


def test_zero_capacity_registry_still_admits_the_first_key() -> None:
    """max_buckets=0 时首个 key 也要能服务：淘汰守卫在空表上安全返回。"""
    registry = BucketRegistry(rate_limit_per_minute=60, burst=1, max_buckets=0)

    ok, wait = registry.consume("k")

    assert ok is True
    assert wait == 0.0


def _settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Any, **overrides: str) -> Settings:
    env_file = tmp_path / "empty.env"
    env_file.write_text("")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    return Settings(_env_file=str(env_file), **overrides)


def test_database_url_rewrites_postgresql_scheme(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """postgresql:// 前缀也要改写成 asyncpg 驱动（与 postgres:// 同口径）。"""
    settings = _settings(
        monkeypatch,
        tmp_path,
        raw_database_url="postgresql://user:pass@localhost:5432/thinkback",
    )

    assert settings.database_url == "postgresql+asyncpg://user:pass@localhost:5432/thinkback"


def test_database_url_passes_through_when_driver_already_specified(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """已带驱动后缀的 URL 原样返回，不做二次改写。"""
    settings = _settings(
        monkeypatch,
        tmp_path,
        raw_database_url="postgresql+asyncpg://user:pass@localhost:5432/thinkback",
    )

    assert settings.database_url == "postgresql+asyncpg://user:pass@localhost:5432/thinkback"
