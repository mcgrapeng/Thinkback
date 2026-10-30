"""变异存活门禁自身的自检。

门禁漏门比门禁误报危险得多：它看起来是绿的。这一层盯的是「门禁在什么都没
审、或者出现了它不认识的状态时，会不会照样放行」。

具体三条：

1. **没审到东西不许放行**。变异体总数为 0，或声明在 ``only_mutate`` 里的
   某个文件一个变异体都没生成 —— 后者正是「范围变了但 mutmut 复用了旧
   副本」那个静默失效的形态。
2. **不认识的状态不许放行**。mutmut 的状态词表比「存活 / 无测试 / 超时」
   长，还有 suspicious / skipped / not checked / interrupted。以前只认前三个，
   后面的直接从门禁视野里消失。
3. **状态漂移要报出来**。基线里登记的是「存活」，这次跑出来是「超时」，
   说明当初的判断需要重看一眼。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.eval import check_mutation_survivors as gate

REPO_ROOT = Path(__file__).resolve().parents[2]


def _meta(mutants: Path, source: str, codes: dict[str, int | None]) -> None:
    path = mutants / f"{source}.meta"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"exit_code_by_key": codes}), encoding="utf-8")


def test_an_empty_mutants_tree_has_no_population(monkeypatch: pytest.MonkeyPatch) -> None:
    """mutants/ 是空的 → 一个变异体都没评估。"""
    empty = REPO_ROOT / "tests" / "eval" / "_no_such_mutants"
    monkeypatch.setattr(gate, "MUTANTS_DIR", empty)
    total, per_file = gate.evaluated_population()
    assert total == 0
    assert per_file == {}


def test_the_population_counts_every_evaluated_mutant(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """被杀的也在计数里 —— 这个数字回答的是「审了多少」，不是「还剩几个」。"""
    mutants = tmp_path / "mutants"
    _meta(mutants, "src/a.py", {"m1": 1, "m2": 0})
    _meta(mutants, "src/b.py", {"m3": 1})
    monkeypatch.setattr(gate, "MUTANTS_DIR", mutants)

    total, per_file = gate.evaluated_population()

    assert total == 3
    assert per_file == {"src/a.py": 2, "src/b.py": 1}


def test_the_population_does_not_count_mutants_that_were_never_evaluated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """exit_code 是 None 的那些是「注册了但还没评到」—— 不算审过了。

    mutmut 审计开始时就把全部变异体注册进 ``.meta``，没评到的那个
    exit_code 是 ``None``。把它们算进「本次评估」，门禁报出来的分母
    就大于真正审过的量 —— 那个数字回答的是「审了多少」。
    """
    mutants = tmp_path / "mutants"
    _meta(mutants, "src/a.py", {"m1": 1, "m2": None, "m3": 0})
    monkeypatch.setattr(gate, "MUTANTS_DIR", mutants)

    total, per_file = gate.evaluated_population()

    assert total == 2
    assert per_file == {"src/a.py": 2}


def test_the_gate_waits_for_mutmut_to_flush_its_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """读到 not checked 要等 mutmut 落盘，不是一律判失败。

    ``mutmut run`` 返回时 worker 可能还有结果没写进 ``.meta``，那些变异体
    在快照里是 ``not checked``。不等着就判失败，会让门禁在每次全量重审后
    **随机红一次**，而它报出来的「新增未杀死变异体」是假的。
    """
    calls: list[int] = []

    def fake_read() -> dict[str, str]:
        calls.append(1)
        if len(calls) < 3:
            return {"m1": gate.NOT_CHECKED}
        return {"m1": "survived"}

    monkeypatch.setattr(gate, "_read_results_once", fake_read)
    monkeypatch.setattr(gate, "SETTLE_SECONDS", 0.0)

    assert gate.read_results() == {"m1": "survived"}
    assert len(calls) == 3


def test_a_persistently_not_checked_mutant_is_still_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """等到底还是 not checked —— 那就是真的没审到，门禁仍要报出来。

    等待只是排除「落盘时序」这一个原因，不是把 not checked 变成放行。
    """
    calls: list[int] = []

    def fake_read() -> dict[str, str]:
        calls.append(1)
        return {"m1": gate.NOT_CHECKED}

    monkeypatch.setattr(gate, "_read_results_once", fake_read)
    monkeypatch.setattr(gate, "SETTLE_SECONDS", 0.0)
    monkeypatch.setattr(gate, "SETTLE_ATTEMPTS", 3)

    assert gate.read_results() == {"m1": gate.NOT_CHECKED}
    assert len(calls) == 3


def test_a_scoped_file_without_mutants_is_reported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """声明在范围内却没有变异体 —— 那个文件这一轮根本没被审。"""
    mutants = tmp_path / "mutants"
    _meta(mutants, "src/a.py", {"m1": 1})
    monkeypatch.setattr(gate, "MUTANTS_DIR", mutants)
    monkeypatch.setattr(gate, "scoped_source_files", lambda: ["src/a.py", "src/b.py"])

    assert gate.unaudited_scoped_files({"src/a.py": 1}) == ["src/b.py"]


def test_the_declared_scope_is_read_from_pyproject() -> None:
    """范围取自 pyproject 的 only_mutate —— 与 mutmut 读的是同一份配置。"""
    files = gate.scoped_source_files()
    assert "src/thinkback/domain/recall_policy.py" in files
    assert "src/thinkback/memory/service.py" not in files


def test_only_killed_counts_as_protected() -> None:
    """只有 killed 算有保护。"""
    grouped = gate.not_killed_by_status(
        {"m1": "killed", "m2": "survived", "m3": "suspicious", "m4": "skipped"}
    )
    assert "m1" not in grouped.get("killed", set())
    assert grouped["survived"] == {"m2"}
    assert grouped["suspicious"] == {"m3"}
    assert grouped["skipped"] == {"m4"}


def test_an_unknown_status_is_not_silently_dropped() -> None:
    """以前只认三个已知状态，其余无视 —— 新状态一出现就从门禁视野里消失。"""
    results = {"m1": "survived", "m2": "not checked"}
    grouped = gate.not_killed_by_status(results)
    assert set(grouped) == {"survived", "not checked"}
    assert grouped["not checked"] == {"m2"}


def test_every_known_status_has_its_own_baseline_bucket() -> None:
    """三个已知状态各有登记桶；其余状态归 accepted_other。"""
    baseline = json.loads((REPO_ROOT / "tests" / "eval" / "mutation_survivors.json").read_text())
    assert "accepted_survivors" in baseline
    assert "accepted_no_tests" in baseline
    assert "accepted_timeouts" in baseline
    assert "accepted_other" in baseline
    assert {"survived", "no tests", "timeout"} == gate.KNOWN_STATUSES
