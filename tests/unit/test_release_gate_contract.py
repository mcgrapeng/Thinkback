"""发布门禁的配置契约。

这些断言盯的是「门禁本身」：配置写在 YAML/JS 里，人会改，改坏了不会
有人立刻发现，直到线上出故障才楚。所以把关键门禁钉成断言。
"""

from __future__ import annotations

from pathlib import Path

import yaml


def _ci_workflow() -> dict:
    return yaml.safe_load(Path(".github/workflows/ci.yml").read_text(encoding="utf-8"))


def test_ci_runs_the_deterministic_gate_set() -> None:
    """CI 必须真的跑这些门禁：README 与 pyproject 曾声称跑 make check，实际没有。"""
    workflow = _ci_workflow()
    jobs = workflow["jobs"]

    for job in ("lint-and-typecheck", "unit-tests", "supply-chain-audit", "api-fuzz"):
        assert job in jobs, f"CI 缺少门禁 job: {job}"

    # 变异审计 + 自包含评测（含基线门禁）。
    assert "mutation-and-eval" in jobs
    # 前端：审计、组件测试、生产构建、端到端。
    assert "web-gates" in jobs
    # 镜像可构建。
    assert "docker-build-verify" in jobs

    steps = "\n".join(
        str(step) for job_steps in jobs.values() for step in job_steps.get("steps", [])
    )
    assert "make check" in steps
    assert "pytest tests/e2e" in steps
    assert "npm audit" in steps
    assert "check_mutation_survivors.py" in steps
    assert "check_baseline.py" in steps


def test_kustomization_entry_exists_and_renders() -> None:
    """`make k8s-apply` 用 `kubectl apply -k k8s`：目录必须有 kustomization。"""
    kustomization = Path("k8s/kustomization.yaml")
    assert kustomization.is_file(), "k8s/ 缺少 kustomization.yaml，发布命令不可用"

    payload = yaml.safe_load(kustomization.read_text(encoding="utf-8"))
    for resource in payload["resources"]:
        assert (Path("k8s") / resource).is_file(), f"kustomization 引用了不存在的清单: {resource}"


def test_load_smoke_fails_on_correctness_checks() -> None:
    """k6 冒烟必须让正确性 check 决定退出码。"""
    script = Path("tests/load/smoke.k6.js").read_text(encoding="utf-8")
    assert "checks:" in script
    assert "rate==1.0" in script
    # 正确性断言必须都在：任务落终态、未降级、召回本轮事实、L3 里召回到。
    assert "append task completed" in script
    assert "recall 未降级" in script
    assert "recall 命中本轮事实" in script
    # L3 专项断言：只看「有没有召回到」会被 L2 拼接摘要兜底 —— L3 压根没
    # 写进去也会显示通过，这条断言专门盯长期记忆。
    assert "recall 命中 L3 长期记忆" in script
    # 每轮唯一标记：round_id 复用会走幂等重试路径，测的是另一条路径。
    assert "RUN_ID" in script


def test_python_dependencies_include_playwright_for_e2e() -> None:
    """e2e 依赖必须声明在 dev 组里：干净环境跑 make test-e2e 不能缺模块。"""
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    assert "playwright" in pyproject
