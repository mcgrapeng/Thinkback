"""记忆类型 profile 的装载与失败可见性。

profile 决定某个 namespace 用哪套记忆类型规则。装载失败时静默回落到默认
注册表，等于「配置看起来生效了，实际跑的是另一套规则」——生产环境必须
报错，开发环境才允许告警回落。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from thinkback.api.dependencies import build_registry_provider
from thinkback.domain.memory_types import MemoryTypeRegistry
from thinkback.infra.config import Settings


def _write_profile(profile_dir: Path, namespace: str, types: dict[str, dict]) -> None:
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / f"{namespace}.json").write_text(
        json.dumps({"namespace": namespace, "types": types}, ensure_ascii=False),
        encoding="utf-8",
    )


def test_profile_defines_namespace_types(tmp_path: Path) -> None:
    """namespace 的类型来自自己的 profile 文件。"""
    _write_profile(
        tmp_path,
        "semiconductor",
        {
            "profile": {"extractor": "profile", "source_roles": ["user"]},
            "process_preference": {"extractor": "mem0", "source_roles": ["user"]},
        },
    )
    settings = Settings(memory_type_profile_dir=str(tmp_path))
    provider = build_registry_provider(settings)

    registry = provider("semiconductor")

    assert isinstance(registry, MemoryTypeRegistry)
    assert registry.names() == ["process_preference", "profile"]


def test_development_falls_back_to_default_registry(tmp_path: Path) -> None:
    """开发环境：profile 缺失或损坏 → 告警回落，不挡启动。"""
    settings = Settings(memory_type_profile_dir=str(tmp_path), environment="development")
    provider = build_registry_provider(settings)

    registry = provider("semiconductor")

    assert "profile" in registry.names()
    assert "preference" in registry.names()
    # 回落到的是**内置默认**注册表，不是某个 namespace 的 profile。
    assert "process_preference" not in registry.names()


def test_production_fails_when_profile_missing(tmp_path: Path) -> None:
    """生产环境：非默认 namespace 缺 profile → 直接报错。"""
    settings = Settings(
        memory_type_profile_dir=str(tmp_path), environment="production", admin_token="t"
    )
    provider = build_registry_provider(settings)

    with pytest.raises(RuntimeError, match="no memory type profile"):
        provider("semiconductor")


def test_production_fails_when_profile_invalid(tmp_path: Path) -> None:
    """生产环境：profile 损坏 → 直接报错，不静默换策略。"""
    _write_profile(tmp_path, "semiconductor", {"profile": {"extractor": "profile"}})
    (tmp_path / "semiconductor.json").write_text("{ broken json", encoding="utf-8")
    settings = Settings(
        memory_type_profile_dir=str(tmp_path), environment="production", admin_token="t"
    )
    provider = build_registry_provider(settings)

    with pytest.raises(RuntimeError, match="could not be loaded"):
        provider("semiconductor")


def test_production_allows_default_namespace_without_profile(tmp_path: Path) -> None:
    """生产环境：默认 namespace 没有 profile 是正常的（内置类型集）。"""
    settings = Settings(
        memory_type_profile_dir=str(tmp_path), environment="production", admin_token="t"
    )
    provider = build_registry_provider(settings)

    registry = provider("thinkback")

    assert "profile" in registry.names()


def test_docker_image_ships_profiles() -> None:
    """镜像必须带上 profiles/：否则容器里的行业 namespace 全部回落默认。"""
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    assert "COPY profiles/ /app/profiles/" in dockerfile
