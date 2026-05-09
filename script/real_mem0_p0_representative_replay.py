"""Representative replay reporting for P0 production precheck."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

REQUIRED_REPRESENTATIVE_CATEGORIES = (
    "slot_conflict",
    "negative_control",
    "isolation",
    "delete_rebuild",
)


def build_representative_replay_report(
    *,
    run_id: str,
    started_at: str,
    ended_at: str,
    suite_reports: list[dict[str, Any]],
) -> dict[str, Any]:
    coverage = _coverage_by_category(suite_reports)
    failed_suites = [
        str(report.get("suite_id", report.get("run_id", "-")))
        for report in suite_reports
        if not bool(report.get("passed"))
    ]
    failed_categories = sorted([
        category
        for category in REQUIRED_REPRESENTATIVE_CATEGORIES
        if coverage.get(category, 0) <= 0
    ])
    representative_probe_passed = not failed_suites and not failed_categories
    return {
        "run_id": run_id,
        "phase": "representative_replay",
        "started_at": started_at,
        "ended_at": ended_at,
        "passed": representative_probe_passed,
        "representative_probe_passed": representative_probe_passed,
        "suite_count": len(suite_reports),
        "suite_ids": [str(report.get("suite_id", report.get("run_id", "-"))) for report in suite_reports],
        "failed_suites": failed_suites,
        "required_categories": list(REQUIRED_REPRESENTATIVE_CATEGORIES),
        "coverage": coverage,
        "failed_categories": failed_categories,
        "worst_recall_p95_ms": _worst_recall_latency(suite_reports, "p95"),
        "worst_recall_p99_ms": _worst_recall_latency(suite_reports, "p99"),
        "suite_reports": suite_reports,
    }


def render_representative_replay_markdown(report: dict[str, Any]) -> str:
    conclusion = "通过" if report.get("passed") else "未通过"
    lines = [
        "# Thinkback P0 代表性样本回放报告",
        "",
        "## 1. 结论",
        "",
        "| 项目 | 结果 |",
        "| --- | --- |",
        f"| run_id | `{report['run_id']}` |",
        f"| started_at | `{report['started_at']}` |",
        f"| ended_at | `{report['ended_at']}` |",
        f"| 代表性样本回放 | {conclusion} |",
        f"| suite_count | {report.get('suite_count', 0)} |",
        f"| failed_suites | {', '.join(report.get('failed_suites', [])) or '-'} |",
        f"| failed_categories | {', '.join(report.get('failed_categories', [])) or '-'} |",
        f"| worst recall p95 | {report.get('worst_recall_p95_ms', 0)}ms |",
        f"| worst recall p99 | {report.get('worst_recall_p99_ms', 0)}ms |",
        "",
        "## 2. 覆盖明细",
        "",
        "| 类别 | case 数 |",
        "| --- | ---: |",
    ]
    coverage = report.get("coverage", {})
    for category in report.get("required_categories", REQUIRED_REPRESENTATIVE_CATEGORIES):
        lines.append(f"| {category} | {coverage.get(category, 0)} |")
    lines.append("")
    return "\n".join(lines)


def write_representative_replay_report(
    report: dict[str, Any], *, output_dir: Path
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{report['run_id']}.json"
    md_path = output_dir / f"{report['run_id']}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    md_path.write_text(render_representative_replay_markdown(report), encoding="utf-8")
    return json_path, md_path


def _coverage_by_category(suite_reports: list[dict[str, Any]]) -> dict[str, int]:
    coverage = {category: 0 for category in REQUIRED_REPRESENTATIVE_CATEGORIES}
    for report in suite_reports:
        for section_name in ("quality", "concurrent_recall", "post_delete"):
            section = report.get(section_name, {})
            for case_result in section.get("case_results", []):
                if case_result.get("status") != "passed":
                    continue
                category = str(case_result.get("category", ""))
                if category in coverage:
                    coverage[category] += 1
    return coverage


def _worst_recall_latency(reports: list[dict[str, Any]], percentile: str) -> int:
    return max((_recall_latency(report, percentile) for report in reports), default=0)


def _recall_latency(report: dict[str, Any], percentile: str) -> int:
    latency = report.get("concurrent_recall", {}).get("latency_ms", {}).get("recall", {})
    return int(latency.get(percentile, 0))


def _load_report(path: Path) -> dict[str, Any]:
    report = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise ValueError(f"representative replay input must be a JSON object: {path}")
    if "suite_id" not in report:
        raise ValueError(f"representative replay input must be a p0-short report: {path}")
    return dict(report)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a P0 representative replay report.")
    parser.add_argument("--output-dir", default="docs/reports")
    parser.add_argument("reports", nargs="+")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    started_at = datetime.now(UTC).isoformat()
    suite_reports = [_load_report(Path(path)) for path in args.reports]
    report = build_representative_replay_report(
        run_id=f"p0-representative-replay-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}",
        started_at=started_at,
        ended_at=datetime.now(UTC).isoformat(),
        suite_reports=suite_reports,
    )
    json_path, md_path = write_representative_replay_report(report, output_dir=Path(args.output_dir))
    print(f"representative replay report json: {json_path}")
    print(f"representative replay report markdown: {md_path}")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    if not report["passed"]:
        raise RuntimeError(f"P0 representative replay failed: {report['failed_categories']}")


if __name__ == "__main__":
    main()
