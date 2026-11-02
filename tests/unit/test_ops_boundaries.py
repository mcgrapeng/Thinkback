"""运维边界回归测试。

钉死三件「谁在什么时候能写数据库 / 后端」的事：

1. 迁移必须走 advisory lock（lifespan 与迁移 Job 共用一把锁）；
2. 迁移 Job 不能裸跑 alembic（否则与应用 lifespan 并发 DDL）；
3. API 副本数保持单写者。
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from thinkback.infra.database import migration

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_migration_wrapper_delegates_to_advisory_lock_variant(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """lifespan 用的异步包装必须落到带锁的同步实现上。"""
    calls: list[str] = []

    def _fake_locked_migration() -> None:
        calls.append(threading.current_thread().name)

    monkeypatch.setattr(migration, "run_migrations_with_advisory_lock", _fake_locked_migration)

    asyncio.run(migration.run_migrations_serialized_async())

    assert len(calls) == 1
    # 迁移是同步的（alembic 自己 asyncio.run），必须离开事件循环线程。
    assert calls[0] != threading.current_thread().name


def test_migration_lock_id_is_a_stable_constant() -> None:
    """锁 key 必须是常量：每次调用换一把锁等于没有锁。"""
    assert isinstance(migration._MIGRATION_LOCK_ID, int)
    assert migration._MIGRATION_LOCK_ID == 0x7E1BA6C0


def test_migrate_job_uses_locked_entrypoint() -> None:
    """迁移 Job 必须与应用 lifespan 走同一把 advisory lock。"""
    manifest = (REPO_ROOT / "k8s" / "job-migrate.yaml").read_text(encoding="utf-8")
    assert "thinkback.infra.database.migration" in manifest
    assert "alembic" not in manifest.replace("alembic upgrade", "")


def test_api_deployment_keeps_single_replica() -> None:
    """单写者：多副本 + 每次启动都迁移 = 并发 DDL。"""
    manifest = (REPO_ROOT / "k8s" / "deployment-api.yaml").read_text(encoding="utf-8")
    assert "replicas: 1" in manifest


def test_k8s_serves_memory_type_profiles_without_rebuilding_the_image() -> None:
    """企业新增记忆类型只能改配置，不该要求重建镜像。

    profile 目录配成「两个目录按顺序找」：企业自己的挂载点在前，镜像内置的
    在后。ConfigMap 是 optional 的 —— 没建它时挂成空目录，服务照常起，
    注册表回落到镜像内置的那份。
    """
    configmap = (REPO_ROOT / "k8s" / "configmap.yaml").read_text(encoding="utf-8")
    deployment = (REPO_ROOT / "k8s" / "deployment-api.yaml").read_text(encoding="utf-8")

    assert "MEMORY_TYPE_PROFILE_DIR" in configmap
    assert "/etc/thinkback/profiles" in configmap
    # 镜像内置的目录必须在搜索路径里，否则挂了企业目录就把内置的遮掉了。
    assert "/app/profiles" in configmap

    assert "name: profiles" in deployment
    assert "mountPath: /etc/thinkback/profiles" in deployment
    assert "name: thinkback-profiles" in deployment
    assert "optional: true" in deployment


def test_k8s_exposes_the_namespace_allowlist_setting() -> None:
    """部署口径的 namespace 白名单要能被配上。

    不配 = 不限制（保持内网免鉴权下的既有行为），但「一个部署服务一个租户」
    这条约定得有一个能落地的旋钮 —— 否则它只活在文档里。清单里要有这个键，
    接入方才知道去哪儿填。
    """
    configmap = (REPO_ROOT / "k8s" / "configmap.yaml").read_text(encoding="utf-8")

    assert "MEMORY_ALLOWED_NAMESPACES" in configmap
