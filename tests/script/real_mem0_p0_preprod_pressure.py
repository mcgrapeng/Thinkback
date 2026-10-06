"""Build and render P0 pre-production pressure summaries."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

P0_PREPROD_REQUIRED_PHASES = [
    "baseline",
    "stress",
    "spike",
    "soak",
    "fault_injection",
    "representative_replay",
]

_PHASE_ARGS = {
    "baseline": {
        "recall_concurrency": 10,
        "recall_requests": 100,
        "append_concurrency": 10,
    },
    "stress": {
        "recall_concurrency": 50,
        "recall_requests": 100,
        "append_concurrency": 10,
    },
    "spike": {
        "recall_concurrency": 100,
        "recall_requests": 100,
        "append_concurrency": 3,
    },
    "soak_probe": {
        "recall_concurrency": 30,
        "recall_requests": 100,
        "append_concurrency": 10,
    },
    "soak": {
        "recall_concurrency": 30,
        "recall_requests": 100,
        "append_concurrency": 10,
    },
}

_DURATION_PHASE_TARGET_SECONDS = {
    "baseline": 10 * 60,
    "stress": 15 * 60,
    "soak": 10 * 60,
}

_DEFAULT_DURATION_PHASE_INTERVAL_SECONDS = {
    "baseline": 0,
    "stress": 0,
    "soak": 300,
}


def build_preprod_report(
    *,
    run_id: str,
    started_at: str,
    ended_at: str,
    phase_reports: dict[str, list[dict[str, Any]]],
    required_phases: list[str],
) -> dict[str, Any]:
    phase_results = {
        phase: _summarize_phase(phase, reports) for phase, reports in sorted(phase_reports.items())
    }
    missing_phases = [phase for phase in required_phases if phase not in phase_reports]
    failed_phases = [
        phase
        for phase in required_phases
        if phase in phase_results and not bool(phase_results[phase]["passed"])
    ]
    failed_production_phases = [
        phase
        for phase in required_phases
        if phase in phase_results
        and not bool(phase_results[phase].get("production_phase_passed", True))
    ]
    requested_phases_passed = not failed_phases
    production_precheck_passed = (
        requested_phases_passed and not missing_phases and not failed_production_phases
    )
    return {
        "run_id": run_id,
        "started_at": started_at,
        "ended_at": ended_at,
        "required_phases": required_phases,
        "executed_phases": sorted(phase_reports),
        "missing_phases": missing_phases,
        "failed_phases": failed_phases,
        "failed_production_phases": failed_production_phases,
        "requested_phases_passed": requested_phases_passed,
        "production_precheck_passed": production_precheck_passed,
        "phase_results": phase_results,
    }


def build_preprod_report_from_phase_reports(
    *,
    run_id: str,
    started_at: str,
    ended_at: str,
    phase_reports: dict[str, list[dict[str, Any]]],
    required_phases: list[str],
) -> dict[str, Any]:
    return build_preprod_report(
        run_id=run_id,
        started_at=started_at,
        ended_at=ended_at,
        phase_reports=phase_reports,
        required_phases=required_phases,
    )


def render_preprod_report_markdown(report: dict[str, Any]) -> str:
    conclusion = "通过" if report.get("production_precheck_passed") else "未通过"
    requested = "通过" if report.get("requested_phases_passed") else "未通过"
    missing = ", ".join(report.get("missing_phases", [])) or "-"
    failed = ", ".join(report.get("failed_phases", [])) or "-"
    failed_production = ", ".join(report.get("failed_production_phases", [])) or "-"
    lines = [
        "# thinkback P0 生产前压测汇总报告",
        "",
        "## 1. 结论",
        "",
        "| 项目 | 结果 |",
        "| --- | --- |",
        f"| run_id | `{report['run_id']}` |",
        f"| started_at | `{report['started_at']}` |",
        f"| ended_at | `{report['ended_at']}` |",
        f"| 已执行阶段 | {', '.join(report.get('executed_phases', [])) or '-'} |",
        f"| 缺失阶段 | {missing} |",
        f"| 失败阶段 | {failed} |",
        f"| 未满足正式生产阶段 | {failed_production} |",
        f"| 已执行阶段门禁 | {requested} |",
        f"| 生产前完整压测 | {conclusion} |",
        "",
        "## 2. 阶段明细",
        "",
        "| 阶段 | 子报告数 | 结论 | worst recall p95 | worst recall p99 | 失败报告 |",
        "| --- | ---: | --- | ---: | ---: | --- |",
    ]
    for phase, result in report.get("phase_results", {}).items():
        failed_children = ", ".join(result.get("failed_child_reports", [])) or "-"
        phase_conclusion = "通过" if result.get("passed") else "未通过"
        lines.append(
            "| "
            f"{phase} | "
            f"{result.get('report_count', 0)} | "
            f"{phase_conclusion} | "
            f"{result.get('worst_recall_p95_ms', 0)} | "
            f"{result.get('worst_recall_p99_ms', 0)} | "
            f"{failed_children} |"
        )
    lines.append("")
    return "\n".join(lines)


def write_preprod_report(report: dict[str, Any], *, output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{report['run_id']}.json"
    md_path = output_dir / f"{report['run_id']}.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    md_path.write_text(render_preprod_report_markdown(report), encoding="utf-8")
    return json_path, md_path


def build_duration_phase_report(
    *,
    run_id: str,
    phase: str,
    started_at: str,
    ended_at: str,
    target_duration_seconds: int,
    actual_duration_seconds: float,
    child_reports: list[dict[str, Any]],
) -> dict[str, Any]:
    failed_child_reports = [
        str(report.get("suite_id", report.get("run_id", "-")))
        for report in child_reports
        if not bool(report.get("passed"))
    ]
    official_min_duration_seconds = _DURATION_PHASE_TARGET_SECONDS.get(
        phase, target_duration_seconds
    )
    required_duration_seconds = max(target_duration_seconds, official_min_duration_seconds)
    duration_gate_passed = actual_duration_seconds >= required_duration_seconds
    child_gate_passed = not failed_child_reports
    return {
        "run_id": run_id,
        "phase": phase,
        "started_at": started_at,
        "ended_at": ended_at,
        "target_duration_seconds": target_duration_seconds,
        "official_min_duration_seconds": official_min_duration_seconds,
        "required_duration_seconds": required_duration_seconds,
        "actual_duration_seconds": round(actual_duration_seconds, 3),
        "duration_gate_passed": duration_gate_passed,
        "probe_only": not duration_gate_passed,
        "passed": child_gate_passed and duration_gate_passed,
        "child_gate_passed": child_gate_passed,
        "report_count": len(child_reports),
        "failed_child_reports": failed_child_reports,
        "suite_ids": [
            str(report.get("suite_id", report.get("run_id", "-"))) for report in child_reports
        ],
        "worst_recall_p95_ms": _worst_recall_latency(child_reports, "p95"),
        "worst_recall_p99_ms": _worst_recall_latency(child_reports, "p99"),
        "child_reports": child_reports,
    }


def render_duration_phase_markdown(report: dict[str, Any]) -> str:
    conclusion = "通过" if report.get("passed") else "未通过"
    duration = "通过" if report.get("duration_gate_passed") else "未满足，按短探针记录"
    child_gate = "通过" if report.get("child_gate_passed", report.get("passed")) else "未通过"
    lines = [
        "# thinkback P0 持续压测阶段报告",
        "",
        "## 1. 结论",
        "",
        "| 项目 | 结果 |",
        "| --- | --- |",
        f"| run_id | `{report['run_id']}` |",
        f"| phase | {report['phase']} |",
        f"| started_at | `{report['started_at']}` |",
        f"| ended_at | `{report['ended_at']}` |",
        f"| 整体结论 | {conclusion} |",
        f"| 子报告门禁 | {child_gate} |",
        f"| 持续时间门禁 | {duration} |",
        f"| target_duration_seconds | {report['target_duration_seconds']} |",
        f"| actual_duration_seconds | {report['actual_duration_seconds']} |",
        f"| report_count | {report['report_count']} |",
        f"| worst recall p95 | {report['worst_recall_p95_ms']}ms |",
        f"| worst recall p99 | {report['worst_recall_p99_ms']}ms |",
        f"| failed_child_reports | {', '.join(report.get('failed_child_reports', [])) or '-'} |",
        "",
        "## 2. 子报告",
        "",
        "| suite_id | passed | recall_concurrency | recall_requests | append_concurrency | recall p95 | recall p99 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for child in report.get("child_reports", []):
        config = child.get("config", {})
        latency = child.get("concurrent_recall", {}).get("latency_ms", {}).get("recall", {})
        lines.append(
            "| "
            f"`{child.get('suite_id', child.get('run_id', '-'))}` | "
            f"{'通过' if child.get('passed') else '未通过'} | "
            f"{config.get('recall_concurrency', '-')} | "
            f"{config.get('recall_requests', '-')} | "
            f"{config.get('append_concurrency', '-')} | "
            f"{latency.get('p95', 0)} | "
            f"{latency.get('p99', 0)} |"
        )
    lines.append("")
    return "\n".join(lines)


def write_duration_phase_report(report: dict[str, Any], *, output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{report['run_id']}.json"
    md_path = output_dir / f"{report['run_id']}.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    md_path.write_text(render_duration_phase_markdown(report), encoding="utf-8")
    return json_path, md_path


def build_spike_phase_report(
    *,
    run_id: str,
    started_at: str,
    ended_at: str,
    leg_reports: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    failed_legs = [
        leg_name for leg_name, report in leg_reports.items() if not bool(report.get("passed"))
    ]
    recovery_gate_passed = bool(leg_reports.get("recovery_10", {}).get("passed"))
    child_reports = list(leg_reports.values())
    return {
        "run_id": run_id,
        "phase": "spike",
        "started_at": started_at,
        "ended_at": ended_at,
        "passed": not failed_legs and recovery_gate_passed,
        "failed_legs": failed_legs,
        "recovery_gate_passed": recovery_gate_passed,
        "report_count": len(child_reports),
        "suite_ids": [
            str(report.get("suite_id", report.get("run_id", "-"))) for report in child_reports
        ],
        "worst_recall_p95_ms": _worst_recall_latency(child_reports, "p95"),
        "worst_recall_p99_ms": _worst_recall_latency(child_reports, "p99"),
        "leg_reports": leg_reports,
    }


def render_spike_phase_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# thinkback P0 Spike 恢复曲线报告",
        "",
        "## 1. 结论",
        "",
        "| 项目 | 结果 |",
        "| --- | --- |",
        f"| run_id | `{report['run_id']}` |",
        f"| started_at | `{report['started_at']}` |",
        f"| ended_at | `{report['ended_at']}` |",
        f"| 整体结论 | {'通过' if report.get('passed') else '未通过'} |",
        f"| recovery_gate | {'通过' if report.get('recovery_gate_passed') else '未通过'} |",
        f"| failed_legs | {', '.join(report.get('failed_legs', [])) or '-'} |",
        f"| worst recall p95 | {report.get('worst_recall_p95_ms', 0)}ms |",
        f"| worst recall p99 | {report.get('worst_recall_p99_ms', 0)}ms |",
        "",
        "## 2. 阶段明细",
        "",
        "| 阶段 | suite_id | passed | recall_concurrency | recall p95 | recall p99 |",
        "| --- | --- | --- | ---: | ---: | ---: |",
    ]
    for leg, child in report.get("leg_reports", {}).items():
        config = child.get("config", {})
        latency = child.get("concurrent_recall", {}).get("latency_ms", {}).get("recall", {})
        lines.append(
            "| "
            f"{leg} | "
            f"`{child.get('suite_id', child.get('run_id', '-'))}` | "
            f"{'通过' if child.get('passed') else '未通过'} | "
            f"{config.get('recall_concurrency', '-')} | "
            f"{latency.get('p95', 0)} | "
            f"{latency.get('p99', 0)} |"
        )
    lines.append("")
    return "\n".join(lines)


def write_spike_phase_report(report: dict[str, Any], *, output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{report['run_id']}.json"
    md_path = output_dir / f"{report['run_id']}.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    md_path.write_text(render_spike_phase_markdown(report), encoding="utf-8")
    return json_path, md_path


def _phase_command(
    phase: str,
    *,
    report_dir: str,
    python_executable: str,
    script_path: str,
) -> list[str]:
    if phase not in _PHASE_ARGS:
        raise ValueError(f"unsupported executable P0 preprod phase: {phase}")
    phase_args = _PHASE_ARGS[phase]
    return [
        python_executable,
        script_path,
        "--recall-concurrency",
        str(phase_args["recall_concurrency"]),
        "--recall-requests",
        str(phase_args["recall_requests"]),
        "--append-concurrency",
        str(phase_args["append_concurrency"]),
        "--report-dir",
        report_dir,
    ]


def _spike_phase_commands(
    *,
    report_dir: str,
    python_executable: str,
    script_path: str,
) -> list[list[str]]:
    commands = []
    for concurrency in (10, 100, 10):
        commands.append(
            [
                python_executable,
                script_path,
                "--recall-concurrency",
                str(concurrency),
                "--recall-requests",
                "100",
                "--append-concurrency",
                "3",
                "--report-dir",
                report_dir,
            ]
        )
    return commands


def _load_latest_report(report_dir: Path, before: set[Path]) -> dict[str, Any]:
    after = set(report_dir.glob("p0-short-*.json"))
    created = sorted(after - before, key=lambda path: path.stat().st_mtime)
    if not created:
        raise RuntimeError(f"phase command did not create a p0-short JSON report in {report_dir}")
    return json.loads(created[-1].read_text(encoding="utf-8"))


def _run_duration_child_command(
    *,
    phase: str,
    iteration: int,
    command: list[str],
    report_dir: Path,
    before: set[Path],
    process_runner: Any = subprocess.run,
) -> dict[str, Any]:
    log_dir = report_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"p0-duration-{phase}-iteration-{iteration}.log"
    try:
        completed = process_runner(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        log_path.write_text(
            _render_duration_child_log(command=command, stdout=stdout, stderr=stderr),
            encoding="utf-8",
        )
        return _failed_duration_child_report(
            phase=phase,
            iteration=iteration,
            command=command,
            error=f"{exc}; log={log_path}",
        )
    stdout = getattr(completed, "stdout", "") or ""
    stderr = getattr(completed, "stderr", "") or ""
    log_path.write_text(
        _render_duration_child_log(command=command, stdout=stdout, stderr=stderr),
        encoding="utf-8",
    )
    report = _load_latest_report(report_dir, before)
    report["duration_child_log_path"] = str(log_path)
    return report


def _render_duration_child_log(*, command: list[str], stdout: str, stderr: str) -> str:
    return "\n".join(
        [
            f"COMMAND: {' '.join(command)}",
            "",
            "STDOUT:",
            stdout,
            "",
            "STDERR:",
            stderr,
            "",
        ]
    )


def _render_cli_summary(report: dict[str, Any]) -> str:
    report_id = report.get("run_id", report.get("suite_id", "-"))
    parts = [
        f"run_id={report_id}",
        f"phase={report.get('phase', '-')}",
        f"passed={report.get('passed')}",
    ]
    if "duration_gate_passed" in report:
        parts.append(f"duration_gate_passed={report.get('duration_gate_passed')}")
        parts.append(f"actual_duration_seconds={report.get('actual_duration_seconds')}")
        parts.append(f"report_count={report.get('report_count', 0)}")
    if "production_precheck_passed" in report:
        parts.append(f"production_precheck_passed={report.get('production_precheck_passed')}")
        parts.append(f"failed_phases={','.join(report.get('failed_phases', [])) or '-'}")
    if "recovery_gate_passed" in report:
        parts.append(f"recovery_gate_passed={report.get('recovery_gate_passed')}")
        parts.append(f"failed_legs={','.join(report.get('failed_legs', [])) or '-'}")
    return "P0 report summary: " + " ".join(parts)


def _run_duration_phase(
    *,
    phase: str,
    target_duration_seconds: int,
    max_iterations: int,
    iteration_interval_seconds: int | None,
    report_dir: Path,
    python_executable: str,
    short_pressure_script: str,
) -> dict[str, Any]:
    started_at = datetime.now(UTC).isoformat()
    started_monotonic = time.monotonic()
    child_reports: list[dict[str, Any]] = []
    iteration = 0
    while time.monotonic() - started_monotonic < target_duration_seconds:
        if max_iterations and iteration >= max_iterations:
            break
        iteration += 1
        before = set(report_dir.glob("p0-short-*.json"))
        command = _phase_command(
            phase,
            report_dir=str(report_dir),
            python_executable=python_executable,
            script_path=short_pressure_script,
        )
        print(f"running P0 duration phase {phase} iteration {iteration}: {' '.join(command)}")
        child_report = _run_duration_child_command(
            phase=phase,
            iteration=iteration,
            command=command,
            report_dir=report_dir,
            before=before,
        )
        child_reports.append(child_report)
        print(
            "completed P0 duration phase "
            f"{phase} iteration {iteration}: "
            f"suite_id={child_report.get('suite_id', child_report.get('run_id', '-'))} "
            f"passed={child_report.get('passed')} "
            f"log={child_report.get('duration_child_log_path', '-')}"
        )
        if not bool(child_report.get("passed")):
            break
        interval_seconds = _soak_iteration_interval(
            phase,
            override_seconds=iteration_interval_seconds,
        )
        remaining_seconds = target_duration_seconds - (time.monotonic() - started_monotonic)
        if interval_seconds > 0 and remaining_seconds > 0:
            time.sleep(min(interval_seconds, remaining_seconds))
    actual_duration_seconds = time.monotonic() - started_monotonic
    return build_duration_phase_report(
        run_id=f"p0-duration-{phase}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}",
        phase=phase,
        started_at=started_at,
        ended_at=datetime.now(UTC).isoformat(),
        target_duration_seconds=target_duration_seconds,
        actual_duration_seconds=actual_duration_seconds,
        child_reports=child_reports,
    )


def _soak_iteration_interval(phase: str, override_seconds: int | None = None) -> int:
    if override_seconds is not None:
        return max(0, override_seconds)
    return _DEFAULT_DURATION_PHASE_INTERVAL_SECONDS.get(phase, 0)


def _failed_duration_child_report(
    *,
    phase: str,
    iteration: int,
    command: list[str],
    error: str,
) -> dict[str, Any]:
    return {
        "suite_id": f"p0-{phase}-iteration-{iteration}-failed",
        "passed": False,
        "failed_sections": ["duration_child_command"],
        "duration_child_failure": {
            "phase": phase,
            "iteration": iteration,
            "command": command,
            "error": error,
        },
        "quality": {"passed": False, "failed_cases": [], "latency_ms": {"recall": {}}},
        "concurrent_recall": {"passed": False, "failed_cases": [], "latency_ms": {"recall": {}}},
        "append_probe": {"passed": False, "failed_cases": [error], "latency_ms": {"append": {}}},
        "post_delete": {"passed": False, "failed_cases": [], "latency_ms": {"recall": {}}},
    }


def _run_spike_phase(
    *,
    report_dir: Path,
    python_executable: str,
    short_pressure_script: str,
) -> dict[str, Any]:
    started_at = datetime.now(UTC).isoformat()
    leg_reports: dict[str, dict[str, Any]] = {}
    leg_names = ("warmup_10", "peak_100", "recovery_10")
    for leg, command in zip(
        leg_names,
        _spike_phase_commands(
            report_dir=str(report_dir),
            python_executable=python_executable,
            script_path=short_pressure_script,
        ),
        strict=True,
    ):
        before = set(report_dir.glob("p0-short-*.json"))
        print(f"running P0 full spike leg {leg}: {' '.join(command)}")
        subprocess.run(command, check=True)
        leg_reports[leg] = _load_latest_report(report_dir, before)
    return build_spike_phase_report(
        run_id=f"p0-spike-full-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}",
        started_at=started_at,
        ended_at=datetime.now(UTC).isoformat(),
        leg_reports=leg_reports,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run P0 pre-production pressure phases.")
    parser.add_argument(
        "--phase",
        action="append",
        choices=["baseline", "stress", "spike", "soak_probe"],
        help="Executable phase to run. Repeat for multiple phases.",
    )
    parser.add_argument("--report-dir", default="docs/memory/report")
    parser.add_argument("--output-dir", default="docs/memory/report")
    parser.add_argument("--python-executable", default=sys.executable)
    parser.add_argument(
        "--short-pressure-script", default="tests/script/real_mem0_p0_short_pressure.py"
    )
    parser.add_argument("--duration-phase", choices=["baseline", "stress", "soak"])
    parser.add_argument("--target-duration-seconds", type=int)
    parser.add_argument("--max-iterations", type=int, default=0)
    parser.add_argument(
        "--iteration-interval-seconds",
        type=int,
        help="Delay between successful duration child suites. Defaults to 300s for soak and 0 for other phases.",
    )
    parser.add_argument("--full-spike", action="store_true")
    parser.add_argument(
        "--phase-report",
        action="append",
        default=[],
        help="Existing phase report in phase=path format. Repeat for multiple phases.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if args.full_spike:
        report = _run_spike_phase(
            report_dir=Path(args.report_dir),
            python_executable=args.python_executable,
            short_pressure_script=args.short_pressure_script,
        )
        json_path, md_path = write_spike_phase_report(report, output_dir=Path(args.output_dir))
        print(f"spike phase report json: {json_path}")
        print(f"spike phase report markdown: {md_path}")
        print(_render_cli_summary(report))
        if not report["passed"]:
            raise RuntimeError(f"P0 full spike failed: {report['failed_legs']}")
        return
    if args.duration_phase:
        target_duration_seconds = (
            args.target_duration_seconds
            if args.target_duration_seconds is not None
            else _DURATION_PHASE_TARGET_SECONDS[args.duration_phase]
        )
        report = _run_duration_phase(
            phase=args.duration_phase,
            target_duration_seconds=target_duration_seconds,
            max_iterations=args.max_iterations,
            iteration_interval_seconds=args.iteration_interval_seconds,
            report_dir=Path(args.report_dir),
            python_executable=args.python_executable,
            short_pressure_script=args.short_pressure_script,
        )
        json_path, md_path = write_duration_phase_report(report, output_dir=Path(args.output_dir))
        print(f"duration phase report json: {json_path}")
        print(f"duration phase report markdown: {md_path}")
        print(_render_cli_summary(report))
        if not report["passed"]:
            raise RuntimeError(f"P0 duration phase failed: {report['failed_child_reports']}")
        return
    if args.phase_report:
        phase_reports = _load_phase_report_args(args.phase_report)
        report = build_preprod_report_from_phase_reports(
            run_id=f"p0-preprod-summary-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}",
            started_at=datetime.now(UTC).isoformat(),
            ended_at=datetime.now(UTC).isoformat(),
            phase_reports=phase_reports,
            required_phases=P0_PREPROD_REQUIRED_PHASES,
        )
        json_path, md_path = write_preprod_report(report, output_dir=Path(args.output_dir))
        print(f"preprod report json: {json_path}")
        print(f"preprod report markdown: {md_path}")
        print(_render_cli_summary(report))
        _raise_if_preprod_report_failed(report)
        return
    if not args.phase:
        raise RuntimeError("--phase or --phase-report is required unless --duration-phase is set")

    started_at = datetime.now(UTC).isoformat()
    run_id = f"p0-preprod-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}"
    report_dir = Path(args.report_dir)
    phase_reports: dict[str, list[dict[str, Any]]] = {}
    for phase in args.phase:
        before = set(report_dir.glob("p0-short-*.json"))
        command = _phase_command(
            phase,
            report_dir=args.report_dir,
            python_executable=args.python_executable,
            script_path=args.short_pressure_script,
        )
        print(f"running P0 preprod phase {phase}: {' '.join(command)}")
        subprocess.run(command, check=True)
        phase_reports.setdefault(phase, []).append(_load_latest_report(report_dir, before))
    report = build_preprod_report(
        run_id=run_id,
        started_at=started_at,
        ended_at=datetime.now(UTC).isoformat(),
        phase_reports=phase_reports,
        required_phases=P0_PREPROD_REQUIRED_PHASES,
    )
    json_path, md_path = write_preprod_report(report, output_dir=Path(args.output_dir))
    print(f"preprod report json: {json_path}")
    print(f"preprod report markdown: {md_path}")
    print(_render_cli_summary(report))
    _raise_if_preprod_report_failed(report)


def _raise_if_preprod_report_failed(report: dict[str, Any]) -> None:
    if bool(report["production_precheck_passed"]):
        return
    if not bool(report["requested_phases_passed"]):
        raise SystemExit(1)
    raise SystemExit(1)


def _summarize_phase(phase: str, reports: list[dict[str, Any]]) -> dict[str, Any]:
    failed_child_reports = [
        _report_id(report) for report in reports if not bool(report.get("passed"))
    ]
    duration_reports = [report for report in reports if "duration_gate_passed" in report]
    duration_required = phase in {"baseline", "stress", "soak"}
    duration_gate_passed = bool(duration_reports) and all(
        _duration_report_meets_current_gate(phase, report) for report in duration_reports
    )
    if not duration_required and not duration_reports:
        duration_gate_passed = True
    failed_duration_reports = [
        str(report.get("run_id", "-"))
        for report in duration_reports
        if not _duration_report_meets_current_gate(phase, report)
    ]
    spike_reports = [report for report in reports if "recovery_gate_passed" in report]
    spike_required = phase == "spike"
    recovery_gate_passed = bool(spike_reports) and all(
        bool(report.get("recovery_gate_passed")) for report in spike_reports
    )
    if not spike_required and not spike_reports:
        recovery_gate_passed = True
    failed_spike_reports = [
        str(report.get("run_id", "-"))
        for report in spike_reports
        if not bool(report.get("recovery_gate_passed"))
    ]
    representative_reports = [
        report
        for report in reports
        if "duration_gate_passed" not in report and "recovery_gate_passed" not in report
    ]
    representative_probe_passed = bool(representative_reports) and all(
        bool(report.get("passed")) for report in representative_reports
    )
    passed = not failed_child_reports and not failed_duration_reports and not failed_spike_reports
    production_phase_passed = not failed_child_reports
    if duration_required:
        production_phase_passed = production_phase_passed and duration_gate_passed
    if spike_required:
        production_phase_passed = production_phase_passed and recovery_gate_passed
    return {
        "phase": phase,
        "passed": passed,
        "production_phase_passed": production_phase_passed,
        "report_count": len(reports),
        "failed_child_reports": failed_child_reports,
        "duration_gate_passed": duration_gate_passed,
        "failed_duration_reports": failed_duration_reports,
        "recovery_gate_passed": recovery_gate_passed,
        "failed_spike_reports": failed_spike_reports,
        "representative_probe_passed": representative_probe_passed,
        "suite_ids": [_report_id(report) for report in reports],
        "worst_recall_p95_ms": _worst_recall_latency(reports, "p95"),
        "worst_recall_p99_ms": _worst_recall_latency(reports, "p99"),
    }


def _duration_report_meets_current_gate(phase: str, report: dict[str, Any]) -> bool:
    current_min_duration_seconds = _DURATION_PHASE_TARGET_SECONDS.get(phase)
    if current_min_duration_seconds is None:
        return bool(report.get("duration_gate_passed"))
    return float(report.get("actual_duration_seconds", 0)) >= current_min_duration_seconds


def _load_phase_report_args(phase_report_args: list[str]) -> dict[str, list[dict[str, Any]]]:
    phase_reports: dict[str, list[dict[str, Any]]] = {}
    for item in phase_report_args:
        if "=" not in item:
            raise ValueError("--phase-report must use phase=path format")
        phase, raw_path = item.split("=", 1)
        if phase not in P0_PREPROD_REQUIRED_PHASES:
            raise ValueError(f"unsupported P0 preprod phase report: {phase}")
        path = Path(raw_path)
        report = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(report, dict):
            raise ValueError(f"phase report must be a JSON object: {path}")
        phase_reports.setdefault(phase, []).append(dict(report))
    return phase_reports


def _worst_recall_latency(reports: list[dict[str, Any]], percentile: str) -> int:
    values = [_recall_latency(report, percentile) for report in reports]
    return max(values, default=0)


def _recall_latency(report: dict[str, Any], percentile: str) -> int:
    legacy_value = (
        report.get("concurrent_recall", {}).get("latency_ms", {}).get("recall", {}).get(percentile)
    )
    if legacy_value is not None:
        return int(legacy_value)
    return int(report.get(f"worst_recall_{percentile}_ms", 0))


def _report_id(report: dict[str, Any]) -> str:
    return str(report.get("suite_id", report.get("run_id", "-")))


if __name__ == "__main__":
    main()
