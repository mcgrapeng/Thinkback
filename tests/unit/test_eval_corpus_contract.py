"""垂类评测语料自身的契约（语料是门禁的一部分，门禁也得被门禁）。

``tests/eval/semiconductor_cases.json`` 是垂类评测的**输入**，而
``tests/eval/baseline_semiconductor.json`` 的 ``required_cases`` 名单是它的
防回血清单 —— 两条基线一起保证「语料不被悄悄改小」。但语料本身的质量
（断言有没有效力、类型声明对不对）此前**没有任何检查**。

这份用例守住三件事，每一件都对应过一次真实事故：

1. **禁用词必须真的被写进去过**（防空转断言）。
   ``case_semi_correction_records_new_value`` 的 ``forbidden_terms`` 是
   ``["45nm"]``，而 45nm 从未出现在任何一轮里 —— 这条断言恒真：它验证的
   不是「改口后旧值不回流」，而是「从没写过的东西没出现」。用例名字带着
   correction，最容易被误读为「改口场景已覆盖」。

2. **recall 类用例必须声明 ``memory_type``**（防空类型落回）。
   垂类每个类型有**自己的抽取指令**，指令里有一份属性名清单。落回默认的
   ``profile`` 类型时，清单里只有「负责角色 / 负责产线 / 负责工序段 /
   所属团队」四条 —— 机台、良率、客户要求这些事实根本不在清单里，抽不
   出来。真实栈实测：35 条 recall 用例全部落回默认类型 → 通过率 22.5%
   （自包含模式 100%，因为假后端不抽取，**掩盖了这个问题**）。

3. **``forbidden_terms`` 不得与 ``expected_terms`` 交叠**（防自相矛盾）。

4. **每条用例至少声明期望词或禁词**（防空跑）。
   期望词与禁词都空的用例，除了「这条会过」之外什么都没验。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

CORPUS_PATH = Path(__file__).resolve().parents[1] / "eval" / "semiconductor_cases.json"
BASELINE_PATH = Path(__file__).resolve().parents[1] / "eval" / "baseline_semiconductor.json"

_CORPUS: dict[str, Any] = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
_CASES: list[dict[str, Any]] = _CORPUS["cases"]
_BY_NAME: dict[str, dict[str, Any]] = {case["name"]: case for case in _CASES}


def _all_text(case: dict[str, Any]) -> str:
    """用例写进系统的全部正文（user + assistant）。"""
    return " ".join(
        f"{round_data.get('user', '')} {round_data.get('assistant', '')}"
        for round_data in case["rounds"]
    )


def test_the_corpus_is_not_empty() -> None:
    """语料存在且非空 —— 这条用例本身就是「语料还在」的防回血。"""
    assert _CASES, "垂类语料是空的"
    assert all(case.get("name") for case in _CASES), "有用例没有名字"


def test_the_case_names_are_unique() -> None:
    """用例名唯一 —— 重名会让基线的 ``required_cases`` 名单失真。"""
    names = [case["name"] for case in _CASES]
    duplicates = {name for name in names if names.count(name) > 1}
    assert duplicates == set(), f"用例名重复：{sorted(duplicates)}"


@pytest.mark.parametrize("case", _CASES, ids=[case["name"] for case in _CASES])
def test_a_forbidden_term_was_actually_written(case: dict[str, Any]) -> None:
    """禁用词必须真的出现在某一轮里。

    没写进去过的词不可能被召回 —— 断言它「没出现」是恒真的，测不到任何
    行为（旧值回流、隔离失效、泄漏都会被它放过）。
    """
    text = _all_text(case)
    for term in case.get("forbidden_terms", []):
        assert term in text, (
            f"{case['name']} 的禁用词 {term!r} 从未被写进任何一轮 —— 这条断言恒真，测不到任何行为"
        )


@pytest.mark.parametrize("case", _CASES, ids=[case["name"] for case in _CASES])
def test_expected_and_forbidden_terms_do_not_overlap(case: dict[str, Any]) -> None:
    """期望词与禁用词不得交叠 —— 同时要求出现与不得出现，自相矛盾。"""
    expected = set(case.get("expected_terms", []))
    forbidden = set(case.get("forbidden_terms", []))
    overlap = expected & forbidden
    assert overlap == set(), f"{case['name']} 的期望词与禁用词交叠：{sorted(overlap)}"


@pytest.mark.parametrize(
    "case",
    [case for case in _CASES if case["kind"] == "recall"],
    ids=[case["name"] for case in _CASES if case["kind"] == "recall"],
)
def test_a_recall_case_declares_its_memory_type(case: dict[str, Any]) -> None:
    """recall 类用例必须声明 ``memory_type``。

    垂类每个类型带自己的抽取指令（属性名清单不同）。不声明就落回默认
    ``profile`` 类型，清单里只有 4 条属性 —— 机台、良率、客户要求这些
    事实抽不出来，真实栈通过率掉到 22.5%。

    这条断言是**在语料写下的那一刻**就拦住「忘了声明类型」，而不是等到
    真实栈评测跑完才发现（那一轮要几十分钟）。
    """
    assert case.get("memory_type"), (
        f"{case['name']} 是 recall 用例但没声明 memory_type —— "
        "它会落回默认 profile 类型，其抽取指令的属性清单不覆盖本用例的事实"
    )


@pytest.mark.parametrize("case", _CASES, ids=[case["name"] for case in _CASES])
def test_every_case_declares_what_it_asserts(case: dict[str, Any]) -> None:
    """每条用例至少有一条期望词或一条禁用词。

    两者都空的用例除了「会过」之外什么都没验。
    """
    kind = case["kind"]
    if kind in ("reject_type", "ttl_expiration"):
        # 这两种 kind 断言的是「机制」（被拒 / 带到期日），不断言召回内容
        return
    assert case.get("expected_terms") or case.get("forbidden_terms"), (
        f"{case['name']} 既没有期望词也没有禁用词 —— 它什么都没验"
    )


@pytest.mark.parametrize(
    "case",
    [case for case in _CASES if case["kind"] == "recall"],
    ids=[case["name"] for case in _CASES if case["kind"] == "recall"],
)
def test_a_recall_case_has_at_least_one_round(case: dict[str, Any]) -> None:
    """recall 用例至少有一轮 —— 没有轮次就没什么可召回的。"""
    assert case["rounds"], f"{case['name']} 没有任何轮次"


def test_the_corpus_covers_multi_round_conversations() -> None:
    """语料必须有多轮用例。

    此前 40 条语料**全是单轮** —— 用户在第 2 轮改口、跨轮补充事实这两种
    最常见的形态一条都没测。而「多轮改口后旧值不回流」正是记忆系统最核心
    的行为之一（冲突仲裁 + 过期判定都只在多轮下才被真正触发）。

    这条断言防止语料再次退化回单轮。
    """
    multi_round = [case["name"] for case in _CASES if len(case["rounds"]) > 1]
    assert len(multi_round) >= 2, (
        f"语料里只有 {len(multi_round)} 条多轮用例（{multi_round}）—— "
        "多轮改口与跨轮补充是记忆系统最核心的行为，语料不能只有单轮"
    )


def test_the_baseline_lists_every_corpus_case() -> None:
    """自包含基线的 ``required_cases`` 必须列出全部语料。

    用例集漂移（删掉几条让通过率虚高）要挡住 —— 名单缺一条就等于那条
    用例可以被静默删掉。
    """
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    listed = set(baseline["required_cases"])
    missing = set(_BY_NAME) - listed
    assert missing == set(), f"基线漏了这些用例：{sorted(missing)}"


def test_every_corpus_case_belongs_to_a_known_category() -> None:
    """用例的类别必须在基线登记的类别里。

    新增类别却没在基线里登记 → 那条类别的通过率不受任何地板约束。
    """
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    known = set(baseline["min_pass_rate_by_category"])
    used = {case["category"] for case in _CASES}
    assert used <= known, f"这些类别没有通过率地板：{sorted(used - known)}"
