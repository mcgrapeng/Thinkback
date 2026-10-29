"""数据分类准入判定的契约测试。

``domain/classification.py`` 是**敏感数据能不能落库**的唯一判定点。它的三条
承诺此前都没有被钉住：

1. 未知分类直接报错（不静默降级成 personal）；
2. 调用方不能把策略降级（取最严格的一级）；
3. SENSITIVE / RESTRICTED 一律拒绝存储。

这三个判定都是「错了不会报错、只会静默放行或静默丢数据」的类型 ——
上层用例全绿也不代表这里被测过。这一层单独钉死。
"""

from __future__ import annotations

from typing import Any

import pytest

from thinkback.domain.classification import (
    assert_storable,
    blocks_storage,
    resolve_classification,
    strictest_classification,
)


def test_absent_metadata_falls_back_to_personal() -> None:
    """没给 metadata → 按个人数据处理（历史默认）。"""
    assert resolve_classification(None) == "personal"


def test_empty_metadata_falls_back_to_personal() -> None:
    assert resolve_classification({}) == "personal"


def test_metadata_without_the_classification_key_falls_back_to_personal() -> None:
    """键缺失与键存在但值为空是两条不同的路，都归到默认分类。"""
    assert resolve_classification({"other": "x"}) == "personal"


def test_empty_classification_value_falls_back_to_personal() -> None:
    """``data_classification: ""`` 是「没声明」，不是「非法值」。

    非法值要报错；空串报错会让「按默认值填」的调用方整轮失败。
    """
    assert resolve_classification({"data_classification": ""}) == "personal"


def test_declared_classification_is_returned_verbatim() -> None:
    """声明了就按声明的走 —— 降级只能由更严的一级发生。"""
    assert resolve_classification({"data_classification": "sensitive"}) == "sensitive"
    assert resolve_classification({"data_classification": "normal"}) == "normal"


def test_classification_key_lookup_is_case_sensitive() -> None:
    """键名大小写不同就是没声明 —— 不做「宽容匹配」。

    宽容匹配会让 ``DATA_CLASSIFICATION`` 与 ``data_classification`` 在不同
    调用方手里表示两件事，而数据库里只存了一种。
    """
    assert resolve_classification({"DATA_CLASSIFICATION": "sensitive"}) == "personal"


def test_unknown_classification_raises_with_the_offending_value() -> None:
    """未知值报错，且消息里带上那个值 —— 静默降级等于按更低级别存了下去。"""
    with pytest.raises(ValueError, match="unknown data_classification") as excinfo:
        resolve_classification({"data_classification": "top-secret"})
    assert "top-secret" in str(excinfo.value)


@pytest.mark.parametrize("classification", ["sensitive", "restricted"])
def test_sensitive_and_restricted_block_storage(classification: str) -> None:
    assert blocks_storage(classification) is True


@pytest.mark.parametrize("classification", ["normal", "personal"])
def test_normal_and_personal_do_not_block_storage(classification: str) -> None:
    assert blocks_storage(classification) is False


@pytest.mark.parametrize("classification", ["sensitive", "restricted"])
def test_assert_storable_rejects_blocked_classifications(classification: str) -> None:
    """禁止级别必须真的抛 —— 漏抛就是敏感数据落库。"""
    with pytest.raises(ValueError) as excinfo:
        assert_storable(classification)
    assert "sensitive_or_restricted" in str(excinfo.value)
    # 消息里要带上具体级别，客户端才知道是哪一级被拒。
    assert classification in str(excinfo.value)


@pytest.mark.parametrize("classification", ["normal", "personal"])
def test_assert_storable_allows_storable_classifications(classification: str) -> None:
    assert_storable(classification)


def test_strictest_picks_the_most_restrictive() -> None:
    """取最严的一级：调用方不能把 sensitive 类型降级成 normal 写入。"""
    assert strictest_classification("normal", "sensitive") == "sensitive"
    assert strictest_classification("personal", "restricted") == "restricted"


def test_strictest_does_not_follow_alphabetical_order() -> None:
    """严格性顺序是 normal < personal < sensitive < restricted。

    这不是字典序 —— ``restricted`` 在字典序里小于 ``sensitive``，按字典序取最大
    会把 restricted 判成 personal 级别放行。
    """
    assert strictest_classification("restricted", "sensitive") == "restricted"


def test_strictest_with_no_arguments_falls_back_to_personal() -> None:
    assert strictest_classification() == "personal"


def test_strictest_ignores_empty_arguments() -> None:
    """空串等于「没声明」—— 不该因为它而报错，也不该让它盖掉真实级别。"""
    assert strictest_classification("", "sensitive") == "sensitive"
    assert strictest_classification("", "") == "personal"


def test_strictest_rejects_unknown_values_with_the_offending_value() -> None:
    with pytest.raises(ValueError, match="unknown data_classification") as excinfo:
        strictest_classification("normal", "not-a-level")
    assert "not-a-level" in str(excinfo.value)


def test_strictest_accepts_a_single_known_value() -> None:
    assert strictest_classification("sensitive") == "sensitive"


def test_classification_metadata_may_carry_unrelated_keys() -> None:
    """metadata 里别的键不该影响分类判定。"""
    metadata: dict[str, Any] = {"data_classification": "personal", "tenant": "acme"}
    assert resolve_classification(metadata) == "personal"
