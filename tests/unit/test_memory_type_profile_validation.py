"""记忆类型 profile 的校验矩阵。

``domain/memory_types.py`` 的 ``registry_from_dict`` 对每一条非法配置
**大声报错**（ADR-0004 / D29）。这条契约是整套「配置看起来生效了，
实际跑的是另一套规则」的防线：任何一条校验被删掉，一个写错的 profile
就会静默加载 —— 企业以为在跑自己的记忆类型规则，实际跑的是内置默认规则。

这类分支全是**只在出错时才走到**的路径，所以行覆盖率天然偏低
（92%），而缺的正好是它们。逐条钉住。

每条用例只改一个字段 —— 这样失败时能一眼看出是哪条校验松了。
"""

from __future__ import annotations

from typing import Any

import pytest

from thinkback.domain.memory_types import registry_from_dict

_NAMESPACE = "semiconductor"

_VALID_TYPE: dict[str, Any] = {
    "extractor": "mem0",
    "source_roles": ["user"],
    "conflict": "overwrite",
    "retention": "forever",
    "classification": "personal",
}


def _payload(
    type_overrides: dict[str, Any] | None = None,
    top_overrides: dict[str, Any] | None = None,
) -> Any:
    """一份合法 profile，只改指定的部分。

    ``type_overrides`` 改类型定义里的字段，``top_overrides`` 改 profile 顶层。
    两层分开 —— 混在一起会把类型字段误放到顶层，报的错也就指错了地方。
    """

    type_def = dict(_VALID_TYPE)
    type_def.update(type_overrides or {})
    payload: dict[str, Any] = {"namespace": _NAMESPACE, "types": {"profile": type_def}}
    payload.update(top_overrides or {})
    return payload


def _expect_rejected(payload: Any, *, match: str = "") -> None:
    with pytest.raises(ValueError) as excinfo:
        registry_from_dict(payload, namespace=_NAMESPACE)
    if match:
        assert match in str(excinfo.value)


# ---------------------------------------------------------------------------
# 顶层
# ---------------------------------------------------------------------------


def test_a_valid_profile_loads() -> None:
    registry = registry_from_dict(_payload(), namespace=_NAMESPACE)
    assert registry.names() == ["profile"]


def test_a_non_object_payload_is_rejected() -> None:
    _expect_rejected([], match="must be an object")


def test_unknown_top_level_fields_are_rejected() -> None:
    """拼错的顶层键必须报错 —— 静默忽略等于「我以为配了」。"""
    _expect_rejected(
        _payload(top_overrides={"version": "v1", "extra": "oops"}), match="unknown fields"
    )


def test_a_non_object_types_field_is_rejected() -> None:
    _expect_rejected(
        _payload(top_overrides={"types": ["profile"]}), match="'types' must be an object"
    )


# ---------------------------------------------------------------------------
# 类型定义
# ---------------------------------------------------------------------------


def test_a_non_object_type_definition_is_rejected() -> None:
    _expect_rejected(
        {"namespace": _NAMESPACE, "types": {"profile": "not-an-object"}},
        match="definition must be an object",
    )


def test_unknown_type_fields_are_rejected() -> None:
    _expect_rejected(_payload(type_overrides={"extractor_policy": "x"}), match="unknown fields")


def test_missing_required_fields_are_rejected() -> None:
    payload = _payload()
    types = payload["types"]
    assert isinstance(types, dict)
    definition = types["profile"]
    assert isinstance(definition, dict)
    del definition["classification"]
    _expect_rejected(payload)


def test_an_unknown_extractor_is_rejected() -> None:
    _expect_rejected(_payload(type_overrides={"extractor": "magic"}), match="unknown extractor")


def test_an_unknown_conflict_policy_is_rejected() -> None:
    _expect_rejected(_payload(type_overrides={"conflict": "sometimes"}), match="unknown conflict")


def test_an_unknown_retention_is_rejected() -> None:
    _expect_rejected(_payload(type_overrides={"retention": "maybe"}), match="unknown retention")


def test_empty_source_roles_are_rejected() -> None:
    _expect_rejected(_payload(type_overrides={"source_roles": []}), match="non-empty list")


def test_non_string_source_roles_are_rejected() -> None:
    _expect_rejected(_payload(type_overrides={"source_roles": [1, 2]}), match="non-empty list")


def test_a_reviewed_type_may_not_accept_the_user_role() -> None:
    """「需要人工复核」的类型不能直接吃用户原话 —— 否则复核无意义。"""
    _expect_rejected(
        _payload(type_overrides={"review_required": True, "source_roles": ["user"]}),
        match="'user' role",
    )


def test_a_reviewed_type_may_accept_a_non_user_role() -> None:
    registry = registry_from_dict(
        _payload(type_overrides={"review_required": True, "source_roles": ["system"]}),
        namespace=_NAMESPACE,
    )
    assert registry.names() == ["profile"]


# ---------------------------------------------------------------------------
# TTL 与召回预算
# ---------------------------------------------------------------------------


def test_a_ttl_retention_needs_a_positive_ttl_days() -> None:
    _expect_rejected(_payload(type_overrides={"retention": "ttl"}), match="ttl_days")


def test_a_non_positive_ttl_days_is_rejected() -> None:
    _expect_rejected(
        _payload(type_overrides={"retention": "ttl", "ttl_days": 0}), match="positive integer"
    )


def test_ttl_days_is_rejected_when_retention_is_forever() -> None:
    """retention=forever 时带 ttl_days 是配置写错 —— 不能默默生效一半。"""
    _expect_rejected(
        _payload(type_overrides={"retention": "forever", "ttl_days": 30}), match="only allowed"
    )


def test_a_non_object_recall_block_is_rejected() -> None:
    _expect_rejected(_payload(type_overrides={"recall": 5}), match="'recall' must be an object")


def test_unknown_recall_fields_are_rejected() -> None:
    _expect_rejected(
        _payload(type_overrides={"recall": {"budget": 10, "extra": 1}}), match="unknown fields"
    )


def test_a_non_positive_recall_budget_is_rejected() -> None:
    _expect_rejected(_payload(type_overrides={"recall": {"budget": 0}}), match="positive integer")


def test_a_boolean_recall_budget_is_rejected() -> None:
    """``True`` 是 int 的子类 —— 不挡下来就会变成「预算 1 token」。"""
    _expect_rejected(
        _payload(type_overrides={"recall": {"budget": True}}), match="positive integer"
    )


def test_a_valid_recall_budget_is_accepted() -> None:
    registry = registry_from_dict(
        _payload(type_overrides={"recall": {"budget": 50}}), namespace=_NAMESPACE
    )
    assert registry.names() == ["profile"]


# ---------------------------------------------------------------------------
# 抽取指令路径
# ---------------------------------------------------------------------------


def test_an_empty_extraction_prompt_path_is_rejected() -> None:
    _expect_rejected(
        _payload(type_overrides={"extraction_prompt": "  "}), match="must not be empty"
    )


def test_an_absolute_extraction_prompt_path_is_rejected() -> None:
    """绝对路径能读到 profile 目录之外的文件 —— 部署面输入必须挡住。"""
    _expect_rejected(
        _payload(type_overrides={"extraction_prompt": "/etc/passwd"}), match="relative path"
    )


def test_a_path_traversal_extraction_prompt_path_is_rejected() -> None:
    _expect_rejected(
        _payload(type_overrides={"extraction_prompt": "../../secret.md"}), match="relative path"
    )


def test_an_extraction_prompt_without_a_loader_is_rejected() -> None:
    _expect_rejected(
        _payload(type_overrides={"extraction_prompt": "prompt.md"}),
        match="requires a prompt loader",
    )


def test_a_valid_extraction_prompt_is_loaded() -> None:
    registry = registry_from_dict(
        _payload(type_overrides={"extraction_prompt": "prompt.md"}),
        namespace=_NAMESPACE,
        prompt_loader=lambda _path: "PROMPT BODY",
    )
    assert registry.names() == ["profile"]
