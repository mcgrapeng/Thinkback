"""Focused real-quality regression checks for Thinkback -> Mem0 -> Qdrant."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha1
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from dotenv import load_dotenv

from infra.config import Settings
from memory.repositories import MemoryIndexEntry, SqlAlchemyMemoryRepository


def _compact_identifier(*parts: object, max_length: int = 128) -> str:
    raw = re.sub(
        r"-+",
        "-",
        "-".join(
            re.sub(r"[^a-zA-Z0-9_-]", "-", str(part)).strip("-")
            for part in parts
            if str(part).strip()
        ),
    ).strip("-")
    if len(raw) <= max_length:
        return raw
    digest = sha1(raw.encode("utf-8")).hexdigest()[:8]
    suffix_budget = min(48, max_length - len(digest) - 2)
    prefix_budget = max_length - suffix_budget - len(digest) - 2
    return f"{raw[:prefix_budget]}-{digest}-{raw[-suffix_budget:]}"


@dataclass(frozen=True)
class QualityScope:
    run_id: str
    user_id: str
    character_id: str
    session_id: str

    @property
    def scope_suffix(self) -> str:
        raw = f"{self.character_id}-{self.session_id}"
        return re.sub(r"[^a-zA-Z0-9_-]", "-", raw)[-48:]

    def round_id(self, index: int) -> str:
        return _compact_identifier(self.run_id, self.scope_suffix, f"round-{index}", max_length=113)

    def request_id(self, label: str) -> str:
        return _compact_identifier(self.run_id, self.scope_suffix, label, max_length=128)

    def operation_id(self, label: str) -> str:
        # 中文注释：服务端任务 ID 会加 memory-delete/rebuild 前缀，operation_id 必须预留前缀空间。
        return _compact_identifier(self.run_id, self.scope_suffix, label, "op", max_length=113)

    def for_character(self, character_id: str, session_id: str) -> QualityScope:
        return QualityScope(
            run_id=self.run_id,
            user_id=self.user_id,
            character_id=character_id,
            session_id=session_id,
        )

    def for_user(self, user_id: str, session_id: str) -> QualityScope:
        return QualityScope(
            run_id=self.run_id,
            user_id=user_id,
            character_id=self.character_id,
            session_id=session_id,
        )


@dataclass(frozen=True)
class EvaluationCase:
    name: str
    category: str
    query: str
    expected_terms: list[str] | list[list[str]]
    forbidden_terms: list[str]
    intent: str = "memory_query"
    threshold: float = 0.5
    limit: int = 10
    metric_tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class QueryResult:
    case_name: str
    recalled_text: str
    l3_count: int = 0
    all_recalled_text: str = ""
    l3_contents: list[str] | None = None


@dataclass(frozen=True)
class MemorySnapshot:
    memory_text: str
    memory_status: str
    context_type: str = "real_user"
    fact_subject: str = "user"
    roleplay_mode: str = "off"


@dataclass
class LatencyMetrics:
    samples_by_operation: dict[str, list[float]] | None = None

    def __post_init__(self) -> None:
        if self.samples_by_operation is None:
            self.samples_by_operation = {
                "append": [],
                "recall": [],
                "delete": [],
                "rebuild": [],
            }

    def record(self, operation: str, elapsed_seconds: float) -> None:
        assert self.samples_by_operation is not None
        self.samples_by_operation.setdefault(operation, []).append(elapsed_seconds)

    def as_dict(self) -> dict[str, dict[str, int]]:
        assert self.samples_by_operation is not None
        operations = ("append", "recall", "delete", "rebuild")
        return {
            operation: self._percentiles(self.samples_by_operation.get(operation, []))
            for operation in operations
        }

    @staticmethod
    def _percentiles(samples: list[float]) -> dict[str, int]:
        if not samples:
            return {"p50": 0, "p95": 0, "p99": 0}
        ordered = sorted(samples)
        return {
            "p50": _nearest_rank_ms(ordered, 0.50),
            "p95": _nearest_rank_ms(ordered, 0.95),
            "p99": _nearest_rank_ms(ordered, 0.99),
        }


@dataclass
class RequestMetrics:
    request_count: int = 0
    retry_count: int = 0
    transient_failure_count: int = 0
    non_transient_failure_count: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "request_count": self.request_count,
            "retry_count": self.retry_count,
            "transient_failure_count": self.transient_failure_count,
            "non_transient_failure_count": self.non_transient_failure_count,
        }


def build_production_quality_cases() -> list[EvaluationCase]:
    # 中文注释：这组 case 只扩展首版主链路质量风险，不包含稳定性、并发或容量测试。
    # 重点覆盖真实用户容易出问题的几类语义风险：纠错后旧值污染、未知事实误召回、
    # 中英混写/转述表达漂移，以及跨用户/角色/现实剧情隔离。
    return [
        EvaluationCase(
            name="nickname-current",
            category="slot_conflict",
            query="现在应该怎么称呼用户？",
            intent="preference",
            expected_terms=["小鹏"],
            forbidden_terms=["阿鹏"],
        ),
        EvaluationCase(
            name="cat-current",
            category="slot_conflict",
            query="用户的猫叫什么？",
            expected_terms=["麻薯"],
            forbidden_terms=["团子", "露露", "泡芙"],
        ),
        EvaluationCase(
            name="location-current",
            category="slot_conflict",
            query="用户现在住在哪？",
            expected_terms=[["上海", "Shanghai"]],
            forbidden_terms=["杭州", "Hangzhou"],
        ),
        EvaluationCase(
            name="work-status-current",
            category="slot_conflict",
            query="用户现在的工作状态是什么？",
            expected_terms=["Moonshot"],
            forbidden_terms=["评估其它机会", "evaluating"],
        ),
        EvaluationCase(
            name="communication-current",
            category="slot_conflict",
            query="用户希望你怎么给建议？",
            intent="preference",
            expected_terms=[["简洁直接", "concise", "direct"]],
            forbidden_terms=["先安慰", "reassurance"],
        ),
        EvaluationCase(
            name="birthday-current",
            category="slot_conflict",
            query="用户生日是哪天？",
            expected_terms=[["6月1日", "June 1"]],
            forbidden_terms=["5月20日", "May 20"],
        ),
        EvaluationCase(
            name="drink-current",
            category="slot_conflict",
            query="用户现在喜欢喝什么？",
            intent="preference",
            expected_terms=[["茶", "tea"]],
            forbidden_terms=["咖啡", "coffee"],
        ),
        EvaluationCase(
            name="food-current",
            category="slot_conflict",
            query="用户现在喜欢吃什么？",
            intent="preference",
            expected_terms=[["寿司", "sushi"]],
            forbidden_terms=["汉堡", "hamburger", "burger"],
        ),
        EvaluationCase(
            name="dog-current",
            category="slot_conflict",
            query="用户的狗叫什么？",
            expected_terms=["豆包"],
            forbidden_terms=["麻薯", "团子", "露露", "泡芙"],
        ),
        EvaluationCase(
            name="sleep-reminder-current",
            category="slot_conflict",
            query="用户现在还讨厌被提醒睡觉吗？",
            intent="preference",
            expected_terms=[["温和", "gentle", "接受", "okay"]],
            forbidden_terms=["讨厌", "dislikes"],
        ),
        EvaluationCase(
            name="calendar-current",
            category="slot_conflict",
            query="用户偏好的会议时间是什么？",
            intent="preference",
            expected_terms=[["下午", "afternoon"]],
            forbidden_terms=["上午", "morning"],
        ),
        EvaluationCase(
            name="exercise-current",
            category="slot_conflict",
            query="用户现在喜欢什么运动？",
            intent="preference",
            expected_terms=[["游泳", "swimming"]],
            forbidden_terms=["跑步", "running"],
        ),
        EvaluationCase(
            name="negative-bird",
            category="negative_control",
            query="用户的鸟叫什么？",
            expected_terms=[],
            forbidden_terms=["麻薯", "团子", "豆包", "猫", "狗", "cat", "dog"],
        ),
        EvaluationCase(
            name="negative-rabbit",
            category="negative_control",
            query="用户的兔子叫什么？",
            expected_terms=[],
            forbidden_terms=["麻薯", "豆包", "猫", "狗", "cat", "dog"],
        ),
        EvaluationCase(
            name="negative-phone",
            category="negative_control",
            query="用户现在用什么手机？",
            expected_terms=[],
            forbidden_terms=["iPhone", "Android", "手机", "phone"],
        ),
        EvaluationCase(
            name="negative-never-shared-phone",
            category="negative_control",
            query="用户从未说过的手机号是多少？",
            expected_terms=[],
            forbidden_terms=["手机号", "phone number", "138", "139"],
        ),
        EvaluationCase(
            name="negative-family",
            category="negative_control",
            query="用户的姐姐叫什么？",
            expected_terms=[],
            forbidden_terms=["姐姐", "sister", "家人", "family"],
        ),
        EvaluationCase(
            name="negative-company-location",
            category="negative_control",
            query="用户公司的办公室在哪一层？",
            expected_terms=[],
            forbidden_terms=["楼", "floor", "office"],
        ),
        EvaluationCase(
            name="negative-roleplay-cat",
            category="isolation",
            query="用户现实里的猫叫什么？",
            expected_terms=["麻薯"],
            forbidden_terms=["露露"],
            metric_tags=("roleplay_real_mix_rate",),
        ),
        EvaluationCase(
            name="roleplay-cat",
            category="isolation",
            query="剧情设定里的猫叫什么？",
            expected_terms=["露露"],
            forbidden_terms=["麻薯", "团子"],
            metric_tags=("roleplay_real_mix_rate",),
        ),
        EvaluationCase(
            name="roleplay-place",
            category="isolation",
            query="剧情设定里的据点在哪？",
            expected_terms=[["海边灯塔", "lighthouse"]],
            forbidden_terms=["上海", "杭州"],
            metric_tags=("roleplay_real_mix_rate",),
        ),
        EvaluationCase(
            name="negative-cross-user-cat",
            category="isolation",
            query="用户的猫叫什么？",
            expected_terms=["麻薯"],
            forbidden_terms=["奶盖"],
            metric_tags=("cross_user_leak_rate",),
        ),
        EvaluationCase(
            name="negative-cross-user-drink",
            category="isolation",
            query="用户喜欢喝什么？",
            expected_terms=[["茶", "tea"]],
            forbidden_terms=["可乐", "cola"],
            metric_tags=("cross_user_leak_rate",),
        ),
        EvaluationCase(
            name="negative-cross-character-cat",
            category="isolation",
            query="用户的猫叫什么？",
            expected_terms=["麻薯"],
            forbidden_terms=["泡芙"],
            metric_tags=("cross_character_leak_rate",),
        ),
        EvaluationCase(
            name="negative-cross-character-food",
            category="isolation",
            query="用户喜欢吃什么？",
            expected_terms=[["寿司", "sushi"]],
            forbidden_terms=["披萨", "pizza"],
            metric_tags=("cross_character_leak_rate",),
        ),
        EvaluationCase(
            name="cat-current-english-paraphrase",
            category="expression_drift",
            query="What is the user's cat called now?",
            expected_terms=["麻薯"],
            forbidden_terms=["团子", "露露", "泡芙"],
            metric_tags=("known_drift_regression_pass_rate",),
        ),
        EvaluationCase(
            name="nickname-current-paraphrase",
            category="expression_drift",
            query="How should I address the user?",
            intent="preference",
            expected_terms=["小鹏"],
            forbidden_terms=["阿鹏"],
            metric_tags=("known_drift_regression_pass_rate",),
        ),
        EvaluationCase(
            name="location-current-english-paraphrase",
            category="expression_drift",
            query="Where does the user live now?",
            expected_terms=[["上海", "Shanghai"]],
            forbidden_terms=["杭州", "Hangzhou"],
            metric_tags=("known_drift_regression_pass_rate",),
        ),
        EvaluationCase(
            name="drink-current-english-paraphrase",
            category="expression_drift",
            query="What drink does the user currently prefer?",
            intent="preference",
            expected_terms=[["茶", "tea"]],
            forbidden_terms=["咖啡", "coffee"],
            metric_tags=("known_drift_regression_pass_rate",),
        ),
        EvaluationCase(
            name="communication-current-paraphrase",
            category="expression_drift",
            query="用户现在希望建议是直接一点还是先安慰？",
            intent="preference",
            expected_terms=[["简洁直接", "concise", "direct"]],
            forbidden_terms=["先安慰", "reassurance"],
            metric_tags=("known_drift_regression_pass_rate",),
        ),
        EvaluationCase(
            name="birthday-current-english-paraphrase",
            category="expression_drift",
            query="What date is the user's birthday?",
            expected_terms=[["6月1日", "June 1"]],
            forbidden_terms=["5月20日", "May 20"],
            metric_tags=("known_drift_regression_pass_rate",),
        ),
    ]


def build_post_delete_quality_cases() -> list[EvaluationCase]:
    # 中文注释：生命周期样本单独放在这里，便于报告区分单条删除、session 删除、
    # all 删除和 rebuild 后复活四类风险。
    return [
        EvaluationCase(
            name="deleted-cat-after-delete",
            category="delete_memory",
            query="用户的猫叫什么？",
            expected_terms=[],
            forbidden_terms=["麻薯", "团子", "露露", "泡芙", "猫", "cat"],
            metric_tags=("delete_memory_residue_rate",),
        ),
        EvaluationCase(
            name="deleted-cat-after-rebuild",
            category="rebuild",
            query="用户的猫叫什么？",
            expected_terms=[],
            forbidden_terms=["麻薯", "团子", "露露", "泡芙", "猫", "cat"],
            metric_tags=("rebuild_resurrection_rate",),
        ),
        EvaluationCase(
            name="nickname-after-rebuild",
            category="rebuild",
            query="现在应该怎么称呼用户？",
            intent="preference",
            expected_terms=["小鹏"],
            forbidden_terms=["阿鹏"],
        ),
        EvaluationCase(
            name="deleted-session-drink-after-delete",
            category="delete_session",
            query="用户喜欢喝什么？",
            expected_terms=[],
            forbidden_terms=["乌龙茶", "oolong", "茶"],
            metric_tags=("delete_session_residue_rate",),
        ),
        EvaluationCase(
            name="deleted-session-food-after-delete",
            category="delete_session",
            query="用户喜欢吃什么夜宵？",
            expected_terms=[],
            forbidden_terms=["拉面", "ramen", "夜宵"],
            metric_tags=("delete_session_residue_rate",),
        ),
        EvaluationCase(
            name="deleted-session-drink-after-rebuild",
            category="rebuild",
            query="用户喜欢喝什么？",
            expected_terms=[],
            forbidden_terms=["乌龙茶", "oolong", "茶"],
            metric_tags=("rebuild_resurrection_rate",),
        ),
        EvaluationCase(
            name="deleted-all-nickname-after-delete",
            category="delete_all",
            query="现在应该怎么称呼用户？",
            expected_terms=[],
            forbidden_terms=["小鹏", "阿鹏"],
            metric_tags=("delete_all_residue_rate",),
        ),
        EvaluationCase(
            name="deleted-all-dog-after-delete",
            category="delete_all",
            query="用户的狗叫什么？",
            expected_terms=[],
            forbidden_terms=["豆包", "狗", "dog"],
            metric_tags=("delete_all_residue_rate",),
        ),
        EvaluationCase(
            name="deleted-all-nickname-after-rebuild",
            category="rebuild",
            query="现在应该怎么称呼用户？",
            expected_terms=[],
            forbidden_terms=["小鹏", "阿鹏"],
            metric_tags=("rebuild_resurrection_rate",),
        ),
    ]


def build_quality_report(
    cases: list[EvaluationCase],
    results: list[QueryResult],
    *,
    active_memory_count: int,
    active_after_rebuild: int,
    request_metrics: RequestMetrics | None = None,
    latency_metrics: LatencyMetrics | None = None,
    active_memory_snapshots: list[MemorySnapshot] | None = None,
) -> dict[str, Any]:
    results_by_name = {result.case_name: result for result in results}
    failed_cases: list[str] = []
    case_results: list[dict[str, Any]] = []
    category_totals: dict[str, dict[str, int]] = {}
    recalled_cases = 0
    precise_cases = 0
    false_positive_cases = 0
    conflict_polluted_cases = 0
    negative_cases = 0
    relevant_item_count = 0
    total_l3_item_count = 0
    top1_hits = 0
    reciprocal_rank_sum = 0.0
    case_passed_by_name: dict[str, bool] = {}

    for case in cases:
        result = results_by_name.get(case.name, QueryResult(case_name=case.name, recalled_text=""))
        recalled_text = result.recalled_text.lower()
        expected_hit = _matches_expected_terms(recalled_text, case.expected_terms)
        forbidden_hit = any(term.lower() in recalled_text for term in case.forbidden_terms)
        is_negative = not case.expected_terms
        l3_contents = result.l3_contents if result.l3_contents is not None else (
            result.recalled_text.splitlines() if result.recalled_text else []
        )
        total_l3_item_count += len(l3_contents)
        relevant_indexes = [
            index
            for index, content in enumerate(l3_contents)
            if _matches_expected_terms(content.lower(), case.expected_terms)
            and not any(term.lower() in content.lower() for term in case.forbidden_terms)
        ]
        relevant_item_count += len(relevant_indexes)
        if relevant_indexes:
            if relevant_indexes[0] == 0:
                top1_hits += 1
            reciprocal_rank_sum += 1 / (relevant_indexes[0] + 1)
        category = case.category
        category_totals.setdefault(category, {"case_count": 0, "passed_count": 0})
        category_totals[category]["case_count"] += 1
        case_passed = False
        failure_reason: str | None = None
        if is_negative:
            negative_cases += 1
            if forbidden_hit:
                false_positive_cases += 1
                failure_reason = "false_positive"
                failed_cases.append(case.name)
            else:
                case_passed = True
                recalled_cases += 1
                precise_cases += 1
        else:
            if expected_hit and not forbidden_hit:
                case_passed = True
                recalled_cases += 1
                precise_cases += 1
            if forbidden_hit:
                conflict_polluted_cases += 1
            if not expected_hit or forbidden_hit:
                failure_reason = "missing_expected" if not expected_hit else "conflict_pollution"
                failed_cases.append(case.name)
        if case_passed:
            category_totals[category]["passed_count"] += 1
        case_passed_by_name[case.name] = case_passed
        case_results.append(
            {
                "name": case.name,
                "category": case.category,
                "expected_hit": expected_hit,
                "forbidden_hit": forbidden_hit,
                "negative": is_negative,
                "failure_reason": failure_reason,
                "l3_count": result.l3_count,
                "status": "passed" if case_passed else "failed",
            }
        )

    category_metrics = {
        category: {
            **totals,
            "pass_rate": totals["passed_count"] / max(1, totals["case_count"]),
        }
        for category, totals in category_totals.items()
        if totals["case_count"] > 0
    }

    case_count = max(1, len(cases))
    recall_at_10 = recalled_cases / case_count
    precision_at_10 = precise_cases / case_count
    case_pass_rate = (case_count - len(failed_cases)) / case_count
    positive_case_count = len(cases) - negative_cases
    item_precision_at_10 = relevant_item_count / max(1, total_l3_item_count)
    top1_hit_rate = top1_hits / max(1, positive_case_count)
    mrr = reciprocal_rank_sum / max(1, positive_case_count)
    irrelevant_l3_per_query = (total_l3_item_count - relevant_item_count) / case_count
    false_positive_rate = false_positive_cases / max(1, negative_cases)
    conflict_pollution_rate = conflict_polluted_cases / case_count
    duplicate_active_rate = _duplicate_active_rate(active_memory_snapshots or [])
    delete_memory_residue_rate = _tagged_forbidden_hit_rate(
        cases,
        results_by_name,
        "delete_memory_residue_rate",
    )
    delete_session_residue_rate = _tagged_forbidden_hit_rate(
        cases,
        results_by_name,
        "delete_session_residue_rate",
    )
    delete_all_residue_rate = _tagged_forbidden_hit_rate(
        cases,
        results_by_name,
        "delete_all_residue_rate",
    )
    rebuild_resurrection_rate = _tagged_forbidden_hit_rate(
        cases,
        results_by_name,
        "rebuild_resurrection_rate",
    )
    cross_user_leak_rate = _tagged_forbidden_hit_rate(
        cases,
        results_by_name,
        "cross_user_leak_rate",
    )
    cross_character_leak_rate = _tagged_forbidden_hit_rate(
        cases,
        results_by_name,
        "cross_character_leak_rate",
    )
    roleplay_real_mix_rate = _tagged_forbidden_hit_rate(
        cases,
        results_by_name,
        "roleplay_real_mix_rate",
    )
    known_drift_regression_pass_rate = _tagged_case_pass_rate(
        cases,
        case_passed_by_name,
        "known_drift_regression_pass_rate",
    )
    request_metrics_dict = (request_metrics or RequestMetrics()).as_dict()
    transient_retry_rate = request_metrics_dict["transient_failure_count"] / max(1, request_metrics_dict["request_count"])
    report = {
        "case_count": len(cases),
        "positive_case_count": positive_case_count,
        "negative_case_count": negative_cases,
        "case_pass_rate": case_pass_rate,
        "recall_at_10": recall_at_10,
        "precision_at_10": precision_at_10,
        "item_precision_at_10": item_precision_at_10,
        "top1_hit_rate": top1_hit_rate,
        "mrr": mrr,
        "irrelevant_l3_per_query": irrelevant_l3_per_query,
        "false_positive_rate": false_positive_rate,
        "conflict_pollution_rate": conflict_pollution_rate,
        "critical_slot_pass_rate": None,
        "delete_memory_residue_rate": delete_memory_residue_rate,
        "delete_session_residue_rate": delete_session_residue_rate,
        "delete_all_residue_rate": delete_all_residue_rate,
        "rebuild_resurrection_rate": rebuild_resurrection_rate,
        "cross_user_leak_rate": cross_user_leak_rate,
        "cross_character_leak_rate": cross_character_leak_rate,
        "roleplay_real_mix_rate": roleplay_real_mix_rate,
        "dirty_summary_recall_rate": None,
        "source_ref_loss_rate": None,
        "known_drift_regression_pass_rate": known_drift_regression_pass_rate,
        "http_5xx_rate": None,
        "timeout_rate": None,
        "idempotency_failure_rate": None,
        "transient_retry_rate": transient_retry_rate,
        # Legacy aliases kept temporarily for old dashboards and historical report builders.
        "delete_residue_rate": 0.0,
        "cross_scope_leak_rate": 0.0,
        "duplicate_active_rate": duplicate_active_rate,
        "active_memory_count": active_memory_count,
        "active_after_rebuild": active_after_rebuild,
        "failed_cases": failed_cases,
        "case_results": case_results,
        "category_metrics": category_metrics,
        "request_metrics": request_metrics_dict,
        "latency_ms": (latency_metrics or LatencyMetrics()).as_dict(),
        "metric_automation_status": _metric_automation_status(),
    }
    failed_metrics = _failed_metric_gates(report)
    report["failed_metrics"] = failed_metrics
    report["passed"] = not failed_cases and not failed_metrics
    return report


def _metric_automation_status() -> dict[str, str]:
    return {
        "case_pass_rate": "automatic",
        "recall_at_10": "automatic",
        "precision_at_10": "automatic",
        "item_precision_at_10": "automatic",
        "top1_hit_rate": "automatic",
        "mrr": "automatic",
        "irrelevant_l3_per_query": "automatic",
        "conflict_pollution_rate": "automatic",
        "false_positive_rate": "automatic",
        "duplicate_active_rate": "automatic",
        "delete_memory_residue_rate": "case_gate",
        "delete_session_residue_rate": "case_gate",
        "delete_all_residue_rate": "case_gate",
        "rebuild_resurrection_rate": "case_gate",
        "cross_user_leak_rate": "case_gate",
        "cross_character_leak_rate": "case_gate",
        "roleplay_real_mix_rate": "case_gate",
        "known_drift_regression_pass_rate": "automatic",
    }


def _nearest_rank_ms(ordered_samples: list[float], percentile: float) -> int:
    index = max(0, min(len(ordered_samples) - 1, int(len(ordered_samples) * percentile + 0.999999) - 1))
    return int(round(ordered_samples[index] * 1000))


def _duplicate_active_rate(active_memory_snapshots: list[MemorySnapshot]) -> float:
    active = [
        snapshot
        for snapshot in active_memory_snapshots
        if snapshot.memory_status.lower() == "active"
    ]
    if not active:
        return 0.0
    slot_counts: dict[str, int] = {}
    for snapshot in active:
        slot = _memory_conflict_slot_for_report(snapshot.memory_text)
        if slot is None:
            continue
        slot_key = "|".join(
            (
                slot,
                _normalized_partition_value(snapshot.context_type, "real_user"),
                _normalized_partition_value(snapshot.fact_subject, "user"),
                _normalized_partition_value(snapshot.roleplay_mode, "off"),
            )
        )
        slot_counts[slot_key] = slot_counts.get(slot_key, 0) + 1
    duplicate_count = sum(count - 1 for count in slot_counts.values() if count > 1)
    return duplicate_count / len(active)


def _normalized_partition_value(value: str, default: str) -> str:
    return (value or default).strip().lower()


def _failed_metric_gates(report: dict[str, Any]) -> list[dict[str, Any]]:
    gates: tuple[tuple[str, str, float], ...] = (
        ("case_pass_rate", "==", 1.0),
        ("recall_at_10", ">=", 0.95),
        ("precision_at_10", ">=", 0.95),
        ("false_positive_rate", "<=", 0.02),
        ("conflict_pollution_rate", "<=", 0.02),
        ("duplicate_active_rate", "==", 0.0),
        ("delete_memory_residue_rate", "<=", 0.0),
        ("delete_session_residue_rate", "<=", 0.0),
        ("delete_all_residue_rate", "<=", 0.0),
        ("rebuild_resurrection_rate", "<=", 0.0),
        ("cross_user_leak_rate", "<=", 0.0),
        ("cross_character_leak_rate", "<=", 0.0),
        ("roleplay_real_mix_rate", "<=", 0.0),
        ("known_drift_regression_pass_rate", ">=", 0.95),
    )
    failed: list[dict[str, Any]] = []
    for metric, operator, expected in gates:
        if report.get(metric) is None:
            continue
        actual = float(report[metric])
        if operator == ">=" and actual < expected:
            failed.append({"metric": metric, "actual": actual, "expected": f">= {expected:g}"})
        elif operator == "<=" and actual > expected:
            failed.append({"metric": metric, "actual": actual, "expected": f"<= {expected:g}"})
        elif operator == "==" and actual != expected:
            failed.append({"metric": metric, "actual": actual, "expected": f"== {expected:g}"})
    return failed


def _tagged_forbidden_hit_rate(
    cases: list[EvaluationCase],
    results_by_name: dict[str, QueryResult],
    metric_tag: str,
) -> float | None:
    tagged_cases = [case for case in cases if metric_tag in case.metric_tags]
    if not tagged_cases:
        return None
    leaked_count = 0
    for case in tagged_cases:
        result = results_by_name.get(case.name, QueryResult(case_name=case.name, recalled_text=""))
        l3_text = "\n".join(result.l3_contents or [result.recalled_text]).lower()
        if any(term.lower() in l3_text for term in case.forbidden_terms):
            leaked_count += 1
    return leaked_count / len(tagged_cases)


def _tagged_case_pass_rate(
    cases: list[EvaluationCase],
    case_passed_by_name: dict[str, bool],
    metric_tag: str,
) -> float | None:
    tagged_cases = [case for case in cases if metric_tag in case.metric_tags]
    if not tagged_cases:
        return None
    passed_count = sum(1 for case in tagged_cases if case_passed_by_name.get(case.name, False))
    return passed_count / len(tagged_cases)


def _memory_conflict_slot_for_report(memory_text: str) -> str | None:
    lowered = memory_text.lower()
    if any(marker in lowered for marker in ("cat named", "猫")):
        return "pet_name:cat"
    if any(marker in lowered for marker in ("dog named", "狗")):
        return "pet_name:dog"
    if any(marker in lowered for marker in ("bird named", "parrot named", "鸟", "鹦鹉")):
        return "pet_name:bird"
    if any(marker in lowered for marker in ("rabbit named", "bunny named", "兔")):
        return "pet_name:rabbit"
    if any(marker in lowered for marker in ("called", "preferred name", "addressed as", "称呼")):
        return "preferred_nickname"
    if any(marker in lowered for marker in ("lives in", "住", "搬")):
        return "current_location"
    if any(marker in lowered for marker in ("work status", "offer", "job", "工作状态")):
        return "current_work_status"
    if any(marker in lowered for marker in ("communication preference", "建议", "沟通")):
        return "communication_preference"
    if any(marker in lowered for marker in ("birthday", "生日")):
        return "birthday"
    if any(marker in lowered for marker in ("favorite drink", "drink preference", "饮品", "饮料")):
        return "favorite:drink"
    if any(marker in lowered for marker in ("favorite food", "food preference", "食物")):
        return "favorite:food"
    if any(marker in lowered for marker in ("meeting time preference", "会议时间", "开会时间")):
        return "meeting_time_preference"
    if any(marker in lowered for marker in ("exercise preference", "运动偏好", "喜欢什么运动")):
        return "exercise_preference"
    if any(marker in lowered for marker in ("story-world base location", "剧情设定里的据点", "据点")):
        return "story_world_base_location"
    if any(marker in lowered for marker in ("sleep reminder", "提醒睡觉", "睡眠提醒")):
        return "sleep_reminder_preference"
    return None


def _matches_expected_terms(recalled_text: str, expected_terms: list[str] | list[list[str]]) -> bool:
    if not expected_terms:
        return False
    for expected in expected_terms:
        if isinstance(expected, list):
            if not any(term.lower() in recalled_text for term in expected):
                return False
        elif expected.lower() not in recalled_text:
            return False
    return True


def _new_scope() -> QualityScope:
    run_id = f"real-quality-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}"
    normalized = re.sub(r"[^a-zA-Z0-9_-]", "-", run_id)
    return QualityScope(
        run_id=normalized,
        user_id=f"{normalized}-user",
        character_id=f"{normalized}-character",
        session_id=f"{normalized}-session",
    )


def _is_transient_http_failure(exc: HTTPError) -> bool:
    if exc.code not in {502, 503, 504}:
        return False
    detail = exc.read().decode("utf-8", errors="replace")
    reason = getattr(exc, "reason", "") or getattr(exc, "msg", "")
    # 中文注释：有些 FastAPI/网关 502 只把 Bad Gateway 放在 HTTP reason，
    # 响应体未必包含 timeout/provider 等字样；质量 runner 应把这类上游瞬时失败纳入重试。
    lowered = f"{detail}\n{reason}".lower()
    transient_markers = (
        "timed out",
        "timeout",
        "remote end closed connection",
        "provider",
        "temporarily unavailable",
        "bad gateway",
        "service unavailable",
        "gateway timeout",
    )
    return any(marker in lowered for marker in transient_markers)


def _is_transient_network_failure(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, ConnectionRefusedError):
        return True
    lowered = str(reason).lower()
    return any(
        marker in lowered
        for marker in (
            "timed out",
            "timeout",
            "temporarily unavailable",
            "connection refused",
        )
    )


def _post_json(
    base_url: str,
    path: str,
    payload: dict[str, Any],
    *,
    request_metrics: RequestMetrics | None = None,
    latency_metrics: LatencyMetrics | None = None,
    operation: str | None = None,
    max_attempts: int = 3,
    sleep_seconds: float = 1.0,
) -> dict[str, Any]:
    if request_metrics:
        request_metrics.request_count += 1
    request = Request(
        f"{base_url.rstrip('/')}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    for attempt in range(1, max_attempts + 1):
        started_at = time.perf_counter()
        try:
            with urlopen(request, timeout=180) as response:
                body = response.read().decode("utf-8")
            if latency_metrics and operation:
                latency_metrics.record(operation, time.perf_counter() - started_at)
            return json.loads(body) if body else {}
        except HTTPError as exc:
            if latency_metrics and operation:
                latency_metrics.record(operation, time.perf_counter() - started_at)
            transient = _is_transient_http_failure(exc)
            if transient and attempt < max_attempts:
                if request_metrics:
                    request_metrics.retry_count += 1
                    request_metrics.transient_failure_count += 1
                time.sleep(sleep_seconds)
                continue
            if request_metrics:
                if transient:
                    request_metrics.transient_failure_count += 1
                else:
                    request_metrics.non_transient_failure_count += 1
            detail = "transient HTTP failure" if transient else exc.reason
            raise RuntimeError(f"HTTP {exc.code} from {path}: {detail}") from exc
        except (TimeoutError, URLError, OSError) as exc:
            if latency_metrics and operation:
                latency_metrics.record(operation, time.perf_counter() - started_at)
            transient = _is_transient_network_failure(exc)
            if transient and attempt < max_attempts:
                if request_metrics:
                    request_metrics.retry_count += 1
                    request_metrics.transient_failure_count += 1
                time.sleep(sleep_seconds)
                continue
            if request_metrics:
                if transient:
                    request_metrics.transient_failure_count += 1
                else:
                    request_metrics.non_transient_failure_count += 1
            detail = "transient network failure" if transient else str(exc)
            raise RuntimeError(f"network failure from {path}: {detail}") from exc
    raise RuntimeError(f"HTTP request attempts exhausted for {path}")


def _append_round(
    base_url: str,
    scope: QualityScope,
    index: int,
    user_text: str,
    *,
    metadata: dict[str, Any] | None = None,
    request_metrics: RequestMetrics | None = None,
    latency_metrics: LatencyMetrics | None = None,
) -> None:
    timestamp = datetime(2026, 5, 6, 10, index, tzinfo=UTC)
    response = _post_json(
        base_url,
        "/memory/append",
        {
            "request_id": scope.request_id(f"append-{index}"),
            "user_id": scope.user_id,
            "character_id": scope.character_id,
            "session_id": scope.session_id,
            "round_id": scope.round_id(index),
            "round_index": index,
            "messages": [
                {
                    "message_id": f"{scope.run_id}-{index}-user",
                    "role": "user",
                    "content": user_text,
                    "timestamp": timestamp.isoformat(),
                },
                {
                    "message_id": f"{scope.run_id}-{index}-assistant",
                    "role": "assistant",
                    "content": "我会记住这个信息，并按当前事实更新后续称呼。",
                    "timestamp": (timestamp + timedelta(seconds=3)).isoformat(),
                },
            ],
            "source_timestamp": (timestamp + timedelta(seconds=3)).isoformat(),
            "metadata": metadata or {},
        },
        request_metrics=request_metrics,
        latency_metrics=latency_metrics,
        operation="append",
    )
    if response.get("status") not in {"completed", "already_done"}:
        raise RuntimeError(f"append round {index} failed: {response}")


def _recall(
    base_url: str,
    scope: QualityScope,
    query: str,
    *,
    intent: str = "memory_query",
    threshold: float = 0.5,
    limit: int = 10,
    request_metrics: RequestMetrics | None = None,
    latency_metrics: LatencyMetrics | None = None,
) -> list[dict[str, Any]]:
    response = _post_json(
        base_url,
        "/memory/recall",
        {
            "user_id": scope.user_id,
            "character_id": scope.character_id,
            "session_id": scope.session_id,
            "query": query,
            "intent": intent,
            "l3_limit": limit,
            "l3_score_threshold": threshold,
            "token_budget": 4000,
        },
        request_metrics=request_metrics,
        latency_metrics=latency_metrics,
        operation="recall",
    )
    return list(response.get("items", []))


def _l3_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in items if item.get("layer") == "L3"]


def _query_result(
    base_url: str,
    scope: QualityScope,
    case: EvaluationCase,
    *,
    request_metrics: RequestMetrics | None = None,
    latency_metrics: LatencyMetrics | None = None,
) -> QueryResult:
    items = _recall(
        base_url,
        scope,
        case.query,
        intent=case.intent,
        threshold=case.threshold,
        limit=case.limit,
        request_metrics=request_metrics,
        latency_metrics=latency_metrics,
    )
    l3_items = _l3_items(items)
    l3_contents = [str(item.get("content", "")) for item in l3_items]
    return QueryResult(
        case_name=case.name,
        recalled_text="\n".join(l3_contents),
        l3_count=len(l3_items),
        all_recalled_text="\n".join(str(item.get("content", "")) for item in items),
        l3_contents=l3_contents,
    )


def _active_memories(
    repository: SqlAlchemyMemoryRepository, scope: QualityScope
) -> list[MemoryIndexEntry]:
    memories: list[MemoryIndexEntry] = repository.active_memories(scope.user_id, scope.character_id)
    return memories


def _find_active_memory(
    active_memories: list[MemoryIndexEntry],
    *,
    required_terms: tuple[str, ...],
    context_type: str = "real_user",
    fact_subject: str = "user",
) -> MemoryIndexEntry:
    for memory in active_memories:
        lowered = memory.memory_text.lower()
        if (
            memory.context_type == context_type
            and memory.fact_subject == fact_subject
            and any(term.lower() in lowered for term in required_terms)
        ):
            return memory
    active_text = "\n".join(memory.memory_text for memory in active_memories)
    raise RuntimeError(f"active memory not found for terms {required_terms}:\n{active_text}")


def _memory_snapshots(active_memories: list[MemoryIndexEntry]) -> list[MemorySnapshot]:
    return [
        MemorySnapshot(
            memory_text=memory.memory_text,
            memory_status=memory.memory_status.value,
            context_type=memory.context_type,
            fact_subject=memory.fact_subject,
            roleplay_mode=memory.roleplay_mode,
        )
        for memory in active_memories
    ]


def _assert_no_duplicate_backend_ids(active_memories: list[Any]) -> None:
    backend_ids = [memory.backend_memory_id for memory in active_memories]
    duplicate_ids = sorted({backend_id for backend_id in backend_ids if backend_ids.count(backend_id) > 1})
    if duplicate_ids:
        raise RuntimeError(f"duplicate active backend ids: {duplicate_ids}")


def _assert_nickname_converged(repository: SqlAlchemyMemoryRepository, scope: QualityScope) -> None:
    active = _active_memories(repository, scope)
    active_text = "\n".join(memory.memory_text for memory in active)
    old_active = [memory.memory_text for memory in active if "阿鹏" in memory.memory_text]
    new_active = [memory.memory_text for memory in active if "小鹏" in memory.memory_text]
    if old_active:
        raise RuntimeError(f"old nickname is still active: {old_active}")
    if not new_active:
        raise RuntimeError(f"new nickname was not indexed as active. active memories:\n{active_text}")


def _assert_active_memories_do_not_contain_forbidden(
    repository: SqlAlchemyMemoryRepository,
    scope: QualityScope,
    forbidden_terms: list[str],
) -> None:
    active = _active_memories(repository, scope)
    active_text = "\n".join(memory.memory_text for memory in active)
    polluted_terms = [term for term in forbidden_terms if term.lower() in active_text.lower()]
    if polluted_terms:
        raise RuntimeError(
            f"active memories still contain forbidden terms {polluted_terms}:\n{active_text}"
        )


def _assert_negative_query_is_clean(items: list[dict[str, Any]]) -> None:
    l3_text = "\n".join(str(item.get("content", "")) for item in _l3_items(items))
    if any(term in l3_text for term in ("团子", "麻薯", "豆包", "猫", "狗")) or any(
        term in l3_text.lower() for term in ("cat", "dog")
    ):
        raise RuntimeError(f"negative pet query recalled unrelated pet memory:\n{l3_text}")


def _assert_deleted_rebuild_does_not_inflate(
    before_delete_count: int,
    after_rebuild_active: list[Any],
) -> None:
    _assert_no_duplicate_backend_ids(after_rebuild_active)
    if len(after_rebuild_active) > before_delete_count + 1:
        raise RuntimeError(
            "rebuild inflated active business index too much: "
            f"before_delete={before_delete_count}, after_rebuild={len(after_rebuild_active)}"
        )


def _assert_active_scope_empty(repository: SqlAlchemyMemoryRepository, scope: QualityScope) -> None:
    active = _active_memories(repository, scope)
    if active:
        active_text = "\n".join(memory.memory_text for memory in active)
        raise RuntimeError(f"active memories should be empty after delete_all:\n{active_text}")


def main() -> None:
    load_dotenv()
    settings = Settings()
    if not settings.openai_api_key or not settings.qdrant_url:
        raise RuntimeError("OPENAI_API_KEY and QDRANT_URL are required")
    base_url = os.environ.get("THINKBACK_API_URL", "http://127.0.0.1:18082")
    repository = SqlAlchemyMemoryRepository()
    scope = _new_scope()
    request_metrics = RequestMetrics()
    latency_metrics = LatencyMetrics()

    print(f"quality scope: run_id={scope.run_id}")
    other_user_scope = scope.for_user(
        f"{scope.run_id}-other-user",
        f"{scope.run_id}-other-user-session",
    )
    other_character_scope = scope.for_character(
        f"{scope.run_id}-other-character",
        f"{scope.run_id}-other-session",
    )
    lifecycle_scope = scope.for_character(
        f"{scope.run_id}-lifecycle-character",
        f"{scope.run_id}-lifecycle-session-main",
    )
    lifecycle_session_delete_scope = lifecycle_scope.for_character(
        lifecycle_scope.character_id,
        f"{scope.run_id}-lifecycle-session-delete",
    )
    _append_round(base_url, scope, 1, "请记住，我喜欢别人叫我阿鹏。", request_metrics=request_metrics, latency_metrics=latency_metrics)
    _append_round(base_url, scope, 2, "纠正一下：以后不要叫我阿鹏，请叫我小鹏。", request_metrics=request_metrics, latency_metrics=latency_metrics)
    _append_round(base_url, scope, 3, "我养了一只猫，名字叫团子。", request_metrics=request_metrics, latency_metrics=latency_metrics)
    _append_round(base_url, scope, 4, "我喜欢晚上复盘工作压力，但不喜欢被说教。", request_metrics=request_metrics, latency_metrics=latency_metrics)
    _append_round(base_url, scope, 5, "如果我焦虑，请先帮我拆解问题，再给建议。", request_metrics=request_metrics, latency_metrics=latency_metrics)
    _append_round(base_url, scope, 6, "更正一下，我的猫不叫团子，叫麻薯。", request_metrics=request_metrics, latency_metrics=latency_metrics)
    _append_round(base_url, scope, 7, "我之前住在杭州，现在已经搬到上海。", request_metrics=request_metrics, latency_metrics=latency_metrics)
    _append_round(base_url, scope, 8, "工作状态更新：我已经接受 Moonshot 的 offer，不再评估其它机会。", request_metrics=request_metrics, latency_metrics=latency_metrics)
    _append_round(base_url, scope, 9, "沟通偏好也更新：我现在更想要简洁直接的建议，不需要先安慰。", request_metrics=request_metrics, latency_metrics=latency_metrics)
    _append_round(base_url, scope, 10, "生日纠正一下，不是5月20日，是6月1日。", request_metrics=request_metrics, latency_metrics=latency_metrics)
    _append_round(base_url, scope, 11, "饮品偏好改成茶，不再喝咖啡。", request_metrics=request_metrics, latency_metrics=latency_metrics)
    _append_round(base_url, scope, 12, "我现在可以接受温和的提醒睡觉。", request_metrics=request_metrics, latency_metrics=latency_metrics)
    _append_round(base_url, scope, 13, "我养了一只狗，名字叫豆包。", request_metrics=request_metrics, latency_metrics=latency_metrics)
    _append_round(base_url, scope, 14, "食物偏好改成寿司，不再吃汉堡。", request_metrics=request_metrics, latency_metrics=latency_metrics)
    _append_round(base_url, scope, 16, "会议时间偏好改一下：不要上午，尽量安排在下午。", request_metrics=request_metrics, latency_metrics=latency_metrics)
    _append_round(base_url, scope, 17, "运动偏好更新：现在喜欢游泳，不再坚持跑步。", request_metrics=request_metrics, latency_metrics=latency_metrics)
    _append_round(
        base_url,
        scope,
        15,
        "剧情设定里，我养了一只猫叫露露。",
        metadata={
            "context_type": "roleplay",
            "roleplay_mode": "on",
            "fact_subject": "story_world",
            "backend_categories": ["story_world"],
        },
        request_metrics=request_metrics,
        latency_metrics=latency_metrics,
    )
    _append_round(
        base_url,
        other_user_scope,
        1,
        "我只告诉另一个用户：我的猫叫奶盖。",
        request_metrics=request_metrics,
        latency_metrics=latency_metrics,
    )
    _append_round(
        base_url,
        other_user_scope,
        2,
        "我只告诉另一个用户：我喜欢喝可乐。",
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
    _append_round(
        base_url,
        other_character_scope,
        2,
        "我只告诉这个角色：我喜欢吃披萨。",
        request_metrics=request_metrics,
        latency_metrics=latency_metrics,
    )
    _append_round(
        base_url,
        scope,
        18,
        "剧情设定里的据点在海边灯塔。",
        metadata={
            "context_type": "roleplay",
            "roleplay_mode": "on",
            "fact_subject": "story_world",
            "backend_categories": ["story_world"],
        },
        request_metrics=request_metrics,
        latency_metrics=latency_metrics,
    )
    _append_round(base_url, lifecycle_scope, 1, "请记住，我喜欢别人叫我阿鹏。", request_metrics=request_metrics, latency_metrics=latency_metrics)
    _append_round(base_url, lifecycle_scope, 2, "纠正一下：以后不要叫我阿鹏，请叫我小鹏。", request_metrics=request_metrics, latency_metrics=latency_metrics)
    _append_round(base_url, lifecycle_scope, 3, "我养了一只猫，名字叫团子。", request_metrics=request_metrics, latency_metrics=latency_metrics)
    _append_round(base_url, lifecycle_scope, 4, "更正一下，我的猫不叫团子，叫麻薯。", request_metrics=request_metrics, latency_metrics=latency_metrics)
    _append_round(base_url, lifecycle_scope, 5, "我养了一只狗，名字叫豆包。", request_metrics=request_metrics, latency_metrics=latency_metrics)
    _append_round(base_url, lifecycle_session_delete_scope, 1, "这个会话里请记住，我喜欢喝乌龙茶。", request_metrics=request_metrics, latency_metrics=latency_metrics)
    _append_round(base_url, lifecycle_session_delete_scope, 2, "这个会话里请记住，我喜欢吃拉面当夜宵。", request_metrics=request_metrics, latency_metrics=latency_metrics)

    active_after_append = _active_memories(repository, scope)
    _assert_no_duplicate_backend_ids(active_after_append)
    _assert_nickname_converged(repository, scope)
    _assert_active_memories_do_not_contain_forbidden(
        repository,
        scope,
        [
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
            "上午",
            "morning",
            "跑步",
            "running",
        ],
    )

    cases = build_production_quality_cases()
    results = [
        _query_result(base_url, scope, case, request_metrics=request_metrics, latency_metrics=latency_metrics)
        for case in cases
    ]
    report = build_quality_report(
        cases,
        results,
        active_memory_count=len(active_after_append),
        active_after_rebuild=len(active_after_append),
        request_metrics=request_metrics,
        latency_metrics=latency_metrics,
        active_memory_snapshots=_memory_snapshots(active_after_append),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    if not report["passed"]:
        details = "\n".join(
            f"{result.case_name}: {result.recalled_text}" for result in results
            if result.case_name in set(report["failed_cases"])
        )
        if report["failed_metrics"]:
            details = "\n".join((details, json.dumps(report["failed_metrics"], ensure_ascii=False)))
        raise RuntimeError(f"quality report failed:\n{details}")

    dog_recall = _recall(
        base_url,
        scope,
        "用户的鸟叫什么？",
        threshold=0.5,
        request_metrics=request_metrics,
        latency_metrics=latency_metrics,
    )
    _assert_negative_query_is_clean(dog_recall)

    lifecycle_after_append = _active_memories(repository, lifecycle_scope)
    delete_target = _find_active_memory(
        lifecycle_after_append,
        required_terms=("麻薯", "Mashu"),
    )
    delete_response = _post_json(
        base_url,
        "/memory/delete",
        {
            "request_id": scope.request_id("delete-cat"),
            "user_id": lifecycle_scope.user_id,
            "character_id": lifecycle_scope.character_id,
            "scope": "memory",
            "operation_id": lifecycle_scope.operation_id("delete-cat"),
            "memory_id": delete_target.memory_id,
        },
        request_metrics=request_metrics,
        latency_metrics=latency_metrics,
        operation="delete",
    )
    if delete_response.get("affected_memories") != 1:
        raise RuntimeError(f"delete did not affect one memory: {delete_response}")

    post_delete_cases = build_post_delete_quality_cases()
    deleted_after_delete_cases = [
        case for case in post_delete_cases if case.name == "deleted-cat-after-delete"
    ]
    deleted_after_delete_results = [
        _query_result(base_url, lifecycle_scope, case, request_metrics=request_metrics, latency_metrics=latency_metrics)
        for case in deleted_after_delete_cases
    ]

    delete_session_response = _post_json(
        base_url,
        "/memory/delete",
        {
            "request_id": lifecycle_scope.request_id("delete-session"),
            "user_id": lifecycle_scope.user_id,
            "character_id": lifecycle_scope.character_id,
            "scope": "session",
            "operation_id": lifecycle_scope.operation_id("delete-session"),
            "session_id": lifecycle_session_delete_scope.session_id,
        },
        request_metrics=request_metrics,
        latency_metrics=latency_metrics,
        operation="delete",
    )
    if delete_session_response.get("affected_memories", 0) < 1:
        raise RuntimeError(f"delete session did not affect memories: {delete_session_response}")
    deleted_session_cases = [
        case for case in post_delete_cases if case.category == "delete_session"
    ]
    deleted_session_results = [
        _query_result(base_url, lifecycle_session_delete_scope, case, request_metrics=request_metrics, latency_metrics=latency_metrics)
        for case in deleted_session_cases
    ]

    _post_json(
        base_url,
        "/memory/rebuild",
        {
            "request_id": lifecycle_scope.request_id("rebuild-after-memory-session-delete"),
            "user_id": lifecycle_scope.user_id,
            "character_id": lifecycle_scope.character_id,
            "operation_id": lifecycle_scope.operation_id("rebuild-after-memory-session-delete"),
        },
        request_metrics=request_metrics,
        latency_metrics=latency_metrics,
        operation="rebuild",
    )
    after_memory_session_rebuild = _active_memories(repository, lifecycle_scope)
    _assert_deleted_rebuild_does_not_inflate(len(lifecycle_after_append), after_memory_session_rebuild)
    _assert_nickname_converged(repository, lifecycle_scope)
    _assert_active_memories_do_not_contain_forbidden(
        repository,
        lifecycle_scope,
        [
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
            "乌龙茶",
            "oolong",
            "拉面",
            "ramen",
        ],
    )

    memory_session_rebuild_cases = [
        case
        for case in post_delete_cases
        if case.name
        in {
            "deleted-cat-after-rebuild",
            "nickname-after-rebuild",
            "deleted-session-drink-after-rebuild",
        }
    ]
    memory_session_rebuild_results = [
        _query_result(base_url, lifecycle_scope, case, request_metrics=request_metrics, latency_metrics=latency_metrics)
        for case in memory_session_rebuild_cases
    ]

    delete_all_response = _post_json(
        base_url,
        "/memory/delete",
        {
            "request_id": lifecycle_scope.request_id("delete-all"),
            "user_id": lifecycle_scope.user_id,
            "character_id": lifecycle_scope.character_id,
            "scope": "all",
            "operation_id": lifecycle_scope.operation_id("delete-all"),
        },
        request_metrics=request_metrics,
        latency_metrics=latency_metrics,
        operation="delete",
    )
    if delete_all_response.get("affected_memories", 0) < 1:
        raise RuntimeError(f"delete all did not affect memories: {delete_all_response}")
    _assert_active_scope_empty(repository, lifecycle_scope)
    delete_all_cases = [
        case for case in post_delete_cases if case.category == "delete_all"
    ]
    delete_all_results = [
        _query_result(base_url, lifecycle_scope, case, request_metrics=request_metrics, latency_metrics=latency_metrics)
        for case in delete_all_cases
    ]

    _post_json(
        base_url,
        "/memory/rebuild",
        {
            "request_id": lifecycle_scope.request_id("rebuild-after-delete-all"),
            "user_id": lifecycle_scope.user_id,
            "character_id": lifecycle_scope.character_id,
            "operation_id": lifecycle_scope.operation_id("rebuild-after-delete-all"),
        },
        request_metrics=request_metrics,
        latency_metrics=latency_metrics,
        operation="rebuild",
    )
    after_delete_all_rebuild = _active_memories(repository, lifecycle_scope)
    _assert_active_scope_empty(repository, lifecycle_scope)
    delete_all_rebuild_cases = [
        case for case in post_delete_cases if case.name == "deleted-all-nickname-after-rebuild"
    ]
    delete_all_rebuild_results = [
        _query_result(base_url, lifecycle_scope, case, request_metrics=request_metrics, latency_metrics=latency_metrics)
        for case in delete_all_rebuild_cases
    ]
    post_delete_results = (
        deleted_after_delete_results
        + deleted_session_results
        + memory_session_rebuild_results
        + delete_all_results
        + delete_all_rebuild_results
    )
    deleted_residue = [
        result.case_name
        for result in post_delete_results
        if any(
            term in result.all_recalled_text
            for term in ("麻薯", "团子", "露露", "泡芙")
        )
    ]
    if deleted_residue:
        details = "\n".join(
            f"{result.case_name}: {result.all_recalled_text}" for result in post_delete_results
            if result.case_name in set(deleted_residue)
        )
        raise RuntimeError(f"deleted cat residue found in any recall layer:\n{details}")
    post_delete_report = build_quality_report(
        post_delete_cases,
        post_delete_results,
        active_memory_count=len(lifecycle_after_append),
        active_after_rebuild=len(after_delete_all_rebuild),
        request_metrics=request_metrics,
        latency_metrics=latency_metrics,
        active_memory_snapshots=_memory_snapshots(after_delete_all_rebuild),
    )
    print(json.dumps(post_delete_report, ensure_ascii=False, indent=2, sort_keys=True))
    if not post_delete_report["passed"]:
        details = "\n".join(
            f"{result.case_name}: {result.recalled_text}" for result in post_delete_results
            if result.case_name in set(post_delete_report["failed_cases"])
        )
        if post_delete_report["failed_metrics"]:
            details = "\n".join(
                (details, json.dumps(post_delete_report["failed_metrics"], ensure_ascii=False))
            )
        raise RuntimeError(f"post-delete quality report failed:\n{details}")

    print(
        "quality regression passed: "
        f"active_after_append={len(active_after_append)}, "
        f"lifecycle_active_after_append={len(lifecycle_after_append)}, "
        f"active_after_rebuild={len(after_delete_all_rebuild)}"
    )


if __name__ == "__main__":
    main()
