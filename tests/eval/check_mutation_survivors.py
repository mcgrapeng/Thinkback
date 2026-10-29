"""变异存活基线门禁：出现**新增**未被杀死的变异体即失败。

mutmut 3.8 即使有存活变异体也退出 0，``mutmut results`` 只是一张给人看的
快照——光跑 mutmut 不构成门禁。本脚本把当前结果与已验收基线
（``tests/eval/mutation_survivors.json``）比对：

- 出现基线之外的**未杀死**变异体 → 退出 1（新增盲区：补测试，或登记理由）
- 基线里的变异体已被杀死 → 只提示，不失败（是进步，可顺手清理基线）

门禁本身也要能失败。下面两种「什么都没审但门禁照样绿」的情况会判失败：

一、**没审到东西**。变异体总数为 0（范围是空的、或 mutants/ 树是坏的），
或者声明在 ``only_mutate`` 里的某个文件一个变异体都没生成——后者正是
「范围变了但 mutmut 复用了旧副本」那个静默失效的形态：新增文件进范围，
变异体数为 0，门禁照样绿，实际什么都没审计。

二、**出现了不认识的状态**。mutmut 的状态词表比「存活 / 无测试 / 超时」长，
还有 suspicious / skipped / not checked / 「check was interrupted by user」。
以前只认前三个，后面的直接从门禁视野里消失。现在按「没被杀死」这条规则
兜底：任何状态只要不是 killed，就必须出现在已验收基线里。

用法：``uv run --group dev python tests/eval/check_mutation_survivors.py``
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE_PATH = Path(__file__).with_name("mutation_survivors.json")
MUTANTS_DIR = REPO_ROOT / "mutants"
# mutmut results 的输出形如 "    <变异体名>: <状态>"
RESULT_LINE = re.compile(r"^\s*(?P<name>\S+):\s+(?P<status>.+?)\s*$")
KILLED = "killed"
SURVIVED = "survived"
NO_TESTS = "no tests"
# 超时也是「没被杀掉」：测试没跑完，可能是变异体让代码死循环，也可能是
# 墙钟估算偏了。**两种都不该被静默放过** —— 门禁以前只认 survived / no tests，
# timeout 直接从门禁视野里消失了，等于「跑不完的用例」这一类盲区无人看守。
TIMEOUT = "timeout"
# 其余状态（suspicious / skipped / not checked / interrupted）以前也从门禁
# 视野里消失。它们归到基线的 accepted_other，同样要登记理由。
OTHER = "other"
# 这三个是已验收基线各自有独立登记桶的状态；其余状态归 accepted_other。
KNOWN_STATUSES = frozenset({SURVIVED, NO_TESTS, TIMEOUT})


def read_results() -> dict[str, str]:
    """跑 ``mutmut results``，返回 {变异体名: 状态}（只含未被杀死的变异体）。"""

    completed = subprocess.run(
        [sys.executable, "-m", "mutmut", "results"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    results: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        matched = RESULT_LINE.match(line)
        if matched:
            results[matched.group("name")] = matched.group("status")
    return results


def scoped_source_files() -> list[str]:
    """``[tool.mutmut]`` 声明的变异范围（相对仓库根的文件列表）。

    ``only_mutate`` 为空时，整个 ``source_paths`` 都在范围内。
    """

    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    mutmut_config = config.get("tool", {}).get("mutmut", {})
    patterns = [str(pattern) for pattern in (mutmut_config.get("only_mutate") or [])]
    if patterns:
        return sorted(set(patterns))
    found: list[str] = []
    for root in [str(root) for root in (mutmut_config.get("source_paths") or [])]:
        base = REPO_ROOT / root
        if base.is_dir():
            found.extend(
                str(path.relative_to(REPO_ROOT)) for path in base.rglob("*.py") if path.is_file()
            )
    return sorted(set(found))


def evaluated_population() -> tuple[int, dict[str, int]]:
    """（真的评估过的变异体总数，每个声明文件各生成多少个）。

    数据来自 ``mutants/`` 下每个模块的 ``.meta``：``exit_code_by_key`` 记着
    这一个模块里每个变异体的结局（被杀 / 存活 / 超时…）。被杀的也在里面——
    所以这个集合是「这次真的审了多少」，而不是「还剩几个没杀掉」。
    """

    per_file: dict[str, int] = {}
    total = 0
    for meta_path in sorted(MUTANTS_DIR.rglob("*.meta")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        codes = meta.get("exit_code_by_key")
        if not isinstance(codes, dict):
            continue
        source = str(meta_path.relative_to(MUTANTS_DIR))[: -len(".meta")]
        per_file[source] = len(codes)
        total += len(codes)
    return total, per_file


def unaudited_scoped_files(per_file: dict[str, int]) -> list[str]:
    """声明在范围内、却一个变异体都没生成的文件。

    这是「范围变了但 mutmut 复用了旧副本」的形态：文件进了 only_mutate，
    mutants/ 里那份副本的 mtime 比源文件新，mutmut 直接复用了它那个空的
    变异体集合。门禁看起来是绿的，实际那个文件一行都没审。
    """

    missing: list[str] = []
    for source in scoped_source_files():
        if per_file.get(source, 0) == 0:
            missing.append(source)
    return missing


def not_killed_by_status(results: dict[str, str]) -> dict[str, set[str]]:
    """按状态分组的「没被杀死」的变异体。

    规则很简单：**只有 killed 算有保护**，其余状态一律要进基线。mutmut 的
    状态词表以后再长（suspicious / skipped / not checked / interrupted…），
    这条规则也不会漏 —— 以前是「认几个已知状态，其余无视」，新状态一出现
    就从门禁视野里消失了。
    """

    grouped: dict[str, set[str]] = {}
    for name, status in results.items():
        if status == KILLED:
            continue
        grouped.setdefault(status, set()).add(name)
    return grouped


def main() -> int:
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    accepted_survivors = set(baseline["accepted_survivors"])
    accepted_no_tests = set(baseline["accepted_no_tests"])
    accepted_timeouts = set(baseline.get("accepted_timeouts", []))
    accepted_other = set(baseline.get("accepted_other", []))

    # 门禁的前提：这次真的审到了东西。
    total, per_file = evaluated_population()
    if total == 0:
        print(
            "变异存活基线：一个变异体都没评估 —— 审计范围是空的或 mutants/ 树是坏的。"
            "\n先跑 `uv run --group dev mutmut run --max-children 8` 再校验。",
            file=sys.stderr,
        )
        return 1
    unaudited = unaudited_scoped_files(per_file)
    if unaudited:
        print(
            f"变异存活基线：{len(unaudited)} 个声明在变异范围内的文件没有生成任何变异体 —— "
            "这些文件这一轮根本没被审：",
            file=sys.stderr,
        )
        for source in unaudited:
            print(f"  - {source}", file=sys.stderr)
        print(
            "\n范围变了但 mutants/ 里留着旧副本时就会这样：mutmut 按副本 mtime 判断"
            "「这份文件已有变异体」，直接复用那个空集合。\n"
            "跑 `uv run --group dev python tests/eval/refresh_mutation_stats.py` 自愈，"
            "或把该文件从 only_mutate 里去掉。",
            file=sys.stderr,
        )
        return 1

    results = read_results()
    grouped = not_killed_by_status(results)
    survivors = grouped.get(SURVIVED, set())
    no_tests = grouped.get(NO_TESTS, set())
    timeouts = grouped.get(TIMEOUT, set())
    # 其余状态（suspicious / skipped / not checked / interrupted）也要进基线
    others = (
        set().union(*(names for status, names in grouped.items() if status not in KNOWN_STATUSES))
        if grouped
        else set()
    )

    new_survivors = sorted(survivors - accepted_survivors)
    new_no_tests = sorted(no_tests - accepted_no_tests)
    new_timeouts = sorted(timeouts - accepted_timeouts)
    new_others = sorted(others - accepted_other)
    # 已经登记在别的桶里、但这次换了状态的 —— 也要报出来：状态漂移说明
    # 当时的判断需要重新看一眼。
    drifted = sorted(
        (survivors & accepted_no_tests)
        | (survivors & accepted_timeouts)
        | (survivors & accepted_other)
        | (no_tests & accepted_survivors)
        | (no_tests & accepted_timeouts)
        | (no_tests & accepted_other)
        | (timeouts & accepted_survivors)
        | (timeouts & accepted_no_tests)
        | (timeouts & accepted_other)
        | (others & accepted_survivors)
        | (others & accepted_no_tests)
        | (others & accepted_timeouts)
    )
    fixed = sorted(
        (accepted_survivors - survivors)
        | (accepted_no_tests - no_tests)
        | (accepted_timeouts - timeouts)
        | (accepted_other - others)
    )

    print(
        f"变异存活基线：本次评估 {total} 个变异体；当前存活 {len(survivors)} 个 / "
        f"无测试 {len(no_tests)} 个 / 超时 {len(timeouts)} 个 / 其他状态 {len(others)} 个；"
        f"已验收 {len(accepted_survivors)} 个 / {len(accepted_no_tests)} 个 / "
        f"{len(accepted_timeouts)} 个 / {len(accepted_other)} 个"
    )
    if fixed:
        print(f"已被杀死（可从基线删除，共 {len(fixed)} 个）：")
        for name in fixed:
            print(f"  - {name}")
    if new_survivors or new_no_tests or new_timeouts or new_others:
        print(
            f"新增未被杀死的变异体 {len(new_survivors) + len(new_no_tests) + len(new_timeouts) + len(new_others)} 个："
        )
        for name in new_survivors:
            print(f"  + survived: {name}")
        for name in new_no_tests:
            print(f"  + no tests: {name}")
        for name in new_timeouts:
            print(f"  + timeout: {name}")
        for name in new_others:
            print(f"  + {results[name]}: {name}")
        print(
            "补测试把它们杀掉，或在 tests/eval/mutation_survivors.json 里登记验收理由"
            "（超时登记到 accepted_timeouts，其余状态登记到 accepted_other）。"
        )
        return 1
    if drifted:
        print(
            f"有 {len(drifted)} 个变异体的状态与基线登记的不一致（登记理由可能需要重看）：",
            file=sys.stderr,
        )
        for name in drifted:
            print(f"  ! {results[name]}: {name}", file=sys.stderr)
    print("无新增未被杀死的变异体，基线一致。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
