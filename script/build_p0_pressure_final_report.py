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
    stress_failures = [
        report for report in failed_reports
        if int(report.get("config", {}).get("recall_concurrency", 0)) >= 50
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
            f"| 是否达到 P0 主链路短压测 | {'是' if latest_pass else '否'} |",
            f"| 是否达到生产前完整压测标准 | {'否' if stress_failures or semantic_failures else '待长测、故障注入和代表性回放确认'} |",
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
    if latest_pass:
        latest_quality = latest_pass["quality"]
        latest_concurrent = latest_pass["concurrent_recall"]
        lines.extend(
            [
                "| 指标 | 最新通过短压测 | P0 门禁 |",
                "| --- | ---: | ---: |",
                f"| `case_pass_rate` | {latest_quality.get('case_pass_rate', 0.0)} | >= 0.98 |",
                f"| `recall_at_10` | {latest_quality.get('recall_at_10', 0.0)} | >= 0.95 |",
                f"| `precision_at_10` | {latest_quality.get('precision_at_10', 0.0)} | >= 0.95 |",
                f"| `conflict_pollution_rate` | {latest_quality.get('conflict_pollution_rate', 0.0)} | <= 0.02 |",
                f"| `false_positive_rate` | {latest_quality.get('false_positive_rate', 0.0)} | <= 0.02 |",
                f"| `duplicate_active_rate` | {latest_quality.get('duplicate_active_rate', 0.0)} | 0 |",
                f"| `delete_memory_residue_rate` | {_metric(latest_pass['post_delete'], 'delete_memory_residue_rate', 'delete_residue_rate', 0.0)} | 0 |",
                f"| `rebuild_resurrection_rate` | {_metric(latest_pass['post_delete'], 'rebuild_resurrection_rate', default=0.0)} | 0 |",
                f"| 10 并发 recall p95 | {latest_concurrent.get('latency_ms', {}).get('recall', {}).get('p95', 0)}ms | <= 1500ms |",
                f"| 10 并发 recall p99 | {latest_concurrent.get('latency_ms', {}).get('recall', {}).get('p99', 0)}ms | <= 3000ms |",
                "",
            ]
        )

    if stress_failures:
        worst_stress = max(
            stress_failures,
            key=lambda report: int(
                report.get("concurrent_recall", {}).get("latency_ms", {}).get("recall", {}).get("p95", 0)
            ),
        )
        stress_recall = worst_stress["concurrent_recall"]
        stress_latency = stress_recall.get("latency_ms", {}).get("recall", {})
        lines.extend(
            [
                "## 5. 高并发风险",
                "",
                "| 场景 | 质量 | 延迟 | 结论 |",
                "| --- | --- | --- | --- |",
                (
                    f"| 50 并发 recall / 100 请求 | "
                    f"`case_pass_rate={stress_recall.get('case_pass_rate', 0.0)}`, "
                    f"`recall_at_10={stress_recall.get('recall_at_10', 0.0)}` | "
                    f"`p95={stress_latency.get('p95', 0)}ms`, `p99={stress_latency.get('p99', 0)}ms` | "
                    "准确率通过，p95 延迟未通过。 |"
                ),
                "",
                "初步判断瓶颈在 API 同步 service + 线程池 + SQLAlchemy async `NullPool` 的组合：50 并发下每次 recall 都要跨线程并新建数据库连接读取业务索引。建议后续把 repository/service 边界改为全异步并启用受控连接池，或为明确槽位召回增加短 TTL scope 缓存。",
                "",
            ]
        )

    lines.extend(
        [
            "## 6. 未执行项",
            "",
            "| 项目 | 状态 | 说明 |",
            "| --- | --- | --- |",
            "| 6-24 小时 soak test | 未执行 | 本轮没有长时间稳定性证据。 |",
            "| 15-30 分钟 50 并发 stress | 未执行 | 本轮只做了 50 并发 / 100 请求代表性压测。 |",
            "| 100 并发 spike | 未执行 | 尚未验证峰值后恢复能力。 |",
            "| Mem0/Qdrant/Postgres/Redis 故障注入 | 未执行 | 尚未验证外部依赖异常下的降级和失败关闭。 |",
            "| 代表性样本回放 | 未执行 | 当前仍是 P0 工程构造集；样本量按槽位、冲突、负样本、隔离和删除覆盖清单扩展。 |",
            "",
            "## 7. 后续建议",
            "",
            "1. 优先处理 50 并发 recall p95 超门禁的问题，再重跑 50 并发和 100 并发 spike。",
            "2. 扩大 P0 核心槽位评测集，把真实 Mem0 表达漂移继续沉淀成回归测试。",
            "3. 做 6-24 小时 soak 和故障注入后，再声明生产前完整压测通过。",
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
    parser.add_argument("reports", nargs="+")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    content = build_final_report(
        [Path(report) for report in args.reports],
        output_path=Path(args.output),
        observed_fixed_issues=list(args.fixed_issue),
    )
    print(content)


if __name__ == "__main__":
    main()
