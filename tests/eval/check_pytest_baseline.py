"""pytest 套件的防回血基线：删用例、跳过用例都必须是个显式动作。

变异基线（``tests/eval/mutation_survivors.json``）盯的是「变异体有没有被杀掉」，
它看不见**用例本身被删或被跳过**。删掉一条用例，剩下的套件照样全绿 —— 门禁不会有
任何反应，而那条用例原本钉住的行为就此失去保护。

所以这里记一份用例清单，基线里的每一条都必须**真的跑了**：

- 用例不在了（删了、改名了、文件没了）→ 失败；
- 用例还在但被 ``skip`` / ``xfail`` 掉了 → 也失败。**收集到了不等于跑到了**：
  一条被跳过的用例等于没有保护，而它是静默的 —— 门禁必须当它不存在。

基线文件本身也要校验：空清单、缺 ``tests`` 键、``count`` 与实际条数不一致，
一律判失败。清单被弄坏时**不得**退化成「没有缺失项」而放行（fail-open）。

``--update`` 记的是**这次真的跑了**的用例，不是「收集到的」。收集到但被
skip / xfail 掉的用例不写进基线 —— 它没有保护价值，写进去只会让门禁永远
红着、而没有一条解除办法。删用例和 skip 用例于是有了同一个出口：
确认这些行为确实不再需要保护，再跑一次 ``--update``。

用法：
    python tests/eval/check_pytest_baseline.py                     # 校验（缺失即失败）
    python tests/eval/check_pytest_baseline.py --update            # 重新生成基线
    python tests/eval/check_pytest_baseline.py --suite tests/fuzz --baseline <路径>
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from defusedxml import ElementTree as ET

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SUITE = "tests/unit"
DEFAULT_BASELINE = REPO_ROOT / "tests" / "eval" / "pytest_baseline.json"


class BaselineError(Exception):
    """基线本身不可信 —— 判失败，绝不放行。"""


def _run_pytest(suite: str, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "uv",
            "run",
            "--group",
            "dev",
            "pytest",
            "-o",
            "addopts=",
            suite,
            *extra,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def collect_test_ids(suite: str) -> list[str]:
    """收集该套件的用例 ID（``tests/unit/...::test_xxx``）。

    ``-o addopts=""`` 是必须的：项目 addopts 里的 ``-q`` 会让 ``--collect-only``
    只输出「每个文件几条」而不是用例 ID，基线就退化成「文件级计数」——
    少一条又补一条的替换删法就抓不到了。
    """
    result = _run_pytest(suite, "--collect-only", "-q")
    if result.returncode != 0:
        raise BaselineError(f"用例收集失败，退出码 {result.returncode}\n{result.stdout[-2000:]}")

    ids = []
    for line in result.stdout.splitlines():
        line = line.strip()
        # 收集输出形如 tests/unit/test_x.py::test_y[param]
        if line.startswith("tests/") and "::" in line:
            ids.append(line)
    if not ids:
        raise BaselineError(f"没有收集到任何用例（suite={suite}）")
    return sorted(set(ids))


def _junit_key(node_id: str) -> tuple[str, str]:
    """把 pytest 的用例 ID 转成 junit XML 里的 (classname, name)。

    junit 的 classname 是「模块点路径 + 可选的类名」，name 是用例名（含参数化后缀）。
    这里从用例 ID 反推，规则与 pytest 的 junitxml 一致：
    ``tests/unit/test_x.py::TestGroup::test_in`` → (``tests.unit.test_x.TestGroup``, ``test_in``）
    """
    parts = node_id.split("::")
    path = parts[0]
    module = path[:-3].replace("/", ".") if path.endswith(".py") else path.replace("/", ".")
    if len(parts) == 1:
        return (module, "")
    name = parts[-1]
    classname = ".".join([module, *parts[1:-1]]) if len(parts) > 2 else module
    return (classname, name)


def load_baseline(path: Path) -> list[str]:
    """读取基线；任何不可信之处都抛 BaselineError（由调用方判失败）。"""
    if not path.is_file():
        raise BaselineError(f"基线文件不存在：{path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise BaselineError(f"基线文件不可读或不是合法 JSON：{path}（{exc}）") from exc
    if not isinstance(payload, dict):
        raise BaselineError(f"基线格式不对（顶层应是对象）：{path}")

    tests = payload.get("tests")
    if not isinstance(tests, list):
        raise BaselineError(f"基线缺少 tests 键或类型不对：{path}")
    if not tests:
        raise BaselineError(f"基线是空的：{path} —— 空基线等于没有门禁")
    if not all(isinstance(item, str) and item.strip() for item in tests):
        raise BaselineError(f"基线里有空条目或非字符串条目：{path}")

    count = payload.get("count")
    if count is not None:
        try:
            declared = int(count)
        except (TypeError, ValueError) as exc:
            raise BaselineError(f"基线的 count 不是整数：{path}") from exc
        if declared != len(tests):
            raise BaselineError(
                f"基线内部不一致：count={declared}，实际 {len(tests)} 条 —— 基线被截断或手改过：{path}"
            )
    return sorted(set(tests))


def executed_test_keys(
    suite: str, junit_path: Path | None
) -> tuple[set[tuple[str, str]], set[tuple[str, str]], str]:
    """跑一遍该套件，返回（真的执行了的、skip 掉的）(classname, name) 集合。

    junit 里带 ``<skipped>`` 子元素的是被 skip / xfail 掉的 —— 收集到了但没跑，
    不算「有保护」。两者分开报：一个是「用例没了」，一个是「用例还在但没跑」，
    修法不一样。

    ``junit_path`` 给定时复用那份报告（CI 里与测试同一次跑），否则自己跑一遍。
    """
    if junit_path is not None:
        report = junit_path
    else:
        report = Path(tempfile.mkdtemp(prefix="pytest-baseline-")) / "junit.xml"
        result = _run_pytest(suite, "-q", f"--junitxml={report}")
        if result.returncode not in (0, 1):
            raise BaselineError(
                f"跑测试套件失败，退出码 {result.returncode}\n{result.stdout[-2000:]}"
            )
        if not report.is_file():
            raise BaselineError(f"没能生成 junit 报告：{report}")

    try:
        tree = ET.parse(report)
    except (ET.ParseError, OSError) as exc:
        raise BaselineError(f"junit 报告不可读：{report}（{exc}）") from exc

    executed: set[tuple[str, str]] = set()
    skipped: set[tuple[str, str]] = set()
    for case in tree.iter("testcase"):
        key = (case.get("classname", ""), case.get("name", ""))
        if case.find("skipped") is not None:
            # skip / xfail：收集到了但没跑。
            skipped.add(key)
            continue
        executed.add(key)
    if not executed:
        raise BaselineError(f"junit 报告里没有任何执行记录：{report}")
    return executed, skipped, str(report)


def write_baseline(path: Path, ids: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "note": "pytest 套件的防回血基线：删用例或跳过用例要连带更新本文件（显式动作）",
                "count": len(ids),
                "tests": ids,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=False,
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="校验 pytest 套件的防回血基线")
    parser.add_argument("--update", action="store_true", help="重新生成基线文件")
    parser.add_argument("--suite", default=DEFAULT_SUITE, help="要校验的套件（默认 tests/unit）")
    parser.add_argument("--baseline", default=None, help="基线文件路径（默认按套件推导）")
    parser.add_argument(
        "--junit", default=None, help="复用已有的 junit 报告（CI 里与测试同一次跑）"
    )
    args = parser.parse_args()

    baseline_path = (
        Path(args.baseline)
        if args.baseline
        else REPO_ROOT / "tests" / "eval" / f"pytest_baseline_{Path(args.suite).name}.json"
    )
    if args.baseline is None and args.suite == DEFAULT_SUITE:
        baseline_path = DEFAULT_BASELINE

    if args.update:
        try:
            collected = collect_test_ids(args.suite)
            executed, _, report_path = executed_test_keys(args.suite, None)
        except BaselineError as exc:
            print(f"基线更新失败：{exc}", file=sys.stderr)
            return 1
        kept = [node_id for node_id in collected if _junit_key(node_id) in executed]
        dropped = [node_id for node_id in collected if _junit_key(node_id) not in executed]
        if not kept:
            print(
                f"基线更新失败：没有任何用例真的跑起来（报告 {report_path}）—— "
                "写出一份空基线等于取消门禁",
                file=sys.stderr,
            )
            return 1
        write_baseline(baseline_path, kept)
        print(f"基线已更新：{len(kept)} 条用例（{baseline_path}）")
        if dropped:
            # 这些用例被 skip / xfail 掉了 —— 它们没有保护价值，所以不进基线。
            # 点名出来，是因为「让一条用例不再受保护」应该是看得见的动作。
            print(f"另有 {len(dropped)} 条用例没真的跑，未纳入基线：")
            for node_id in dropped:
                print(f"  - {node_id}")
        return 0

    try:
        baseline = load_baseline(baseline_path)
        executed, skipped, report_path = executed_test_keys(
            args.suite, Path(args.junit) if args.junit else None
        )
    except BaselineError as exc:
        print(f"基线校验失败：{exc}", file=sys.stderr)
        return 1

    missing: list[str] = []
    not_run: list[str] = []
    for node_id in baseline:
        key = _junit_key(node_id)
        if key in executed:
            continue
        missing.append(node_id)
        if key in skipped:
            not_run.append(node_id)

    if missing:
        print(
            f"基线里的 {len(missing)} 条用例没有真的跑起来"
            f"（基线 {len(baseline)} 条，套件 {args.suite}，报告 {report_path}）：",
            file=sys.stderr,
        )
        for item in missing:
            reason = "被 skip/xfail" if item in not_run else "用例不在了"
            print(f"  - {item}  [{reason}]", file=sys.stderr)
        print(
            "\n删用例或把它 skip 掉都要连带更新基线："
            f"python tests/eval/check_pytest_baseline.py --suite {args.suite} --update\n"
            "确认这些行为确实不再需要保护，再更新。",
            file=sys.stderr,
        )
        return 1

    print(f"用例基线一致：{len(baseline)} 条全部执行（套件 {args.suite}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
