"""Run real memory quality evaluation and write comparable reports."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPORT_PREFIX = "AI虚拟社交记忆服务首版主链路质量评测报告"
DEFAULT_DOCS_DIR = Path("docs")
QUALITY_SCRIPT = Path("script/real_mem0_quality_regression.py")

_METRIC_SPECS: tuple[tuple[str, str, bool], ...] = (
    ("case_pass_rate", "higher", True),
    ("recall_at_10", "higher", True),
    ("precision_at_10", "higher", True),
    ("conflict_pollution_rate", "lower", True),
    ("false_positive_rate", "lower", True),
    ("duplicate_active_rate", "lower", True),
    ("delete_memory_residue_rate", "lower", True),
    ("rebuild_resurrection_rate", "lower", True),
    ("cross_user_leak_rate", "lower", True),
    ("cross_character_leak_rate", "lower", True),
    ("roleplay_real_mix_rate", "lower", True),
    ("top1_hit_rate", "higher", False),
    ("mrr", "higher", False),
    ("item_precision_at_10", "higher", False),
    ("irrelevant_l3_per_query", "lower", False),
    ("known_drift_regression_pass_rate", "higher", False),
)


@dataclass(frozen=True)
class ReportArtifacts:
    json_path: Path
    markdown_path: Path


def extract_run_id(output: str) -> str | None:
    match = re.search(r"quality scope: run_id=([A-Za-z0-9_-]+)", output)
    return match.group(1) if match else None


def extract_quality_json_reports(output: str) -> list[dict[str, Any]]:
    decoder = json.JSONDecoder()
    reports: list[dict[str, Any]] = []
    index = 0
    while index < len(output):
        brace_index = output.find("{", index)
        if brace_index == -1:
            break
        try:
            value, end_index = decoder.raw_decode(output[brace_index:])
        except json.JSONDecodeError:
            index = brace_index + 1
            continue
        if isinstance(value, dict) and _looks_like_quality_report(value):
            reports.append(value)
        index = brace_index + end_index
    return reports


def build_quality_evaluation_run_report(
    *,
    run_id: str,
    child_returncode: int,
    quality_report: dict[str, Any] | None,
    post_delete_report: dict[str, Any] | None,
    stdout: str,
    stderr: str,
) -> dict[str, Any]:
    passed = (
        child_returncode == 0
        and bool(quality_report)
        and bool(quality_report.get("passed"))
        and bool(post_delete_report)
        and bool(post_delete_report.get("passed"))
    )
    failure_phase = None if passed else _failure_phase(child_returncode, quality_report, post_delete_report)
    return {
        "report_type": "real_memory_quality_evaluation",
        "run_id": run_id,
        "generated_at": datetime.now(UTC).isoformat(),
        "passed": passed,
        "failure_phase": failure_phase,
        "quality": quality_report,
        "post_delete": post_delete_report,
        "stdout_tail": _tail(stdout),
        "stderr_tail": _tail(stderr),
    }


def write_quality_evaluation_report_artifacts(
    docs_dir: Path,
    report: dict[str, Any],
) -> ReportArtifacts:
    docs_dir.mkdir(parents=True, exist_ok=True)
    run_id = str(report["run_id"])
    previous = _load_latest_previous_report(docs_dir, run_id)
    json_path = docs_dir / f"{REPORT_PREFIX}-{run_id}.json"
    markdown_path = docs_dir / f"{REPORT_PREFIX}-{run_id}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(render_quality_evaluation_markdown(report, previous), encoding="utf-8")
    return ReportArtifacts(json_path=json_path, markdown_path=markdown_path)


def render_quality_evaluation_markdown(
    report: dict[str, Any],
    previous: dict[str, Any] | None = None,
) -> str:
    run_id = str(report["run_id"])
    quality = _quality(report)
    post_delete = _post_delete(report)
    lines = [
        f"# {REPORT_PREFIX}",
        "",
        "## 1. 结论摘要",
        "",
        "| 字段 | 值 |",
        "| --- | --- |",
        f"| `run_id` | `{run_id}` |",
        f"| 生成时间 | `{report.get('generated_at', '-')}` |",
        f"| 最终结论 | {'通过' if report.get('passed') else '未通过'} |",
        f"| 失败阶段 | `{report.get('failure_phase') or '-'}` |",
        f"| 对比报告 | `{previous['run_id'] if previous else '-'}` |",
        f"| 评测脚本 | `{QUALITY_SCRIPT}` |",
        "",
        "## 2. 指标对比",
        "",
        "| 指标 | 上次结果 | 本轮结果 | 变化 | 趋势 |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    previous_quality = _quality(previous) if previous else {}
    previous_post_delete = _post_delete(previous) if previous else {}
    for metric, direction, _hard_gate in _METRIC_SPECS:
        current_value = _metric_value(metric, quality, post_delete)
        previous_value = _metric_value(metric, previous_quality, previous_post_delete)
        delta = _metric_delta(current_value, previous_value)
        trend = _metric_trend(delta, direction)
        lines.append(
            "| "
            f"`{metric}` | "
            f"{_format_metric(previous_value)} | "
            f"{_format_metric(current_value)} | "
            f"{_format_delta(delta)} | "
            f"{trend} |"
        )

    lines.extend(
        [
            "",
            "## 3. 门禁结果",
            "",
            "| 指标 | 类型 | 结果 |",
            "| --- | --- | --- |",
        ]
    )
    for metric, _direction, hard_gate in _METRIC_SPECS:
        if not hard_gate:
            continue
        current_value = _metric_value(metric, quality, post_delete)
        lines.append(f"| `{metric}` | 硬门禁 | {_format_metric(current_value)} |")

    lines.extend(
        [
            "",
            "## 4. 场景覆盖",
            "",
            "| 场景 | case 数 | 通过数 | 通过率 |",
            "| --- | ---: | ---: | ---: |",
        ]
    )
    category_metrics = _merged_category_metrics(quality, post_delete)
    if category_metrics:
        for category, item in sorted(category_metrics.items()):
            lines.append(
                "| "
                f"`{category}` | "
                f"{item.get('case_count', 0)} | "
                f"{item.get('passed_count', 0)} | "
                f"{_format_metric(item.get('pass_rate'))} |"
            )
    else:
        lines.append("| - | 0 | 0 | - |")

    lines.extend(
        [
            "",
            "## 5. 失败证据",
            "",
            "| 来源 | 失败 case | 失败指标 |",
            "| --- | --- | --- |",
            _failure_row("质量主链路", quality),
            _failure_row("删除与重建", post_delete),
            "",
            "## 6. 自动化状态",
            "",
            "| 字段 | 状态 |",
            "| --- | --- |",
        ]
    )
    automation_status = quality.get("metric_automation_status", {}) if quality else {}
    if automation_status:
        for field, status in sorted(automation_status.items()):
            lines.append(f"| `{field}` | `{status}` |")
    else:
        lines.append("| - | - |")

    lines.extend(
        [
            "",
            "## 7. 原始执行摘要",
            "",
            "```text",
            str(report.get("stderr_tail") or report.get("stdout_tail") or "-"),
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def run_real_quality_evaluation(docs_dir: Path = DEFAULT_DOCS_DIR) -> ReportArtifacts:
    completed = subprocess.run(
        [sys.executable, str(QUALITY_SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
    )
    combined_output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    run_id = extract_run_id(combined_output) or f"real-quality-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}"
    child_reports = extract_quality_json_reports(completed.stdout)
    quality_report = child_reports[0] if len(child_reports) >= 1 else None
    post_delete_report = child_reports[1] if len(child_reports) >= 2 else None
    report = build_quality_evaluation_run_report(
        run_id=run_id,
        child_returncode=completed.returncode,
        quality_report=quality_report,
        post_delete_report=post_delete_report,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )
    return write_quality_evaluation_report_artifacts(docs_dir, report)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docs-dir", type=Path, default=DEFAULT_DOCS_DIR)
    args = parser.parse_args()
    artifacts = run_real_quality_evaluation(args.docs_dir)
    print(f"wrote json report: {artifacts.json_path}")
    print(f"wrote markdown report: {artifacts.markdown_path}")


def _looks_like_quality_report(value: dict[str, Any]) -> bool:
    quality_keys = {"case_count", "case_pass_rate", "recall_at_10", "precision_at_10", "passed"}
    return bool(quality_keys.intersection(value))


def _failure_phase(
    child_returncode: int,
    quality_report: dict[str, Any] | None,
    post_delete_report: dict[str, Any] | None,
) -> str:
    if child_returncode != 0 and quality_report is None:
        return "environment_or_execution"
    if quality_report and not quality_report.get("passed"):
        return "quality_gate"
    if post_delete_report and not post_delete_report.get("passed"):
        return "delete_rebuild_gate"
    return "environment_or_execution"


def _tail(text: str, max_lines: int = 80) -> str:
    lines = text.splitlines()
    return "\n".join(lines[-max_lines:])


def _load_latest_previous_report(docs_dir: Path, current_run_id: str) -> dict[str, Any] | None:
    candidates = sorted(docs_dir.glob(f"{REPORT_PREFIX}-*.json"))
    reports: list[dict[str, Any]] = []
    for candidate in candidates:
        try:
            report = json.loads(candidate.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if report.get("report_type") != "real_memory_quality_evaluation":
            continue
        if report.get("run_id") == current_run_id:
            continue
        reports.append(report)
    if not reports:
        return None
    return max(reports, key=lambda item: str(item.get("generated_at", "")))


def _quality(report: dict[str, Any] | None) -> dict[str, Any]:
    if not report:
        return {}
    return report.get("quality") or report


def _post_delete(report: dict[str, Any] | None) -> dict[str, Any]:
    if not report:
        return {}
    return report.get("post_delete") or {}


def _metric_value(
    metric: str,
    quality: dict[str, Any],
    post_delete: dict[str, Any],
) -> float | int | None:
    if metric in post_delete and post_delete.get(metric) is not None:
        return post_delete.get(metric)
    if metric in quality and quality.get(metric) is not None:
        return quality.get(metric)
    return None


def _metric_delta(
    current_value: float | int | None,
    previous_value: float | int | None,
) -> float | None:
    if current_value is None or previous_value is None:
        return None
    return round(float(current_value) - float(previous_value), 6)


def _metric_trend(delta: float | None, direction: str) -> str:
    if delta is None:
        return "-"
    if delta == 0:
        return "持平"
    improved = delta > 0 if direction == "higher" else delta < 0
    return "提升" if improved else "下降"


def _format_metric(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        if value.is_integer():
            return f"{value:.1f}"
        return f"{value:.6f}".rstrip("0").rstrip(".")
    return str(value)


def _format_delta(value: float | None) -> str:
    if value is None:
        return "-"
    if value > 0:
        return f"+{_format_metric(value)}"
    return _format_metric(value)


def _merged_category_metrics(
    quality: dict[str, Any],
    post_delete: dict[str, Any],
) -> dict[str, dict[str, float | int]]:
    merged: dict[str, dict[str, float | int]] = {}
    for report in (quality, post_delete):
        for category, item in (report.get("category_metrics", {}) if report else {}).items():
            current = merged.setdefault(category, {"case_count": 0, "passed_count": 0, "pass_rate": 0.0})
            current["case_count"] = int(current["case_count"]) + int(item.get("case_count", 0))
            current["passed_count"] = int(current["passed_count"]) + int(item.get("passed_count", 0))
    for item in merged.values():
        case_count = int(item["case_count"])
        item["pass_rate"] = float(item["passed_count"]) / case_count if case_count else 0.0
    return merged


def _failure_row(label: str, report: dict[str, Any]) -> str:
    if not report:
        return f"| {label} | - | - |"
    failed_cases = ", ".join(report.get("failed_cases", [])) or "-"
    failed_metrics = ", ".join(
        item.get("metric", str(item)) if isinstance(item, dict) else str(item)
        for item in report.get("failed_metrics", [])
    ) or "-"
    return f"| {label} | `{failed_cases}` | `{failed_metrics}` |"


if __name__ == "__main__":
    main()
