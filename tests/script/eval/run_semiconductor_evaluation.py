"""半导体垂类记忆评测。

与 ``tests/script/run_self_contained_evaluation.py``（通用能力评测）互补：
那份测「记忆系统本身对不对」，这份测「换成半导体垂类对话助手的语料后还对不对」
—— 工艺咨询 / 设备选型 / 良率分析 / 客户支持四类场景的专业词能不能被记住、
被召回；以及垂类机制（namespace 隔离、类型准入、保留期、assistant 不成为事实）。

两种运行模式，报告里会显式标注模式，别把两种分数混着看：

- 默认（``contract`` / 自包含）：InMemoryMemoryRepository + FakeMemoryBackend，
  不联网、确定性、可进 CI 门禁。
  **只证明编排契约**（幂等、分层、namespace 隔离、类型准入、TTL、降级）：
  FakeMemoryBackend 不跑 LLM 抽取，直接把整轮原文当记忆存下来，
  所以这里的「抽出来了」是假的，抽取质量与语义召回质量都不作数。
- ``--real``：真实 PG + 真实 Milvus + 真实 Mem0 + 真实 LLM/embedding，
  看真实抽取与语义召回的水位（不进 CI，需要本地栈）。

真实模式的失败按原因分桶，一眼看出是「没抽出来」「抽出来但缺事实」
还是「事实已在索引里却没召回」：

- ``extraction_empty``：后端没产出任何事件（抽取空）
- ``extraction_missing_fact``：产出了事件，但抽出的文本里没有期望事实
- ``retrieval_missing_fact``：事实已在索引里，召回却没带回来
- ``forbidden_term_leaked``：召回了不该出现的禁用词（泄漏）
- ``mechanism_violation``：垂类机制用例不满足契约（隔离 / 类型准入 / TTL）

用法::

    uv run --group dev python tests/script/eval/run_semiconductor_evaluation.py
    uv run --group dev python tests/script/eval/run_semiconductor_evaluation.py --real
    uv run --group dev python tests/script/eval/check_baseline.py \\
        --report-dir docs/memory/report/semiconductor \\
        --baseline tests/eval/baseline_semiconductor.json
    uv run --group dev python tests/script/eval/check_baseline.py \\
        --report-dir docs/memory/report/semiconductor \\
        --baseline tests/eval/baseline_semiconductor_real.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from thinkback.memory.schemas import (
    AppendMemoryRequest,
    MemoryMessage,
    MessageRole,
    RecallMemoryRequest,
)

# 仓库根要放上 sys.path 才能 import tests.script.eval.*（与 tests/script/ 下其他脚本同一套路）
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tests.script.eval.check_baseline import check_report  # noqa: E402

CASES_PATH = Path("tests/eval/semiconductor_cases.json")
REPORT_DIR = Path("docs/memory/report/semiconductor")
REPORT_TYPE = "thinking_memory_semiconductor_vertical_evaluation"
REAL_BASELINE_PATH = Path("tests/eval/baseline_semiconductor_real.json")

MODE_CONTRACT = "self_contained"
MODE_REAL = "real"

MODE_LABELS = {
    MODE_CONTRACT: "contract/self-contained",
    MODE_REAL: "real",
}

MODE_DESCRIPTIONS = {
    MODE_CONTRACT: (
        "InMemoryMemoryRepository + FakeMemoryBackend，不联网、确定性。"
        "FakeMemoryBackend 不跑 LLM 抽取，直接把整轮原文当记忆存下来 —— "
        "所以只证明编排契约（幂等、分层、namespace 隔离、类型准入、TTL、降级），"
        "不证明抽取质量，也不证明语义召回质量。"
    ),
    MODE_REAL: (
        "真实 PG + Milvus + Mem0 + LLM/embedding。"
        "能测真实抽取与语义召回水位；门槛见 tests/eval/baseline_semiconductor_real.json。"
    ),
}

# 失败归因桶：键 = 报告里的桶名，值 = 人话解释
FAILURE_BUCKETS = {
    "extraction_empty": "后端没产出任何事件（抽取空）",
    "extraction_missing_fact": "产出了事件，但抽出的文本里没有期望事实",
    "retrieval_missing_fact": "事实已在索引里，召回却没带回来",
    "forbidden_term_leaked": "召回了不该出现的禁用词（泄漏）",
    "mechanism_violation": "垂类机制用例不满足契约（隔离 / 类型准入 / TTL）",
}


@dataclass
class CaseResult:
    name: str
    category: str
    passed: bool
    detail: str
    elapsed_ms: float
    failure_bucket: str | None = None


def _msg(message_id: str, role: MessageRole, content: str) -> MemoryMessage:
    return MemoryMessage(
        message_id=message_id,
        role=role,
        content=content,
        timestamp=datetime.now(UTC),
    )


def _append(
    service: Any,
    *,
    namespace: str,
    round_id: str,
    user_text: str,
    assistant_text: str,
    metadata: dict[str, Any] | None = None,
    user_id: str = "semi-user",
    session_id: str = "semi-session",
) -> list[Any]:
    response = service.append(
        AppendMemoryRequest(
            request_id=f"req-{round_id}",
            user_id=user_id,
            session_id=session_id,
            round_id=round_id,
            namespace=namespace,
            messages=[
                _msg(f"{round_id}-u", MessageRole.USER, user_text),
                _msg(f"{round_id}-a", MessageRole.ASSISTANT, assistant_text),
            ],
            source_timestamp=datetime.now(UTC),
            metadata=metadata or {},
        )
    )
    return list(getattr(response, "l3_events", []) or [])


def _recall_texts(
    service: Any,
    *,
    namespace: str,
    query: str,
    user_id: str = "semi-user",
    session_id: str = "semi-session",
) -> list[str]:
    response = service.recall(
        RecallMemoryRequest(
            user_id=user_id,
            session_id=session_id,
            namespace=namespace,
            query=query,
        )
    )
    return [item.content for item in response.items if item.layer == "L3"]


def _extracted_texts(repository: Any, user_id: str, namespace: str) -> list[str]:
    """索引里已落库的长期记忆正文 —— 即后端抽出来的事实。"""
    if repository is None:
        return []
    try:
        entries = repository.active_memories(user_id, namespace)
    except Exception:  # noqa: BLE001 - 取不到索引不该让评测崩掉，归因退化成「抽取出事件但缺事实」
        return []
    return [str(getattr(entry, "memory_text", "")) for entry in entries]


def _classify_failure(
    *,
    events: list[Any],
    extracted_texts: list[str],
    missing: list[str],
    leaked: list[str],
) -> str:
    """把一次失败归到一个原因桶。"""
    if leaked:
        return "forbidden_term_leaked"
    if not events:
        return "extraction_empty"
    if any(term in text for term in missing for text in extracted_texts):
        return "retrieval_missing_fact"
    return "extraction_missing_fact"


def _make_service(real: bool, repository: Any = None) -> tuple[Any, Any, Any]:
    """返回 (service, backend, repository)。

    自包含模式用内存仓储 + fake 后端；``--real`` 用真实 PG / Milvus / Mem0。
    两种模式都挂上垂类 profile 的类型注册表（namespace = semiconductor）。

    真实模式的仓储必须整个运行期共用：``SqlAlchemyMemoryRepository`` 自带事件
    循环，每次新建会换一个 loop，跨用例调用会抛 "attached to a different loop"。
    """
    from thinkback.api.dependencies import build_registry_provider, get_memory_backend
    from thinkback.infra.config import settings
    from thinkback.memory.backends import FakeMemoryBackend
    from thinkback.memory.repositories import InMemoryMemoryRepository, SqlAlchemyMemoryRepository
    from thinkback.memory.service import MemoryService

    registry_provider = build_registry_provider(settings)
    if real:
        if repository is None:
            repository = SqlAlchemyMemoryRepository()
        backend = get_memory_backend(settings)
    else:
        repository = repository or InMemoryMemoryRepository()
        backend = FakeMemoryBackend()
    service = MemoryService(
        repository=repository,
        backend=backend,
        registry_provider=registry_provider,
        l3_write_mode="sync",
    )
    return service, backend, repository


def _run_recall_case(
    case: dict[str, Any],
    namespace: str,
    real: bool,
    user_id: str,
    repository: Any,
    run_token: str,
) -> CaseResult:
    started = time.perf_counter()
    service, _backend, service_repository = _make_service(real, repository)
    events: list[Any] = []
    for index, round_data in enumerate(case["rounds"]):
        events.extend(
            _append(
                service,
                namespace=namespace,
                round_id=f"{case['name']}-{run_token}-r{index}",
                user_text=round_data["user"],
                assistant_text=round_data["assistant"],
                user_id=user_id,
            )
        )
    texts = _recall_texts(service, namespace=namespace, query=case["query"], user_id=user_id)
    missing = [
        term for term in case.get("expected_terms", []) if not any(term in text for text in texts)
    ]
    leaked = [
        term for term in case.get("forbidden_terms", []) if any(term in text for text in texts)
    ]
    passed = not missing and not leaked
    bucket = None
    if not passed:
        bucket = _classify_failure(
            events=events,
            extracted_texts=_extracted_texts(service_repository, user_id, namespace),
            missing=missing,
            leaked=leaked,
        )
    return CaseResult(
        case["name"],
        case["category"],
        passed,
        f"missing={missing} leaked={leaked} events={events} texts={texts}",
        (time.perf_counter() - started) * 1000,
        bucket,
    )


def _run_namespace_isolation_case(
    case: dict[str, Any],
    namespace: str,
    real: bool,
    user_id: str,
    repository: Any,
    run_token: str,
) -> CaseResult:
    started = time.perf_counter()
    service, _backend, service_repository = _make_service(real, repository)
    events: list[Any] = []
    for index, round_data in enumerate(case["rounds"]):
        events.extend(
            _append(
                service,
                namespace=namespace,
                round_id=f"{case['name']}-{run_token}-r{index}",
                user_text=round_data["user"],
                assistant_text=round_data["assistant"],
                user_id=user_id,
            )
        )
    scoped = _recall_texts(service, namespace=namespace, query=case["query"], user_id=user_id)
    other = _recall_texts(service, namespace="thinkback", query=case["query"], user_id=user_id)
    missing = [
        term for term in case.get("expected_terms", []) if not any(term in text for text in scoped)
    ]
    passed = not missing and not other
    bucket = None
    if not passed:
        bucket = (
            "mechanism_violation"
            if other
            else _classify_failure(
                events=events,
                extracted_texts=_extracted_texts(service_repository, user_id, namespace),
                missing=missing,
                leaked=[],
            )
        )
    return CaseResult(
        case["name"],
        case["category"],
        passed,
        f"missing={missing} scoped={scoped} other_namespace={other}",
        (time.perf_counter() - started) * 1000,
        bucket,
    )


def _run_reject_type_case(
    case: dict[str, Any],
    namespace: str,
    real: bool,
    user_id: str,
    repository: Any,
    run_token: str,
) -> CaseResult:
    started = time.perf_counter()
    service, _backend, _repository = _make_service(real, repository)
    round_data = case["rounds"][0]
    try:
        _append(
            service,
            namespace=namespace,
            round_id=f"{case['name']}-{run_token}-r0",
            user_text=round_data["user"],
            assistant_text=round_data["assistant"],
            user_id=user_id,
            metadata={"memory_type": case["memory_type"]},
        )
    except ValueError as exc:
        return CaseResult(
            case["name"],
            case["category"],
            "requires review" in str(exc) or "not registered" in str(exc),
            f"rejected: {exc}",
            (time.perf_counter() - started) * 1000,
        )
    return CaseResult(
        case["name"],
        case["category"],
        False,
        f"未被拒绝：{case['memory_type']} 写进去了",
        (time.perf_counter() - started) * 1000,
        "mechanism_violation",
    )


def _run_ttl_case(
    case: dict[str, Any],
    namespace: str,
    real: bool,
    user_id: str,
    repository: Any,
    run_token: str,
) -> CaseResult:
    """TTL 类型必须把过期日期交给引擎（Mem0 expiration_date）。"""
    started = time.perf_counter()
    service, backend, _repository = _make_service(real, repository)
    captured: dict[str, Any] = {}
    original_add = backend.add

    def capturing_add(messages, **kwargs):  # type: ignore[no-untyped-def]
        captured["expiration_date"] = kwargs.get("expiration_date")
        return original_add(messages, **kwargs)

    backend.add = capturing_add
    round_data = case["rounds"][0]
    _append(
        service,
        namespace=namespace,
        round_id=f"{case['name']}-{run_token}-r0",
        user_text=round_data["user"],
        assistant_text=round_data["assistant"],
        user_id=user_id,
        metadata={"memory_type": case["memory_type"]},
    )
    expiration = captured.get("expiration_date")
    passed = isinstance(expiration, str) and len(expiration) == 10
    return CaseResult(
        case["name"],
        case["category"],
        passed,
        f"expiration_date={expiration}",
        (time.perf_counter() - started) * 1000,
        None if passed else "mechanism_violation",
    )


RUNNERS = {
    "recall": _run_recall_case,
    "namespace_isolation": _run_namespace_isolation_case,
    "reject_type": _run_reject_type_case,
    "ttl_expiration": _run_ttl_case,
}


def run_cases(corpus: dict[str, Any], *, real: bool) -> list[CaseResult]:
    namespace = corpus.get("namespace", "semiconductor")
    # 真实模式下仓储必须共用（自带事件循环），自包含模式每个用例独立内存仓储。
    shared_repository = None
    if real:
        from thinkback.memory.repositories import SqlAlchemyMemoryRepository

        shared_repository = SqlAlchemyMemoryRepository()
    run_token = uuid4().hex[:8]
    results: list[CaseResult] = []
    try:
        for case in corpus["cases"]:
            runner = RUNNERS.get(case.get("kind", "recall"))
            if runner is None:
                results.append(
                    CaseResult(
                        case["name"],
                        case["category"],
                        False,
                        f"未知 kind={case['kind']}",
                        0.0,
                        "mechanism_violation",
                    )
                )
                continue
            # 每个用例一个独立 user，且带 run_token：真实栈上 Milvus / PG 会累积
            # 记忆，复用 user 会让引擎把新事实当重复跳过（抽取为空），也会让旧
            # 记忆污染召回 —— 测量结果就不真实了。
            user_id = f"semi-{case['name']}-{run_token}"
            try:
                results.append(runner(case, namespace, real, user_id, shared_repository, run_token))
            except Exception as exc:  # noqa: BLE001 - 评测要记录失败而不是中断
                results.append(
                    CaseResult(
                        case["name"],
                        case["category"],
                        False,
                        f"raised: {type(exc).__name__}: {exc}",
                        0.0,
                        "mechanism_violation",
                    )
                )
    finally:
        if shared_repository is not None:
            shared_repository.close()
    return results


def aggregate(results: list[CaseResult]) -> dict[str, Any]:
    by_category: dict[str, dict[str, Any]] = {}
    total_passed = 0
    for result in results:
        bucket = by_category.setdefault(
            result.category, {"passed": 0, "count": 0, "pass_rate": 0.0}
        )
        bucket["count"] += 1
        bucket["passed"] += 1 if result.passed else 0
        total_passed += 1 if result.passed else 0
    for bucket in by_category.values():
        bucket["pass_rate"] = bucket["passed"] / bucket["count"] if bucket["count"] else 0.0
    failure_buckets = {
        name: {
            "count": sum(1 for result in results if result.failure_bucket == name),
            "rate": (
                sum(1 for result in results if result.failure_bucket == name) / len(results)
                if results
                else 0.0
            ),
        }
        for name in FAILURE_BUCKETS
    }
    return {
        "total_pass_rate": total_passed / len(results) if results else 0.0,
        "total_passed": total_passed,
        "total_count": len(results),
        "by_category": dict(sorted(by_category.items())),
        "failure_buckets": failure_buckets,
    }


def render_markdown(report: dict[str, Any]) -> str:
    mode = report["mode"]
    lines = [
        "# 半导体垂类记忆评测",
        "",
        f"- 运行 ID: `{report['run_id']}`",
        f"- 生成时间: `{report['generated_at']}`",
        f"- 模式: `{mode}`（{report['mode_label']}）",
        f"- 模式说明: {report['mode_description']}",
        f"- 总通过率: **{report['aggregate']['total_pass_rate']:.1%}** "
        f"({report['aggregate']['total_passed']}/{report['aggregate']['total_count']})",
        "",
        "## 类别通过率",
        "",
        "| 类别 | 通过率 | 通过/总数 |",
        "| --- | ---: | ---: |",
    ]
    for category, metric in report["aggregate"]["by_category"].items():
        lines.append(
            f"| {category} | {metric['pass_rate']:.1%} | {metric['passed']}/{metric['count']} |"
        )
    lines += [
        "",
        "## 失败归因",
        "",
        "| 归因桶 | 用例数 | 占比 | 含义 |",
        "| --- | ---: | ---: | --- |",
    ]
    for name, metric in report["aggregate"]["failure_buckets"].items():
        lines.append(
            f"| `{name}` | {metric['count']} | {metric['rate']:.1%} | {FAILURE_BUCKETS[name]} |"
        )
    if mode == MODE_CONTRACT:
        lines += [
            "",
            "> 自包含模式用 fake 后端，归因桶只反映编排链路，不代表真实抽取质量。",
        ]
    lines += [
        "",
        "## 用例结果",
        "",
        "| 用例 | 类别 | 通过 | 归因桶 | 耗时 (ms) | 关键证据 |",
        "| --- | --- | :---: | --- | ---: | --- |",
    ]
    for result in report["results"]:
        ok = "✅" if result["passed"] else "❌"
        detail = result["detail"].replace("|", "\\|").replace("\n", " ")
        bucket = result.get("failure_bucket") or "-"
        lines.append(
            f"| `{result['name']}` | {result['category']} | {ok} | {bucket} | "
            f"{result['elapsed_ms']:.1f} | {detail} |"
        )
    lines.append("")
    return "\n".join(lines)


def _real_baseline_gate(report: dict[str, Any], baseline_path: Path) -> list[str]:
    """真实栈报告对照真实栈基线；基线读不到时抛 OSError / JSONDecodeError。"""
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    return check_report(report, baseline)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--real",
        action="store_true",
        help="跑真实栈（PG + Milvus + Mem0 + LLM）；默认自包含（内存仓储 + fake 后端）",
    )
    parser.add_argument("--cases", type=Path, default=CASES_PATH)
    parser.add_argument("--report-dir", type=Path, default=REPORT_DIR)
    parser.add_argument(
        "--baseline",
        type=Path,
        default=REAL_BASELINE_PATH,
        help="真实栈模式对照的基线（自包含模式不用）",
    )
    args = parser.parse_args(argv[1:])

    if args.real:
        # 本机装了个人 SOCKS 代理：会把对内网网关的请求劫持走。
        for proxy_var in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ):
            os.environ.pop(proxy_var, None)

    corpus = json.loads(args.cases.read_text(encoding="utf-8"))
    results = run_cases(corpus, real=args.real)
    aggregate_result = aggregate(results)
    run_id = f"semi-eval-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:6]}"
    mode = MODE_REAL if args.real else MODE_CONTRACT
    report = {
        "report_type": REPORT_TYPE,
        "run_id": run_id,
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": mode,
        "mode_label": MODE_LABELS[mode],
        "mode_description": MODE_DESCRIPTIONS[mode],
        "aggregate": aggregate_result,
        "results": [
            {
                "name": result.name,
                "category": result.category,
                "passed": result.passed,
                "detail": result.detail,
                "elapsed_ms": result.elapsed_ms,
                "failure_bucket": result.failure_bucket,
            }
            for result in results
        ],
    }

    args.report_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.report_dir / f"{run_id}.json"
    md_path = args.report_dir / f"{run_id}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    print(
        f"通过率: {aggregate_result['total_pass_rate']:.1%} "
        f"({aggregate_result['total_passed']}/{aggregate_result['total_count']})"
    )
    print(f"模式: {mode}（{MODE_LABELS[mode]}）")
    print(f"报告: {json_path}")
    print(f"       {md_path}")

    if not args.real:
        # 自包含模式是 CI 门禁：契约必须 100% 满足，不留「差不多」。
        return 0 if aggregate_result["total_pass_rate"] == 1.0 else 1

    # 真实模式按真实栈基线判达标（抽取水位本身就有 12.5% 的缺口，
    # 用 100% 卡会永远是红的，等于没有门禁）。
    try:
        violations = _real_baseline_gate(report, args.baseline)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"真实栈基线读取失败: {exc}", file=sys.stderr)
        return 2
    if violations:
        print(f"真实栈基线未达标（{args.baseline}）:", file=sys.stderr)
        for violation in violations:
            print(f"  - {violation}", file=sys.stderr)
        return 1
    print(f"真实栈基线达标（{args.baseline}）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
