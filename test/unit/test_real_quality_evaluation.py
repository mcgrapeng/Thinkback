import json
from io import BytesIO
from urllib.error import HTTPError

from script import real_mem0_quality_regression
from script.build_p0_pressure_final_report import build_final_report
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
