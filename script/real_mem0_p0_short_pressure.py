"""P0 short pressure suite for real Thinkback -> Mem0 -> Qdrant checks."""

from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from dotenv import load_dotenv

from infra.config import Settings
from memory.repositories import MemoryIndexEntry, SqlAlchemyMemoryRepository

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "script"

from .real_mem0_quality_regression import (
    EvaluationCase,
    LatencyMetrics,
    QualityScope,
    QueryResult,
    RequestMetrics,
    _active_memories,
    _append_round,
    _assert_active_memories_do_not_contain_forbidden,
    _assert_deleted_rebuild_does_not_inflate,
    _assert_nickname_converged,
    _assert_no_duplicate_backend_ids,
    _memory_snapshots,
    _post_json,
    _query_result,
    build_quality_report,
)


@dataclass(frozen=True)
class P0Scenario:
    index: int
    user_text: str
    metadata: dict[str, Any] | None = None


def build_pressure_suite_report(
    *,
    suite_id: str,
    started_at: str,
    ended_at: str,
    config: dict[str, Any],
    quality_report: dict[str, Any],
    concurrent_recall_report: dict[str, Any],
    append_probe: dict[str, Any],
    post_delete_report: dict[str, Any],
    limitations: list[str],
) -> dict[str, Any]:
    sections = {
        "quality": quality_report,
        "concurrent_recall": concurrent_recall_report,
        "append_probe": append_probe,
        "post_delete": post_delete_report,
    }
    latency_gate_failures = _latency_gate_failures(
        sections,
        recall_p95_gate_ms=int(config.get("recall_p95_gate_ms", 1500)),
        recall_p99_gate_ms=int(config.get("recall_p99_gate_ms", 3000)),
    )
    failed_sections = [
        section_name for section_name, section in sections.items() if not bool(section.get("passed"))
    ]
    failed_sections.extend(
        section
        for section in sorted({failure["section"] for failure in latency_gate_failures})
        if section not in failed_sections
    )
    return {
        "suite_id": suite_id,
        "started_at": started_at,
        "ended_at": ended_at,
        "config": config,
        "passed": not failed_sections,
        "failed_sections": failed_sections,
        "latency_gate_failures": latency_gate_failures,
        "summary": {
            "case_pass_rate": quality_report.get("case_pass_rate", 0.0),
            "recall_at_10": quality_report.get("recall_at_10", 0.0),
            "precision_at_10": quality_report.get("precision_at_10", 0.0),
            "conflict_pollution_rate": quality_report.get("conflict_pollution_rate", 0.0),
            "false_positive_rate": quality_report.get("false_positive_rate", 0.0),
            "duplicate_active_rate": quality_report.get("duplicate_active_rate", 0.0),
        },
        "quality": quality_report,
        "concurrent_recall": concurrent_recall_report,
        "append_probe": append_probe,
        "post_delete": post_delete_report,
        "limitations": limitations,
    }


def _latency_gate_failures(
    sections: dict[str, dict[str, Any]],
    *,
    recall_p95_gate_ms: int,
    recall_p99_gate_ms: int,
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for section_name in ("quality", "concurrent_recall", "post_delete"):
        recall_latency = sections[section_name].get("latency_ms", {}).get("recall", {})
        p95 = int(recall_latency.get("p95", 0))
        p99 = int(recall_latency.get("p99", 0))
        if p95 > recall_p95_gate_ms:
            failures.append(
                {
                    "section": section_name,
                    "metric": "recall.p95",
                    "actual": p95,
                    "expected": f"<= {recall_p95_gate_ms}",
                }
            )
        if p99 > recall_p99_gate_ms:
            failures.append(
                {
                    "section": section_name,
                    "metric": "recall.p99",
                    "actual": p99,
                    "expected": f"<= {recall_p99_gate_ms}",
                }
            )
    return failures


def render_pressure_suite_markdown(report: dict[str, Any]) -> str:
    conclusion = "通过" if report.get("passed") else "未通过"
    lines = [
        "# Thinkback P0 压测报告",
        "",
        "## 1. 结论",
        "",
        "| 项目 | 结果 |",
        "| --- | --- |",
        f"| 整体结论 | {conclusion} |",
        f"| suite_id | `{report['suite_id']}` |",
        f"| started_at | `{report['started_at']}` |",
        f"| ended_at | `{report['ended_at']}` |",
        f"| failed_sections | `{', '.join(report['failed_sections']) or '-'}` |",
        "",
        "## 2. 核心门禁",
        "",
        "| 指标 | 结果 |",
        "| --- | ---: |",
    ]
    for metric, value in report["summary"].items():
        lines.append(f"| `{metric}` | {value} |")
    lines.extend(["", "### 延迟门禁失败", ""])
    if report.get("latency_gate_failures"):
        for failure in report["latency_gate_failures"]:
            lines.append(
                f"- {failure['section']} `{failure['metric']}` = {failure['actual']}，"
                f"要求 {failure['expected']}。"
            )
    else:
        lines.append("- 无。")
    lines.extend(
        [
            "",
            "## 3. 分段结果",
            "",
            "| 分段 | 是否通过 | 失败用例 | 失败指标 |",
            "| --- | --- | --- | --- |",
        ]
    )
    for section_name in ("quality", "concurrent_recall", "append_probe", "post_delete"):
        section = report[section_name]
        passed = "通过" if section.get("passed") else "未通过"
        failed_cases = ", ".join(str(item) for item in section.get("failed_cases", [])) or "-"
        failed_metrics = json.dumps(section.get("failed_metrics", []), ensure_ascii=False)
        lines.append(f"| {section_name} | {passed} | {failed_cases} | `{failed_metrics}` |")
    lines.extend(
        [
            "",
            "## 4. 延迟",
            "",
            "| 分段 | append p95 | recall p95 | delete p95 | rebuild p95 |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for section_name in ("quality", "concurrent_recall", "post_delete"):
        latency = report[section_name].get("latency_ms", {})
        lines.append(
            "| "
            f"{section_name} | "
            f"{latency.get('append', {}).get('p95', 0)} | "
            f"{latency.get('recall', {}).get('p95', 0)} | "
            f"{latency.get('delete', {}).get('p95', 0)} | "
            f"{latency.get('rebuild', {}).get('p95', 0)} |"
        )
    append_latency = report["append_probe"].get("latency_ms", {})
    lines.append(
        "| append_probe | "
        f"{append_latency.get('append', {}).get('p95', 0)} | 0 | 0 | 0 |"
    )
    lines.extend(["", "## 5. 限制与未执行长测", ""])
    for limitation in report.get("limitations", []):
        lines.append(f"- {limitation}")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    args = _parse_args()
    load_dotenv()
    settings = Settings()
    if not settings.openai_api_key or not settings.qdrant_url:
        raise RuntimeError("OPENAI_API_KEY and QDRANT_URL are required")

    started_at = datetime.now(UTC).isoformat()
    suite_id = f"p0-short-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}"
    base_url = os.environ.get("THINKBACK_API_URL", "http://127.0.0.1:18082")
    repository = SqlAlchemyMemoryRepository()
    scope = _new_suite_scope(suite_id)
    request_metrics = RequestMetrics()
    latency_metrics = LatencyMetrics()

    print(f"p0 pressure suite: suite_id={suite_id}")
    other_character_scope = scope.for_character(f"{suite_id}-other-character", f"{suite_id}-other-session")
    _seed_p0_scope(
        base_url,
        scope,
        other_character_scope=other_character_scope,
        request_metrics=request_metrics,
        latency_metrics=latency_metrics,
    )
    active_after_append = _active_memories(repository, scope)
    _assert_no_duplicate_backend_ids(active_after_append)
    _assert_nickname_converged(repository, scope)
    _assert_active_memories_do_not_contain_forbidden(repository, scope, _forbidden_active_terms())

    cases = _p0_cases()
    quality_results = [
        _query_result(base_url, scope, case, request_metrics=request_metrics, latency_metrics=latency_metrics)
        for case in cases
    ]
    quality_report = build_quality_report(
        cases,
        quality_results,
        active_memory_count=len(active_after_append),
        active_after_rebuild=len(active_after_append),
        request_metrics=request_metrics,
        latency_metrics=latency_metrics,
        active_memory_snapshots=_memory_snapshots(active_after_append),
    )

    concurrent_recall_report = _run_concurrent_recall_probe(
        base_url=base_url,
        scope=scope,
        cases=cases,
        active_memory_count=len(active_after_append),
        active_memory_snapshots=_memory_snapshots(active_after_append),
        concurrency=args.recall_concurrency,
        request_count=args.recall_requests,
    )
    append_probe = _run_append_probe(
        base_url=base_url,
        suite_id=suite_id,
        concurrency=args.append_concurrency,
    )
    post_delete_report = _run_delete_rebuild_probe(
        base_url=base_url,
        scope=scope,
        repository=repository,
        request_metrics=request_metrics,
        latency_metrics=latency_metrics,
        active_after_append=active_after_append,
    )

    ended_at = datetime.now(UTC).isoformat()
    report = build_pressure_suite_report(
        suite_id=suite_id,
        started_at=started_at,
        ended_at=ended_at,
        config={
            "base_url": base_url,
            "recall_concurrency": args.recall_concurrency,
            "recall_requests": args.recall_requests,
            "append_concurrency": args.append_concurrency,
            "recall_p95_gate_ms": args.recall_p95_gate_ms,
            "recall_p99_gate_ms": args.recall_p99_gate_ms,
        },
        quality_report=quality_report,
        concurrent_recall_report=concurrent_recall_report,
        append_probe=append_probe,
        post_delete_report=post_delete_report,
        limitations=_limitations(),
    )
    _write_reports(args.report_dir, suite_id, report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    if not report["passed"]:
        raise RuntimeError(f"P0 short pressure suite failed: {report['failed_sections']}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a real P0 short pressure suite.")
    parser.add_argument("--recall-concurrency", type=int, default=10)
    parser.add_argument("--recall-requests", type=int, default=20)
    parser.add_argument("--append-concurrency", type=int, default=3)
    parser.add_argument("--recall-p95-gate-ms", type=int, default=1500)
    parser.add_argument("--recall-p99-gate-ms", type=int, default=3000)
    parser.add_argument("--report-dir", default="docs/reports")
    return parser.parse_args()


def _new_suite_scope(suite_id: str) -> QualityScope:
    return QualityScope(
        run_id=suite_id,
        user_id=f"{suite_id}-user",
        character_id=f"{suite_id}-character",
        session_id=f"{suite_id}-session",
    )


def _seed_p0_scope(
    base_url: str,
    scope: QualityScope,
    *,
    other_character_scope: QualityScope,
    request_metrics: RequestMetrics,
    latency_metrics: LatencyMetrics,
) -> None:
    for scenario in _p0_scenarios():
        _append_round(
            base_url,
            scope,
            scenario.index,
            scenario.user_text,
            metadata=scenario.metadata,
            request_metrics=request_metrics,
            latency_metrics=latency_metrics,
        )
    _append_round(
        base_url,
        other_character_scope,
        1,
        "我只告诉这个角色：我的猫叫泡芙。",
        request_metrics=request_metrics,
        latency_metrics=latency_metrics,
    )


def _p0_scenarios() -> list[P0Scenario]:
    return [
        P0Scenario(1, "请记住，我喜欢别人叫我阿鹏。"),
        P0Scenario(2, "纠正一下：以后不要叫我阿鹏，请叫我小鹏。"),
        P0Scenario(3, "我养了一只猫，名字叫团子。"),
        P0Scenario(4, "我喜欢晚上复盘工作压力，但不喜欢被说教。"),
        P0Scenario(5, "如果我焦虑，请先帮我拆解问题，再给建议。"),
        P0Scenario(6, "更正一下，我的猫不叫团子，叫麻薯。"),
        P0Scenario(7, "我之前住在杭州，现在已经搬到上海。"),
        P0Scenario(8, "工作状态更新：我已经接受 Moonshot 的 offer，不再评估其它机会。"),
        P0Scenario(9, "沟通偏好也更新：我现在更想要简洁直接的建议，不需要先安慰。"),
        P0Scenario(10, "生日纠正一下，不是5月20日，是6月1日。"),
        P0Scenario(11, "饮品偏好改成茶，不再喝咖啡。"),
        P0Scenario(12, "我现在可以接受温和的提醒睡觉。"),
        P0Scenario(13, "我养了一只狗，名字叫豆包。"),
        P0Scenario(14, "食物偏好改成寿司，不再吃汉堡。"),
        P0Scenario(
            15,
            "剧情设定里，我养了一只猫叫露露。",
            {
                "context_type": "roleplay",
                "roleplay_mode": "on",
                "fact_subject": "story_world",
                "backend_categories": ["story_world"],
            },
        ),
    ]


def _p0_cases() -> list[EvaluationCase]:
    return [
        EvaluationCase("nickname-current", "slot_conflict", "现在应该怎么称呼用户？", ["小鹏"], ["阿鹏"], intent="preference"),
        EvaluationCase("cat-current", "slot_conflict", "用户的猫叫什么？", ["麻薯"], ["团子", "露露", "泡芙"]),
        EvaluationCase("location-current", "slot_conflict", "用户现在住在哪？", [["上海", "Shanghai"]], ["杭州", "Hangzhou"]),
        EvaluationCase("work-status-current", "slot_conflict", "用户现在的工作状态是什么？", ["Moonshot"], ["评估其它机会", "evaluating"]),
        EvaluationCase("communication-current", "slot_conflict", "用户希望你怎么给建议？", [["简洁直接", "concise", "direct"]], ["先安慰", "reassurance"], intent="preference"),
        EvaluationCase("birthday-current", "slot_conflict", "用户生日是哪天？", [["6月1日", "June 1"]], ["5月20日", "May 20"]),
        EvaluationCase("drink-current", "slot_conflict", "用户现在喜欢喝什么？", [["茶", "tea"]], ["咖啡", "coffee"], intent="preference"),
        EvaluationCase("food-current", "slot_conflict", "用户现在喜欢吃什么？", [["寿司", "sushi"]], ["汉堡", "hamburger", "burger"], intent="preference"),
        EvaluationCase("dog-current", "slot_conflict", "用户的狗叫什么？", ["豆包"], ["麻薯", "团子", "露露", "泡芙"]),
        EvaluationCase("sleep-reminder-current", "slot_conflict", "用户现在还讨厌被提醒睡觉吗？", [["温和", "gentle", "接受", "okay"]], ["讨厌", "dislikes"], intent="preference"),
        EvaluationCase("negative-bird", "negative_control", "用户的鸟叫什么？", [], ["麻薯", "团子", "豆包", "猫", "狗", "cat", "dog"]),
        EvaluationCase("negative-roleplay-cat", "isolation", "用户现实里的猫叫什么？", ["麻薯"], ["露露"]),
        EvaluationCase("roleplay-cat", "isolation", "剧情设定里的猫叫什么？", ["露露"], ["麻薯", "团子"]),
        EvaluationCase("negative-cross-character-cat", "isolation", "用户的猫叫什么？", ["麻薯"], ["泡芙"]),
    ]


def _run_concurrent_recall_probe(
    *,
    base_url: str,
    scope: QualityScope,
    cases: list[EvaluationCase],
    active_memory_count: int,
    active_memory_snapshots: list[Any],
    concurrency: int,
    request_count: int,
) -> dict[str, Any]:
    request_metrics = RequestMetrics()
    latency_metrics = LatencyMetrics()
    selected_cases = [cases[index % len(cases)] for index in range(request_count)]
    results: list[QueryResult] = []
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [
            executor.submit(
                _query_result,
                base_url,
                scope,
                case,
                request_metrics=request_metrics,
                latency_metrics=latency_metrics,
            )
            for case in selected_cases
        ]
        for future in as_completed(futures):
            results.append(future.result())
    report = build_quality_report(
        selected_cases,
        results,
        active_memory_count=active_memory_count,
        active_after_rebuild=active_memory_count,
        request_metrics=request_metrics,
        latency_metrics=latency_metrics,
        active_memory_snapshots=active_memory_snapshots,
    )
    return dict(report)


def _run_append_probe(*, base_url: str, suite_id: str, concurrency: int) -> dict[str, Any]:
    request_metrics = RequestMetrics()
    latency_metrics = LatencyMetrics()
    probe_scopes = [
        QualityScope(
            run_id=f"{suite_id}-append-probe-{index}",
            user_id=f"{suite_id}-append-probe-user-{index}",
            character_id=f"{suite_id}-append-probe-character-{index}",
            session_id=f"{suite_id}-append-probe-session-{index}",
        )
        for index in range(1, concurrency + 1)
    ]
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [
            executor.submit(
                _append_round,
                base_url,
                scope,
                1,
                f"并发探针 {index}：请记住我喜欢喝茶。",
                request_metrics=request_metrics,
                latency_metrics=latency_metrics,
            )
            for index, scope in enumerate(probe_scopes, start=1)
        ]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:  # noqa: BLE001
                failures.append(str(exc))
    return {
        "passed": not failures and request_metrics.non_transient_failure_count == 0,
        "success_count": len(probe_scopes) - len(failures),
        "failure_count": len(failures),
        "failures": failures,
        "request_metrics": request_metrics.as_dict(),
        "latency_ms": latency_metrics.as_dict(),
        "failed_cases": failures,
        "failed_metrics": [] if not failures else [{"metric": "append_probe_failures", "actual": len(failures), "expected": "== 0"}],
    }


def _run_delete_rebuild_probe(
    *,
    base_url: str,
    scope: QualityScope,
    repository: SqlAlchemyMemoryRepository,
    request_metrics: RequestMetrics,
    latency_metrics: LatencyMetrics,
    active_after_append: list[MemoryIndexEntry],
) -> dict[str, Any]:
    delete_target = next(
        memory
        for memory in active_after_append
        if ("麻薯" in memory.memory_text or "Mashu" in memory.memory_text)
        and memory.context_type == "real_user"
        and memory.fact_subject == "user"
    )
    delete_response = _post_json(
        base_url,
        "/memory/delete",
        {
            "request_id": scope.request_id("delete-cat"),
            "user_id": scope.user_id,
            "character_id": scope.character_id,
            "scope": "memory",
            "operation_id": scope.operation_id("delete-cat"),
            "memory_id": delete_target.memory_id,
        },
        request_metrics=request_metrics,
        latency_metrics=latency_metrics,
        operation="delete",
    )
    if delete_response.get("affected_memories") != 1:
        raise RuntimeError(f"delete did not affect one memory: {delete_response}")
    _post_json(
        base_url,
        "/memory/rebuild",
        {
            "request_id": scope.request_id("rebuild"),
            "user_id": scope.user_id,
            "character_id": scope.character_id,
            "operation_id": scope.operation_id("rebuild"),
        },
        request_metrics=request_metrics,
        latency_metrics=latency_metrics,
        operation="rebuild",
    )
    after_rebuild = _active_memories(repository, scope)
    _assert_deleted_rebuild_does_not_inflate(len(active_after_append), after_rebuild)
    _assert_nickname_converged(repository, scope)
    _assert_active_memories_do_not_contain_forbidden(repository, scope, _forbidden_active_terms())
    post_delete_cases = [
        EvaluationCase("deleted-cat-not-recalled", "delete_rebuild", "用户的猫叫什么？", [], ["麻薯", "团子", "露露", "泡芙", "猫", "cat"]),
        EvaluationCase("nickname-after-rebuild", "delete_rebuild", "现在应该怎么称呼用户？", ["小鹏"], ["阿鹏"], intent="preference"),
    ]
    post_delete_results = [
        _query_result(base_url, scope, case, request_metrics=request_metrics, latency_metrics=latency_metrics)
        for case in post_delete_cases
    ]
    deleted_residue = [
        result.case_name
        for result in post_delete_results
        if any(term in result.all_recalled_text for term in ("麻薯", "团子", "露露", "泡芙"))
    ]
    if deleted_residue:
        raise RuntimeError(f"deleted cat residue found in recall layers: {deleted_residue}")
    return dict(build_quality_report(
        post_delete_cases,
        post_delete_results,
        active_memory_count=len(active_after_append),
        active_after_rebuild=len(after_rebuild),
        request_metrics=request_metrics,
        latency_metrics=latency_metrics,
        active_memory_snapshots=_memory_snapshots(after_rebuild),
    ))


def _forbidden_active_terms() -> list[str]:
    return [
        "阿鹏",
        "团子",
        "Hangzhou",
        "杭州",
        "evaluating",
        "评估其它机会",
        "reassurance",
        "5月20日",
        "咖啡",
        "coffee",
        "汉堡",
        "hamburger",
    ]


def _limitations() -> list[str]:
    return [
        "本短压测不替代 6-24 小时 soak test。",
        "本短压测不替代 50/100 并发容量压测。",
        "本短压测不执行 Mem0、Qdrant、Postgres 故障注入。",
        "本短压测使用工程构造样本，不代表线上全量用户分布。",
    ]


def _write_reports(report_dir: str, suite_id: str, report: dict[str, Any]) -> None:
    output_dir = Path(report_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{suite_id}.json"
    md_path = output_dir / f"{suite_id}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    md_path.write_text(render_pressure_suite_markdown(report), encoding="utf-8")
    print(f"pressure report json: {json_path}")
    print(f"pressure report markdown: {md_path}")


if __name__ == "__main__":
    main()
