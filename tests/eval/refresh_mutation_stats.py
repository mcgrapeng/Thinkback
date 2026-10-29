"""变异审计前置：清掉过期的「函数↔测试」映射，重跑「无测试」与「存活」判定。

mutmut 把「哪个测试覆盖哪个函数」的映射缓存在 ``mutants/mutmut-stats.json``。
缓存的有效性由配置指纹与被监视文件决定，**不含测试文件内容** —— 改了测试，
映射还是旧的，于是新增的覆盖看不见，变异体会被误判成「无测试」。

同理，已经记成「无测试」的判定本身就是「映射没覆盖到」的产物，不可信：
每次审计都要把它们重跑一遍，重跑后仍是「无测试」才是真盲区。

**存活判定有同样的毛病，而且更隐蔽**：mutmut 不会重跑已经跑过的变异体
（源码没变就直接沿用上次结论）。于是「昨天写的测试今天才杀掉某个存活变异体」
这件事永远看不见 —— 变异预算花在哪、补的测试有没有真的补上，门禁都答不出来。
所以存活的那批也要每次重跑：新测试杀掉的存活体会从 ``mutmut results`` 里消失，
基线里对应条目随之作废（``check_mutation_survivors.py`` 只提示、不判失败）。

用法（``make mutation-audit`` 的第一步）::

    uv run --group dev python tests/eval/refresh_mutation_stats.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MUTANTS_DIR = REPO_ROOT / "mutants"
STATS_PATH = MUTANTS_DIR / "mutmut-stats.json"
# mutmut 用退出码 33 表示「没有测试覆盖到」（见 mutmut/__main__.py）。
NO_TESTS_EXIT_CODE = 33
# 退出码 0 = 变异体没被杀死（测试在变异后仍然通过）。
SURVIVED_EXIT_CODE = 0


def collect_mutants_by_outcome() -> tuple[list[str], list[str]]:
    """扫描 mutants 下的 meta，返回（无测试的、存活的）变异体名。"""

    no_tests: list[str] = []
    survived: list[str] = []
    for meta_path in sorted(MUTANTS_DIR.rglob("*.meta")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        exit_codes = meta.get("exit_code_by_key")
        if not isinstance(exit_codes, dict):
            continue
        for name, code in exit_codes.items():
            if code == NO_TESTS_EXIT_CODE:
                no_tests.append(name)
            elif code == SURVIVED_EXIT_CODE:
                survived.append(name)
    return sorted(no_tests), sorted(survived)


def main() -> int:
    # 映射缓存过期是「无测试」误判的根源：先删掉，让 mutmut 重新收集。
    if STATS_PATH.exists():
        STATS_PATH.unlink()
        print("dropped stale mutants/mutmut-stats.json (mapping is re-collected)")

    no_test_names, survived_names = collect_mutants_by_outcome()
    if not no_test_names and not survived_names:
        print("no mutants marked 'no tests' or 'survived'")
        return 0

    # 显式点名重跑：mutmut 只对「被点名的变异体」强制重跑，
    # 源码没变时它会直接沿用上次的结论。新加的测试对存活的变异体有没有杀伤力，
    # 只有重跑才知道。
    names = [*no_test_names, *survived_names]
    print(
        f"re-running {len(no_test_names)} mutant(s) marked 'no tests' "
        f"and {len(survived_names)} marked 'survived'"
    )
    return subprocess.call(
        [sys.executable, "-m", "mutmut", "run", "--max-children", "8", *names],
        cwd=REPO_ROOT,
    )


if __name__ == "__main__":
    sys.exit(main())
