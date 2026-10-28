"""记忆类型注册表：类型定义来自配置，代码不写行业规则。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from thinkback.domain.memory_types import (
    default_registry,
    load_registry,
    registry_from_dict,
)


def test_default_registry_covers_current_behaviour() -> None:
    registry = default_registry()

    assert registry.get("profile").extractor == "profile"
    assert registry.get("preference").source_roles == ("user",)
    # 程序性规则只能由管理员写入且需审批
    assert registry.get("procedure").source_roles == ("admin",)
    assert registry.get("procedure").review_required is True
    # 状态类记忆有保留期
    assert registry.get("state").retention == "ttl"
    assert registry.get("state").ttl_days == 90


def test_unregistered_type_raises() -> None:
    registry = default_registry()

    with pytest.raises(KeyError):
        registry.get("not-registered")
    assert registry.has("not-registered") is False


def test_assistant_is_not_a_default_source_role() -> None:
    registry = default_registry()

    assert registry.get("preference").allows_role("user") is True
    assert registry.get("preference").allows_role("assistant") is False
    assert registry.get("preference").allows_extraction() is True
    assert registry.get("procedure").allows_extraction() is False


def test_registry_from_dict_keeps_config_values() -> None:
    registry = registry_from_dict(
        {
            "types": {
                "yield_watch": {
                    "extractor": "mem0",
                    "source_roles": ["user", "tool"],
                    "conflict": "append",
                    "retention": "ttl",
                    "ttl_days": 90,
                    "classification": "personal",
                    "description": "良率与缺陷密度的观察",
                }
            }
        },
        namespace="semiconductor",
    )

    spec = registry.get("yield_watch")
    assert spec.retention == "ttl"
    assert spec.ttl_days == 90
    assert spec.classification == "personal"
    assert spec.conflict == "append"
    assert spec.description == "良率与缺陷密度的观察"
    assert registry.namespace == "semiconductor"


def test_registry_drops_config_without_behavior() -> None:
    """没有行为消费者的配置项直接不认：不能给人「配了就生效」的错觉。"""
    registry = registry_from_dict(
        {
            "types": {
                "profile": {
                    "extractor": "mem0",
                    "entity_vocabulary": ["CMP"],
                    "recall_budget": 400,
                    "layer": "L3",
                }
            }
        },
        namespace="semiconductor",
    )

    spec = registry.get("profile")
    assert not hasattr(spec, "entity_vocabulary")
    assert not hasattr(spec, "recall_budget")


def test_load_registry_reads_namespace_profile(tmp_path: Path) -> None:
    (tmp_path / "semiconductor.json").write_text(
        json.dumps(
            {
                "types": {
                    "process_preference": {
                        "extractor": "mem0",
                        "source_roles": ["user"],
                        "classification": "personal",
                        "conflict": "overwrite",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    registry = load_registry(tmp_path, "semiconductor")
    assert registry.names() == ["process_preference"]
    assert registry.get("process_preference").classification == "personal"

    # 没有 profile 的 namespace 回落到默认注册表
    fallback = load_registry(tmp_path, "unknown")
    assert "profile" in fallback.names()


def test_registry_is_immutable() -> None:
    registry = default_registry()

    with pytest.raises(TypeError):
        registry.types["x"] = registry.get("profile")  # type: ignore[index]


def test_unknown_classification_in_profile_is_rejected_at_load() -> None:
    """配置里的分类级别必须是 DataClassification 的取值，载入时就报错。"""
    with pytest.raises(ValueError, match="unknown classification"):
        registry_from_dict(
            {"types": {"x": {"classification": "confidential"}}},
            namespace="semiconductor",
        )


def test_shipped_semiconductor_profile_loads() -> None:
    """随仓库提供的半导体 profile 必须能被加载且自洽。"""
    from pathlib import Path

    profile_dir = Path(__file__).resolve().parents[2] / "profiles"
    registry = load_registry(profile_dir, "semiconductor")

    assert "process_preference" in registry.names()
    assert "procedure" in registry.names()
    # 程序性规则必须要求审批
    assert registry.get("procedure").review_required is True
    # 时效性类型必须带 TTL
    assert registry.get("state").ttl_days == 90
    # 冲突策略按类型区分：设备上下文是追加型，工艺偏好是覆盖型
    assert registry.get("equipment_context").conflict == "append"
    assert registry.get("process_preference").conflict == "overwrite"
