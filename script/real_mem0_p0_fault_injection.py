"""Fault-injection reporting helpers for P0 production precheck."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from dotenv import dotenv_values


@dataclass(frozen=True)
class FaultScenarioSpec:
    name: str
    expected_dependency: str
    env_overrides: dict[str, str]


def build_fault_injection_report(
    *,
    run_id: str,
    started_at: str,
    ended_at: str,
    scenarios: list[dict[str, Any]],
) -> dict[str, Any]:
    scenario_results = [_normalize_scenario(scenario) for scenario in scenarios]
    failed_scenarios = [
        str(result["name"]) for result in scenario_results if not bool(result["passed"])
    ]
    return {
        "run_id": run_id,
        "started_at": started_at,
        "ended_at": ended_at,
        "passed": not failed_scenarios,
        "failed_scenarios": failed_scenarios,
        "scenario_results": scenario_results,
    }


def build_library_fault_scenario_specs(closed_port: int | None = None) -> list[FaultScenarioSpec]:
    blocked_port = str(closed_port or int(os.getenv("INNIES_MEMORY_FAULT_CLOSED_PORT", "9")))
    return [
        FaultScenarioSpec(
            name="mem0_library_model_unavailable",
            expected_dependency="mem0",
            env_overrides={
                "MEMORY_LLM_BASE_URL": f"http://127.0.0.1:{blocked_port}/v1",
            },
        ),
        FaultScenarioSpec(
            name="milvus_unavailable",
            expected_dependency="milvus",
            env_overrides={
                "MILVUS_URL": f"http://127.0.0.1:{blocked_port}",
                "MILVUS_USER": "",
                "MILVUS_PASSWORD": "",
            },
        ),
        FaultScenarioSpec(
            name="postgres_unavailable",
            expected_dependency="database",
            env_overrides={
                "POSTGRES_HOST": "127.0.0.1",
                "POSTGRES_PORT": blocked_port,
            },
        ),
    ]


def evaluate_readiness_fault_scenario(
    *,
    name: str,
    expected_dependency: str,
    failure_status_code: int,
    failure_payload: dict[str, Any],
    recovery_status_code: int,
    recovery_payload: dict[str, Any],
) -> dict[str, Any]:
    failure_dependencies = failure_payload.get("dependencies", {})
    failure_dependency = failure_dependencies.get(expected_dependency, {})
    failure_observed = (
        failure_status_code == 503
        and failure_payload.get("status") == "not_ready"
        and failure_dependency.get("status") == "not_ready"
    ) or (
        failure_status_code == 0
        and failure_payload.get("status") == "not_ready"
        and bool(failure_payload.get("error"))
    )
    recovery_dependencies = recovery_payload.get("dependencies", {})
    recovery_dependency = recovery_dependencies.get(expected_dependency, {})
    recovery_verified = (
        recovery_status_code == 200
        and recovery_payload.get("status") == "ready"
        and all(
            dependency.get("status") == "ready" for dependency in recovery_dependencies.values()
        )
        and recovery_dependency.get("status") == "ready"
    )
    failure_detail = _readiness_detail(
        expected_dependency=expected_dependency,
        payload=failure_payload,
        status_code=failure_status_code,
    )
    recovery_detail = _readiness_detail(
        expected_dependency=expected_dependency,
        payload=recovery_payload,
        status_code=recovery_status_code,
    )
    return {
        "name": name,
        "expected_dependency": expected_dependency,
        "injected": True,
        "failure_observed": failure_observed,
        "recovered": recovery_verified,
        "recovery_verified": recovery_verified,
        "failure_status_code": failure_status_code,
        "recovery_status_code": recovery_status_code,
        "failure_detail": failure_detail,
        "recovery_detail": recovery_detail,
    }


def render_fault_injection_markdown(report: dict[str, Any]) -> str:
    conclusion = "通过" if report.get("passed") else "未通过"
    lines = [
        "# innies-memory P0 故障注入报告",
        "",
        "## 1. 结论",
        "",
        "| 项目 | 结果 |",
        "| --- | --- |",
        f"| run_id | `{report['run_id']}` |",
        f"| started_at | `{report['started_at']}` |",
        f"| ended_at | `{report['ended_at']}` |",
        f"| 故障注入 | {conclusion} |",
        f"| 失败场景 | {', '.join(report.get('failed_scenarios', [])) or '-'} |",
        "",
        "## 2. 场景明细",
        "",
        "| 场景 | 结论 | 注入失败可观测 | 恢复验证 | 失败细节 | 恢复细节 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for result in report.get("scenario_results", []):
        lines.append(
            "| "
            f"{result['name']} | "
            f"{'通过' if result['passed'] else '未通过'} | "
            f"{result['failure_observed']} | "
            f"{result['recovery_verified']} | "
            f"{result.get('failure_detail', '-')} | "
            f"{result.get('recovery_detail', '-')} |"
        )
    lines.append("")
    return "\n".join(lines)


def write_fault_injection_report(report: dict[str, Any], *, output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{report['run_id']}.json"
    md_path = output_dir / f"{report['run_id']}.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    md_path.write_text(render_fault_injection_markdown(report), encoding="utf-8")
    return json_path, md_path


def _normalize_scenario(scenario: dict[str, Any]) -> dict[str, Any]:
    result = dict(scenario)
    result["passed"] = all(
        bool(result.get(field))
        for field in ("injected", "failure_observed", "recovered", "recovery_verified")
    )
    result.setdefault("failure_detail", "-")
    result.setdefault("recovery_detail", "-")
    return result


def _readiness_detail(
    *,
    expected_dependency: str,
    payload: dict[str, Any],
    status_code: int,
) -> str:
    dependency = payload.get("dependencies", {}).get(expected_dependency, {})
    if status_code == 0:
        return f"readiness probe failed; error: {payload.get('error', '-')}"
    status = dependency.get("status", "-")
    detail = dependency.get("detail", "-")
    return f"readiness HTTP {status_code}; {expected_dependency} {status}: {detail}"


def _unused_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        return int(server.getsockname()[1])


def _readiness(base_url: str, *, timeout_seconds: float = 2.0) -> tuple[int, dict[str, Any]]:
    request = Request(
        f"{base_url.rstrip('/')}/health/ready", headers={"Accept": "application/json"}
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8")
            return int(response.status), json.loads(body)
    except HTTPError as exc:
        body = exc.read().decode("utf-8")
        return int(exc.code), json.loads(body)
    except (OSError, URLError) as exc:
        return 0, {"status": "not_ready", "dependencies": {}, "error": str(exc)}


def _wait_for_readiness(
    base_url: str,
    *,
    expected_status_code: int,
    timeout_seconds: float,
    probe_timeout_seconds: float = 2.0,
) -> tuple[int, dict[str, Any]]:
    deadline = time.monotonic() + timeout_seconds
    last_status_code = 0
    last_payload: dict[str, Any] = {"status": "not_ready", "dependencies": {}}
    while time.monotonic() < deadline:
        last_status_code, last_payload = _readiness(
            base_url,
            timeout_seconds=probe_timeout_seconds,
        )
        if last_status_code == expected_status_code:
            return last_status_code, last_payload
        time.sleep(0.5)
    return last_status_code, last_payload


def _start_api_process(
    *,
    port: int,
    env_overrides: dict[str, str],
    log_path: Path,
) -> subprocess.Popen[str]:
    env = os.environ.copy()
    env.update(
        {key: str(value) for key, value in dotenv_values(".env").items() if value is not None}
    )
    env.update(env_overrides)
    env["PYTHONPATH"] = "src"
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "innies_memory.api.app:app",
        "--app-dir",
        "src",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("w", encoding="utf-8")
    return subprocess.Popen(
        command,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )


def _stop_api_process(process: subprocess.Popen[str]) -> None:
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def run_readiness_fault_injection(
    *,
    output_dir: Path,
    log_dir: Path,
    startup_timeout_seconds: float = 20.0,
    probe_timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    started_at = datetime.now(UTC).isoformat()
    scenarios: list[dict[str, Any]] = []
    for spec in build_library_fault_scenario_specs():
        failure_port = _unused_local_port()
        failure_process = _start_api_process(
            port=failure_port,
            env_overrides=spec.env_overrides,
            log_path=log_dir / f"{spec.name}-failure.log",
        )
        try:
            failure_status, failure_payload = _wait_for_readiness(
                f"http://127.0.0.1:{failure_port}",
                expected_status_code=503,
                timeout_seconds=startup_timeout_seconds,
                probe_timeout_seconds=probe_timeout_seconds,
            )
        finally:
            _stop_api_process(failure_process)

        recovery_port = _unused_local_port()
        recovery_process = _start_api_process(
            port=recovery_port,
            env_overrides={},
            log_path=log_dir / f"{spec.name}-recovery.log",
        )
        try:
            recovery_status, recovery_payload = _wait_for_readiness(
                f"http://127.0.0.1:{recovery_port}",
                expected_status_code=200,
                timeout_seconds=startup_timeout_seconds,
                probe_timeout_seconds=probe_timeout_seconds,
            )
        finally:
            _stop_api_process(recovery_process)

        scenarios.append(
            evaluate_readiness_fault_scenario(
                name=spec.name,
                expected_dependency=spec.expected_dependency,
                failure_status_code=failure_status,
                failure_payload=failure_payload,
                recovery_status_code=recovery_status,
                recovery_payload=recovery_payload,
            )
        )
    report = build_fault_injection_report(
        run_id=f"p0-fault-injection-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}",
        started_at=started_at,
        ended_at=datetime.now(UTC).isoformat(),
        scenarios=scenarios,
    )
    write_fault_injection_report(report, output_dir=output_dir)
    return report


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run P0 readiness fault injection for Mem0 Library mode."
    )
    parser.add_argument("--output-dir", default="docs/memory/report")
    parser.add_argument("--log-dir", default="/tmp/innies-memory-p0-fault-injection")
    parser.add_argument("--startup-timeout-seconds", type=float, default=20.0)
    parser.add_argument("--probe-timeout-seconds", type=float, default=15.0)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    report = run_readiness_fault_injection(
        output_dir=Path(args.output_dir),
        log_dir=Path(args.log_dir),
        startup_timeout_seconds=args.startup_timeout_seconds,
        probe_timeout_seconds=args.probe_timeout_seconds,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    if not report["passed"]:
        raise RuntimeError(f"P0 fault injection failed: {report['failed_scenarios']}")


if __name__ == "__main__":
    main()
