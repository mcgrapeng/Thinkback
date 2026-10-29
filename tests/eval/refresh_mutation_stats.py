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

**变异范围变了要整棵重生成**：``only_mutate`` 新增一个文件后，mutmut 不一定
会为它生成变异体。``mutants/`` 里已经有一份该文件的**普通副本**（它此前在
范围外，被当作「忽略文件」拷进去的），而 mutmut 判断「这份文件已有变异体」
的依据是**副本的 mtime 比源文件新** —— 满足就直接复用那个空的变异体集合，
一个变异体都不生成。于是：新文件进范围 → 变异体数为 0 → 门禁照样绿，
**实际什么都没审计**。这个洞静默的：mutmut 不报错，``mutmut results``
也不显示异常，只是审计范围比声明的。

所以这里对「决定存在哪些变异体」的配置字段取指纹：指纹变了就**精确清掉**
会遮蔽变异生成的残留物（见 ``clear_stale_scope_artifacts``），让新范围真的
被变异。测试选择列表（``pytest_add_cli_args_test_selection``）不参与指纹 ——
它不影响变异体集合，只影响映射缓存（映射缓存本来就会被丢弃）。

用法（``make mutation-audit`` 的第一步）::

    uv run --group dev python tests/eval/refresh_mutation_stats.py
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MUTANTS_DIR = REPO_ROOT / "mutants"
STATS_PATH = MUTANTS_DIR / "mutmut-stats.json"
SCOPE_FINGERPRINT_PATH = MUTANTS_DIR / "mutmut-scope.sha256"
# 只有这几个字段决定「存在哪些变异体」；它们变了才需要整棵重生成。
SCOPE_FIELDS = ("source_paths", "paths_to_mutate", "only_mutate", "do_not_mutate")
# mutmut 用退出码 33 表示「没有测试覆盖到」（见 mutmut/__main__.py）。
NO_TESTS_EXIT_CODE = 33
# 退出码 0 = 变异体没被杀死（测试在变异后仍然通过）。
SURVIVED_EXIT_CODE = 0


def compute_scope_fingerprint(pyproject: Path | None = None) -> str:
    """「决定存在哪些变异体」的配置的指纹。

    只取 ``SCOPE_FIELDS``：测试选择列表变了不影响变异体集合，
    只需要重收映射（映射缓存本来就会被丢弃）。
    """

    path = pyproject or (REPO_ROOT / "pyproject.toml")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    config = data.get("tool", {}).get("mutmut", {})
    scope = {field: config.get(field) for field in SCOPE_FIELDS}
    return hashlib.sha256(
        json.dumps(scope, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def scope_changed(fingerprint: str) -> bool:
    """指纹与上次记录的不一致（没有记录也算变了）。"""

    if not SCOPE_FINGERPRINT_PATH.is_file():
        return True
    return SCOPE_FINGERPRINT_PATH.read_text(encoding="utf-8").strip() != fingerprint


def scope_patterns(pyproject: Path | None = None) -> list[str]:
    """当前声明的变异范围（``only_mutate``）。

    空列表 = 整个 ``source_paths`` 都在范围内（mutmut 的口径）。
    """

    path = pyproject or (REPO_ROOT / "pyproject.toml")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    config = data.get("tool", {}).get("mutmut", {})
    return [str(pattern) for pattern in (config.get("only_mutate") or [])]


def in_scope(source_path: str, patterns: list[str]) -> bool:
    """与 mutmut ``_should_include_for_mutation`` 同口径的匹配。

    mutmut 的口径：``only_mutate`` 为空则全都算；否则只有 ``.py`` 结尾的
    文件、且路径命中某个 glob 才算。口径不一致会让清理脚本删错文件。
    """

    if not patterns:
        return True
    if not source_path.endswith(".py"):
        return False
    return any(fnmatch.fnmatch(source_path, pattern) for pattern in patterns)


def clear_stale_scope_artifacts(patterns: list[str]) -> list[str]:
    """删掉会**遮蔽**变异生成的残留，让新范围真正被变异。

    两种残留都会让门禁静默失效：

    1. **进范围但没有 ``.meta`` 的文件**：``mutants/`` 里留着它此前在范围外
       时拷进去的普通副本，而 mutmut 判断「这份文件已有变异体」的依据是
       **副本 mtime 比源文件新** —— 满足就直接复用那个**空集合**，
       新增文件一个变异体都不生成，门禁照样绿。
    2. **出范围但还留着 ``.meta`` 的文件**：变异体仍在 ``mutmut results``
       里被计数，审计范围比声明的大（旧结论被当成现状）。

    只删这三个文件（源副本 + ``.meta`` + ``.spans``），不动其余内容 ——
    ``mutants/`` 是可再生的构建产物，精确清理比整棵删掉安全。
    """

    removed: list[str] = []
    # 进范围但没有 .meta → 删掉遮蔽生成的那份普通副本
    for source_path in _iter_source_files():
        if not in_scope(source_path, patterns):
            continue
        meta_path = MUTANTS_DIR / f"{source_path}.meta"
        if meta_path.exists():
            continue
        stale_copy = MUTANTS_DIR / source_path
        if not stale_copy.exists():
            continue
        stale_copy.unlink()
        removed.append(source_path)
    # 出范围但还有 .meta → 删掉过期的变异体记录
    for meta_path in sorted(MUTANTS_DIR.rglob("*.meta")):
        source_path = str(meta_path.relative_to(MUTANTS_DIR))[: -len(".meta")]
        if in_scope(source_path, patterns):
            continue
        meta_path.unlink()
        spans_path = meta_path.with_suffix(".spans")
        if spans_path.exists():
            spans_path.unlink()
        removed.append(source_path)
    return sorted(set(removed))


def _iter_source_files() -> list[str]:
    """``source_paths`` 下的源文件（相对仓库根，如 ``src/a/b.py``）。"""

    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    config = data.get("tool", {}).get("mutmut", {})
    roots = [str(root) for root in (config.get("source_paths") or [])]
    found: list[str] = []
    for root in roots:
        base = REPO_ROOT / root
        if not base.is_dir():
            continue
        found.extend(
            str(path.relative_to(REPO_ROOT)) for path in base.rglob("*.py") if path.is_file()
        )
    return sorted(found)


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
    fingerprint = compute_scope_fingerprint()
    if scope_changed(fingerprint):
        # 范围变了：mutants/ 里可能有「范围外的旧副本」，其 mtime 比源文件新，
        # mutmut 会当成「这份文件已有变异体」直接复用那个**空集合** ——
        # 新增文件一个变异体都不生成，门禁照样绿。精确清掉这些遮蔽物，
        # 让新范围真正被变异（mutmut/ 是可再生的构建产物）。
        removed = clear_stale_scope_artifacts(scope_patterns())
        if removed:
            print(f"mutation scope changed → cleared {len(removed)} stale artifact(s):")
            for item in removed:
                print(f"  - {item}")
        else:
            print("mutation scope changed → no stale artifacts to clear")
        MUTANTS_DIR.mkdir(parents=True, exist_ok=True)
        SCOPE_FINGERPRINT_PATH.write_text(fingerprint + "\n", encoding="utf-8")

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
