"""评测基线守卫自检：防回归逻辑本身必须可验证。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests.script.eval.check_baseline import check


def _write(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _report(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "report_type": "thinking_memory_self_contained_evaluation",
        "aggregate": {
            "total_pass_rate": 1.0,
            "by_category": {"主路径": {"pass_rate": 1.0, "passed": 2, "count": 2}},
        },
        "results": [{"name": "case-a"}, {"name": "case-b"}],
    }
    base.update(overrides)
    return base


def _baseline(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "report_type": "thinking_memory_self_contained_evaluation",
        "min_total_pass_rate": 1.0,
        "min_pass_rate_by_category": {"主路径": 1.0},
        "required_cases": ["case-a", "case-b"],
    }
    base.update(overrides)
    return base


def test_baseline_passes_on_healthy_report(tmp_path: Path) -> None:
    report_path = _write(tmp_path / "report.json", _report())
    baseline_path = _write(tmp_path / "baseline.json", _baseline())
    assert check(report_path, baseline_path) == []


def test_baseline_catches_pass_rate_drop(tmp_path: Path) -> None:
    report = _report()
    report["aggregate"]["total_pass_rate"] = 0.5
    report["aggregate"]["by_category"]["主路径"] = {"pass_rate": 0.5, "passed": 1, "count": 2}
    violations = check(
        _write(tmp_path / "report.json", report),
        _write(tmp_path / "baseline.json", _baseline()),
    )
    assert any("total pass rate" in violation for violation in violations)
    assert any("主路径" in violation for violation in violations)


def test_baseline_catches_case_set_shrink_and_missing_category(tmp_path: Path) -> None:
    report = _report()
    report["results"] = [{"name": "case-a"}]
    del report["aggregate"]["by_category"]["主路径"]
    violations = check(
        _write(tmp_path / "report.json", report),
        _write(tmp_path / "baseline.json", _baseline()),
    )
    assert any("missing required cases" in violation for violation in violations)
    assert any("category disappeared" in violation for violation in violations)


def test_baseline_rejects_report_type_mismatch(tmp_path: Path) -> None:
    violations = check(
        _write(tmp_path / "report.json", _report(report_type="other")),
        _write(tmp_path / "baseline.json", _baseline()),
    )
    assert violations == [
        "report_type mismatch: report='other' baseline='thinking_memory_self_contained_evaluation'"
    ]
