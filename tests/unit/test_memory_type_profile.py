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
            "profile": {
                "extractor": "mem0",
                "source_roles": ["user"],
                "conflict": "overwrite",
                "retention": "forever",
                "classification": "personal",
            },
            "process_preference": {
                "extractor": "mem0",
                "source_roles": ["user"],
                "conflict": "overwrite",
                "retention": "forever",
                "classification": "personal",
            },
        },
    )
    settings = Settings(memory_type_profile_dir=str(tmp_path))
    provider = build_registry_provider(settings)

    registry = provider("semiconductor")

    assert isinstance(registry, MemoryTypeRegistry)
    assert registry.names() == ["process_preference", "profile"]


def test_a_profile_in_a_later_directory_is_found(tmp_path: Path) -> None:
    """profile 目录可以配多个：企业自己的挂载点在前，镜像内置的在后。

    单目录时企业要新增一个记忆类型就得重建镜像 —— 配置驱动的插件形态
    做不到这件事。
    """
    enterprise_dir = tmp_path / "etc"
    builtin_dir = tmp_path / "app"
    _write_profile(
        enterprise_dir,
        "acme",
        {
            "profile": {
                "extractor": "mem0",
                "source_roles": ["user"],
                "conflict": "overwrite",
                "retention": "forever",
                "classification": "personal",
            }
        },
    )
    settings = Settings(
        memory_type_profile_dir=f"{enterprise_dir},{builtin_dir}",
        environment="production",
    )
    provider = build_registry_provider(settings)

    registry = provider("acme")

    assert registry.names() == ["profile"]


def test_the_first_directory_wins_when_both_hold_the_namespace(tmp_path: Path) -> None:
    """顺序即优先级：先命中的目录说了算，后面的不参与。"""
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    _write_profile(
        first_dir,
        "acme",
        {
            "profile": {
                "extractor": "mem0",
                "source_roles": ["user"],
                "conflict": "overwrite",
                "retention": "forever",
                "classification": "personal",
            }
        },
    )
    _write_profile(
        second_dir,
        "acme",
        {
            "profile": {
                "extractor": "mem0",
                "source_roles": ["user"],
                "conflict": "overwrite",
                "retention": "forever",
                "classification": "personal",
            },
            "only_in_second": {
                "extractor": "mem0",
                "source_roles": ["user"],
                "conflict": "append",
                "retention": "forever",
                "classification": "personal",
            },
        },
    )
    settings = Settings(
        memory_type_profile_dir=f"{first_dir},{second_dir}",
        environment="production",
    )
    provider = build_registry_provider(settings)

    registry = provider("acme")

    assert registry.names() == ["profile"]


def test_the_extraction_prompt_resolves_against_the_directory_that_supplied_the_profile(
    tmp_path: Path,
) -> None:
    """指令文件相对命中 profile 的那个目录解析，不是相对第一个目录。"""
    enterprise_dir = tmp_path / "etc"
    builtin_dir = tmp_path / "app"
    (enterprise_dir / "prompts").mkdir(parents=True)
    (enterprise_dir / "prompts" / "rule.v1.md").write_text("企业自己的指令", encoding="utf-8")
    _write_profile(
        enterprise_dir,
        "acme",
        {
            "profile": {
                "extractor": "mem0",
                "source_roles": ["user"],
                "conflict": "overwrite",
                "retention": "forever",
                "classification": "personal",
                "extraction_prompt": "prompts/rule.v1.md",
            }
        },
    )
    settings = Settings(
        memory_type_profile_dir=f"{enterprise_dir},{builtin_dir}",
        environment="production",
    )
    provider = build_registry_provider(settings)

    registry = provider("acme")

    assert registry.get("profile").extraction_prompt_text == "企业自己的指令"
    assert registry.get("profile").extraction_prompt_version == "v1"


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
    settings = Settings(memory_type_profile_dir=str(tmp_path), environment="production")
    provider = build_registry_provider(settings)

    with pytest.raises(RuntimeError, match="no memory type profile"):
        provider("semiconductor")


def test_production_fails_when_profile_invalid(tmp_path: Path) -> None:
    """生产环境：profile 损坏 → 直接报错，不静默换策略。"""
    _write_profile(tmp_path, "semiconductor", {"profile": {"extractor": "profile"}})
    (tmp_path / "semiconductor.json").write_text("{ broken json", encoding="utf-8")
    settings = Settings(memory_type_profile_dir=str(tmp_path), environment="production")
    provider = build_registry_provider(settings)

    with pytest.raises(RuntimeError, match="could not be loaded"):
        provider("semiconductor")


def test_production_allows_default_namespace_without_profile(tmp_path: Path) -> None:
    """生产环境：默认 namespace 没有 profile 是正常的（内置类型集）。"""
    settings = Settings(memory_type_profile_dir=str(tmp_path), environment="production")
    provider = build_registry_provider(settings)

    registry = provider("thinkback")

    assert "profile" in registry.names()


def test_profile_is_loaded_at_call_time_not_cached(tmp_path: Path) -> None:
    """profile 每次调用都重读盘：新增/修改 profile 不需要重启服务。

    这是「接入新企业 = 加一份配置」的底座：profile 目录由 ConfigMap 挂进
    容器，文件一落地就能被下一次调用读到。
    """
    settings = Settings(memory_type_profile_dir=str(tmp_path))
    provider = build_registry_provider(settings)

    _write_profile(
        tmp_path,
        "acme",
        {
            "profile": {
                "extractor": "mem0",
                "source_roles": ["user"],
                "conflict": "overwrite",
                "retention": "forever",
                "classification": "personal",
            }
        },
    )

    registry = provider("acme")

    assert registry.names() == ["profile"]
    assert registry.namespace == "acme"


def test_docker_image_ships_profiles() -> None:
    """镜像必须带上 profiles/：否则容器里的行业 namespace 全部回落默认。"""
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    assert "COPY profiles/ /app/profiles/" in dockerfile


async def test_admin_validate_endpoint_reports_every_profile_file(tmp_path: Path) -> None:
    """治理台的配置校验端点：逐份报告，坏文件不遮蔽好文件。

    生产模式下非法 profile 会让该 namespace 的每次请求都失败（fail-closed），
    部署方需要一条独立于请求路径的校验通道 —— 用运行时同一个加载器试载，
    报告才与实际生效的行为一致。
    """
    from fastapi.testclient import TestClient

    from thinkback.api.app import create_app
    from thinkback.infra.config import settings

    good_dir = tmp_path / "profiles"
    good_dir.mkdir()
    (good_dir / "acme.json").write_text(
        json.dumps(
            {
                "namespace": "acme",
                "types": {
                    "profile": {
                        "extractor": "mem0",
                        "source_roles": ["user"],
                        "conflict": "overwrite",
                        "retention": "forever",
                        "classification": "personal",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (good_dir / "broken.json").write_text("{ not json", encoding="utf-8")

    # 端点在请求时读 settings 单例：补丁要覆盖整个请求，不能建完 app 就还原。
    # 不用 `with TestClient(...)`：进上下文会触发 lifespan 的数据库迁移，
    # mutants/ 审计树里没有 alembic.ini —— 那里也要能跑这条用例。
    original_dir = settings.memory_type_profile_dir
    settings.memory_type_profile_dir = str(good_dir)
    try:
        app = create_app()
        client = TestClient(app)
        response = client.get("/admin/api/memory-types/validate")
    finally:
        settings.memory_type_profile_dir = original_dir

    assert response.status_code == 200
    payload = response.json()
    assert payload["validated"] == 2
    assert payload["ok"] is False
    by_file = {item["file"]: item for item in payload["files"]}
    assert by_file["acme.json"]["ok"] is True
    assert by_file["acme.json"]["types"] == 1
    assert by_file["broken.json"]["ok"] is False
    assert "JSONDecodeError" in by_file["broken.json"]["error"]
