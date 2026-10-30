"""记忆类型注册表：类型定义来自配置，代码不写行业规则。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from thinkback.domain.memory_types import (
    default_registry,
    extract_attribute_name,
    load_registry,
    registry_from_dict,
)


def test_default_registry_covers_current_behaviour() -> None:
    registry = default_registry()

    assert registry.get("profile").extractor == "mem0"
    assert registry.get("preference").source_roles == ("user",)
    # 程序性规则只能由管理员写入且需审批
    assert registry.get("procedure").source_roles == ("admin",)
    assert registry.get("procedure").review_required is True
    # 状态类记忆有保留期
    assert registry.get("state").retention == "ttl"
    assert registry.get("state").ttl_days == 90


#: 默认注册表的**全量**取值。这张表是「不配置 profile 时的行为」的唯一
#: 定义 —— 任何一格被改写，记忆系统就会在没有任何配置的情况下换一个行为。
#:
#: 为什么逐格钉死：默认注册表是一大坨字面量，改坏其中一格（比如把
#: ``event`` 的 source_roles 里的 "tool" 去掉、把 ``state`` 的 ttl_days
#: 从 90 改成 91）不会有任何异常，只会安静地换一种行为。这类变异体
#: 只能靠「逐格断言」杀掉。
DEFAULT_TYPE_TABLE: dict[str, dict[str, object]] = {
    "profile": {
        "extractor": "mem0",
        "source_roles": ("user",),
        "conflict": "overwrite",
        "retention": "forever",
        "ttl_days": None,
        "classification": "personal",
        "review_required": False,
        "recall_budget": None,
        "extraction_prompt": "",
    },
    "preference": {
        "extractor": "mem0",
        "source_roles": ("user",),
        "conflict": "overwrite",
        "retention": "forever",
        "ttl_days": None,
        "classification": "personal",
        "review_required": False,
        "recall_budget": None,
        "extraction_prompt": "",
    },
    "event": {
        "extractor": "mem0",
        "source_roles": ("user", "tool"),
        "conflict": "append",
        "retention": "forever",
        "ttl_days": None,
        "classification": "personal",
        "review_required": False,
        "recall_budget": None,
        "extraction_prompt": "",
    },
    "state": {
        "extractor": "mem0",
        "source_roles": ("user", "tool"),
        "conflict": "overwrite",
        "retention": "ttl",
        "ttl_days": 90,
        "classification": "personal",
        "review_required": False,
        "recall_budget": None,
        "extraction_prompt": "",
    },
    "commitment": {
        "extractor": "mem0",
        "source_roles": ("user", "tool"),
        "conflict": "append",
        "retention": "ttl",
        "ttl_days": 30,
        "classification": "personal",
        "review_required": False,
        "recall_budget": None,
        "extraction_prompt": "",
    },
    "plan": {
        "extractor": "mem0",
        "source_roles": ("user",),
        "conflict": "overwrite",
        "retention": "forever",
        "ttl_days": None,
        "classification": "personal",
        "review_required": False,
        "recall_budget": None,
        "extraction_prompt": "",
    },
    "constraint": {
        "extractor": "mem0",
        "source_roles": ("user",),
        "conflict": "overwrite",
        "retention": "forever",
        "ttl_days": None,
        "classification": "personal",
        "review_required": False,
        "recall_budget": None,
        "extraction_prompt": "",
    },
    "procedure": {
        "extractor": "none",
        "source_roles": ("admin",),
        "conflict": "none",
        "retention": "forever",
        "ttl_days": None,
        "classification": "restricted",
        "review_required": True,
        "recall_budget": None,
        "extraction_prompt": "",
    },
}


def test_the_default_registry_has_exactly_these_types() -> None:
    """多一个或少一个类型都不行 —— 新类型默认生效 = 行为被悄悄改了。"""

    assert default_registry().names() == sorted(DEFAULT_TYPE_TABLE)


@pytest.mark.parametrize("type_name", sorted(DEFAULT_TYPE_TABLE))
def test_every_default_type_keeps_its_exact_policy(type_name: str) -> None:
    """逐格断言默认类型的策略：来源角色、冲突策略、保留期、敏感级别。"""

    spec = default_registry().get(type_name)
    expected = DEFAULT_TYPE_TABLE[type_name]

    assert spec.name == type_name
    assert spec.extractor == expected["extractor"]
    assert spec.source_roles == expected["source_roles"]
    assert spec.conflict == expected["conflict"]
    assert spec.retention == expected["retention"]
    assert spec.ttl_days == expected["ttl_days"]
    assert spec.classification == expected["classification"]
    assert spec.review_required == expected["review_required"]
    assert spec.recall_budget == expected["recall_budget"]
    assert spec.extraction_prompt == expected["extraction_prompt"]


@pytest.mark.parametrize("type_name", sorted(DEFAULT_TYPE_TABLE))
def test_every_default_type_keeps_its_description(type_name: str) -> None:
    """类型说明虽然不参与行为，但它是运维在管理台看到的口径 —— 也钉死。"""

    assert default_registry().get(type_name).description != ""


# ---------------------------------------------------------------------------
# 注册表的边界值与默认值
# ---------------------------------------------------------------------------


def _profile(types: dict[str, dict[str, object]]) -> dict[str, object]:
    return {
        "types": {
            name: {
                "extractor": "mem0",
                "source_roles": ["user"],
                "classification": "personal",
                "conflict": "overwrite",
                "retention": "forever",
                **extra,
            }
            for name, extra in types.items()
        }
    }


def test_a_ttl_of_one_day_is_accepted() -> None:
    """ttl_days 只要为正整数就行 —— 1 天也是合法配置。

    边界在 0 与 1 之间：把 ``<= 0`` 写成 ``<= 1`` 会把「一天后过期」这种
    合法配置判成配置错误，服务起不来。
    """

    registry = registry_from_dict(
        _profile({"x": {"retention": "ttl", "ttl_days": 1}}), namespace="thinkback"
    )

    assert registry.get("x").ttl_days == 1


def test_a_ttl_of_zero_days_is_rejected() -> None:
    with pytest.raises(ValueError, match="ttl_days"):
        registry_from_dict(
            _profile({"x": {"retention": "ttl", "ttl_days": 0}}), namespace="thinkback"
        )


def test_a_recall_budget_of_one_token_is_accepted() -> None:
    """预算为 1 虽然极端，但它是合法配置 —— 边界在 0 与 1 之间。"""

    registry = registry_from_dict(_profile({"x": {"recall": {"budget": 1}}}), namespace="thinkback")

    assert registry.get("x").recall_budget == 1


def test_a_recall_budget_of_zero_is_rejected() -> None:
    """预算为 0 = 该类型一条都放不进召回 —— 配置错误。"""

    with pytest.raises(ValueError, match="budget"):
        registry_from_dict(_profile({"x": {"recall": {"budget": 0}}}), namespace="thinkback")


def test_the_registry_version_defaults_to_one() -> None:
    registry = registry_from_dict(_profile({"x": {}}), namespace="thinkback")

    assert registry.version == "1"


def test_the_description_defaults_to_an_empty_string() -> None:
    """缺 description 不是 None —— 下游拿它做展示文案。"""

    registry = registry_from_dict(_profile({"x": {}}), namespace="thinkback")

    assert registry.get("x").description == ""


def test_review_required_defaults_to_false() -> None:
    registry = registry_from_dict(_profile({"x": {}}), namespace="thinkback")

    assert registry.get("x").review_required is False


def test_an_unregistered_type_names_itself_in_the_error() -> None:
    """报错要指名道姓 —— 配错了要能一眼看出是哪个类型。"""

    with pytest.raises(KeyError, match="no-such-type"):
        default_registry().get("no-such-type")


# ---------------------------------------------------------------------------
# 属性名抽取：冲突键的来源
# ---------------------------------------------------------------------------


def test_the_attribute_name_comes_from_the_first_separator() -> None:
    """「工艺：节点：3nm」取第一个冒号前的「工艺」—— 第一个才是属性名。"""

    assert extract_attribute_name("工艺：节点：3nm") == "工艺"


def test_the_attribute_name_accepts_the_half_width_colon() -> None:
    assert extract_attribute_name("process node: 3nm") == "process node"


def test_the_attribute_name_is_stripped() -> None:
    assert extract_attribute_name("  工艺  ：3nm") == "工艺"


def test_a_text_without_a_separator_has_no_attribute_name() -> None:
    assert extract_attribute_name("用户负责 3nm") is None


def test_a_text_with_an_empty_value_has_no_attribute_name() -> None:
    """冒号后为空 → 取不到属性名（调用方回退到旧口径）。"""

    assert extract_attribute_name("工艺：   ") is None


def test_an_over_long_attribute_name_is_not_taken() -> None:
    """属性名过长 → 不取（它不是属性名，是一整句话）。"""

    assert extract_attribute_name(f"{'x' * 200}：3nm") is None


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


def test_registry_rejects_unknown_fields() -> None:
    """未知字段直接报错，不静默忽略。

    写错字段名而静默忽略是最难查的一类配置问题：配置看起来生效了，实际
    跑的是另一套规则。D29。
    """
    with pytest.raises(ValueError, match="unknown fields"):
        registry_from_dict(
            {
                "types": {
                    "profile": {
                        "extractor": "mem0",
                        "source_roles": ["user"],
                        "conflict": "overwrite",
                        "retention": "forever",
                        "classification": "personal",
                        "entity_vocabulary": ["CMP"],
                    }
                }
            },
            namespace="semiconductor",
        )


def test_registry_requires_core_fields() -> None:
    """缺必填字段直接报错：静默用默认值等于跑另一套策略。"""
    with pytest.raises(ValueError, match="missing required fields"):
        registry_from_dict(
            {"types": {"profile": {"extractor": "mem0"}}},
            namespace="semiconductor",
        )


def test_registry_rejects_ttl_without_days() -> None:
    """retention=ttl 必须给 ttl_days，否则过期语义是假的。"""
    with pytest.raises(ValueError, match="ttl_days"):
        registry_from_dict(
            {
                "types": {
                    "state": {
                        "extractor": "mem0",
                        "source_roles": ["user"],
                        "conflict": "overwrite",
                        "retention": "ttl",
                        "classification": "personal",
                    }
                }
            },
            namespace="semiconductor",
        )


def test_registry_rejects_knobs_without_consumer() -> None:
    """没有运行时消费者的旋钮一律拒绝，不留成"配了也不生效"的假开关。

    这三个旋钮曾经是"空配置"：`layer` 从来没有消费者，`recall.path` 的
    instruction/schedule 两种路径从未实现，`extractor=profile` 与
    `extractor=mem0` 行为完全相同。留着它们只会让接入方以为配了就生效。
    """
    base = {
        "extractor": "mem0",
        "source_roles": ["user"],
        "conflict": "overwrite",
        "retention": "forever",
        "classification": "personal",
    }

    with pytest.raises(ValueError, match="unknown fields"):
        registry_from_dict(
            {"types": {"profile": {**base, "layer": "L3"}}},
            namespace="semiconductor",
        )

    with pytest.raises(ValueError, match="unknown fields"):
        registry_from_dict(
            {"types": {"profile": {**base, "recall": {"path": "fact"}}}},
            namespace="semiconductor",
        )

    with pytest.raises(ValueError, match="unknown extractor"):
        registry_from_dict(
            {"types": {"profile": {**base, "extractor": "profile"}}},
            namespace="semiconductor",
        )


def test_registry_rejects_reviewed_type_accepting_user_role() -> None:
    """要审批的类型不能接受 user 来源：审批就没有意义了。"""
    with pytest.raises(ValueError, match="review_required"):
        registry_from_dict(
            {
                "types": {
                    "procedure": {
                        "extractor": "none",
                        "source_roles": ["user", "admin"],
                        "conflict": "none",
                        "retention": "forever",
                        "classification": "restricted",
                        "review_required": True,
                    }
                }
            },
            namespace="semiconductor",
        )


def test_registry_rejects_prompt_path_escape() -> None:
    """抽取指令路径必须留在 profile 目录内。"""
    with pytest.raises(ValueError, match="relative path"):
        registry_from_dict(
            {
                "types": {
                    "profile": {
                        "extractor": "mem0",
                        "source_roles": ["user"],
                        "conflict": "overwrite",
                        "retention": "forever",
                        "classification": "personal",
                        "extraction_prompt": "../../etc/passwd",
                    }
                }
            },
            namespace="semiconductor",
        )


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
                        "retention": "forever",
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
            {
                "types": {
                    "x": {
                        "extractor": "mem0",
                        "source_roles": ["user"],
                        "conflict": "overwrite",
                        "retention": "forever",
                        "classification": "confidential",
                    }
                }
            },
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


def test_the_attribute_name_length_limit_is_inclusive() -> None:
    """属性名长度上限是**闭区间**：正好 16 个字要收下，17 个字才丢。

    边界写成 ``< 16``（而不是 ``<= 16``）就会把 16 字的名字判成「不像属性名」，
    调用方退回旧口径 —— 事实的归属悄悄变了，界面上看不出来。
    """
    from thinkback.domain.memory_types import ATTRIBUTE_NAME_MAX_LEN, extract_attribute_name

    at_limit = "1" * ATTRIBUTE_NAME_MAX_LEN
    over_limit = "1" * (ATTRIBUTE_NAME_MAX_LEN + 1)

    assert extract_attribute_name(f"{at_limit}：值") == at_limit
    assert extract_attribute_name(f"{over_limit}：值") is None


def test_a_type_without_an_extraction_prompt_gets_empty_prompt_fields() -> None:
    """没配抽取指令的类型：指令正文与版本号都是空串，不是 None。

    写 None 会让「这个类型没有自己的抽取指令」变成「这个类型的抽取指令是
    None」—— 拼 prompt 时才炸，而那时候已经写进记忆了。
    """
    registry = registry_from_dict(_profile({"x": {}}), namespace="thinkback")
    spec = registry.get("x")

    assert spec.extraction_prompt_text == ""
    assert spec.extraction_prompt_version == ""
    assert spec.recall_budget is None
