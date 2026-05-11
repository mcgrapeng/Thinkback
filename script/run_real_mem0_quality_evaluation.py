"""Run real memory quality evaluation and write comparable reports."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPORT_PREFIX = "AI虚拟社交记忆服务首版主链路质量评测报告"
DEFAULT_DOCS_DIR = Path("docs")
DEFAULT_TIMEOUT_SECONDS = 900
QUALITY_SCRIPT = Path("script/real_mem0_quality_regression.py")

_METRIC_SPECS: tuple[tuple[str, str, bool], ...] = (
    ("case_pass_rate", "higher", True),
    ("recall_at_10", "higher", True),
    ("precision_at_10", "higher", True),
    ("conflict_pollution_rate", "lower", True),
    ("false_positive_rate", "lower", True),
    ("duplicate_active_rate", "lower", True),
    ("delete_memory_residue_rate", "lower", True),
    ("delete_session_residue_rate", "lower", True),
    ("delete_all_residue_rate", "lower", True),
    ("rebuild_resurrection_rate", "lower", True),
    ("cross_user_leak_rate", "lower", True),
    ("cross_character_leak_rate", "lower", True),
    ("roleplay_real_mix_rate", "lower", True),
    ("top1_hit_rate", "higher", False),
    ("mrr", "higher", False),
    ("item_precision_at_10", "higher", False),
    ("irrelevant_l3_per_query", "lower", False),
    ("known_drift_regression_pass_rate", "higher", True),
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
    postgres_database: str | None = None,
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
        "postgres_database": postgres_database or _postgres_database_from_environment(),
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
    report_stem = _next_report_stem(docs_dir, report)
    json_path = docs_dir / f"{report_stem}.json"
    markdown_path = docs_dir / f"{report_stem}.md"
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
        f"| PostgreSQL 数据库 | `{report.get('postgres_database') or '-'}` |",
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

    lines.extend(_dataset_boundary_lines(report, quality, post_delete))

    lines.extend(
        [
            "",
            "## 6. 失败证据",
            "",
            "| 来源 | 失败 case | 失败指标 |",
            "| --- | --- | --- |",
            _failure_row("质量主链路", quality),
            _failure_row("删除与重建", post_delete),
            "",
            "## 7. 自动化状态",
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
            "## 8. 原始执行摘要",
            "",
            "```text",
            str(report.get("stderr_tail") or report.get("stdout_tail") or "-"),
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def run_real_quality_evaluation(
    docs_dir: Path = DEFAULT_DOCS_DIR,
    *,
    timeout_seconds: int | None = None,
) -> ReportArtifacts:
    effective_timeout_seconds = timeout_seconds or _quality_timeout_seconds_from_environment()
    try:
        completed = subprocess.run(
            [sys.executable, str(QUALITY_SCRIPT)],
            check=False,
            capture_output=True,
            text=True,
            timeout=effective_timeout_seconds,
        )
        child_returncode = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
    except subprocess.TimeoutExpired as exc:
        # 中文注释：真实模型/Mem0 调用可能被外部服务卡住；超时也要落报告，避免评测命令无限挂起。
        stdout = _timeout_stream_text(exc.output)
        stderr = "\n".join(
            part
            for part in (
                _timeout_stream_text(exc.stderr),
                f"quality evaluation timed out after {effective_timeout_seconds}s",
            )
            if part
        )
        child_returncode = 124
    combined_output = "\n".join(part for part in (stdout, stderr) if part)
    run_id = extract_run_id(combined_output) or f"real-quality-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}"
    child_reports = extract_quality_json_reports(stdout)
    quality_report = child_reports[0] if len(child_reports) >= 1 else None
    post_delete_report = child_reports[1] if len(child_reports) >= 2 else None
    report = build_quality_evaluation_run_report(
        run_id=run_id,
        child_returncode=child_returncode,
        quality_report=quality_report,
        post_delete_report=post_delete_report,
        stdout=stdout,
        stderr=stderr,
    )
    return write_quality_evaluation_report_artifacts(docs_dir, report)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docs-dir", type=Path, default=DEFAULT_DOCS_DIR)
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=None,
        help="Maximum seconds to wait for the child real-quality script.",
    )
    args = parser.parse_args()
    artifacts = run_real_quality_evaluation(args.docs_dir, timeout_seconds=args.timeout_seconds)
    print(f"wrote json report: {artifacts.json_path}")
    print(f"wrote markdown report: {artifacts.markdown_path}")


def _looks_like_quality_report(value: dict[str, Any]) -> bool:
    quality_keys = {"case_count", "case_pass_rate", "recall_at_10", "precision_at_10", "passed"}
    return bool(quality_keys.intersection(value))


def _postgres_database_from_environment() -> str:
    explicit_database = os.environ.get("POSTGRES_DATABASE")
    if explicit_database:
        return explicit_database
    database_url = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_DSN")
    if database_url:
        return database_url.rstrip("/").rsplit("/", maxsplit=1)[-1].split("?", maxsplit=1)[0]
    return "liaoriver_memory"


def _quality_timeout_seconds_from_environment() -> int:
    raw_value = os.environ.get("QUALITY_EVALUATION_TIMEOUT_SECONDS")
    if raw_value:
        try:
            value = int(raw_value)
        except ValueError:
            return DEFAULT_TIMEOUT_SECONDS
        return max(1, value)
    return DEFAULT_TIMEOUT_SECONDS


def _timeout_stream_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


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


def _next_report_stem(docs_dir: Path, report: dict[str, Any]) -> str:
    generated_date = _report_date(report)
    sequence = _next_daily_report_sequence(docs_dir, generated_date)
    return f"{REPORT_PREFIX}-{generated_date}-{sequence:03d}"


def _report_date(report: dict[str, Any]) -> str:
    raw_generated_at = str(report.get("generated_at") or "")
    try:
        return datetime.fromisoformat(raw_generated_at).strftime("%Y%m%d")
    except ValueError:
        return datetime.now(UTC).strftime("%Y%m%d")


def _next_daily_report_sequence(docs_dir: Path, generated_date: str) -> int:
    pattern = re.compile(rf"^{re.escape(REPORT_PREFIX)}-{generated_date}-(\d{{3}})\.(?:json|md)$")
    sequences: list[int] = []
    for path in docs_dir.glob(f"{REPORT_PREFIX}-{generated_date}-*.*"):
        match = pattern.match(path.name)
        if match:
            sequences.append(int(match.group(1)))
    return max(sequences, default=0) + 1


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


def _dataset_boundary_lines(
    report: dict[str, Any],
    quality: dict[str, Any],
    post_delete: dict[str, Any],
) -> list[str]:
    quality_case_count = int(quality.get("case_count") or 0)
    post_delete_case_count = int(post_delete.get("case_count") or 0)
    total_case_count = quality_case_count + post_delete_case_count
    first_version_gate = "足够支撑，当前报告已通过" if report.get("passed") else "未通过，不能支撑"
    production_quality_sufficient = _production_quality_dataset_sufficient(quality, post_delete)
    broad_production_coverage = (
        "核心质量风险已达到首版门禁，覆盖槽位纠错、负样本、表达漂移、隔离、"
        "单条删除、session 删除、all 删除和 rebuild 复活；但仍不代表广义生产级泛化覆盖已经充分。"
        if production_quality_sufficient
        else (
            "不代表广义生产级泛化覆盖已经充分；当前样本是受控核心样本，"
            "主要覆盖槽位纠错、负样本、隔离、删除和重建。"
        )
    )
    stability_scope = "按本轮要求，本报告不包含稳定性测试。"
    return [
        "",
        "## 5. 数据集充分性说明",
        "",
        "> 中文注释：这里的“足够”只按当前首版主链路质量方案判断；如果要证明更宽泛的生产泛化能力，需要另行扩展真实业务分布样本。",
        "",
        "| 判断项 | 结论 |",
        "| --- | --- |",
        f"| 质量 case 总数 | {total_case_count} |",
        f"| 主链路质量 case | {quality_case_count} |",
        f"| 删除与重建 case | {post_delete_case_count} |",
        f"| 首版主链路质量门禁 | {first_version_gate} |",
        f"| 首版核心质量覆盖 | {'达到' if production_quality_sufficient else '未达到'} |",
        f"| 广义生产级覆盖 | {broad_production_coverage} |",
        f"| 稳定性测试 | {stability_scope} |",
    ]


def _production_quality_dataset_sufficient(
    quality: dict[str, Any],
    post_delete: dict[str, Any],
) -> bool:
    category_metrics = _merged_category_metrics(quality, post_delete)

    def case_count(category: str) -> int:
        return int(category_metrics.get(category, {}).get("case_count", 0))

    required_category_counts = {
        "slot_conflict": 12,
        "negative_control": 6,
        "isolation": 6,
        "expression_drift": 6,
        "delete_memory": 1,
        "delete_session": 2,
        "delete_all": 2,
        "rebuild": 4,
    }
    has_required_counts = all(
        case_count(category) >= minimum
        for category, minimum in required_category_counts.items()
    )
    required_metrics_clean = all(
        value in {0}
        for value in (
            quality.get("conflict_pollution_rate"),
            quality.get("false_positive_rate"),
            quality.get("duplicate_active_rate"),
            quality.get("cross_user_leak_rate"),
            quality.get("cross_character_leak_rate"),
            quality.get("roleplay_real_mix_rate"),
            post_delete.get("delete_memory_residue_rate"),
            post_delete.get("delete_session_residue_rate"),
            post_delete.get("delete_all_residue_rate"),
            post_delete.get("rebuild_resurrection_rate"),
        )
    )
    return (
        bool(quality.get("passed"))
        and bool(post_delete.get("passed"))
        and has_required_counts
        and required_metrics_clean
        and float(quality.get("known_drift_regression_pass_rate") or 0.0) >= 0.95
    )


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
