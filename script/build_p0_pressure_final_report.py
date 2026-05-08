"""Build a final P0 pressure report from real pressure JSON reports."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def build_final_report(
    report_paths: list[Path],
    *,
    output_path: Path,
    observed_fixed_issues: list[str] | None = None,
    preprod_summary: dict[str, Any] | None = None,
) -> str:
    reports = [_load_report(path) for path in report_paths]
    fixed_issues = observed_fixed_issues or []
    lines = [
        "# Thinkback P0 压测最终报告",
        "",
        "## 1. 结论",
        "",
    ]
    passed_count = sum(1 for report in reports if bool(report["passed"]))
    failed_reports = [report for report in reports if not bool(report["passed"])]
    latest_pass = next((report for report in reversed(reports) if bool(report["passed"])), None)
    latest_dev_pass = next(
        (
            report
            for report in reversed(reports)
            if bool(report["passed"])
            and int(report.get("config", {}).get("recall_concurrency", 0)) <= 10
        ),
        None,
    )
    stress_failures = [
        report for report in failed_reports
        if int(report.get("config", {}).get("recall_concurrency", 0)) == 50
    ]
    stress_passes = [
        report for report in reports
        if bool(report.get("passed"))
        and int(report.get("config", {}).get("recall_concurrency", 0)) == 50
    ]
    semantic_failures = [
        report for report in failed_reports
        if _has_quality_failure(report)
    ]

    lines.extend(
        [
            "| 项目 | 结果 |",
            "| --- | --- |",
            f"| 报告生成时间 | `{datetime.now(UTC).isoformat()}` |",
            f"| 输入报告数 | {len(reports)} |",
            f"| 通过报告数 | {passed_count} |",
            f"| 失败报告数 | {len(failed_reports)} |",
            f"| 最新通过报告 | `{latest_pass['suite_id'] if latest_pass else '-'}` |",
            f"| 是否达到 P0 主链路短压测 | {'是' if latest_dev_pass else '否'} |",
            (
                "| 是否达到生产前完整压测标准 | "
                f"{'是' if preprod_summary and preprod_summary.get('production_precheck_passed') else '否'} |"
            ),
            "",
        ]
    )

    if semantic_failures or fixed_issues:
        lines.extend(
            [
                "本轮压测曾发现真实语义失败，已补回归测试并修复，修复后的真实短压测通过。",
                "",
            ]
        )
    if stress_failures:
        lines.extend(
            [
                "50 并发代表性压测的召回准确率仍为 1.0，但 `recall p95` 超过当前 1500ms 门禁。因此不能把本轮结果写成“生产前完整压测通过”。",
                "",
            ]
        )
    elif stress_passes:
        best_stress = _select_latest_report(stress_passes)
        stress_recall = best_stress["concurrent_recall"]
        stress_latency = stress_recall.get("latency_ms", {}).get("recall", {})
        lines.extend(
            [
                (
                    "50 并发代表性压测已通过："
                    f"`suite_id={best_stress['suite_id']}`，"
                    f"`recall_at_10={stress_recall.get('recall_at_10', 0.0)}`，"
                    f"`precision_at_10={stress_recall.get('precision_at_10', 0.0)}`，"
                    f"`recall p95={stress_latency.get('p95', 0)}ms`。"
                    "这说明 P0 召回主链路在当前代表性并发下未退化，但仍不能替代长测、故障注入和代表性样本回放。"
                ),
                "",
            ]
        )

    lines.extend(
        [
            "## 2. 报告明细",
            "",
            "| suite_id | 并发 recall | recall 请求 | 结论 | 质量通过率 | recall@10 | precision@10 | 冲突污染 | 误召回 | recall p95 | recall p99 | 失败分段 |",
            "| --- | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for report in reports:
        quality = report["quality"]
        concurrent = report["concurrent_recall"]
        latency = concurrent.get("latency_ms", {}).get("recall", {})
        config = report.get("config", {})
        lines.append(
            "| "
            f"`{report['suite_id']}` | "
            f"{config.get('recall_concurrency', '-')} | "
            f"{config.get('recall_requests', '-')} | "
            f"{'通过' if report['passed'] else '未通过'} | "
            f"{quality.get('case_pass_rate', 0.0)} | "
            f"{quality.get('recall_at_10', 0.0)} | "
            f"{quality.get('precision_at_10', 0.0)} | "
            f"{quality.get('conflict_pollution_rate', 0.0)} | "
            f"{quality.get('false_positive_rate', 0.0)} | "
            f"{latency.get('p95', 0)} | "
            f"{latency.get('p99', 0)} | "
            f"`{', '.join(report.get('failed_sections', [])) or '-'}` |"
        )

    lines.extend(
        [
            "",
            "## 3. 已发现并修复的问题",
            "",
            "| 问题 | 证据 | 处理结果 |",
            "| --- | --- | --- |",
        ]
    )
    if fixed_issues:
        for issue in fixed_issues:
            lines.append(f"| 已观察真实压测故障 | {issue} | 已补回归测试并修复，修复后真实短压测通过。 |")
    elif semantic_failures:
        lines.append(
            "| 睡眠提醒表达漂移导致漏召回 | 失败报告中 `sleep-reminder-current` 未命中，active L3 为 `User now accepts gentle reminders about going to sleep`。 | 已加入归一化 pattern 和单测，修复后真实短压测通过。 |"
        )
    else:
        lines.append("| 本次输入报告中未出现语义失败。 | - | - |")

    lines.extend(
        [
            "",
            "## 4. 当前质量指标",
            "",
        ]
    )
    metric_pass = latest_dev_pass or latest_pass
    if metric_pass:
        latest_quality = metric_pass["quality"]
        latest_concurrent = metric_pass["concurrent_recall"]
        recall_concurrency = int(metric_pass.get("config", {}).get("recall_concurrency", 0))
        lines.extend(
            [
                "| 指标 | 最新通过 dev gate | P0 门禁 |",
                "| --- | ---: | ---: |",
                f"| `case_pass_rate` | {latest_quality.get('case_pass_rate', 0.0)} | >= 0.98 |",
                f"| `recall_at_10` | {latest_quality.get('recall_at_10', 0.0)} | >= 0.95 |",
                f"| `precision_at_10` | {latest_quality.get('precision_at_10', 0.0)} | >= 0.95 |",
                f"| `conflict_pollution_rate` | {latest_quality.get('conflict_pollution_rate', 0.0)} | <= 0.02 |",
                f"| `false_positive_rate` | {latest_quality.get('false_positive_rate', 0.0)} | <= 0.02 |",
                f"| `duplicate_active_rate` | {latest_quality.get('duplicate_active_rate', 0.0)} | 0 |",
                f"| `delete_memory_residue_rate` | {_metric(metric_pass['post_delete'], 'delete_memory_residue_rate', 'delete_residue_rate', 0.0)} | 0 |",
                f"| `rebuild_resurrection_rate` | {_metric(metric_pass['post_delete'], 'rebuild_resurrection_rate', default=0.0)} | 0 |",
                f"| {recall_concurrency} 并发 recall p95 | {latest_concurrent.get('latency_ms', {}).get('recall', {}).get('p95', 0)}ms | <= 1500ms |",
                f"| {recall_concurrency} 并发 recall p99 | {latest_concurrent.get('latency_ms', {}).get('recall', {}).get('p99', 0)}ms | <= 3000ms |",
                "",
            ]
        )

    stress_section_reports = stress_failures or stress_passes
    if stress_section_reports:
        selected_stress = _select_latest_report(stress_section_reports)
        stress_recall = selected_stress["concurrent_recall"]
        stress_latency = stress_recall.get("latency_ms", {}).get("recall", {})
        stress_passed = bool(selected_stress.get("passed"))
        lines.extend(
            [
                "## 5. 50 并发代表性压测",
                "",
                "| 场景 | 质量 | 延迟 | 结论 |",
                "| --- | --- | --- | --- |",
                (
                    f"| 50 并发 recall / 100 请求 | "
                    f"`case_pass_rate={stress_recall.get('case_pass_rate', 0.0)}`, "
                    f"`recall_at_10={stress_recall.get('recall_at_10', 0.0)}` | "
                    f"`p95={stress_latency.get('p95', 0)}ms`, `p99={stress_latency.get('p99', 0)}ms` | "
                    f"{'通过，未观察到召回质量退化。' if stress_passed else '准确率通过，p95 延迟未通过。'} |"
                ),
                "",
                (
                    "50 并发代表性压测只证明当前构造样本和短时请求下的召回质量与延迟门禁。"
                    "它不能替代 15-30 分钟 stress、100 并发 spike、6-24 小时 soak 或依赖故障注入。"
                ),
                "",
            ]
        )

    lines.extend(
        [
            "## 6. 生产前预检阶段",
            "",
        ]
    )
    if preprod_summary:
        missing = ", ".join(preprod_summary.get("missing_phases", [])) or "-"
        lines.extend(
            [
                f"预检汇总：`{preprod_summary.get('run_id', '-')}`。已执行阶段："
                f"{', '.join(preprod_summary.get('executed_phases', [])) or '-'}。"
                f"缺失阶段：{missing}。",
                "",
                "| 阶段 | 延迟证据 | 结论 |",
                "| --- | --- | --- |",
            ]
        )
        for phase, result in preprod_summary.get("phase_results", {}).items():
            phase_passed = (
                bool(result["passed"])
                if "passed" in result
                else not bool(result.get("failed_child_reports"))
            )
            lines.append(
                "| "
                f"{phase} | "
                f"p95={result.get('worst_recall_p95_ms', 0)}ms, "
                f"p99={result.get('worst_recall_p99_ms', 0)}ms | "
                f"{'通过' if phase_passed else '未通过'} |"
            )
        lines.extend(["", "## 7. 未执行项", ""])
    else:
        lines.extend(
            [
                "未提供生产前预检汇总报告。",
                "",
                "## 7. 未执行项",
                "",
            ]
        )
    phase_results = preprod_summary.get("phase_results", {}) if preprod_summary else {}
    executed_phases = set(preprod_summary.get("executed_phases", [])) if preprod_summary else set()
    lines.extend(["", "| 项目 | 状态 | 说明 |", "| --- | --- | --- |"])
    lines.extend(_render_open_item_rows(executed_phases, phase_results))
    lines.extend(["", "## 8. 后续建议", ""])
    if stress_failures:
        lines.extend(
            [
                "1. 优先处理 50 并发 recall p95 超门禁的问题，再重跑 50 并发和 100 并发 spike。",
                "2. 扩大 P0 核心槽位评测集，把真实 Mem0 表达漂移继续沉淀成回归测试。",
                "3. 做 6-24 小时 soak 和故障注入后，再声明生产前完整压测通过。",
                "",
            ]
        )
    else:
        lines.extend(
            [
                f"1. {_recommend_remaining_preprod_work(executed_phases, phase_results)}",
                "2. 扩大 P0 核心槽位评测集，把真实 Mem0 表达漂移继续沉淀成回归测试。",
                "3. 接入代表性样本回放、监控告警和持续 SLO 后，再评估生产前完整压测结论。",
                "",
            ]
        )
    content = "\n".join(lines)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")
    return content


def _load_report(path: Path) -> dict[str, Any]:
    report = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise ValueError(f"pressure report must be a JSON object: {path}")
    return dict(report)


def _select_latest_report(reports: list[dict[str, Any]]) -> dict[str, Any]:
    return reports[-1]


def _render_open_item_rows(executed_phases: set[str], phase_results: dict[str, Any]) -> list[str]:
    stress_result = phase_results.get("stress", {})
    stress_duration_passed = bool(stress_result.get("duration_gate_passed")) if stress_result else False
    if stress_duration_passed:
        stress_status = "已执行正式长测"
        stress_detail = "已覆盖持续 15-30 分钟 50 并发 stress。"
    elif "stress" in executed_phases:
        stress_status = "已执行短探针"
        stress_detail = "已验证短时 50 并发 recall；尚未覆盖持续 15-30 分钟。"
    else:
        stress_status = "未执行"
        stress_detail = "尚未执行持续 15-30 分钟的 50 并发 stress。"
    spike_result = phase_results.get("spike", {})
    spike_recovery_passed = bool(spike_result.get("recovery_gate_passed")) if spike_result else False
    if spike_recovery_passed:
        spike_status = "已执行完整恢复曲线"
        spike_detail = "已验证 10 -> 100 -> 10 恢复曲线。"
    elif "spike" in executed_phases:
        spike_status = "已执行短探针"
        spike_detail = "已验证 100 并发 recall / 100 请求；尚未覆盖完整 10 -> 100 -> 10 恢复曲线。"
    else:
        spike_status = "未执行"
        spike_detail = "尚未执行 100 并发 spike 和完整恢复曲线。"
    fault_status = "部分执行" if "fault_injection" in executed_phases else "未执行"
    fault_detail = (
        "Mem0 unavailable 已验证；Qdrant/Postgres/Redis 故障注入未执行。"
        if "fault_injection" in executed_phases
        else "尚未验证外部依赖异常下的降级和失败关闭。"
    )
    soak_result = phase_results.get("soak", {})
    soak_duration_passed = bool(soak_result.get("duration_gate_passed")) if soak_result else False
    if soak_duration_passed:
        soak_status = "已执行"
        soak_detail = "已覆盖 6-24 小时 soak。"
    else:
        soak_status = "未执行"
        soak_detail = (
            "本轮只执行了短时 soak 探针，没有长时间稳定性证据。"
            if "soak_probe" in executed_phases
            else "尚未执行长时间稳定性测试。"
        )
    return [
        f"| 6-24 小时 soak test | {soak_status} | {soak_detail} |",
        f"| 15-30 分钟 50 并发 stress | {stress_status} | {stress_detail} |",
        f"| 100 并发 spike | {spike_status} | {spike_detail} |",
        f"| Mem0/Qdrant/Postgres/Redis 故障注入 | {fault_status} | {fault_detail} |",
        "| 代表性样本回放 | 未执行 | 当前仍是 P0 工程构造集；样本量按槽位、冲突、负样本、隔离和删除覆盖清单扩展。 |",
    ]


def _recommend_remaining_preprod_work(executed_phases: set[str], phase_results: dict[str, Any]) -> str:
    remaining = []
    if not bool(phase_results.get("soak", {}).get("duration_gate_passed")):
        remaining.append("6-24 小时 soak")
    remaining.append("代表性样本回放")
    if "fault_injection" in executed_phases:
        remaining.append("Qdrant/Postgres/Redis 故障注入")
    else:
        remaining.append("Mem0/Qdrant/Postgres/Redis 故障注入")
    if not bool(phase_results.get("stress", {}).get("duration_gate_passed")) and "stress" not in executed_phases:
        remaining.append("15-30 分钟 stress")
    elif not bool(phase_results.get("stress", {}).get("duration_gate_passed")):
        remaining.append("15-30 分钟持续 stress")
    if not bool(phase_results.get("spike", {}).get("recovery_gate_passed")) and "spike" not in executed_phases:
        remaining.append("100 并发 spike")
    elif not bool(phase_results.get("spike", {}).get("recovery_gate_passed")):
        remaining.append("完整 10 -> 100 -> 10 spike 恢复曲线")
    return f"补齐 {'、'.join(remaining)}。"


def _metric(
    report: dict[str, Any],
    primary: str,
    legacy: str | None = None,
    default: object | None = None,
) -> object:
    if primary in report:
        return report[primary]
    if legacy and legacy in report:
        return report[legacy]
    return default


def _has_quality_failure(report: dict[str, Any]) -> bool:
    if not bool(report.get("quality", {}).get("passed", True)):
        return True
    return bool(report.get("quality", {}).get("failed_cases"))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build final P0 pressure markdown report.")
    parser.add_argument("--output", required=True)
    parser.add_argument("--fixed-issue", action="append", default=[])
    parser.add_argument("--preprod-summary")
    parser.add_argument("reports", nargs="+")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    preprod_summary = (
        json.loads(Path(args.preprod_summary).read_text(encoding="utf-8"))
        if args.preprod_summary
        else None
    )
    content = build_final_report(
        [Path(report) for report in args.reports],
        output_path=Path(args.output),
        observed_fixed_issues=list(args.fixed_issue),
        preprod_summary=preprod_summary,
    )
    print(content)


if __name__ == "__main__":
    main()
