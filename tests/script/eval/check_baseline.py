"""评测基线守卫：评测报告不得低于对应基线。

防两类回归：
1. 通过率下跌（总通过率或任一类别低于基线）；
2. 用例集漂移（删除/改名用例让通过率看起来回升——基线钉住必有用例名单）。

基线可按 ``mode`` 分档：同一套用例有两种跑法（自包含契约评测 / 真实栈评测），
报告目录混放时只挑 mode 匹配的那一份，避免拿真实栈报告去卡自包含基线（反之亦然）。
真实栈基线还可以加 ``max_failure_rate_by_bucket``：按失败归因桶限比例，
把「没抽出来」「抽出来但缺事实」「事实已在索引却没召回」分开看。

用法：
    uv run python tests/script/eval/check_baseline.py            # 取最新报告
    uv run python tests/script/eval/check_baseline.py --report <path>
    uv run python tests/script/eval/check_baseline.py --report-dir <dir> --baseline <file>

退出码：0=达标；1=回归；2=输入错误。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPORT_DIR = Path("docs/memory/report")
DEFAULT_BASELINE = Path("tests/eval/baseline.json")


def _report_mode(path: Path) -> str | None:
    """报告里的 ``mode``；读不出来就当没有（不参与 mode 过滤）。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    mode = payload.get("mode")
    return mode if isinstance(mode, str) else None


def _latest_report(report_dir: Path, mode: str | None = None) -> Path:
    candidates = sorted(report_dir.glob("*.json"), key=lambda path: path.stat().st_mtime)
    if mode is not None:
        candidates = [path for path in candidates if _report_mode(path) == mode]
    if not candidates:
        suffix = f" with mode={mode}" if mode else ""
        raise FileNotFoundError(f"no evaluation reports under {report_dir}{suffix}")
    return candidates[-1]


def check_report(report: dict[str, Any], baseline: dict[str, Any]) -> list[str]:
    """比对报告与基线，返回违规列表（空列表 = 达标）。"""
    violations: list[str] = []
    if report.get("report_type") != baseline.get("report_type"):
        return [
            f"report_type mismatch: report={report.get('report_type')!r} "
            f"baseline={baseline.get('report_type')!r}"
        ]

    expected_mode = baseline.get("mode")
    if expected_mode is not None and report.get("mode") != expected_mode:
        violations.append(
            f"mode mismatch: report={report.get('mode')!r} baseline={expected_mode!r}"
        )

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

    buckets = aggregate.get("failure_buckets") or {}
    for bucket, max_rate in (baseline.get("max_failure_rate_by_bucket") or {}).items():
        metric = buckets.get(bucket)
        if metric is None:
            violations.append(f"failure bucket missing from report: {bucket}")
            continue
        actual = float(metric.get("rate", 0.0))
        if actual > float(max_rate):
            violations.append(
                f"failure bucket {bucket!r} rate {actual:.1%} > baseline {float(max_rate):.1%}"
            )
    return violations


def check(report_path: Path, baseline_path: Path) -> list[str]:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    return check_report(report, baseline)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=None, help="评测报告 JSON（缺省取最新）")
    parser.add_argument(
        "--report-dir", type=Path, default=REPORT_DIR, help="报告目录（缺省最新一份）"
    )
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    args = parser.parse_args(argv[1:])

    try:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        report_path = (
            args.report
            if args.report is not None
            else _latest_report(args.report_dir, baseline.get("mode"))
        )
        violations = check_report(json.loads(report_path.read_text(encoding="utf-8")), baseline)
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
