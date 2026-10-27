"""半导体垂类记忆评测。

与 ``tests/script/run_self_contained_evaluation.py``（通用能力评测）互补：
那份测「记忆系统本身对不对」，这份测「换成半导体垂类对话助手的语料后还对不对」
—— 工艺咨询 / 设备选型 / 良率分析 / 客户支持四类场景的专业词能不能被记住、
被召回；以及垂类机制（namespace 隔离、类型准入、保留期、assistant 不成为事实）。

两种运行模式：
- 默认（自包含）：InMemoryMemoryRepository + FakeMemoryBackend，不联网，
  确定性，可进 CI 门禁。
- ``--real``：真实 PG + 真实 Milvus + 真实 Mem0 + 真实 LLM/embedding，
  看真实抽取与语义召回的水位（不进 CI，需要本地栈）。

用法::

    uv run --group dev python tests/script/eval/run_semiconductor_evaluation.py
    uv run --group dev python tests/script/eval/run_semiconductor_evaluation.py --real
    uv run --group dev python tests/script/eval/check_baseline.py \\
        --report-dir docs/memory/report/semiconductor \\
        --baseline tests/eval/baseline_semiconductor.json
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

CASES_PATH = Path("tests/eval/semiconductor_cases.json")
REPORT_DIR = Path("docs/memory/report/semiconductor")
REPORT_TYPE = "thinking_memory_semiconductor_vertical_evaluation"


@dataclass
class CaseResult:
    name: str
    category: str
    passed: bool
    detail: str
    elapsed_ms: float


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
    service, _backend, _repository = _make_service(real, repository)
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
    return CaseResult(
        case["name"],
        case["category"],
        passed,
        f"missing={missing} leaked={leaked} events={events} texts={texts}",
        (time.perf_counter() - started) * 1000,
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
    service, _backend, _repository = _make_service(real, repository)
    for index, round_data in enumerate(case["rounds"]):
        _append(
            service,
            namespace=namespace,
            round_id=f"{case['name']}-{run_token}-r{index}",
            user_text=round_data["user"],
            assistant_text=round_data["assistant"],
            user_id=user_id,
        )
    scoped = _recall_texts(service, namespace=namespace, query=case["query"], user_id=user_id)
    other = _recall_texts(service, namespace="thinkback", query=case["query"], user_id=user_id)
    missing = [
        term for term in case.get("expected_terms", []) if not any(term in text for text in scoped)
    ]
    passed = not missing and not other
    return CaseResult(
        case["name"],
        case["category"],
        passed,
        f"missing={missing} scoped={scoped} other_namespace={other}",
        (time.perf_counter() - started) * 1000,
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
                        case["name"], case["category"], False, f"未知 kind={case['kind']}", 0.0
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
    return {
        "total_pass_rate": total_passed / len(results) if results else 0.0,
        "total_passed": total_passed,
        "total_count": len(results),
        "by_category": dict(sorted(by_category.items())),
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# 半导体垂类记忆评测",
        "",
        f"- 运行 ID: `{report['run_id']}`",
        f"- 生成时间: `{report['generated_at']}`",
        f"- 模式: `{report['mode']}`",
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
        "## 用例结果",
        "",
        "| 用例 | 类别 | 通过 | 耗时 (ms) | 关键证据 |",
        "| --- | --- | :---: | ---: | --- |",
    ]
    for result in report["results"]:
        ok = "✅" if result["passed"] else "❌"
        detail = result["detail"].replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| `{result['name']}` | {result['category']} | {ok} | "
            f"{result['elapsed_ms']:.1f} | {detail} |"
        )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--real",
        action="store_true",
        help="跑真实栈（PG + Milvus + Mem0 + LLM）；默认自包含（内存仓储 + fake 后端）",
    )
    parser.add_argument("--cases", type=Path, default=CASES_PATH)
    parser.add_argument("--report-dir", type=Path, default=REPORT_DIR)
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
    report = {
        "report_type": REPORT_TYPE,
        "run_id": run_id,
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": "real" if args.real else "self_contained",
        "aggregate": aggregate_result,
        "results": [
            {
                "name": result.name,
                "category": result.category,
                "passed": result.passed,
                "detail": result.detail,
                "elapsed_ms": result.elapsed_ms,
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
    print(f"报告: {json_path}")
    print(f"       {md_path}")
    return 0 if aggregate_result["total_pass_rate"] == 1.0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
