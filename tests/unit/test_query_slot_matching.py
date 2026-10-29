"""查询侧槽位识别与上下文准入的契约测试。

``domain/slots/query_slots.py`` 决定「这次召回要带哪些槽位的事实」以及
「一条记忆能不能进这次的上下文包」。两条判定此前只有间接覆盖：

- ``memory_context_allowed``：非槽位查询时，记忆必须与 query 共享至少一个
  上下文词。判据写松了 → 无关记忆混进上下文包（模型被干扰）；
  判据写死了 → 相关记忆被挡掉（模型答不出）。
- ``query_is_broad_memory_request``：「用户提过什么」这类宽泛请求要放开
  全部记忆，判据漏了 → 宽泛提问只召回一条。

``query_conflict_slots`` 的宠物分支此前只覆盖到猫/狗/鸟，兔子那条没走到。
"""

from __future__ import annotations

from thinkback.domain.slots.query_slots import (
    context_terms,
    memory_context_allowed,
    query_conflict_slots,
    query_is_broad_memory_request,
)

# ---------------------------------------------------------------------------
# 槽位识别
# ---------------------------------------------------------------------------


def test_pet_name_questions_map_to_the_pet_slot() -> None:
    assert "pet_name:cat" in query_conflict_slots("我的猫叫什么名字")
    assert "pet_name:dog" in query_conflict_slots("我的狗叫什么")
    assert "pet_name:bird" in query_conflict_slots("我的鹦鹉叫什么名字")


def test_rabbit_pet_name_question_maps_to_the_rabbit_slot() -> None:
    """兔子分支此前没走到 —— 加宠物种类时最容易漏的那一条。"""
    assert "pet_name:rabbit" in query_conflict_slots("我的兔子叫什么名字")


def test_a_question_without_a_pet_marker_has_no_pet_slot() -> None:
    assert not [
        slot for slot in query_conflict_slots("我叫什么名字") if slot.startswith("pet_name:")
    ]


def test_compound_question_reports_every_slot_it_hits() -> None:
    """复合提问命中多个槽位 —— 调用方据此决定要不要放开全部记忆。"""
    slots = query_conflict_slots("我的生日是什么时候，我住在哪里")
    assert "current_location" in slots
    assert "birthday" in slots


# ---------------------------------------------------------------------------
# 宽泛记忆请求
# ---------------------------------------------------------------------------


def test_broad_memory_requests_are_recognized() -> None:
    assert query_is_broad_memory_request("用户之前提过什么重要信息") is True
    assert query_is_broad_memory_request("what did the user mention recently") is True


def test_a_specific_question_is_not_a_broad_request() -> None:
    assert query_is_broad_memory_request("我的生日是什么时候") is False


# ---------------------------------------------------------------------------
# 上下文词表
# ---------------------------------------------------------------------------


def test_context_terms_normalize_chinese_and_english_aliases() -> None:
    """中英别名归一到同一个词 —— 「咖啡」与「coffee」要能对上。"""
    assert "coffee" in context_terms("用户喜欢喝咖啡")
    assert "coffee" in context_terms("user likes coffee")


def test_context_terms_drop_stop_words() -> None:
    """the/their/user 之类的高频词不带语义，留下只会制造假交集。"""
    terms = context_terms("the user mentioned their coffee")
    assert "the" not in terms
    assert "user" not in terms
    assert "their" not in terms
    assert "coffee" in terms


def test_context_terms_extract_chinese_ngrams() -> None:
    """中文没有空格，靠 n-gram 建词表。"""
    terms = context_terms("用户喜欢喝咖啡")
    assert "咖啡" in terms


# ---------------------------------------------------------------------------
# 记忆准入
# ---------------------------------------------------------------------------


def test_a_slot_question_admits_any_memory() -> None:
    """query 已命中槽位时不再要求共享上下文词 —— 槽位本身就是准入依据。"""
    assert memory_context_allowed("用户喜欢喝咖啡", "我的生日是什么时候") is True


def test_a_broad_request_admits_any_memory() -> None:
    assert memory_context_allowed("用户喜欢喝咖啡", "用户之前提过什么") is True


def test_a_memory_sharing_a_context_term_is_admitted() -> None:
    assert memory_context_allowed("用户喜欢咖啡", "他的咖啡口味怎么样") is True


def test_a_memory_without_shared_context_is_rejected() -> None:
    """不相关的记忆不进上下文包 —— 住杭州的记忆不该出现在「咖啡口味」里。"""
    assert memory_context_allowed("用户住在杭州", "他的咖啡口味怎么样") is False


def test_a_query_without_context_terms_admits_everything() -> None:
    """query 抽不出任何上下文词时没有判据可依，放行而不是全拒。"""
    assert memory_context_allowed("用户住在杭州", "嗯") is True


def test_a_memory_without_context_terms_is_admitted() -> None:
    """记忆抽不出上下文词时无从判断，放行。"""
    assert memory_context_allowed("……", "他的咖啡口味怎么样") is True


def test_the_memory_may_be_a_plain_string() -> None:
    assert memory_context_allowed("用户喜欢咖啡", "他的咖啡口味怎么样") is True
    assert memory_context_allowed("用户住在杭州", "他的咖啡口味怎么样") is False
