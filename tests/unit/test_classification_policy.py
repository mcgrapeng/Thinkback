"""数据分类准入策略的契约。

``domain/classification.py`` 是「敏感数据不许落库」这条承诺的**唯一执行点**。
分类原先只是被写进 metadata，数据照样落库、照样发给 LLM 和 embedding ——
分类形同虚设。现在它是一个可独立测试的判定。

这条判定一旦被绕过，后果不是「分类不准」，是**受限数据进了向量库**：
凭据、身份证、健康数据都可能被检索出来注入模型上下文。

``strictest_classification`` 则是防止**降级**：类型策略声明 sensitive、
调用方声明 normal 时，取更严格的一级 —— 调用方不能把自己的敏感数据
降级成普通数据写进去。

这些分支此前只在 ``test_memory_service.py`` 里被间接用到（而那个文件
不在变异审计的测试选择里），所以变异体全部存活。逐条钉住。
"""

from __future__ import annotations

import pytest

from thinkback.domain.classification import (
    assert_storable,
    blocks_storage,
    resolve_classification,
    strictest_classification,
)

# ---------------------------------------------------------------------------
# 分类解析
# ---------------------------------------------------------------------------


def test_a_missing_classification_defaults_to_personal() -> None:
    assert resolve_classification(None) == "personal"
    assert resolve_classification({}) == "personal"


def test_an_empty_classification_defaults_to_personal() -> None:
    assert resolve_classification({"data_classification": ""}) == "personal"


def test_an_explicit_classification_is_kept() -> None:
    assert resolve_classification({"data_classification": "normal"}) == "normal"
    assert resolve_classification({"data_classification": "sensitive"}) == "sensitive"


def test_an_unknown_classification_is_rejected() -> None:
    """未知值直接报错 —— 静默降成 personal 会让调用方以为已按敏感级处理。"""
    with pytest.raises(ValueError, match="unknown data_classification"):
        resolve_classification({"data_classification": "top-secret"})


# ---------------------------------------------------------------------------
# 存储闸门
# ---------------------------------------------------------------------------


def test_sensitive_and_restricted_block_storage() -> None:
    assert blocks_storage("sensitive") is True
    assert blocks_storage("restricted") is True


def test_normal_and_personal_allow_storage() -> None:
    assert blocks_storage("normal") is False
    assert blocks_storage("personal") is False


def test_assert_storable_rejects_sensitive_data() -> None:
    with pytest.raises(ValueError, match="sensitive_or_restricted"):
        assert_storable("sensitive")


def test_assert_storable_rejects_restricted_data() -> None:
    with pytest.raises(ValueError, match="sensitive_or_restricted"):
        assert_storable("restricted")


def test_assert_storable_accepts_normal_data() -> None:
    assert_storable("normal")


def test_assert_storable_accepts_personal_data() -> None:
    assert_storable("personal")


# ---------------------------------------------------------------------------
# 取最严格的一级
# ---------------------------------------------------------------------------


def test_the_strictest_classification_wins() -> None:
    assert strictest_classification("normal", "sensitive") == "sensitive"
    assert strictest_classification("personal", "restricted") == "restricted"
    assert strictest_classification("normal", "personal") == "personal"


def test_an_unknown_classification_in_the_set_is_rejected() -> None:
    """未知值直接报错，不静默放行 —— 否则调用方能靠拼错值绕过闸门。"""
    with pytest.raises(ValueError, match="unknown data_classification"):
        strictest_classification("normal", "ultra-secret")


def test_no_classification_falls_back_to_personal() -> None:
    assert strictest_classification() == "personal"
    assert strictest_classification("", "") == "personal"


def test_empty_strings_are_ignored() -> None:
    assert strictest_classification("", "sensitive") == "sensitive"
