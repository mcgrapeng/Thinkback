import json
from io import BytesIO
from urllib.error import HTTPError

from script import real_mem0_quality_regression
from script.build_p0_pressure_final_report import build_final_report
from script.real_mem0_p0_fault_injection import (
    build_fault_injection_report,
    render_fault_injection_markdown,
)
from script.real_mem0_p0_preprod_pressure import (
    _phase_command,
    _spike_phase_commands,
    build_duration_phase_report,
    build_preprod_report,
    build_spike_phase_report,
    render_preprod_report_markdown,
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
    _post_json,
    build_quality_report,
)


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


def test_quality_report_exposes_p0_stable_metric_fields() -> None:
    report = build_quality_report([], [], active_memory_count=0, active_after_rebuild=0)

    assert report["delete_memory_residue_rate"] == 0.0
    assert report["delete_session_residue_rate"] is None
    assert report["delete_all_residue_rate"] is None
    assert report["rebuild_resurrection_rate"] == 0.0
    assert report["cross_user_leak_rate"] is None
    assert report["cross_character_leak_rate"] == 0.0
    assert report["roleplay_real_mix_rate"] == 0.0
    assert report["dirty_summary_recall_rate"] is None
    assert report["source_ref_loss_rate"] is None
    assert report["critical_slot_pass_rate"] is None
    assert report["known_drift_regression_pass_rate"] is None
    assert report["http_5xx_rate"] is None
    assert report["timeout_rate"] is None
    assert report["idempotency_failure_rate"] is None
    assert report["transient_retry_rate"] == 0.0


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


def test_http_502_timeout_is_transient_for_quality_runner() -> None:
    exc = HTTPError(
        url="http://127.0.0.1:18082/memory/append",
        code=502,
        msg="Bad Gateway",
        hdrs={},
        fp=BytesIO(b'{"detail":"mem0 http POST /memories failed: timed out"}'),
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
        limitations=["未执行 6 小时 soak test"],
    )

    markdown = render_pressure_suite_markdown(report)

    assert "# Thinkback P0 压测报告" in markdown
    assert "| 整体结论 | 通过 |" in markdown
    assert "未执行 6 小时 soak test" in markdown


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
            "baseline": {"worst_recall_p95_ms": 153, "worst_recall_p99_ms": 162},
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
    assert "| Mem0/Qdrant/Postgres/Redis 故障注入 | 部分执行 | Mem0 unavailable 已验证；Qdrant/Postgres/Redis 故障注入未执行。 |" in content
    assert "不能替代 15-30 分钟 stress、100 并发 spike" not in content
    assert "补齐 baseline" not in content
    assert "补齐 6-24 小时 soak、代表性样本回放、Qdrant/Postgres/Redis 故障注入" in content


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


def test_preprod_report_does_not_accept_short_probe_as_official_duration_or_full_spike() -> None:
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

    assert report["requested_phases_passed"] is False
    assert report["failed_phases"] == ["stress", "spike"]
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
    assert report["passed"] is True
    assert report["duration_gate_passed"] is False
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
