"""变异审计前置：清掉过期的「函数↔测试」映射，重跑「无测试」判定。

mutmut 把「哪个测试覆盖哪个函数」的映射缓存在 ``mutants/mutmut-stats.json``。
缓存的有效性由配置指纹与被监视文件决定，**不含测试文件内容** —— 改了测试，
映射还是旧的，于是新增的覆盖看不见，变异体会被误判成「无测试」。

同理，已经记成「无测试」的判定本身就是「映射没覆盖到」的产物，不可信：
每次审计都要把它们重跑一遍，重跑后仍是「无测试」才是真盲区。

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


def collect_no_test_mutants() -> list[str]:
    """扫描 mutants 下的 meta，返回判定为「无测试」的变异体名。"""

    names: list[str] = []
    for meta_path in sorted(MUTANTS_DIR.rglob("*.meta")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        exit_codes = meta.get("exit_code_by_key")
        if not isinstance(exit_codes, dict):
            continue
        names.extend(name for name, code in exit_codes.items() if code == NO_TESTS_EXIT_CODE)
    return sorted(names)


def main() -> int:
    # 映射缓存过期是「无测试」误判的根源：先删掉，让 mutmut 重新收集。
    if STATS_PATH.exists():
        STATS_PATH.unlink()
        print("dropped stale mutants/mutmut-stats.json (mapping is re-collected)")

    names = collect_no_test_mutants()
    if not names:
        print("no mutants marked 'no tests'")
        return 0

    print(f"re-running {len(names)} mutants previously marked 'no tests'")
    return subprocess.call(
        [sys.executable, "-m", "mutmut", "run", "--max-children", "8", *names],
        cwd=REPO_ROOT,
    )


if __name__ == "__main__":
    sys.exit(main())
