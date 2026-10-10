"""评测基线守卫：自包含评测报告不得低于 tests/eval/baseline.json。

防两类回归：
1. 通过率下跌（总通过率或任一类别低于基线）；
2. 用例集漂移（删除/改名用例让通过率看起来回升——基线钉住必有用例名单）。

用法：
    uv run python tests/script/eval/check_baseline.py            # 取最新报告
    uv run python tests/script/eval/check_baseline.py --report <path>

退出码：0=达标；1=回归；2=输入错误。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPORT_DIR = Path("docs/memory/report")
DEFAULT_BASELINE = Path("tests/eval/baseline.json")


def _latest_report() -> Path:
    candidates = sorted(REPORT_DIR.glob("*.json"), key=lambda path: path.stat().st_mtime)
    if not candidates:
        raise FileNotFoundError(f"no evaluation reports under {REPORT_DIR}")
    return candidates[-1]


def check(report_path: Path, baseline_path: Path) -> list[str]:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))

    violations: list[str] = []
    if report.get("report_type") != baseline.get("report_type"):
        return [
            f"report_type mismatch: report={report.get('report_type')!r} "
            f"baseline={baseline.get('report_type')!r}"
        ]

    results = report.get("results") or []
    present_cases = {entry.get("name") for entry in results}
    missing = [name for name in baseline.get("required_cases", []) if name not in present_cases]
    if missing:
        violations.append(f"missing required cases ({len(missing)}): {', '.join(sorted(missing))}")

    aggregate = report.get("aggregate") or {}
    total_rate = float(aggregate.get("total_pass_rate", 0.0))
    min_total = float(baseline.get("min_total_pass_rate", 1.0))
    if total_rate < min_total:
        violations.append(f"total pass rate {total_rate:.1%} < baseline {min_total:.1%}")

    by_category = aggregate.get("by_category") or {}
    for category, min_rate in (baseline.get("min_pass_rate_by_category") or {}).items():
        metric = by_category.get(category)
        if metric is None:
            violations.append(f"category disappeared from report: {category}")
            continue
        if float(metric.get("pass_rate", 0.0)) < float(min_rate):
            violations.append(
                f"category {category!r} pass rate {float(metric['pass_rate']):.1%} "
                f"< baseline {float(min_rate):.1%}"
            )
    return violations


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=None, help="评测报告 JSON（缺省取最新）")
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    args = parser.parse_args(argv[1:])

    try:
        report_path = args.report if args.report is not None else _latest_report()
        violations = check(report_path, args.baseline)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"基线检查输入错误: {exc}", file=sys.stderr)
        return 2

    if violations:
        print(f"评测基线未达标（{report_path}）:", file=sys.stderr)
        for violation in violations:
            print(f"  - {violation}", file=sys.stderr)
        return 1
    print(f"评测基线达标（{report_path}）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
