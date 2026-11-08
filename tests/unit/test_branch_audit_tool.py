"""分支覆盖审计工具（``tests/eval/branch_audit.py``）自身的用例。

这个工具的职责是**测量**：报出去的数字要能当依据用。测量工具自己出错，
比没有工具更糟 —— 缺口会被人当真去补，或者反过来被当成已经补过了。

所以这里钉三条：

1. ``--file`` 过滤后，报告里**所有口径一致**（缺口数、按类型、按模块、总量）
2. 分类规则真的能命中（不是写了但跑不到的规则）
3. ``--skip-run`` 拿到的覆盖率数据过期时要说出来
"""

from __future__ import annotations

import os
import time
from typing import Any

from tests.eval import branch_audit
from tests.eval.branch_audit import Gap, build_report, classify_arc


def _coverage(files: dict[str, dict[str, Any]], **totals: Any) -> dict[str, Any]:
    """造一份最小可用的覆盖率 JSON。"""
    base = {
        "num_branches": 100,
        "covered_branches": 90,
        "missing_branches": 10,
        "covered_lines": 900,
        "missing_lines": 100,
    }
    base.update(totals)
    return {
        "totals": base,
        "files": {
            path: {
                "summary": {
                    "num_branches": info[0],
                    "covered_branches": info[1],
                    "missing_branches": info[0] - info[1],
                    "covered_lines": info[2],
                    "missing_lines": 0,
                }
            }
            for path, info in files.items()
        },
    }


def _gap(file: str, line: int, kind: str = "feature") -> Gap:
    return Gap(
        file=file, line=line, to_line=line + 1, arc=f"{line}->{line + 1}", kind=kind, note=""
    )


def test_the_file_filter_leaves_every_number_consistent() -> None:
    """``--file`` 过滤后，报告内部不能出现两个口径。

    曾经的 bug：``gaps`` 列表过滤了，但 ``gaps_by_kind`` / ``gaps_by_file`` /
    ``totals`` 还是全量的数字 —— 同一份报告里「缺口 26 处」和「按类型合计
    62 处」并存。拿着这种报告做决策的人会被带偏。
    """
    coverage = _coverage(
        {
            "src/thinkback/memory/service.py": (40, 36, 400),
            "src/thinkback/api/memory.py": (60, 54, 500),
        }
    )
    gaps = [
        _gap("src/thinkback/memory/service.py", 10, "feature"),
        _gap("src/thinkback/memory/service.py", 20, "error_path"),
        _gap("src/thinkback/api/memory.py", 30, "feature"),
    ]

    report = build_report(coverage, gaps, only_file="service.py")

    assert report["gap_count"] == 2
    assert len(report["gaps"]) == 2
    assert sum(report["gaps_by_kind"].values()) == report["gap_count"]
    assert sum(report["gaps_by_file"].values()) == report["gap_count"]
    assert list(report["gaps_by_file"]) == ["src/thinkback/memory/service.py"]
    # 总量口径也按过滤后的算
    assert report["totals"]["num_branches"] == 40
    assert report["totals"]["covered_branches"] == 36
    assert report["totals"]["missing_branches"] == 4
    assert (
        report["totals"]["missing_branches"]
        == report["totals"]["num_branches"] - (report["totals"]["covered_branches"])
    )
    assert set(report["file_stats"]) == {"src/thinkback/memory/service.py"}


def test_the_unfiltered_report_uses_the_coverage_totals() -> None:
    """不过滤时，报告的总量就是覆盖率数据自己的总量。"""
    coverage = _coverage(
        {
            "src/thinkback/memory/service.py": (40, 36, 400),
            "src/thinkback/api/memory.py": (60, 54, 500),
        }
    )
    gaps = [_gap("src/thinkback/memory/service.py", 10)]

    report = build_report(coverage, gaps)

    assert report["totals"]["num_branches"] == 100
    assert report["totals"]["covered_branches"] == 90
    assert report["gap_count"] == 1


def test_an_isinstance_guard_is_classified_as_an_error_path() -> None:
    """类型防御（``isinstance``）算错误路径。

    它**不是**「不可达分支」：``if not isinstance(x, dict): raise`` 在收到
    坏数据时是会走的。判断一个条件恒假要数据流分析，源码行的形状做不到
    —— 所以工具里没有「不可达分支」这一类，只有诚实的分类。
    """
    kind, _ = classify_arc("if not isinstance(payload, dict):", 10, 11, "raise ValueError('x')")

    assert kind == "error_path"


def test_every_classification_rule_can_actually_fire() -> None:
    """每条规则都要有一条能命中它的用例 —— 跑不到的规则等于没有。"""
    cases = [
        ("except ValueError:", 10, 11, "pass", "error_path"),
        ("finally:", 10, 11, "cleanup()", "error_path"),
        ("self._release_capacity()", 10, 11, "pass", "cleanup"),
        ("if value is None:", 10, 11, "return None", "early_return"),
        ("if value is None:", 10, 9, "pass", "loop_back"),
        ("if value is None:", 10, 11, "do_something()", "feature"),
    ]

    for source_line, line, to_line, body, expected in cases:
        kind, _ = classify_arc(source_line, line, to_line, body)
        assert kind == expected, f"{source_line!r} 被分成了 {kind}，期望 {expected}"


def test_stale_coverage_data_is_called_out(capsys: Any, tmp_path: Any, monkeypatch: Any) -> None:
    """覆盖率数据比源码旧 → 必须说出来。

    这个工具的全部价值是「测量」。拿着过期测量当依据，等于没测。
    """
    coverage_data = tmp_path / ".coverage"
    coverage_data.write_text("stale", encoding="utf-8")
    old = time.time() - 3600
    os.utime(coverage_data, (old, old))

    source_root = tmp_path / "src"
    source_root.mkdir()
    (source_root / "fresh.py").write_text("x = 1\n", encoding="utf-8")

    monkeypatch.setattr(branch_audit, "COVERAGE_DATA", coverage_data)
    monkeypatch.setattr(branch_audit, "SRC_ROOT", source_root)

    branch_audit._warn_if_stale()

    assert "过期" in capsys.readouterr().out


def test_fresh_coverage_data_is_not_called_out(
    capsys: Any, tmp_path: Any, monkeypatch: Any
) -> None:
    """数据比源码新 → 别乱喊。"""
    coverage_data = tmp_path / ".coverage"
    coverage_data.write_text("fresh", encoding="utf-8")

    source_root = tmp_path / "src"
    source_root.mkdir()
    (source_root / "old.py").write_text("x = 1\n", encoding="utf-8")
    old = time.time() - 3600
    os.utime(source_root / "old.py", (old, old))

    monkeypatch.setattr(branch_audit, "COVERAGE_DATA", coverage_data)
    monkeypatch.setattr(branch_audit, "SRC_ROOT", source_root)

    branch_audit._warn_if_stale()

    assert "过期" not in capsys.readouterr().out


def test_missing_coverage_data_is_called_out(capsys: Any, tmp_path: Any, monkeypatch: Any) -> None:
    """``--skip-run`` 但压根没有覆盖率数据 → 也要说。"""
    monkeypatch.setattr(branch_audit, "COVERAGE_DATA", tmp_path / "missing.coverage")

    branch_audit._warn_if_stale()

    assert "没有覆盖率数据" in capsys.readouterr().out


def test_the_written_report_is_consistent_when_a_file_is_filtered(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """端到端跑一次 ``--file``：写到磁盘的报告内部口径必须一致。

    这是审计工具的产出物 —— 报告文件是给人看、给人拿去做决定的，
    里面两个口径并存比没有报告更糟。
    """
    coverage = _coverage(
        {
            "src/thinkback/memory/service.py": (40, 36, 400),
            "src/thinkback/api/memory.py": (60, 54, 500),
        }
    )
    monkeypatch.setattr(branch_audit, "_coverage_json", lambda: coverage)
    monkeypatch.setattr(branch_audit, "_warn_if_stale", lambda: None)

    report_path = tmp_path / "report.json"
    branch_audit.main(["--skip-run", "--file", "service.py", "--report", str(report_path)])

    import json

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["gap_count"] == sum(report["gaps_by_kind"].values())
    assert report["gap_count"] == sum(report["gaps_by_file"].values())
    assert set(report["gaps_by_file"]) <= {"src/thinkback/memory/service.py"}
    assert report["totals"]["num_branches"] == 40
