"""Fault-injection reporting helpers for P0 production precheck."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def build_fault_injection_report(
    *,
    run_id: str,
    started_at: str,
    ended_at: str,
    scenarios: list[dict[str, Any]],
) -> dict[str, Any]:
    scenario_results = [_normalize_scenario(scenario) for scenario in scenarios]
    failed_scenarios = [
        str(result["name"])
        for result in scenario_results
        if not bool(result["passed"])
    ]
    return {
        "run_id": run_id,
        "started_at": started_at,
        "ended_at": ended_at,
        "passed": not failed_scenarios,
        "failed_scenarios": failed_scenarios,
        "scenario_results": scenario_results,
    }


def render_fault_injection_markdown(report: dict[str, Any]) -> str:
    conclusion = "通过" if report.get("passed") else "未通过"
    lines = [
        "# Thinkback P0 故障注入报告",
        "",
        "## 1. 结论",
        "",
        "| 项目 | 结果 |",
        "| --- | --- |",
        f"| run_id | `{report['run_id']}` |",
        f"| started_at | `{report['started_at']}` |",
        f"| ended_at | `{report['ended_at']}` |",
        f"| 故障注入 | {conclusion} |",
        f"| 失败场景 | {', '.join(report.get('failed_scenarios', [])) or '-'} |",
        "",
        "## 2. 场景明细",
        "",
        "| 场景 | 结论 | 注入失败可观测 | 恢复验证 | 失败细节 | 恢复细节 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for result in report.get("scenario_results", []):
        lines.append(
            "| "
            f"{result['name']} | "
            f"{'通过' if result['passed'] else '未通过'} | "
            f"{result['failure_observed']} | "
            f"{result['recovery_verified']} | "
            f"{result.get('failure_detail', '-')} | "
            f"{result.get('recovery_detail', '-')} |"
        )
    lines.append("")
    return "\n".join(lines)


def write_fault_injection_report(report: dict[str, Any], *, output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{report['run_id']}.json"
    md_path = output_dir / f"{report['run_id']}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    md_path.write_text(render_fault_injection_markdown(report), encoding="utf-8")
    return json_path, md_path


def _normalize_scenario(scenario: dict[str, Any]) -> dict[str, Any]:
    result = dict(scenario)
    result["passed"] = all(
        bool(result.get(field))
        for field in ("injected", "failure_observed", "recovered", "recovery_verified")
    )
    result.setdefault("failure_detail", "-")
    result.setdefault("recovery_detail", "-")
    return result
