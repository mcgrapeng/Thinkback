"""上下文别名表的**子串匹配**语义。

``domain/slots/query_slots.context_terms`` 的别名表按**子串**匹配源文本，
不是按整词。后果有两面：

- 好处：``communication`` 里含着 ``cat``，中文记忆「沟通」与英文记忆
  ``communication`` 因此能对上同一个上下文词；
- 代价：``doghouse`` 会被归一成 ``dog``。

这个语义是现状，也是跨语言归一能工作的原因，所以**写死**——要改成整词
匹配，就得连这些断言一起改，不能悄悄变。

每条用例让别名只出现在一个更长的词里：改坏别名的键或值，对应的那条
立刻不成立。
"""

from __future__ import annotations

import pytest

from thinkback.domain.slots.query_slots import context_terms

#: (别名, 归一后的规范词) —— 别名表里的英文条目。
ENGLISH_ALIASES = [
    ("cat", "cat"),
    ("dog", "dog"),
    ("bird", "bird"),
    ("parrot", "bird"),
    ("rabbit", "rabbit"),
    ("bunny", "rabbit"),
    ("coffee", "coffee"),
    ("tea", "tea"),
    ("drink", "drink"),
    ("food", "food"),
    ("job", "job"),
    ("offer", "job"),
    ("birthday", "birthday"),
    ("advice", "advice"),
    ("communication", "communication"),
    ("sleep", "sleep"),
    ("city", "city"),
]


@pytest.mark.parametrize(("alias", "canonical"), ENGLISH_ALIASES)
def test_an_english_alias_also_matches_inside_a_longer_word(alias: str, canonical: str) -> None:
    """别名按子串匹配：长词里的别名同样参与归一。

    整词匹配会让 ``communication`` 里的 ``cat`` 失效 —— 而那正是中文记忆
    「沟通」与英文记忆对得上的原因之一。
    """

    assert canonical in context_terms(f"{alias}land")


def test_the_english_alias_is_normalized_to_its_canonical_term() -> None:
    """别名归一到规范词：``dog`` / ``dogs`` / ``doghouse`` 都归成 ``dog``。"""

    assert "dog" in context_terms("dog")
    assert "dog" in context_terms("dogs")
    assert "dog" in context_terms("doghouse")


def test_a_chinese_marker_normalizes_to_the_same_canonical_term() -> None:
    """中英同义归一到同一个词 —— 这是跨语言召回能对上号的前提。"""

    assert "cat" in context_terms("猫")
    assert "cat" in context_terms("cat")
    assert context_terms("猫") & context_terms("cat")
