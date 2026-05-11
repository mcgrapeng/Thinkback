import json
import subprocess
from io import BytesIO
from urllib.error import HTTPError, URLError

from script import real_mem0_quality_regression
from script.build_p0_pressure_final_report import build_final_report
from script.real_mem0_p0_fault_injection import (
    _start_api_process,
    _wait_for_readiness,
    build_fault_injection_report,
    build_library_fault_scenario_specs,
    evaluate_readiness_fault_scenario,
    render_fault_injection_markdown,
)
from script.real_mem0_p0_preprod_pressure import (
    _DURATION_PHASE_TARGET_SECONDS,
    _failed_duration_child_report,
    _phase_command,
    _render_cli_summary,
    _run_duration_child_command,
    _soak_iteration_interval,
    _spike_phase_commands,
    build_duration_phase_report,
    build_preprod_report,
    build_preprod_report_from_phase_reports,
    build_spike_phase_report,
    render_preprod_report_markdown,
)
from script.real_mem0_p0_representative_replay import (
    build_representative_replay_report,
    render_representative_replay_markdown,
)
from script.real_mem0_p0_short_pressure import (
    build_pressure_suite_report,
    render_pressure_suite_markdown,
)
from script.real_mem0_quality_regression import (
    EvaluationCase,
    LatencyMetrics,
    MemorySnapshot,
    QualityScope,
    QueryResult,
    RequestMetrics,
    _is_transient_http_failure,
    _is_transient_network_failure,
    _post_json,
    build_post_delete_quality_cases,
    build_production_quality_cases,
    build_quality_report,
)

QUALITY_REPORT_PREFIX = "AI虚拟社交记忆服务首版主链路质量评测报告"


def test_quality_report_counts_recall_precision_false_positive_and_conflicts() -> None:
    cases = [
        EvaluationCase(
            name="nickname",
            category="slot_conflict",
            query="现在应该怎么称呼用户？",
            expected_terms=["小鹏"],
            forbidden_terms=["阿鹏"],
        ),
        EvaluationCase(
            name="negative-dog",
            category="negative_control",
            query="用户的狗叫什么？",
            expected_terms=[],
            forbidden_terms=["团子", "猫", "cat"],
        ),
        EvaluationCase(
            name="location",
            category="slot_conflict",
            query="用户现在住在哪？",
            expected_terms=["上海"],
            forbidden_terms=["杭州"],
        ),
    ]
    results = [
        QueryResult(case_name="nickname", recalled_text="User prefers to be called 小鹏"),
        QueryResult(case_name="negative-dog", recalled_text=""),
        QueryResult(case_name="location", recalled_text="User lives in 上海\nUser previously lived in 杭州"),
    ]

    report = build_quality_report(cases, results, active_memory_count=8, active_after_rebuild=8)

    assert report["case_count"] == 3
    assert report["case_pass_rate"] == 2 / 3
    assert report["recall_at_10"] == 2 / 3
    assert report["precision_at_10"] == 2 / 3
    assert report["false_positive_rate"] == 0.0
    assert report["conflict_pollution_rate"] == 1 / 3
    assert report["passed"] is False
    assert report["failed_cases"] == ["location"]


def test_negative_quality_case_allows_unrelated_clean_l3_items() -> None:
    cases = [
        EvaluationCase(
            name="negative-phone",
            category="negative_control",
            query="用户现在用什么手机？",
            expected_terms=[],
            forbidden_terms=["iPhone", "Android", "手机", "phone"],
        ),
    ]
    results = [
        QueryResult(
            case_name="negative-phone",
            recalled_text="User communication preference: 简洁直接的建议",
            l3_count=1,
            l3_contents=["User communication preference: 简洁直接的建议"],
        ),
    ]

    report = build_quality_report(cases, results, active_memory_count=1, active_after_rebuild=1)

    assert report["case_pass_rate"] == 1.0
    assert report["false_positive_rate"] == 0.0
    assert report["failed_cases"] == []


def test_quality_report_includes_item_precision_top1_and_mrr() -> None:
    cases = [
        EvaluationCase(
            name="nickname",
            category="slot_conflict",
            query="怎么称呼用户？",
            expected_terms=["小鹏"],
            forbidden_terms=["阿鹏"],
        ),
        EvaluationCase(
            name="location",
            category="slot_conflict",
            query="用户住哪？",
            expected_terms=["上海"],
            forbidden_terms=["杭州"],
        ),
    ]
    results = [
        QueryResult(
            case_name="nickname",
            recalled_text="User prefers to be called 小鹏\nUser lives in 上海",
            l3_count=2,
            l3_contents=["User prefers to be called 小鹏", "User lives in 上海"],
        ),
        QueryResult(
            case_name="location",
            recalled_text="User likes tea\nUser lives in 上海",
            l3_count=2,
            l3_contents=["User likes tea", "User lives in 上海"],
        ),
    ]

    report = build_quality_report(cases, results, active_memory_count=4, active_after_rebuild=4)

    assert report["item_precision_at_10"] == 0.5
    assert report["top1_hit_rate"] == 0.5
    assert report["mrr"] == 0.75
    assert report["irrelevant_l3_per_query"] == 1.0


def test_quality_report_includes_case_details_and_category_metrics() -> None:
    cases = [
        EvaluationCase(
            name="birthday-current",
            category="slot_conflict",
            query="用户生日是哪天？",
            expected_terms=["June 1"],
            forbidden_terms=["May 20"],
        ),
        EvaluationCase(
            name="negative-dog",
            category="negative_control",
            query="用户的狗叫什么？",
            expected_terms=[],
            forbidden_terms=["猫", "cat"],
        ),
    ]
    results = [
        QueryResult(
            case_name="birthday-current",
            recalled_text="User birthday: June 1",
            l3_count=1,
        ),
        QueryResult(
            case_name="negative-dog",
            recalled_text="User has a cat named 麻薯",
            l3_count=1,
        ),
    ]

    report = build_quality_report(cases, results, active_memory_count=9, active_after_rebuild=9)

    assert report["case_results"] == [
        {
            "name": "birthday-current",
            "category": "slot_conflict",
            "expected_hit": True,
            "forbidden_hit": False,
            "negative": False,
            "failure_reason": None,
            "l3_count": 1,
            "status": "passed",
        },
        {
            "name": "negative-dog",
            "category": "negative_control",
            "expected_hit": False,
            "forbidden_hit": True,
            "negative": True,
            "failure_reason": "false_positive",
            "l3_count": 1,
            "status": "failed",
        },
    ]
    assert report["category_metrics"] == {
        "negative_control": {"case_count": 1, "passed_count": 0, "pass_rate": 0.0},
        "slot_conflict": {"case_count": 1, "passed_count": 1, "pass_rate": 1.0},
    }


def test_quality_scope_round_ids_are_unique_across_characters() -> None:
    scope = QualityScope(
        run_id="quality-run",
        user_id="user-1",
        character_id="character-a",
        session_id="session-a",
    )
    other_scope = scope.for_character("character-b", "session-b")

    assert scope.user_id == other_scope.user_id
    assert scope.character_id != other_scope.character_id
    assert scope.round_id(1) != other_scope.round_id(1)


def test_quality_scope_round_ids_are_unique_across_users() -> None:
    scope = QualityScope(
        run_id="quality-run",
        user_id="user-1",
        character_id="character-a",
        session_id="session-a",
    )
    other_scope = scope.for_user("user-2", "session-b")

    assert scope.user_id != other_scope.user_id
    assert scope.character_id == other_scope.character_id
    assert scope.round_id(1) != other_scope.round_id(1)


def test_quality_scope_identifiers_fit_memory_task_database_columns() -> None:
    run_id = "real-quality-20260510T152500-556362f2"
    scope = QualityScope(
        run_id=run_id,
        user_id=f"{run_id}-user",
        character_id=f"{run_id}-lifecycle-character",
        session_id=f"{run_id}-lifecycle-session-main",
    )

    # 中文注释：真实库 tb_memory_task 的这些字段是 varchar(128)，评测 ID 不能只追求可读而超长。
    operation_id = scope.operation_id("rebuild-after-memory-session-delete")

    assert len(scope.request_id("rebuild-after-memory-session-delete")) <= 128
    assert len(operation_id) <= 113
    assert len(f"memory-rebuild:{operation_id}") <= 128
    assert len(f"memory-delete:{operation_id}") <= 128
    assert len(scope.round_id(99)) <= 113
    assert len(f"memory-extract:{scope.round_id(99)}") <= 128
    assert operation_id.endswith("rebuild-after-memory-session-delete-op")


def test_quality_report_includes_request_metrics() -> None:
    metrics = RequestMetrics(request_count=4, retry_count=2, transient_failure_count=2)

    report = build_quality_report([], [], active_memory_count=0, active_after_rebuild=0, request_metrics=metrics)

    assert report["request_metrics"] == {
        "request_count": 4,
        "retry_count": 2,
        "transient_failure_count": 2,
        "non_transient_failure_count": 0,
    }


def test_quality_report_includes_latency_metrics() -> None:
    latency = LatencyMetrics()
    latency.record("append", 0.010)
    latency.record("append", 0.020)
    latency.record("append", 0.030)
    latency.record("recall", 0.100)

    report = build_quality_report([], [], active_memory_count=0, active_after_rebuild=0, latency_metrics=latency)

    assert report["latency_ms"]["append"] == {"p50": 20, "p95": 30, "p99": 30}
    assert report["latency_ms"]["recall"] == {"p50": 100, "p95": 100, "p99": 100}
    assert report["latency_ms"]["delete"] == {"p50": 0, "p95": 0, "p99": 0}


def test_quality_report_includes_duplicate_active_rate() -> None:
    active_memories = [
        MemorySnapshot(memory_text="User has a cat named 麻薯", memory_status="active"),
        MemorySnapshot(memory_text="User has a cat named 露露", memory_status="active"),
        MemorySnapshot(memory_text="User has a dog named 豆包", memory_status="active"),
    ]

    report = build_quality_report(
        [],
        [],
        active_memory_count=3,
        active_after_rebuild=3,
        active_memory_snapshots=active_memories,
    )

    assert report["duplicate_active_rate"] == 1 / 3


def test_quality_report_exposes_quality_stable_metric_fields() -> None:
    report = build_quality_report([], [], active_memory_count=0, active_after_rebuild=0)

    assert report["delete_memory_residue_rate"] is None
    assert report["delete_session_residue_rate"] is None
    assert report["delete_all_residue_rate"] is None
    assert report["rebuild_resurrection_rate"] is None
    assert report["cross_user_leak_rate"] is None
    assert report["cross_character_leak_rate"] is None
    assert report["roleplay_real_mix_rate"] is None
    assert report["dirty_summary_recall_rate"] is None
    assert report["source_ref_loss_rate"] is None
    assert report["critical_slot_pass_rate"] is None
    assert report["known_drift_regression_pass_rate"] is None
    assert report["http_5xx_rate"] is None
    assert report["timeout_rate"] is None
    assert report["idempotency_failure_rate"] is None
    assert report["transient_retry_rate"] == 0.0
    assert report["metric_automation_status"]["recall_at_10"] == "automatic"
    assert report["metric_automation_status"]["irrelevant_l3_per_query"] == "automatic"
    assert report["metric_automation_status"]["delete_memory_residue_rate"] == "case_gate"
    assert report["metric_automation_status"]["delete_session_residue_rate"] == "case_gate"
    assert report["metric_automation_status"]["delete_all_residue_rate"] == "case_gate"
    assert report["metric_automation_status"]["cross_user_leak_rate"] == "case_gate"
    assert report["metric_automation_status"]["known_drift_regression_pass_rate"] == "automatic"


def test_production_quality_dataset_has_broader_case_coverage() -> None:
    cases = build_production_quality_cases()
    names = {case.name for case in cases}
    category_counts: dict[str, int] = {}
    tagged_counts: dict[str, int] = {}
    for case in cases:
        category_counts[case.category] = category_counts.get(case.category, 0) + 1
        for tag in case.metric_tags:
            tagged_counts[tag] = tagged_counts.get(tag, 0) + 1

    assert len(cases) >= 30
    assert category_counts["slot_conflict"] >= 12
    assert category_counts["negative_control"] >= 6
    assert category_counts["isolation"] >= 6
    assert category_counts["expression_drift"] >= 6
    assert tagged_counts["known_drift_regression_pass_rate"] >= 6
    assert tagged_counts["cross_user_leak_rate"] >= 2
    assert tagged_counts["cross_character_leak_rate"] >= 2
    assert tagged_counts["roleplay_real_mix_rate"] >= 2
    assert "negative-never-shared-phone" in names
    assert "cat-current-english-paraphrase" in names


def test_post_delete_quality_dataset_covers_memory_session_all_and_rebuild() -> None:
    cases = build_post_delete_quality_cases()
    names = {case.name for case in cases}
    tagged_counts: dict[str, int] = {}
    for case in cases:
        for tag in case.metric_tags:
            tagged_counts[tag] = tagged_counts.get(tag, 0) + 1

    assert len(cases) >= 8
    assert "deleted-cat-after-delete" in names
    assert "deleted-session-drink-after-delete" in names
    assert "deleted-all-nickname-after-delete" in names
    assert "deleted-all-nickname-after-rebuild" in names
    assert tagged_counts["delete_memory_residue_rate"] >= 1
    assert tagged_counts["delete_session_residue_rate"] >= 2
    assert tagged_counts["delete_all_residue_rate"] >= 2
    assert tagged_counts["rebuild_resurrection_rate"] >= 3


def test_quality_report_computes_scope_leak_rates_from_tagged_cases() -> None:
    cases = [
        EvaluationCase(
            name="cross-user-clean",
            category="isolation",
            query="用户的猫叫什么？",
            expected_terms=["麻薯"],
            forbidden_terms=["奶盖"],
            metric_tags=("cross_user_leak_rate",),
        ),
        EvaluationCase(
            name="cross-character-leak",
            category="isolation",
            query="用户的猫叫什么？",
            expected_terms=["麻薯"],
            forbidden_terms=["泡芙"],
            metric_tags=("cross_character_leak_rate",),
        ),
        EvaluationCase(
            name="roleplay-clean",
            category="isolation",
            query="剧情设定里的猫叫什么？",
            expected_terms=["露露"],
            forbidden_terms=["麻薯"],
            metric_tags=("roleplay_real_mix_rate",),
        ),
    ]
    results = [
        QueryResult(case_name="cross-user-clean", recalled_text="User has a cat named 麻薯"),
        QueryResult(case_name="cross-character-leak", recalled_text="User has a cat named 泡芙"),
        QueryResult(case_name="roleplay-clean", recalled_text="Story-world cat is 露露"),
    ]

    report = build_quality_report(cases, results, active_memory_count=3, active_after_rebuild=3)

    assert report["cross_user_leak_rate"] == 0.0
    assert report["cross_character_leak_rate"] == 1.0
    assert report["roleplay_real_mix_rate"] == 0.0
    assert {
        "metric": "cross_character_leak_rate",
        "actual": 1.0,
        "expected": "<= 0",
    } in report["failed_metrics"]


def test_quality_report_scope_leak_rates_only_count_l3_content() -> None:
    cases = [
        EvaluationCase(
            name="roleplay-cat",
            category="isolation",
            query="剧情设定里的猫叫什么？",
            expected_terms=["露露"],
            forbidden_terms=["麻薯"],
            metric_tags=("roleplay_real_mix_rate",),
        ),
    ]
    results = [
        QueryResult(
            case_name="roleplay-cat",
            recalled_text="User has a cat named 露露",
            all_recalled_text="L1: 用户现实里的猫叫麻薯\nL3: User has a cat named 露露",
            l3_contents=["User has a cat named 露露"],
        ),
    ]

    report = build_quality_report(cases, results, active_memory_count=1, active_after_rebuild=1)

    assert report["roleplay_real_mix_rate"] == 0.0
    assert report["failed_metrics"] == []


def test_quality_report_computes_delete_and_rebuild_rates_from_tagged_cases() -> None:
    cases = [
        EvaluationCase(
            name="deleted-cat-after-delete",
            category="delete_memory",
            query="用户的猫叫什么？",
            expected_terms=[],
            forbidden_terms=["麻薯"],
            metric_tags=("delete_memory_residue_rate",),
        ),
        EvaluationCase(
            name="deleted-cat-after-rebuild",
            category="rebuild",
            query="用户的猫叫什么？",
            expected_terms=[],
            forbidden_terms=["麻薯"],
            metric_tags=("rebuild_resurrection_rate",),
        ),
    ]
    results = [
        QueryResult(case_name="deleted-cat-after-delete", recalled_text="", l3_count=0),
        QueryResult(case_name="deleted-cat-after-rebuild", recalled_text="User has a cat named 麻薯", l3_count=1),
    ]

    report = build_quality_report(cases, results, active_memory_count=10, active_after_rebuild=10)

    assert report["delete_memory_residue_rate"] == 0.0
    assert report["rebuild_resurrection_rate"] == 1.0
    assert {
        "metric": "rebuild_resurrection_rate",
        "actual": 1.0,
        "expected": "<= 0",
    } in report["failed_metrics"]


def test_quality_report_computes_session_all_delete_and_drift_gates() -> None:
    cases = [
        EvaluationCase(
            name="session-drink-clean",
            category="delete_session",
            query="用户喜欢喝什么？",
            expected_terms=[],
            forbidden_terms=["茶"],
            metric_tags=("delete_session_residue_rate",),
        ),
        EvaluationCase(
            name="all-nickname-leak",
            category="delete_all",
            query="怎么称呼用户？",
            expected_terms=[],
            forbidden_terms=["小鹏"],
            metric_tags=("delete_all_residue_rate",),
        ),
        EvaluationCase(
            name="drift-cat-missing",
            category="expression_drift",
            query="What is the user's cat called?",
            expected_terms=["麻薯"],
            forbidden_terms=["团子"],
            metric_tags=("known_drift_regression_pass_rate",),
        ),
    ]
    results = [
        QueryResult(case_name="session-drink-clean", recalled_text="", l3_count=0),
        QueryResult(case_name="all-nickname-leak", recalled_text="User prefers 小鹏", l3_count=1),
        QueryResult(case_name="drift-cat-missing", recalled_text="", l3_count=0),
    ]

    report = build_quality_report(cases, results, active_memory_count=0, active_after_rebuild=0)

    assert report["delete_session_residue_rate"] == 0.0
    assert report["delete_all_residue_rate"] == 1.0
    assert report["known_drift_regression_pass_rate"] == 0.0
    assert {
        "metric": "delete_all_residue_rate",
        "actual": 1.0,
        "expected": "<= 0",
    } in report["failed_metrics"]
    assert {
        "metric": "known_drift_regression_pass_rate",
        "actual": 0.0,
        "expected": ">= 0.95",
    } in report["failed_metrics"]


def test_quality_report_caps_transient_retry_rate_by_request_count() -> None:
    metrics = RequestMetrics(request_count=4, retry_count=5, transient_failure_count=2)

    report = build_quality_report(
        [],
        [],
        active_memory_count=0,
        active_after_rebuild=0,
        request_metrics=metrics,
    )

    assert report["transient_retry_rate"] == 0.5


def test_real_quality_runner_parses_child_output_reports() -> None:
    from script.run_real_mem0_quality_evaluation import (
        extract_quality_json_reports,
        extract_run_id,
    )

    output = """
quality scope: run_id=real-quality-20260510T120000-a1b2c3d4
{
  "passed": true,
  "case_count": 2,
  "recall_at_10": 1.0
}
noise line
{
  "passed": true,
  "case_count": 1,
  "delete_memory_residue_rate": 0.0
}
quality regression passed: active_after_append=12, active_after_rebuild=11
"""

    reports = extract_quality_json_reports(output)

    assert extract_run_id(output) == "real-quality-20260510T120000-a1b2c3d4"
    assert [report["case_count"] for report in reports] == [2, 1]


def test_real_quality_runner_writes_markdown_with_previous_comparison(tmp_path) -> None:
    from script.run_real_mem0_quality_evaluation import (
        build_quality_evaluation_run_report,
        write_quality_evaluation_report_artifacts,
    )

    previous = build_quality_evaluation_run_report(
        run_id="real-quality-old",
        child_returncode=0,
        quality_report={
            "passed": True,
            "case_count": 15,
            "case_pass_rate": 1.0,
            "recall_at_10": 0.95,
            "precision_at_10": 0.95,
            "conflict_pollution_rate": 0.01,
            "false_positive_rate": 0.0,
            "duplicate_active_rate": 0.0,
            "top1_hit_rate": 0.80,
            "mrr": 0.90,
            "item_precision_at_10": 0.60,
            "irrelevant_l3_per_query": 1.2,
            "failed_cases": [],
            "failed_metrics": [],
            "category_metrics": {},
            "metric_automation_status": {},
        },
        post_delete_report={
            "passed": True,
            "delete_memory_residue_rate": 0.0,
            "rebuild_resurrection_rate": 0.0,
            "failed_cases": [],
            "failed_metrics": [],
        },
        stdout="old",
        stderr="",
    )
    (tmp_path / f"{QUALITY_REPORT_PREFIX}-real-quality-old.json").write_text(
        json.dumps(previous),
        encoding="utf-8",
    )

    current = build_quality_evaluation_run_report(
        run_id="real-quality-new",
        child_returncode=0,
        quality_report={
            "passed": True,
            "case_count": 15,
            "case_pass_rate": 1.0,
            "recall_at_10": 1.0,
            "precision_at_10": 0.93,
            "conflict_pollution_rate": 0.0,
            "false_positive_rate": 0.0,
            "duplicate_active_rate": 0.0,
            "top1_hit_rate": 0.75,
            "mrr": 0.88,
            "item_precision_at_10": 0.65,
            "irrelevant_l3_per_query": 0.8,
            "failed_cases": [],
            "failed_metrics": [],
            "category_metrics": {
                "slot_conflict": {"case_count": 10, "passed_count": 10, "pass_rate": 1.0},
            },
            "metric_automation_status": {"recall_at_10": "automatic"},
        },
        post_delete_report={
            "passed": True,
            "delete_memory_residue_rate": 0.0,
            "rebuild_resurrection_rate": 0.0,
            "failed_cases": [],
            "failed_metrics": [],
            "category_metrics": {
                "delete_memory": {"case_count": 1, "passed_count": 1, "pass_rate": 1.0},
                "rebuild": {"case_count": 1, "passed_count": 1, "pass_rate": 1.0},
            },
        },
        stdout="current",
        stderr="",
    )
    current["generated_at"] = "2026-05-11T01:02:03+00:00"

    artifacts = write_quality_evaluation_report_artifacts(tmp_path, current)
    markdown = artifacts.markdown_path.read_text(encoding="utf-8")

    assert artifacts.json_path.name == f"{QUALITY_REPORT_PREFIX}-20260511-001.json"
    assert f"# {QUALITY_REPORT_PREFIX}" in markdown
    assert "| 对比报告 | `real-quality-old` |" in markdown
    assert "| `recall_at_10` | 0.95 | 1.0 | +0.05 | 提升 |" in markdown
    assert "| `precision_at_10` | 0.95 | 0.93 | -0.02 | 下降 |" in markdown
    assert "| `conflict_pollution_rate` | 0.01 | 0.0 | -0.01 | 提升 |" in markdown
    assert "| `irrelevant_l3_per_query` | 1.2 | 0.8 | -0.4 | 提升 |" in markdown
    assert "| `delete_memory` | 1 | 1 | 1.0 |" in markdown
    assert "| `rebuild` | 1 | 1 | 1.0 |" in markdown
    assert "| `delete_session_residue_rate` | 硬门禁 | - |" in markdown
    assert "| `delete_all_residue_rate` | 硬门禁 | - |" in markdown
    assert "| `known_drift_regression_pass_rate` | 硬门禁 | - |" in markdown


def test_real_quality_runner_uses_date_and_incrementing_sequence_for_report_suffix(tmp_path) -> None:
    from script.run_real_mem0_quality_evaluation import (
        build_quality_evaluation_run_report,
        write_quality_evaluation_report_artifacts,
    )

    (tmp_path / f"{QUALITY_REPORT_PREFIX}-20260511-001.json").write_text("{}", encoding="utf-8")
    (tmp_path / f"{QUALITY_REPORT_PREFIX}-20260511-002.md").write_text("# old", encoding="utf-8")
    (tmp_path / f"{QUALITY_REPORT_PREFIX}-real-quality-legacy.json").write_text("{}", encoding="utf-8")

    current = build_quality_evaluation_run_report(
        run_id="real-quality-new",
        child_returncode=0,
        quality_report={"passed": True, "case_count": 1, "case_pass_rate": 1.0},
        post_delete_report={"passed": True, "case_count": 1},
        stdout="current",
        stderr="",
    )
    current["generated_at"] = "2026-05-11T01:02:03+00:00"

    artifacts = write_quality_evaluation_report_artifacts(tmp_path, current)

    assert artifacts.json_path.name == f"{QUALITY_REPORT_PREFIX}-20260511-003.json"
    assert artifacts.markdown_path.name == f"{QUALITY_REPORT_PREFIX}-20260511-003.md"


def test_real_quality_runner_markdown_explains_dataset_boundary_and_database(tmp_path) -> None:
    from script.run_real_mem0_quality_evaluation import (
        build_quality_evaluation_run_report,
        write_quality_evaluation_report_artifacts,
    )

    current = build_quality_evaluation_run_report(
        run_id="real-quality-dataset-boundary",
        child_returncode=0,
        quality_report={
            "passed": True,
            "case_count": 15,
            "case_pass_rate": 1.0,
            "recall_at_10": 1.0,
            "precision_at_10": 1.0,
            "conflict_pollution_rate": 0.0,
            "false_positive_rate": 0.0,
            "duplicate_active_rate": 0.0,
            "cross_user_leak_rate": 0.0,
            "cross_character_leak_rate": 0.0,
            "roleplay_real_mix_rate": 0.0,
            "failed_cases": [],
            "failed_metrics": [],
            "category_metrics": {
                "slot_conflict": {"case_count": 10, "passed_count": 10, "pass_rate": 1.0},
                "negative_control": {"case_count": 1, "passed_count": 1, "pass_rate": 1.0},
                "isolation": {"case_count": 4, "passed_count": 4, "pass_rate": 1.0},
            },
            "metric_automation_status": {},
        },
        post_delete_report={
            "passed": True,
            "case_count": 3,
            "delete_memory_residue_rate": 0.0,
            "rebuild_resurrection_rate": 0.0,
            "failed_cases": [],
            "failed_metrics": [],
            "category_metrics": {
                "delete_memory": {"case_count": 1, "passed_count": 1, "pass_rate": 1.0},
                "rebuild": {"case_count": 2, "passed_count": 2, "pass_rate": 1.0},
            },
        },
        stdout="current",
        stderr="",
        postgres_database="liaoriver_memory",
    )

    artifacts = write_quality_evaluation_report_artifacts(tmp_path, current)
    markdown = artifacts.markdown_path.read_text(encoding="utf-8")

    assert "| PostgreSQL 数据库 | `liaoriver_memory` |" in markdown
    assert "| 质量 case 总数 | 18 |" in markdown
    assert "| 首版主链路质量门禁 | 足够支撑，当前报告已通过 |" in markdown
    assert "不代表广义生产级泛化覆盖已经充分" in markdown
    assert "按本轮要求，本报告不包含稳定性测试" in markdown


def test_real_quality_runner_markdown_marks_production_quality_dataset_sufficient(tmp_path) -> None:
    from script.run_real_mem0_quality_evaluation import (
        build_quality_evaluation_run_report,
        write_quality_evaluation_report_artifacts,
    )

    current = build_quality_evaluation_run_report(
        run_id="real-quality-production-dataset",
        child_returncode=0,
        quality_report={
            "passed": True,
            "case_count": 31,
            "case_pass_rate": 1.0,
            "recall_at_10": 1.0,
            "precision_at_10": 1.0,
            "conflict_pollution_rate": 0.0,
            "false_positive_rate": 0.0,
            "duplicate_active_rate": 0.0,
            "cross_user_leak_rate": 0.0,
            "cross_character_leak_rate": 0.0,
            "roleplay_real_mix_rate": 0.0,
            "known_drift_regression_pass_rate": 1.0,
            "failed_cases": [],
            "failed_metrics": [],
            "category_metrics": {
                "slot_conflict": {"case_count": 12, "passed_count": 12, "pass_rate": 1.0},
                "negative_control": {"case_count": 6, "passed_count": 6, "pass_rate": 1.0},
                "isolation": {"case_count": 7, "passed_count": 7, "pass_rate": 1.0},
                "expression_drift": {"case_count": 6, "passed_count": 6, "pass_rate": 1.0},
            },
            "metric_automation_status": {},
        },
        post_delete_report={
            "passed": True,
            "case_count": 9,
            "delete_memory_residue_rate": 0.0,
            "delete_session_residue_rate": 0.0,
            "delete_all_residue_rate": 0.0,
            "rebuild_resurrection_rate": 0.0,
            "failed_cases": [],
            "failed_metrics": [],
            "category_metrics": {
                "delete_memory": {"case_count": 1, "passed_count": 1, "pass_rate": 1.0},
                "delete_session": {"case_count": 2, "passed_count": 2, "pass_rate": 1.0},
                "delete_all": {"case_count": 2, "passed_count": 2, "pass_rate": 1.0},
                "rebuild": {"case_count": 4, "passed_count": 4, "pass_rate": 1.0},
            },
        },
        stdout="current",
        stderr="",
        postgres_database="liaoriver_memory",
    )

    artifacts = write_quality_evaluation_report_artifacts(tmp_path, current)
    markdown = artifacts.markdown_path.read_text(encoding="utf-8")

    assert "| 质量 case 总数 | 40 |" in markdown
    assert "| 首版核心质量覆盖 | 达到 |" in markdown
    assert "核心质量风险已达到首版门禁" in markdown
    assert "不代表广义生产级泛化覆盖已经充分" in markdown
    assert "已覆盖生产级质量风险" not in markdown


def test_real_quality_runner_writes_failure_report_without_child_json(tmp_path) -> None:
    from script.run_real_mem0_quality_evaluation import (
        build_quality_evaluation_run_report,
        write_quality_evaluation_report_artifacts,
    )

    current = build_quality_evaluation_run_report(
        run_id="real-quality-env-failed",
        child_returncode=1,
        quality_report=None,
        post_delete_report=None,
        stdout="",
        stderr="RuntimeError: OPENAI_API_KEY and QDRANT_URL are required",
    )

    artifacts = write_quality_evaluation_report_artifacts(tmp_path, current)
    markdown = artifacts.markdown_path.read_text(encoding="utf-8")

    assert current["passed"] is False
    assert current["failure_phase"] == "environment_or_execution"
    assert "| 最终结论 | 未通过 |" in markdown
    assert "OPENAI_API_KEY and QDRANT_URL are required" in markdown


def test_real_quality_runner_writes_failure_report_on_child_timeout(monkeypatch, tmp_path) -> None:
    from script.run_real_mem0_quality_evaluation import run_real_quality_evaluation

    def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise subprocess.TimeoutExpired(
            cmd=kwargs.get("args") or args[0],
            timeout=12,
            output="quality scope: run_id=real-quality-timeout\nappend started",
            stderr="",
        )

    monkeypatch.setattr("script.run_real_mem0_quality_evaluation.subprocess.run", fake_run)

    artifacts = run_real_quality_evaluation(tmp_path, timeout_seconds=12)
    report = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    markdown = artifacts.markdown_path.read_text(encoding="utf-8")

    assert report["run_id"] == "real-quality-timeout"
    assert report["passed"] is False
    assert report["failure_phase"] == "environment_or_execution"
    assert "timed out after 12s" in report["stderr_tail"]
    assert "timed out after 12s" in markdown


def test_quality_report_does_not_count_duplicate_slots_across_context_partitions() -> None:
    active_memories = [
        MemorySnapshot(
            memory_text="User has a cat named 麻薯",
            memory_status="active",
            context_type="real_user",
            fact_subject="user",
            roleplay_mode="off",
        ),
        MemorySnapshot(
            memory_text="In the story world, the user has a cat named 露露",
            memory_status="active",
            context_type="roleplay",
            fact_subject="story_world",
            roleplay_mode="on",
        ),
    ]

    report = build_quality_report(
        [],
        [],
        active_memory_count=2,
        active_after_rebuild=2,
        active_memory_snapshots=active_memories,
    )

    assert report["duplicate_active_rate"] == 0.0


def test_quality_report_lists_failed_metrics_when_gate_fails_without_failed_cases() -> None:
    cases = [
        EvaluationCase(
            name="nickname",
            category="slot_conflict",
            query="怎么称呼用户？",
            expected_terms=["小鹏"],
            forbidden_terms=["阿鹏"],
        )
    ]
    results = [
        QueryResult(
            case_name="nickname",
            recalled_text="User prefers to be called 小鹏",
            l3_count=1,
            l3_contents=["User prefers to be called 小鹏"],
        )
    ]
    active_memories = [
        MemorySnapshot(memory_text="User has a cat named 麻薯", memory_status="active"),
        MemorySnapshot(memory_text="User has a cat named 露露", memory_status="active"),
    ]

    report = build_quality_report(
        cases,
        results,
        active_memory_count=2,
        active_after_rebuild=2,
        active_memory_snapshots=active_memories,
    )

    assert report["failed_cases"] == []
    assert report["passed"] is False
    assert report["failed_metrics"] == [
        {
            "metric": "duplicate_active_rate",
            "actual": 0.5,
            "expected": "== 0",
        }
    ]


def test_quality_report_requires_all_core_cases_to_pass() -> None:
    cases = [
        EvaluationCase(
            name="nickname",
            category="slot_conflict",
            query="现在应该怎么称呼用户？",
            expected_terms=["小鹏"],
            forbidden_terms=["阿鹏"],
        ),
        EvaluationCase(
            name="location",
            category="slot_conflict",
            query="用户现在住在哪？",
            expected_terms=["上海"],
            forbidden_terms=["杭州"],
        ),
    ]
    results = [
        QueryResult(case_name="nickname", recalled_text="User prefers to be called 小鹏"),
        QueryResult(case_name="location", recalled_text=""),
    ]

    report = build_quality_report(cases, results, active_memory_count=2, active_after_rebuild=2)

    assert report["case_pass_rate"] == 0.5
    assert {
        "metric": "case_pass_rate",
        "actual": 0.5,
        "expected": "== 1",
    } in report["failed_metrics"]


def test_http_502_timeout_is_transient_for_quality_runner() -> None:
    exc = HTTPError(
        url="http://127.0.0.1:18082/memory/append",
        code=502,
        msg="Bad Gateway",
        hdrs={},
        fp=BytesIO(b'{"detail":"mem0 http POST /memories failed: timed out"}'),
    )

    assert _is_transient_http_failure(exc) is True


def test_http_502_bad_gateway_reason_is_transient_for_quality_runner() -> None:
    exc = HTTPError(
        url="http://127.0.0.1:18082/memory/recall",
        code=502,
        msg="Bad Gateway",
        hdrs={},
        fp=BytesIO(b'{"detail":"mem0 library search failed"}'),
    )

    assert _is_transient_http_failure(exc) is True


def test_post_json_retries_transient_http_failure(monkeypatch) -> None:
    attempts = 0

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:  # type: ignore[no-untyped-def]
            return None

        def read(self) -> bytes:
            return b'{"status":"completed"}'

    def fake_urlopen(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise HTTPError(
                url="http://127.0.0.1:18082/memory/append",
                code=502,
                msg="Bad Gateway",
                hdrs={},
                fp=BytesIO(b'{"detail":"mem0 http POST /memories failed: timed out"}'),
            )
        return Response()

    metrics = RequestMetrics()
    monkeypatch.setattr(real_mem0_quality_regression, "urlopen", fake_urlopen)

    response = _post_json(
        "http://127.0.0.1:18082",
        "/memory/append",
        {"hello": "world"},
        request_metrics=metrics,
        max_attempts=2,
        sleep_seconds=0,
    )

    assert response == {"status": "completed"}
    assert attempts == 2
    assert metrics.request_count == 1
    assert metrics.retry_count == 1
    assert metrics.transient_failure_count == 1


def test_post_json_retries_transient_network_timeout(monkeypatch) -> None:
    attempts = 0

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:  # type: ignore[no-untyped-def]
            return None

        def read(self) -> bytes:
            return b'{"status":"completed"}'

    def fake_urlopen(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("timed out")
        return Response()

    metrics = RequestMetrics()
    monkeypatch.setattr(real_mem0_quality_regression, "urlopen", fake_urlopen)

    response = _post_json(
        "http://127.0.0.1:18082",
        "/memory/append",
        {"hello": "world"},
        request_metrics=metrics,
        max_attempts=2,
        sleep_seconds=0,
    )

    assert response == {"status": "completed"}
    assert attempts == 2
    assert metrics.retry_count == 1
    assert metrics.transient_failure_count == 1


def test_post_json_retries_connection_refused_during_quality_run(monkeypatch) -> None:
    attempts = 0

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:  # type: ignore[no-untyped-def]
            return None

        def read(self) -> bytes:
            return b'{"status":"completed"}'

    def fake_urlopen(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise URLError(ConnectionRefusedError(61, "Connection refused"))
        return Response()

    metrics = RequestMetrics()
    monkeypatch.setattr(real_mem0_quality_regression, "urlopen", fake_urlopen)

    response = _post_json(
        "http://127.0.0.1:18082",
        "/memory/append",
        {"hello": "world"},
        request_metrics=metrics,
        max_attempts=2,
        sleep_seconds=0,
    )

    assert response == {"status": "completed"}
    assert attempts == 2
    assert metrics.retry_count == 1
    assert metrics.transient_failure_count == 1


def test_post_json_records_non_transient_network_failure(monkeypatch) -> None:
    def fake_urlopen(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise URLError("name or service not known")

    metrics = RequestMetrics()
    monkeypatch.setattr(real_mem0_quality_regression, "urlopen", fake_urlopen)

    try:
        _post_json(
            "http://127.0.0.1:18082",
            "/memory/append",
            {"hello": "world"},
            request_metrics=metrics,
            max_attempts=1,
        )
    except RuntimeError as exc:
        assert "network failure" in str(exc)
    else:
        raise AssertionError("_post_json should raise for non-transient network failure")

    assert metrics.non_transient_failure_count == 1


def test_network_timeout_is_transient_for_quality_runner() -> None:
    assert _is_transient_network_failure(TimeoutError("timed out")) is True
    assert _is_transient_network_failure(URLError("timed out")) is True
    assert _is_transient_network_failure(URLError(ConnectionRefusedError(61, "Connection refused"))) is True
    assert _is_transient_network_failure(URLError("name or service not known")) is False


def test_pressure_suite_report_fails_when_any_gate_fails() -> None:
    passing_report = {
        "passed": True,
        "case_pass_rate": 1.0,
        "recall_at_10": 1.0,
        "precision_at_10": 1.0,
        "failed_cases": [],
        "failed_metrics": [],
        "latency_ms": {"recall": {"p95": 500}, "append": {"p95": 9000}},
    }
    failing_concurrency_report = {
        "passed": False,
        "case_pass_rate": 0.9,
        "recall_at_10": 0.9,
        "precision_at_10": 0.9,
        "failed_cases": ["cat-current#7"],
        "failed_metrics": [{"metric": "recall_at_10", "actual": 0.9, "expected": ">= 0.95"}],
        "latency_ms": {"recall": {"p95": 700}, "append": {"p95": 0}},
    }

    report = build_pressure_suite_report(
        suite_id="suite-1",
        started_at="2026-05-07T10:00:00Z",
        ended_at="2026-05-07T10:05:00Z",
        config={
            "recall_concurrency": 10,
            "recall_requests": 20,
            "append_concurrency": 3,
        },
        quality_report=passing_report,
        concurrent_recall_report=failing_concurrency_report,
        append_probe={
            "passed": True,
            "success_count": 3,
            "failure_count": 0,
            "latency_ms": {"append": {"p95": 12000}},
        },
        post_delete_report=passing_report,
        limitations=[],
    )

    assert report["passed"] is False
    assert report["failed_sections"] == ["concurrent_recall"]
    assert report["summary"]["recall_at_10"] == 1.0
    assert report["concurrent_recall"]["recall_at_10"] == 0.9


def test_pressure_suite_report_applies_recall_latency_gates() -> None:
    report = build_pressure_suite_report(
        suite_id="suite-1",
        started_at="2026-05-07T10:00:00Z",
        ended_at="2026-05-07T10:05:00Z",
        config={"recall_p95_gate_ms": 1500, "recall_p99_gate_ms": 3000},
        quality_report={
            "passed": True,
            "case_pass_rate": 1.0,
            "recall_at_10": 1.0,
            "precision_at_10": 1.0,
            "failed_cases": [],
            "failed_metrics": [],
            "latency_ms": {"append": {"p95": 9000}, "recall": {"p95": 500, "p99": 500}},
        },
        concurrent_recall_report={
            "passed": True,
            "case_pass_rate": 1.0,
            "recall_at_10": 1.0,
            "precision_at_10": 1.0,
            "failed_cases": [],
            "failed_metrics": [],
            "latency_ms": {"append": {"p95": 0}, "recall": {"p95": 3519, "p99": 3519}},
        },
        append_probe={"passed": True, "success_count": 3, "failure_count": 0},
        post_delete_report={
            "passed": True,
            "case_pass_rate": 1.0,
            "recall_at_10": 1.0,
            "precision_at_10": 1.0,
            "failed_cases": [],
            "failed_metrics": [],
            "latency_ms": {"append": {"p95": 9000}, "recall": {"p95": 1700, "p99": 1700}},
        },
        limitations=[],
    )

    assert report["passed"] is False
    assert report["failed_sections"] == ["concurrent_recall", "post_delete"]
    assert report["latency_gate_failures"] == [
        {
            "section": "concurrent_recall",
            "metric": "recall.p95",
            "actual": 3519,
            "expected": "<= 1500",
        },
        {
            "section": "concurrent_recall",
            "metric": "recall.p99",
            "actual": 3519,
            "expected": "<= 3000",
        },
        {
            "section": "post_delete",
            "metric": "recall.p95",
            "actual": 1700,
            "expected": "<= 1500",
        },
    ]


def test_render_pressure_suite_markdown_includes_gates_and_limitations() -> None:
    report = build_pressure_suite_report(
        suite_id="suite-1",
        started_at="2026-05-07T10:00:00Z",
        ended_at="2026-05-07T10:05:00Z",
        config={"recall_concurrency": 10, "recall_requests": 20},
        quality_report={
            "passed": True,
            "case_pass_rate": 1.0,
            "recall_at_10": 1.0,
            "precision_at_10": 1.0,
            "failed_cases": [],
            "failed_metrics": [],
            "latency_ms": {"append": {"p95": 9000}, "recall": {"p95": 500}},
        },
        concurrent_recall_report={
            "passed": True,
            "case_pass_rate": 1.0,
            "recall_at_10": 1.0,
            "precision_at_10": 1.0,
            "failed_cases": [],
            "failed_metrics": [],
            "latency_ms": {"append": {"p95": 0}, "recall": {"p95": 600}},
        },
        append_probe={"passed": True, "success_count": 3, "failure_count": 0},
        post_delete_report={
            "passed": True,
            "case_pass_rate": 1.0,
            "recall_at_10": 1.0,
            "precision_at_10": 1.0,
            "failed_cases": [],
            "failed_metrics": [],
            "latency_ms": {"append": {"p95": 9000}, "recall": {"p95": 500}},
        },
        limitations=["未执行 10 分钟 P0 soak test"],
    )

    markdown = render_pressure_suite_markdown(report)

    assert "# Thinkback P0 压测报告" in markdown
    assert "| 整体结论 | 通过 |" in markdown
    assert "未执行 10 分钟 P0 soak test" in markdown


def test_final_pressure_report_keeps_stress_latency_failure_visible(tmp_path) -> None:
    pass_report = _final_report_fixture("p0-short-pass", passed=True)
    stress_report = _final_report_fixture("p0-short-stress", passed=False)
    stress_report["config"]["recall_concurrency"] = 50
    stress_report["config"]["recall_requests"] = 100
    stress_report["failed_sections"] = ["concurrent_recall"]
    stress_report["latency_gate_failures"] = [
        {
            "section": "concurrent_recall",
            "metric": "recall.p95",
            "actual": 1808,
            "expected": "<= 1500",
        }
    ]
    stress_report["concurrent_recall"]["latency_ms"]["recall"]["p95"] = 1808
    stress_report["concurrent_recall"]["latency_ms"]["recall"]["p99"] = 2107

    pass_path = tmp_path / "pass.json"
    stress_path = tmp_path / "stress.json"
    output_path = tmp_path / "final.md"
    pass_path.write_text(json.dumps(pass_report), encoding="utf-8")
    stress_path.write_text(json.dumps(stress_report), encoding="utf-8")

    content = build_final_report(
        [pass_path, stress_path],
        output_path=output_path,
        observed_fixed_issues=[
            "`sleep-reminder-current` 曾因 `User now accepts gentle reminders about going to sleep` 漏召回。"
        ],
    )

    assert "是否达到生产前完整压测标准 | 否" in content
    assert "本轮压测曾发现真实链路问题" in content
    assert "50 并发 recall / 100 请求" in content
    assert "p95=1808ms" in content
    assert "sleep-reminder-current" in content
    assert output_path.read_text(encoding="utf-8") == content


def test_final_pressure_report_uses_p0_metric_names_and_scope_language(tmp_path) -> None:
    pass_report = _final_report_fixture("p0-short-pass", passed=True)
    report_path = tmp_path / "pass.json"
    output_path = tmp_path / "final.md"
    report_path.write_text(json.dumps(pass_report), encoding="utf-8")

    content = build_final_report([report_path], output_path=output_path)

    assert "`delete_memory_residue_rate`" in content
    assert "`delete_residue_rate`" not in content
    assert "200+ / 1000+" not in content
    assert "P0/P1" not in content
    assert "P0 核心槽位评测集" in content


def test_final_pressure_report_rejects_non_short_report_inputs(tmp_path) -> None:
    duration_report = {
        "run_id": "p0-duration-stress",
        "phase": "stress",
        "passed": True,
        "duration_gate_passed": True,
        "child_reports": [],
    }
    report_path = tmp_path / "duration.json"
    output_path = tmp_path / "final.md"
    report_path.write_text(json.dumps(duration_report), encoding="utf-8")

    try:
        build_final_report([report_path], output_path=output_path)
    except ValueError as exc:
        assert "only accepts p0-short pressure reports" in str(exc)
    else:
        raise AssertionError("build_final_report should reject duration/spike reports")


def test_final_pressure_report_separates_dev_gate_and_50_concurrency_evidence(
    tmp_path,
) -> None:
    dev_report = _final_report_fixture("p0-short-dev", passed=True)
    dev_report["concurrent_recall"]["latency_ms"]["recall"]["p95"] = 131
    dev_report["concurrent_recall"]["latency_ms"]["recall"]["p99"] = 132

    stress_report = _final_report_fixture("p0-short-stress-pass", passed=True)
    stress_report["config"]["recall_concurrency"] = 50
    stress_report["config"]["recall_requests"] = 100
    stress_report["concurrent_recall"]["latency_ms"]["recall"]["p95"] = 920
    stress_report["concurrent_recall"]["latency_ms"]["recall"]["p99"] = 934

    dev_path = tmp_path / "dev.json"
    stress_path = tmp_path / "stress.json"
    output_path = tmp_path / "final.md"
    dev_path.write_text(json.dumps(dev_report), encoding="utf-8")
    stress_path.write_text(json.dumps(stress_report), encoding="utf-8")

    content = build_final_report([dev_path, stress_path], output_path=output_path)

    assert "| 10 并发 recall p95 | 131ms | <= 1500ms |" in content
    assert "| 10 并发 recall p99 | 132ms | <= 3000ms |" in content
    assert "50 并发代表性压测已通过" in content
    assert "`recall p95=920ms`" in content
    assert "50 并发 recall p95 超门禁" not in content


def test_final_pressure_report_uses_latest_passing_50_concurrency_after_old_failure(
    tmp_path,
) -> None:
    old_failed_stress = _final_report_fixture("p0-short-stress-old-fail", passed=False)
    old_failed_stress["config"]["recall_concurrency"] = 50
    old_failed_stress["config"]["recall_requests"] = 100
    old_failed_stress["quality"]["passed"] = False
    old_failed_stress["quality"]["case_pass_rate"] = 0.93
    old_failed_stress["quality"]["recall_at_10"] = 0.93
    old_failed_stress["quality"]["precision_at_10"] = 0.93
    old_failed_stress["quality"]["conflict_pollution_rate"] = 0.07
    old_failed_stress["concurrent_recall"]["passed"] = False
    old_failed_stress["concurrent_recall"]["case_pass_rate"] = 0.93
    old_failed_stress["concurrent_recall"]["recall_at_10"] = 0.93
    old_failed_stress["concurrent_recall"]["precision_at_10"] = 0.93
    old_failed_stress["concurrent_recall"]["conflict_pollution_rate"] = 0.07
    old_failed_stress["failed_sections"] = ["quality", "concurrent_recall"]

    latest_stress = _final_report_fixture("p0-short-stress-latest-pass", passed=True)
    latest_stress["config"]["recall_concurrency"] = 50
    latest_stress["config"]["recall_requests"] = 100
    latest_stress["concurrent_recall"]["latency_ms"]["recall"]["p95"] = 888
    latest_stress["concurrent_recall"]["latency_ms"]["recall"]["p99"] = 971

    paths = []
    for report in (old_failed_stress, latest_stress):
        path = tmp_path / f"{report['suite_id']}.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        paths.append(path)
    output_path = tmp_path / "final.md"

    content = build_final_report(paths, output_path=output_path)

    assert "50 并发代表性压测已通过" in content
    assert "`suite_id=p0-short-stress-latest-pass`" in content
    assert "`recall_at_10=1.0`" in content
    assert "`precision_at_10=1.0`" in content
    assert "`recall p95=888ms`" in content
    assert "P0 召回主链路在当前代表性并发下未退化" in content
    assert "| 50 并发 recall / 100 请求 | `case_pass_rate=1.0`, `recall_at_10=1.0` | `p95=888ms`, `p99=971ms` | 通过，未观察到召回质量退化。 |" in content
    assert "50 并发 recall p95 超门禁" not in content


def test_final_pressure_report_can_reference_preprod_summary(tmp_path) -> None:
    pass_report = _final_report_fixture("p0-short-pass", passed=True)
    report_path = tmp_path / "pass.json"
    output_path = tmp_path / "final.md"
    report_path.write_text(json.dumps(pass_report), encoding="utf-8")
    preprod_summary = {
        "run_id": "p0-preprod-summary",
        "requested_phases_passed": True,
        "production_precheck_passed": False,
        "executed_phases": ["baseline", "stress", "spike", "soak"],
        "missing_phases": ["fault_injection", "representative_replay"],
        "phase_results": {
            "baseline": {"worst_recall_p95_ms": 153, "worst_recall_p99_ms": 162},
            "stress": {"worst_recall_p95_ms": 795, "worst_recall_p99_ms": 823},
            "spike": {"worst_recall_p95_ms": 1310, "worst_recall_p99_ms": 1310},
            "soak": {"worst_recall_p95_ms": 470, "worst_recall_p99_ms": 516},
        },
    }

    content = build_final_report(
        [report_path],
        output_path=output_path,
        preprod_summary=preprod_summary,
    )

    assert "## 6. 生产前预检阶段" in content
    assert "| baseline | p95=153ms, p99=162ms | 通过 |" in content
    assert "| spike | p95=1310ms, p99=1310ms | 通过 |" in content
    assert "fault_injection, representative_replay" in content
    assert "生产前完整压测标准 | 否" in content


def test_final_pressure_report_keeps_50_concurrency_and_spike_latency_separate(
    tmp_path,
) -> None:
    dev_report = _final_report_fixture("p0-dev", passed=True)
    stress_report = _final_report_fixture("p0-stress-50", passed=True)
    stress_report["config"]["recall_concurrency"] = 50
    stress_report["config"]["recall_requests"] = 100
    stress_report["concurrent_recall"]["latency_ms"]["recall"]["p95"] = 795
    stress_report["concurrent_recall"]["latency_ms"]["recall"]["p99"] = 823
    spike_report = _final_report_fixture("p0-spike-100", passed=True)
    spike_report["config"]["recall_concurrency"] = 100
    spike_report["config"]["recall_requests"] = 100
    spike_report["concurrent_recall"]["latency_ms"]["recall"]["p95"] = 1310
    spike_report["concurrent_recall"]["latency_ms"]["recall"]["p99"] = 1310

    paths = []
    for report in (dev_report, stress_report, spike_report):
        path = tmp_path / f"{report['suite_id']}.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        paths.append(path)
    output_path = tmp_path / "final.md"

    content = build_final_report(paths, output_path=output_path)

    assert "| 50 并发 recall / 100 请求 |" in content
    assert "`p95=795ms`, `p99=823ms`" in content
    assert "`p95=1310ms`, `p99=1310ms`" not in content


def test_final_pressure_report_renders_partial_preprod_gaps_precisely(tmp_path) -> None:
    dev_report = _final_report_fixture("p0-dev", passed=True)
    old_stress = _final_report_fixture("p0-stress-old", passed=True)
    old_stress["config"]["recall_concurrency"] = 50
    old_stress["config"]["recall_requests"] = 100
    old_stress["concurrent_recall"]["latency_ms"]["recall"]["p95"] = 920
    old_stress["concurrent_recall"]["latency_ms"]["recall"]["p99"] = 934
    latest_stress = _final_report_fixture("p0-stress-latest", passed=True)
    latest_stress["config"]["recall_concurrency"] = 50
    latest_stress["config"]["recall_requests"] = 100
    latest_stress["concurrent_recall"]["latency_ms"]["recall"]["p95"] = 795
    latest_stress["concurrent_recall"]["latency_ms"]["recall"]["p99"] = 823

    paths = []
    for report in (dev_report, old_stress, latest_stress):
        path = tmp_path / f"{report['suite_id']}.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        paths.append(path)
    output_path = tmp_path / "final.md"
    preprod_summary = {
        "run_id": "p0-preprod-summary",
        "requested_phases_passed": True,
        "production_precheck_passed": False,
        "executed_phases": ["baseline", "fault_injection", "soak_probe", "spike", "stress"],
        "missing_phases": ["soak", "representative_replay"],
        "phase_results": {
            "baseline": {
                "duration_gate_passed": False,
                "worst_recall_p95_ms": 153,
                "worst_recall_p99_ms": 162,
            },
            "fault_injection": {"worst_recall_p95_ms": 0, "worst_recall_p99_ms": 0},
            "soak_probe": {"worst_recall_p95_ms": 470, "worst_recall_p99_ms": 516},
            "spike": {
                "recovery_gate_passed": True,
                "worst_recall_p95_ms": 1310,
                "worst_recall_p99_ms": 1310,
            },
            "stress": {"worst_recall_p95_ms": 795, "worst_recall_p99_ms": 823},
        },
    }

    content = build_final_report(paths, output_path=output_path, preprod_summary=preprod_summary)

    assert "`recall p95=795ms`" in content
    assert "| Mem0/Qdrant/Postgres/Redis 故障注入 | 部分执行 | 已有故障注入报告，但仍有场景未通过或未覆盖。 |" in content
    assert "| 资源饱和观测 | 未执行 | CPU、内存、连接池、队列等待、Qdrant 请求耗时和外部限流尚未形成固定报告。 |" in content
    assert "| 10-15 分钟 baseline | 已执行短探针 | 已验证短时 10 并发 recall；尚未覆盖持续 10-15 分钟。 |" in content
    assert "不能替代 15-30 分钟 stress、100 并发 spike" not in content
    assert "补齐10-15 分钟持续 baseline" in content
    assert "10 分钟 P0 soak、代表性样本回放、Mem0/Qdrant/Postgres/Redis 故障注入、资源饱和观测" in content


def test_final_pressure_report_distinguishes_official_duration_from_probe(tmp_path) -> None:
    dev_report = _final_report_fixture("p0-dev", passed=True)
    report_path = tmp_path / "dev.json"
    output_path = tmp_path / "final.md"
    report_path.write_text(json.dumps(dev_report), encoding="utf-8")
    preprod_summary = {
        "run_id": "p0-preprod-summary",
        "requested_phases_passed": False,
        "production_precheck_passed": False,
        "executed_phases": ["stress", "spike"],
        "missing_phases": ["soak", "fault_injection", "representative_replay"],
        "phase_results": {
            "stress": {
                "passed": False,
                "duration_gate_passed": False,
                "failed_duration_reports": ["p0-duration-stress"],
                "worst_recall_p95_ms": 795,
                "worst_recall_p99_ms": 823,
            },
            "spike": {
                "passed": True,
                "recovery_gate_passed": True,
                "worst_recall_p95_ms": 1310,
                "worst_recall_p99_ms": 1310,
            },
        },
    }

    content = build_final_report([report_path], output_path=output_path, preprod_summary=preprod_summary)

    assert "| stress | p95=795ms, p99=823ms | 未通过 |" in content
    assert "| 15-30 分钟 50 并发 stress | 已执行短探针 | 已验证短时 50 并发 recall；尚未覆盖持续 15-30 分钟。 |" in content
    assert "| 100 并发 spike | 已执行完整恢复曲线 | 已验证 10 -> 100 -> 10 恢复曲线。 |" in content
    assert "不能替代 15-30 分钟 stress、100 并发 spike" not in content
    assert "完整 10 -> 100 -> 10 spike 恢复曲线" not in content


def test_final_pressure_report_marks_fault_and_replay_as_executed_when_preprod_passed(
    tmp_path,
) -> None:
    dev_report = _final_report_fixture("p0-dev", passed=True)
    report_path = tmp_path / "dev.json"
    output_path = tmp_path / "final.md"
    report_path.write_text(json.dumps(dev_report), encoding="utf-8")
    preprod_summary = {
        "run_id": "p0-preprod-summary",
        "requested_phases_passed": True,
        "production_precheck_passed": False,
        "executed_phases": ["fault_injection", "representative_replay"],
        "missing_phases": ["baseline", "stress", "spike", "soak"],
        "phase_results": {
            "fault_injection": {
                "passed": True,
                "worst_recall_p95_ms": 0,
                "worst_recall_p99_ms": 0,
            },
            "representative_replay": {
                "passed": True,
                "representative_probe_passed": True,
                "worst_recall_p95_ms": 44,
                "worst_recall_p99_ms": 53,
            },
        },
    }

    content = build_final_report([report_path], output_path=output_path, preprod_summary=preprod_summary)

    assert (
        "| Mem0/Qdrant/Postgres/Redis 故障注入 | 已执行 | "
        "已验证外部依赖异常下的可观测失败和恢复。 |"
    ) in content
    assert (
        "| 代表性样本回放 | 已执行 | "
        "已覆盖当前 P0 工程构造代表性样本；后续继续接入线上样本分布。 |"
    ) in content
    assert "| 资源饱和观测 | 未执行 |" in content
    assert "代表性样本回放、Mem0/Qdrant/Postgres/Redis 故障注入" not in content


def test_final_pressure_report_distinguishes_failed_soak_from_missing_soak(tmp_path) -> None:
    dev_report = _final_report_fixture("p0-dev", passed=True)
    report_path = tmp_path / "dev.json"
    report_path.write_text(json.dumps(dev_report), encoding="utf-8")
    output_path = tmp_path / "final.md"
    preprod_summary = {
        "run_id": "p0-preprod-summary",
        "executed_phases": ["soak"],
        "missing_phases": [],
        "phase_results": {
            "soak": {
                "passed": False,
                "duration_gate_passed": False,
                "failed_child_reports": ["p0-soak-iteration-191-failed"],
                "failed_duration_reports": ["p0-duration-soak-failed"],
                "worst_recall_p95_ms": 0,
                "worst_recall_p99_ms": 0,
            }
        },
    }

    content = build_final_report([report_path], output_path=output_path, preprod_summary=preprod_summary)

    assert (
        "| 10 分钟 P0 soak test | 已执行但未通过 | "
        "已执行 soak，但存在失败子报告或持续时间未满足门禁。 |"
    ) in content
    assert "| 10 分钟 P0 soak test | 未执行 |" not in content


def test_preprod_report_requires_all_production_precheck_phases() -> None:
    baseline_child = _final_report_fixture("p0-baseline-pass", passed=True)
    baseline_child["config"]["recall_concurrency"] = 10
    baseline_child["config"]["append_concurrency"] = 10
    baseline_child["config"]["recall_requests"] = 100
    baseline_child["concurrent_recall"]["latency_ms"]["recall"]["p95"] = 420
    baseline = build_duration_phase_report(
        run_id="p0-duration-baseline",
        phase="baseline",
        started_at="2026-05-08T00:00:00+00:00",
        ended_at="2026-05-08T00:10:00+00:00",
        target_duration_seconds=600,
        actual_duration_seconds=600,
        child_reports=[baseline_child],
    )

    report = build_preprod_report(
        run_id="preprod-run",
        started_at="2026-05-08T00:00:00+00:00",
        ended_at="2026-05-08T00:10:00+00:00",
        phase_reports={"baseline": [baseline]},
        required_phases=["baseline", "stress", "spike", "soak", "fault_injection", "representative_replay"],
    )

    assert report["requested_phases_passed"] is True
    assert report["production_precheck_passed"] is False
    assert report["missing_phases"] == [
        "stress",
        "spike",
        "soak",
        "fault_injection",
        "representative_replay",
    ]
    assert report["phase_results"]["baseline"]["worst_recall_p95_ms"] == 420
    assert report["phase_results"]["baseline"]["duration_gate_passed"] is True

    markdown = render_preprod_report_markdown(report)
    assert "生产前完整压测 | 未通过" in markdown
    assert "stress, spike, soak, fault_injection, representative_replay" in markdown
    assert "未满足正式生产阶段 | -" in markdown


def test_preprod_report_fails_requested_phase_when_child_report_fails() -> None:
    stress = _final_report_fixture("p0-stress-fail", passed=False)
    stress["config"]["recall_concurrency"] = 50
    stress["config"]["append_concurrency"] = 10
    stress["failed_sections"] = ["concurrent_recall"]
    stress["concurrent_recall"]["latency_ms"]["recall"]["p95"] = 2200

    report = build_preprod_report(
        run_id="preprod-run",
        started_at="2026-05-08T00:00:00+00:00",
        ended_at="2026-05-08T00:20:00+00:00",
        phase_reports={"stress": [stress]},
        required_phases=["stress"],
    )

    assert report["requested_phases_passed"] is False
    assert report["production_precheck_passed"] is False
    assert report["failed_phases"] == ["stress"]
    assert report["phase_results"]["stress"]["failed_child_reports"] == ["p0-stress-fail"]


def test_preprod_report_accepts_short_stress_as_requested_probe_not_full_precheck() -> None:
    stress = _final_report_fixture("p0-short-stress-pass", passed=True)
    stress["config"]["recall_concurrency"] = 50
    stress["config"]["append_concurrency"] = 10
    stress["config"]["recall_requests"] = 100
    stress["concurrent_recall"]["latency_ms"]["recall"]["p95"] = 925
    stress["concurrent_recall"]["latency_ms"]["recall"]["p99"] = 1008

    report = build_preprod_report(
        run_id="preprod-run",
        started_at="2026-05-08T00:00:00+00:00",
        ended_at="2026-05-08T00:20:00+00:00",
        phase_reports={"stress": [stress]},
        required_phases=["baseline", "stress", "spike", "soak", "fault_injection", "representative_replay"],
    )

    assert report["requested_phases_passed"] is True
    assert report["production_precheck_passed"] is False
    assert report["failed_phases"] == []
    assert report["missing_phases"] == ["baseline", "spike", "soak", "fault_injection", "representative_replay"]
    assert report["phase_results"]["stress"]["representative_probe_passed"] is True
    assert report["phase_results"]["stress"]["duration_gate_passed"] is False
    assert report["phase_results"]["stress"]["worst_recall_p95_ms"] == 925

    markdown = render_preprod_report_markdown(report)
    assert "失败阶段 | -" in markdown
    assert "未满足正式生产阶段 | stress" in markdown


def test_preprod_report_does_not_accept_under_duration_probe_as_official_phase() -> None:
    child = _final_report_fixture("p0-short-child", passed=True)
    probe = build_duration_phase_report(
        run_id="p0-duration-stress",
        phase="stress",
        started_at="2026-05-08T03:00:00+00:00",
        ended_at="2026-05-08T03:01:00+00:00",
        target_duration_seconds=900,
        actual_duration_seconds=60,
        child_reports=[child],
    )

    report = build_preprod_report(
        run_id="preprod-run",
        started_at="2026-05-08T03:00:00+00:00",
        ended_at="2026-05-08T03:01:00+00:00",
        phase_reports={"stress": [probe]},
        required_phases=["stress"],
    )

    assert report["requested_phases_passed"] is False
    assert report["production_precheck_passed"] is False
    assert report["failed_phases"] == ["stress"]
    assert report["phase_results"]["stress"]["duration_gate_passed"] is False


def test_preprod_report_marks_short_probe_as_requested_pass_but_not_official_precheck() -> None:
    stress_probe = _final_report_fixture("p0-short-stress", passed=True)
    stress_probe["config"]["recall_concurrency"] = 50
    spike_probe = _final_report_fixture("p0-short-spike", passed=True)
    spike_probe["config"]["recall_concurrency"] = 100

    report = build_preprod_report(
        run_id="preprod-run",
        started_at="2026-05-08T03:00:00+00:00",
        ended_at="2026-05-08T03:05:00+00:00",
        phase_reports={"stress": [stress_probe], "spike": [spike_probe]},
        required_phases=["stress", "spike"],
    )

    assert report["requested_phases_passed"] is True
    assert report["production_precheck_passed"] is False
    assert report["failed_phases"] == []
    assert report["failed_production_phases"] == ["stress", "spike"]
    assert report["phase_results"]["stress"]["duration_gate_passed"] is False
    assert report["phase_results"]["spike"]["recovery_gate_passed"] is False


def test_preprod_phase_command_maps_p0_metric_document_scenarios() -> None:
    baseline = _phase_command(
        "baseline",
        report_dir="docs/reports",
        python_executable=".venv/bin/python",
        script_path="script/real_mem0_p0_short_pressure.py",
    )
    stress = _phase_command(
        "stress",
        report_dir="docs/reports",
        python_executable=".venv/bin/python",
        script_path="script/real_mem0_p0_short_pressure.py",
    )
    spike = _phase_command(
        "spike",
        report_dir="docs/reports",
        python_executable=".venv/bin/python",
        script_path="script/real_mem0_p0_short_pressure.py",
    )
    soak_probe = _phase_command(
        "soak_probe",
        report_dir="docs/reports",
        python_executable=".venv/bin/python",
        script_path="script/real_mem0_p0_short_pressure.py",
    )
    soak = _phase_command(
        "soak",
        report_dir="docs/reports",
        python_executable=".venv/bin/python",
        script_path="script/real_mem0_p0_short_pressure.py",
    )

    assert baseline == [
        ".venv/bin/python",
        "script/real_mem0_p0_short_pressure.py",
        "--recall-concurrency",
        "10",
        "--recall-requests",
        "100",
        "--append-concurrency",
        "10",
        "--report-dir",
        "docs/reports",
    ]
    assert stress[stress.index("--recall-concurrency") + 1] == "50"
    assert stress[stress.index("--append-concurrency") + 1] == "10"
    assert stress[stress.index("--recall-requests") + 1] == "100"
    assert spike[spike.index("--recall-concurrency") + 1] == "100"
    assert soak_probe[soak_probe.index("--recall-concurrency") + 1] == "30"
    assert soak_probe[soak_probe.index("--append-concurrency") + 1] == "10"
    assert soak[soak.index("--recall-concurrency") + 1] == "30"
    assert soak[soak.index("--append-concurrency") + 1] == "10"
    assert soak[soak.index("--recall-requests") + 1] == "100"


def test_soak_iteration_interval_models_sustained_load_not_tight_loop() -> None:
    assert _soak_iteration_interval("baseline") == 0
    assert _soak_iteration_interval("stress") == 0
    assert _soak_iteration_interval("soak") == 300
    assert _soak_iteration_interval("soak", override_seconds=0) == 0
    assert _soak_iteration_interval("soak", override_seconds=60) == 60


def test_p0_soak_duration_gate_is_ten_minutes() -> None:
    assert _DURATION_PHASE_TARGET_SECONDS["soak"] == 600


def test_duration_phase_report_marks_under_duration_runs_as_probe() -> None:
    child = _final_report_fixture("p0-short-child", passed=True)
    child["config"]["recall_concurrency"] = 50
    child["config"]["append_concurrency"] = 10
    child["config"]["recall_requests"] = 100
    child["concurrent_recall"]["latency_ms"]["recall"]["p95"] = 780
    child["concurrent_recall"]["latency_ms"]["recall"]["p99"] = 820

    report = build_duration_phase_report(
        run_id="p0-duration-stress",
        phase="stress",
        started_at="2026-05-08T03:00:00+00:00",
        ended_at="2026-05-08T03:01:00+00:00",
        target_duration_seconds=900,
        actual_duration_seconds=60,
        child_reports=[child],
    )

    assert report["phase"] == "stress"
    assert report["passed"] is False
    assert report["duration_gate_passed"] is False
    assert report["child_gate_passed"] is True
    assert report["probe_only"] is True
    assert report["report_count"] == 1
    assert report["worst_recall_p95_ms"] == 780
    assert report["worst_recall_p99_ms"] == 820


def test_duration_phase_report_uses_official_phase_minimum_not_overridden_dry_run_target() -> None:
    child = _final_report_fixture("p0-short-child", passed=True)

    report = build_duration_phase_report(
        run_id="p0-duration-baseline",
        phase="baseline",
        started_at="2026-05-08T03:00:00+00:00",
        ended_at="2026-05-08T03:02:00+00:00",
        target_duration_seconds=1,
        actual_duration_seconds=120,
        child_reports=[child],
    )

    assert report["target_duration_seconds"] == 1
    assert report["official_min_duration_seconds"] == 600
    assert report["duration_gate_passed"] is False
    assert report["probe_only"] is True


def test_duration_phase_report_requires_child_reports_to_pass() -> None:
    child = _final_report_fixture("p0-short-child", passed=False)
    child["failed_sections"] = ["quality"]

    report = build_duration_phase_report(
        run_id="p0-duration-baseline",
        phase="baseline",
        started_at="2026-05-08T03:00:00+00:00",
        ended_at="2026-05-08T03:15:00+00:00",
        target_duration_seconds=900,
        actual_duration_seconds=900,
        child_reports=[child],
    )

    assert report["passed"] is False
    assert report["duration_gate_passed"] is True
    assert report["failed_child_reports"] == ["p0-short-child"]


def test_failed_duration_child_report_is_auditable() -> None:
    report = _failed_duration_child_report(
        phase="soak",
        iteration=191,
        command=["python", "script/real_mem0_p0_short_pressure.py"],
        error="returned non-zero exit status 1",
    )

    assert report["suite_id"] == "p0-soak-iteration-191-failed"
    assert report["passed"] is False
    assert report["failed_sections"] == ["duration_child_command"]
    assert report["duration_child_failure"]["phase"] == "soak"
    assert report["duration_child_failure"]["iteration"] == 191
    assert "non-zero" in report["duration_child_failure"]["error"]


def test_duration_child_command_captures_output_to_log_file(tmp_path) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    child_report = _final_report_fixture("p0-short-child", passed=True)
    (report_dir / "p0-short-child.json").write_text(json.dumps(child_report), encoding="utf-8")
    command = [
        ".venv/bin/python",
        "script/real_mem0_p0_short_pressure.py",
        "--report-dir",
        str(report_dir),
    ]

    report = _run_duration_child_command(
        phase="soak",
        iteration=7,
        command=command,
        report_dir=report_dir,
        before=set(),
        process_runner=lambda *_args, **_kwargs: type(
            "Completed",
            (),
            {"stdout": "child stdout", "stderr": "child stderr"},
        )(),
    )

    assert report["suite_id"] == "p0-short-child"
    assert report["duration_child_log_path"].endswith("p0-duration-soak-iteration-7.log")
    log_content = (tmp_path / report["duration_child_log_path"]).read_text(encoding="utf-8")
    assert "COMMAND:" in log_content
    assert "child stdout" in log_content
    assert "child stderr" in log_content


def test_cli_summary_omits_embedded_child_report_payload() -> None:
    child = _final_report_fixture("p0-short-child", passed=True)
    report = build_duration_phase_report(
        run_id="p0-duration-soak",
        phase="soak",
        started_at="2026-05-08T03:00:00+00:00",
        ended_at="2026-05-08T03:10:00+00:00",
        target_duration_seconds=600,
        actual_duration_seconds=600,
        child_reports=[child],
    )

    summary = _render_cli_summary(report)

    assert "p0-duration-soak" in summary
    assert "passed=True" in summary
    assert "duration_gate_passed=True" in summary
    assert "child_reports" not in summary
    assert "case_results" not in summary


def test_spike_phase_commands_model_recovery_curve() -> None:
    commands = _spike_phase_commands(
        report_dir="docs/reports",
        python_executable=".venv/bin/python",
        script_path="script/real_mem0_p0_short_pressure.py",
    )

    assert [command[command.index("--recall-concurrency") + 1] for command in commands] == ["10", "100", "10"]
    assert [command[command.index("--recall-requests") + 1] for command in commands] == ["100", "100", "100"]


def test_spike_phase_report_requires_recovery_leg_to_pass() -> None:
    warmup = _final_report_fixture("p0-spike-warmup", passed=True)
    peak = _final_report_fixture("p0-spike-peak", passed=True)
    recovery = _final_report_fixture("p0-spike-recovery", passed=False)
    recovery["failed_sections"] = ["concurrent_recall"]

    report = build_spike_phase_report(
        run_id="p0-spike-full",
        started_at="2026-05-08T03:00:00+00:00",
        ended_at="2026-05-08T03:05:00+00:00",
        leg_reports={"warmup_10": warmup, "peak_100": peak, "recovery_10": recovery},
    )

    assert report["phase"] == "spike"
    assert report["passed"] is False
    assert report["failed_legs"] == ["recovery_10"]
    assert report["recovery_gate_passed"] is False


def test_preprod_report_accepts_full_spike_report_as_official_spike() -> None:
    warmup = _final_report_fixture("p0-spike-warmup", passed=True)
    peak = _final_report_fixture("p0-spike-peak", passed=True)
    recovery = _final_report_fixture("p0-spike-recovery", passed=True)
    spike = build_spike_phase_report(
        run_id="p0-spike-full",
        started_at="2026-05-08T03:00:00+00:00",
        ended_at="2026-05-08T03:05:00+00:00",
        leg_reports={"warmup_10": warmup, "peak_100": peak, "recovery_10": recovery},
    )

    report = build_preprod_report(
        run_id="preprod-run",
        started_at="2026-05-08T03:00:00+00:00",
        ended_at="2026-05-08T03:05:00+00:00",
        phase_reports={"spike": [spike]},
        required_phases=["spike"],
    )

    assert report["requested_phases_passed"] is True
    assert report["production_precheck_passed"] is True
    assert report["phase_results"]["spike"]["recovery_gate_passed"] is True


def test_preprod_report_can_be_built_from_existing_phase_reports() -> None:
    baseline_child = _final_report_fixture("p0-baseline-child", passed=True)
    baseline = build_duration_phase_report(
        run_id="p0-duration-baseline",
        phase="baseline",
        started_at="2026-05-08T00:00:00+00:00",
        ended_at="2026-05-08T00:10:00+00:00",
        target_duration_seconds=600,
        actual_duration_seconds=600,
        child_reports=[baseline_child],
    )
    soak_probe = build_duration_phase_report(
        run_id="p0-duration-soak",
        phase="soak",
        started_at="2026-05-08T00:00:00+00:00",
        ended_at="2026-05-08T00:10:00+00:00",
        target_duration_seconds=600,
        actual_duration_seconds=600,
        child_reports=[_final_report_fixture("p0-soak-child", passed=True)],
    )
    replay = {
        "run_id": "p0-representative-replay",
        "phase": "representative_replay",
        "passed": True,
        "representative_probe_passed": True,
        "worst_recall_p95_ms": 35,
        "worst_recall_p99_ms": 36,
    }

    report = build_preprod_report_from_phase_reports(
        run_id="p0-preprod-summary",
        started_at="2026-05-08T00:00:00+00:00",
        ended_at="2026-05-08T00:12:00+00:00",
        phase_reports={
            "baseline": [baseline],
            "soak": [soak_probe],
            "representative_replay": [replay],
        },
        required_phases=["baseline", "soak", "representative_replay"],
    )

    assert report["executed_phases"] == ["baseline", "representative_replay", "soak"]
    assert report["requested_phases_passed"] is True
    assert report["production_precheck_passed"] is True
    assert report["failed_phases"] == []
    assert report["failed_production_phases"] == []
    assert report["phase_results"]["soak"]["duration_gate_passed"] is True
    assert report["phase_results"]["representative_replay"]["representative_probe_passed"] is True


def test_representative_replay_report_requires_quality_coverage_and_passed_suites() -> None:
    suite = _final_report_fixture("p0-replay-suite", passed=True)
    suite["quality"]["case_results"] = [
        {"name": "nickname-current", "category": "slot_conflict", "status": "passed"},
        {"name": "negative-bird", "category": "negative_control", "status": "passed"},
        {"name": "roleplay-cat", "category": "isolation", "status": "passed"},
    ]
    suite["post_delete"]["case_results"] = [
        {"name": "deleted-cat-not-recalled", "category": "delete_rebuild", "status": "passed"},
    ]

    report = build_representative_replay_report(
        run_id="p0-representative-replay",
        started_at="2026-05-08T04:00:00+00:00",
        ended_at="2026-05-08T04:02:00+00:00",
        suite_reports=[suite],
    )

    assert report["phase"] == "representative_replay"
    assert report["passed"] is True
    assert report["representative_probe_passed"] is True
    assert report["coverage"] == {
        "delete_rebuild": 1,
        "isolation": 1,
        "negative_control": 1,
        "slot_conflict": 1,
    }
    assert report["failed_categories"] == []
    markdown = render_representative_replay_markdown(report)
    assert "代表性样本回放 | 通过" in markdown
    assert "slot_conflict" in markdown


def test_representative_replay_report_fails_when_category_missing() -> None:
    suite = _final_report_fixture("p0-replay-suite", passed=True)
    suite["quality"]["case_results"] = [
        {"name": "nickname-current", "category": "slot_conflict", "status": "passed"},
    ]
    suite["post_delete"]["case_results"] = []

    report = build_representative_replay_report(
        run_id="p0-representative-replay",
        started_at="2026-05-08T04:00:00+00:00",
        ended_at="2026-05-08T04:02:00+00:00",
        suite_reports=[suite],
    )

    assert report["passed"] is False
    assert report["failed_categories"] == ["delete_rebuild", "isolation", "negative_control"]


def test_preprod_report_uses_run_id_for_failed_non_short_reports() -> None:
    suite = _final_report_fixture("p0-replay-suite", passed=True)
    suite["quality"]["case_results"] = [
        {"name": "nickname-current", "category": "slot_conflict", "status": "passed"},
    ]
    replay = build_representative_replay_report(
        run_id="p0-representative-replay",
        started_at="2026-05-08T04:00:00+00:00",
        ended_at="2026-05-08T04:02:00+00:00",
        suite_reports=[suite],
    )

    report = build_preprod_report(
        run_id="preprod-run",
        started_at="2026-05-08T04:00:00+00:00",
        ended_at="2026-05-08T04:02:00+00:00",
        phase_reports={"representative_replay": [replay]},
        required_phases=["representative_replay"],
    )

    assert report["failed_phases"] == ["representative_replay"]
    assert report["phase_results"]["representative_replay"]["failed_child_reports"] == [
        "p0-representative-replay"
    ]


def test_fault_injection_report_requires_injected_failure_and_recovery() -> None:
    report = build_fault_injection_report(
        run_id="fault-run",
        started_at="2026-05-08T02:00:00+00:00",
        ended_at="2026-05-08T02:02:00+00:00",
        scenarios=[
            {
                "name": "mem0_unavailable",
                "injected": True,
                "failure_observed": True,
                "recovered": True,
                "recovery_verified": True,
                "failure_detail": "readiness returned 503 for mem0",
                "recovery_detail": "readiness returned 200",
            },
            {
                "name": "qdrant_unavailable",
                "injected": True,
                "failure_observed": False,
                "recovered": True,
                "recovery_verified": True,
                "failure_detail": "readiness stayed ready",
                "recovery_detail": "readiness returned 200",
            },
        ],
    )

    assert report["passed"] is False
    assert report["failed_scenarios"] == ["qdrant_unavailable"]
    assert report["scenario_results"][0]["passed"] is True
    assert report["scenario_results"][1]["passed"] is False

    markdown = render_fault_injection_markdown(report)
    assert "qdrant_unavailable" in markdown
    assert "readiness stayed ready" in markdown


def test_fault_injection_evaluates_target_dependency_and_recovery() -> None:
    scenario = evaluate_readiness_fault_scenario(
        name="qdrant_unavailable",
        expected_dependency="qdrant",
        failure_status_code=503,
        failure_payload={
            "status": "not_ready",
            "dependencies": {
                "database": {"status": "ready", "detail": "ok"},
                "redis": {"status": "ready", "detail": "ok"},
                "qdrant": {"status": "not_ready", "detail": "connection refused"},
                "mem0": {"status": "not_ready", "detail": "qdrant unavailable"},
            },
        },
        recovery_status_code=200,
        recovery_payload={
            "status": "ready",
            "dependencies": {
                "database": {"status": "ready", "detail": "ok"},
                "redis": {"status": "ready", "detail": "ok"},
                "qdrant": {"status": "ready", "detail": "ok"},
                "mem0": {"status": "ready", "detail": "ok"},
            },
        },
    )

    assert scenario["injected"] is True
    assert scenario["failure_observed"] is True
    assert scenario["recovered"] is True
    assert scenario["recovery_verified"] is True
    assert "qdrant not_ready" in scenario["failure_detail"]


def test_fault_injection_scenario_specs_cover_mem0_library_dependencies() -> None:
    specs = build_library_fault_scenario_specs()

    assert [spec.name for spec in specs] == [
        "mem0_library_model_unavailable",
        "qdrant_unavailable",
        "postgres_unavailable",
        "redis_unavailable",
    ]
    assert {spec.expected_dependency for spec in specs} == {"mem0", "qdrant", "database", "redis"}
    assert specs[0].env_overrides["MEMORY_OPENAI_BASE_URL"].startswith("http://127.0.0.1:")


def test_fault_injection_wait_for_readiness_uses_probe_timeout(monkeypatch) -> None:
    observed_probe_timeouts: list[float] = []

    def fake_readiness(base_url: str, *, timeout_seconds: float) -> tuple[int, dict[str, object]]:
        observed_probe_timeouts.append(timeout_seconds)
        return 200, {"status": "ready", "dependencies": {}}

    monkeypatch.setattr("script.real_mem0_p0_fault_injection._readiness", fake_readiness)

    status_code, payload = _wait_for_readiness(
        "http://127.0.0.1:9999",
        expected_status_code=200,
        timeout_seconds=30,
        probe_timeout_seconds=12,
    )

    assert status_code == 200
    assert payload["status"] == "ready"
    assert observed_probe_timeouts == [12]


def test_fault_injection_api_process_keeps_explicit_environment_over_dotenv(
    monkeypatch,
    tmp_path,
) -> None:
    captured: dict[str, object] = {}

    def fake_popen(command, *, stdout, stderr, text, env):  # type: ignore[no-untyped-def]
        captured["command"] = command
        captured["env"] = env
        return object()

    monkeypatch.setenv("POSTGRES_DATABASE", "liaoriver_memory")
    monkeypatch.setenv("POSTGRES_PORT", "5432")
    monkeypatch.setenv("REDIS_PORT", "6379")
    monkeypatch.setenv("REDIS_PASSWORD", "redis-secret")
    monkeypatch.setattr(
        "script.real_mem0_p0_fault_injection.dotenv_values",
        lambda path: {
            "POSTGRES_DATABASE": "thinkback_real",
            "POSTGRES_PORT": "55432",
            "REDIS_PORT": "56379",
            "REDIS_PASSWORD": "",
            "QDRANT_URL": "http://qdrant-from-dotenv",
        },
    )
    monkeypatch.setattr("script.real_mem0_p0_fault_injection.subprocess.Popen", fake_popen)

    _start_api_process(
        port=18082,
        env_overrides={"QDRANT_URL": "http://127.0.0.1:1"},
        log_path=tmp_path / "api.log",
    )

    env = captured["env"]
    assert env["POSTGRES_DATABASE"] == "liaoriver_memory"  # type: ignore[index]
    assert env["POSTGRES_PORT"] == "5432"  # type: ignore[index]
    assert env["REDIS_PORT"] == "6379"  # type: ignore[index]
    assert env["REDIS_PASSWORD"] == "redis-secret"  # type: ignore[index]
    assert env["QDRANT_URL"] == "http://127.0.0.1:1"  # type: ignore[index]


def _final_report_fixture(suite_id: str, *, passed: bool) -> dict[str, object]:
    quality = {
        "passed": True,
        "case_pass_rate": 1.0,
        "recall_at_10": 1.0,
        "precision_at_10": 1.0,
        "conflict_pollution_rate": 0.0,
        "false_positive_rate": 0.0,
        "duplicate_active_rate": 0.0,
        "failed_cases": [],
        "latency_ms": {"recall": {"p95": 450, "p99": 450}},
    }
    return {
        "suite_id": suite_id,
        "passed": passed,
        "config": {"recall_concurrency": 10, "recall_requests": 20},
        "failed_sections": [] if passed else ["quality"],
        "quality": dict(quality),
        "concurrent_recall": {
            **dict(quality),
            "latency_ms": {"recall": {"p95": 300, "p99": 600}},
        },
        "post_delete": {
            "passed": True,
            "delete_residue_rate": 0.0,
            "rebuild_resurrection_rate": 0.0,
            "latency_ms": {"recall": {"p95": 400, "p99": 400}},
        },
    }
