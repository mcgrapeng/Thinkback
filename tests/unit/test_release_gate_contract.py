"""发布门禁的配置契约。

这些断言盯的是「门禁本身」：配置写在 YAML/JS 里，人会改，改坏了不会
有人立刻发现，直到线上出故障才楚。所以把关键门禁钉成断言。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from tests.eval import check_pytest_baseline as baseline


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
    # 用例防回血基线必须在**阻断路径**上跑，不能只挂在 make check 里 ——
    # CI 的阻断 job 各跑各的命令（不是 make check），门禁挂在哪儿就决定了
    # 它到底拦不拦得住。
    assert "check_pytest_baseline.py" in steps


def test_the_mutation_scope_self_heal_runs_before_the_audit_in_ci() -> None:
    """变异范围自愈必须在 mutmut run **之前**跑，而且要在 CI 的阻断路径上。

    mutmut 按 mutants/ 里副本的 mtime 判断「这份文件已有变异体」：范围变了但
    留着旧副本时，新增文件一个变异体都不生成，门禁照样绿，实际什么都没审。
    这一步只在本地/夜间跑的话，PR 门禁那道缺口就一直在。
    """
    workflow = _ci_workflow()
    steps = workflow["jobs"]["mutation-and-eval"]["steps"]
    commands = [str(step.get("run", "")) for step in steps]

    heal_indexes = [i for i, cmd in enumerate(commands) if "refresh_mutation_stats.py" in cmd]
    audit_indexes = [i for i, cmd in enumerate(commands) if "mutmut run" in cmd]

    assert heal_indexes, "CI 里没有变异范围自愈步骤"
    assert audit_indexes, "CI 里没有变异审计步骤"
    assert max(heal_indexes) < min(audit_indexes), "自愈必须排在 mutmut run 之前"


def test_every_python_suite_ci_runs_is_pinned_by_a_baseline() -> None:
    """CI 跑到的每个 Python 套件都要有基线钉住。

    变异基线盯的是「变异体有没有被杀」，用例基线盯的是「用例还在不在、还跑不跑」。
    少一个套件 = 那个套件的用例可以被静默删掉或跳过。
    """
    workflow = _ci_workflow()
    jobs = workflow["jobs"]

    pinned: set[str] = set()
    for job_steps in jobs.values():
        for step in job_steps.get("steps", []):
            command = str(step.get("run", ""))
            if "check_pytest_baseline.py" not in command:
                continue
            for suite in ("tests/unit", "tests/fuzz", "tests/e2e"):
                if f"--suite {suite}" in command:
                    pinned.add(suite)
            if "--suite" not in command:
                # 默认套件就是 tests/unit
                pinned.add("tests/unit")

    for suite in ("tests/unit", "tests/fuzz", "tests/e2e"):
        assert suite in pinned, f"{suite} 没有被用例基线钉住"

    # 基线文件必须在库里：脚本要读它，缺了就退化成「没有缺失项」而放行。
    for suite in ("tests/unit", "tests/fuzz", "tests/e2e"):
        suffix = "" if suite == "tests/unit" else f"_{Path(suite).name}"
        baseline = Path(f"tests/eval/pytest_baseline{suffix}.json")
        assert baseline.is_file(), f"缺基线文件：{baseline}"


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


def test_the_junit_key_survives_param_ids_containing_double_colons() -> None:
    """参数化 ID 里带 ``::`` 的用例，基线工具不能把键算错。

    IPv6 回环地址 ``::1`` 就是这种 ID。按普通 ``split("::")`` 切，
    ``test_foo[::1]`` 会被切成三段 —— 键跟 junit 报告对不上，那条用例
    在门禁里表现为「没真的跑」，而它其实跑了。基线于是把它当成
    「已删除」，白丢一条保护。
    """
    key = baseline._junit_key(
        "tests/unit/test_mcp_cli.py::test_loopback_hosts_need_no_extra_opt_in[::1]"
    )

    assert key == (
        "tests.unit.test_mcp_cli",
        "test_loopback_hosts_need_no_extra_opt_in[::1]",
    )


def test_the_junit_key_matches_pytest_for_the_ordinary_shapes() -> None:
    """普通形状（函数、类方法、无参数）不能被改坏。"""
    assert baseline._junit_key("tests/unit/test_x.py::test_in") == (
        "tests.unit.test_x",
        "test_in",
    )
    assert baseline._junit_key("tests/unit/test_x.py::TestGroup::test_in") == (
        "tests.unit.test_x.TestGroup",
        "test_in",
    )
    assert baseline._junit_key("tests/unit/test_x.py::test_in[1-2]") == (
        "tests.unit.test_x",
        "test_in[1-2]",
    )
