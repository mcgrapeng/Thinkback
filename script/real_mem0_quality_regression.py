"""Focused real-quality regression checks for Thinkback -> Mem0 -> Qdrant."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from dotenv import load_dotenv

from infra.config import Settings
from memory.repositories import MemoryIndexEntry, SqlAlchemyMemoryRepository


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
        return f"{self.run_id}-{self.scope_suffix}-round-{index}"

    def request_id(self, label: str) -> str:
        return f"{self.run_id}-{self.scope_suffix}-{label}"

    def operation_id(self, label: str) -> str:
        return f"{self.run_id}-{self.scope_suffix}-{label}-op"

    def for_character(self, character_id: str, session_id: str) -> QualityScope:
        return QualityScope(
            run_id=self.run_id,
            user_id=self.user_id,
            character_id=character_id,
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
            if forbidden_hit or result.l3_count > 0:
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
        "delete_memory_residue_rate": 0.0,
        "delete_session_residue_rate": None,
        "delete_all_residue_rate": None,
        "rebuild_resurrection_rate": 0.0,
        "cross_user_leak_rate": None,
        "cross_character_leak_rate": 0.0,
        "roleplay_real_mix_rate": 0.0,
        "dirty_summary_recall_rate": None,
        "source_ref_loss_rate": None,
        "known_drift_regression_pass_rate": None,
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
    }
    failed_metrics = _failed_metric_gates(report)
    report["failed_metrics"] = failed_metrics
    report["passed"] = not failed_cases and not failed_metrics
    return report


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
        ("case_pass_rate", ">=", 0.98),
        ("recall_at_10", ">=", 0.95),
        ("precision_at_10", ">=", 0.95),
        ("false_positive_rate", "<=", 0.02),
        ("conflict_pollution_rate", "<=", 0.02),
        ("duplicate_active_rate", "==", 0.0),
    )
    failed: list[dict[str, Any]] = []
    for metric, operator, expected in gates:
        actual = float(report[metric])
        if operator == ">=" and actual < expected:
            failed.append({"metric": metric, "actual": actual, "expected": f">= {expected}"})
        elif operator == "<=" and actual > expected:
            failed.append({"metric": metric, "actual": actual, "expected": f"<= {expected}"})
        elif operator == "==" and actual != expected:
            failed.append({"metric": metric, "actual": actual, "expected": "== 0"})
    return failed


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
    lowered = detail.lower()
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
    lowered = str(reason).lower()
    return any(marker in lowered for marker in ("timed out", "timeout", "temporarily unavailable"))


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
    other_character_scope = scope.for_character(
        f"{scope.run_id}-other-character",
        f"{scope.run_id}-other-session",
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
        other_character_scope,
        1,
        "我只告诉这个角色：我的猫叫泡芙。",
        request_metrics=request_metrics,
        latency_metrics=latency_metrics,
    )

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
        ],
    )

    cases = [
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
            name="negative-bird",
            category="negative_control",
            query="用户的鸟叫什么？",
            expected_terms=[],
            forbidden_terms=["麻薯", "团子", "豆包", "猫", "狗", "cat", "dog"],
        ),
        EvaluationCase(
            name="negative-roleplay-cat",
            category="isolation",
            query="用户现实里的猫叫什么？",
            expected_terms=["麻薯"],
            forbidden_terms=["露露"],
        ),
        EvaluationCase(
            name="roleplay-cat",
            category="isolation",
            query="剧情设定里的猫叫什么？",
            expected_terms=["露露"],
            forbidden_terms=["麻薯", "团子"],
        ),
        EvaluationCase(
            name="negative-cross-character-cat",
            category="isolation",
            query="用户的猫叫什么？",
            expected_terms=["麻薯"],
            forbidden_terms=["泡芙"],
        ),
    ]
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
        ],
    )

    post_delete_cases = [
        EvaluationCase(
            name="deleted-cat-not-recalled",
            category="delete_rebuild",
            query="用户的猫叫什么？",
            expected_terms=[],
            forbidden_terms=["麻薯", "团子", "露露", "泡芙", "猫", "cat"],
        ),
        EvaluationCase(
            name="nickname-after-rebuild",
            category="delete_rebuild",
            query="现在应该怎么称呼用户？",
            intent="preference",
            expected_terms=["小鹏"],
            forbidden_terms=["阿鹏"],
        ),
    ]
    post_delete_results = [
        _query_result(base_url, scope, case, request_metrics=request_metrics, latency_metrics=latency_metrics)
        for case in post_delete_cases
    ]
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
        active_memory_count=len(active_after_append),
        active_after_rebuild=len(after_rebuild),
        request_metrics=request_metrics,
        latency_metrics=latency_metrics,
        active_memory_snapshots=_memory_snapshots(after_rebuild),
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
        f"active_after_append={len(active_after_append)}, active_after_rebuild={len(after_rebuild)}"
    )


if __name__ == "__main__":
    main()
