"""分支覆盖缺口审计工具 —— 把「哪条分支没走到」变成可重复的测量。

**为什么需要它**：行覆盖率只能回答「这行跑过吗」，回答不了
「这行的**另一半**跑过吗」。一个 ``if`` 只有真分支被执行，行覆盖率是 100%，
但假分支（降级路径、错误路径、兜底路径）可能从来没被验证过。

第十四轮手工测过一次分支覆盖（208 处半覆盖分支），但**没有工具**：
下次再想测，得把同样的分析重做一遍。这份脚本把它变成一条命令。

用法::

    # 跑一遍带分支覆盖的套件并出报告（默认写 docs/memory/report/branch-audit.json）
    uv run --group dev python tests/eval/branch_audit.py

    # 只看某个模块的缺口
    uv run --group dev python tests/eval/branch_audit.py --file service.py

    # 连续两次对比（回归检测：新增的缺口会被点名）
    uv run --group dev python tests/eval/branch_audit.py --baseline <上次报告.json>

报告里每个缺口都带：
- ``file`` / ``line`` —— 位置
- ``arc`` —— 从哪行到哪行的转移没被走过
- ``kind`` —— 自动分类（见 ``classify_arc``）

分类的意义：**85 处缺口里只有一部分是真缺口**，其余是错误路径
或者「已有契约但本处没走到」。不分类就得逐条读源码，分不出优先级。

注意：工具**不**自动识别「不可达的分支」—— 那需要数据流分析，
源码行的形状做不到。真正恒假的分支要人工确认后写进审计报告。
"""

from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
DEFAULT_REPORT = REPO_ROOT / "docs" / "memory" / "report" / "branch-audit.json"

#: 覆盖率数据文件（``coverage run`` 产出的）
COVERAGE_DATA = REPO_ROOT / ".coverage"


@dataclass(frozen=True)
class Gap:
    """一处没走到的分支转移。"""

    file: str
    line: int
    to_line: int
    arc: str
    kind: str
    note: str

    @property
    def key(self) -> str:
        return f"{self.file}:{self.line}->{self.to_line}"


def _run_coverage() -> None:
    """跑一遍带分支覆盖的套件。

    ``--branch`` 是关键：不带它就没有分支数据。
    """
    if COVERAGE_DATA.exists():
        COVERAGE_DATA.unlink()
    subprocess.run(
        [
            "uv",
            "run",
            "--group",
            "dev",
            "coverage",
            "run",
            "--branch",
            "--source=src/thinkback",
            "-m",
            "pytest",
            "tests/unit",
            "-q",
            "-p",
            "no:warnings",
        ],
        cwd=REPO_ROOT,
        check=True,
    )


def _newest_source_mtime() -> float:
    """被测源码里最新的修改时间。

    用来判断现存的覆盖率数据是不是已经过期（源码改了、数据没重跑）。
    """
    newest = 0.0
    for path in (SRC_ROOT).rglob("*.py"):
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue
    return newest


def _warn_if_stale() -> None:
    """覆盖率数据比源码旧 → 说清楚，别让人拿着过期数字做决策。

    这个工具的全部价值是「测量」，给出过期测量等于没测。
    """
    if not COVERAGE_DATA.exists():
        print("::warning::没有覆盖率数据（.coverage 不存在）—— 请去掉 --skip-run 重跑")
        return
    data_mtime = COVERAGE_DATA.stat().st_mtime
    source_mtime = _newest_source_mtime()
    if source_mtime > data_mtime:
        print(
            "::warning::覆盖率数据比源码旧（源码改过之后没重跑）—— "
            "下面的数字是过期的，去掉 --skip-run 重跑"
        )


def _coverage_json() -> dict[str, Any]:
    out = REPO_ROOT / "docs" / "memory" / "report" / "branch-audit-coverage.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["uv", "run", "--group", "dev", "coverage", "json", "-o", str(out)],
        cwd=REPO_ROOT,
        check=True,
    )
    return json.loads(out.read_text(encoding="utf-8"))


def classify_arc(source_line: str, line: int, to_line: int, body: str = "") -> tuple[str, str]:
    """给一处缺口分类。

    分类依据是**源码行的形状**（`if` 那行 + 分支体第一行），
    不是行号 —— 换句话说，规则本身与具体模块无关，换个模块也适用。

    返回 ``(kind, note)``：

    - ``error_path`` —— 错误 / 降级路径
    - ``cleanup`` —— 资源归还类（finally / 释放 / 归还槽位）
    - ``early_return`` —— 早退 / 短路分支
    - ``loop_back`` —— 循环回边（提前 break 的那一侧）
    - ``feature`` —— 正常功能分支

    注意：这里**没有**「不可达分支」这一类。判断一个条件恒假需要数据流
    分析，源码行的形状做不到 —— 之前那版规则（``isinstance`` 且带 ``raise``）
    一次都没命中过，属于写了但跑不到的规则；真要标不可达分支只能人工确认，
    写在审计报告里，不要让工具假装能自动识别。
    """
    text = source_line.strip()

    # 异常处理
    if text.startswith("except") or text.startswith("finally"):
        return "error_path", "异常处理分支"

    # 资源释放 / 槽位归还
    if any(marker in text for marker in ("release", "close()", "discard", "shutdown", "free()")):
        return "cleanup", "资源归还分支"

    # 类型判断（isinstance 防御）：不是 dict 就抛错的那类 —— 算错误路径。
    if text.startswith("if not isinstance(") or text.startswith("if isinstance("):
        return "error_path", "类型判断分支"

    # 循环回边：弧的终点在分支体里（回到循环头）
    if to_line < line and body.strip():
        return "loop_back", "循环提前退出的一侧"

    # 早退守卫：if ...: return（分支体是 return）
    if text.startswith("if ") and body.strip().startswith("return"):
        return "early_return", "早退/短路分支"

    # 「x is None」+ 后续有动作 —— 数据缺省分支
    if " is None" in text:
        return "feature", "数据缺省分支"

    return "feature", "功能分支"


def collect_gaps(coverage: dict[str, Any]) -> list[Gap]:
    """从覆盖率 JSON 里取出所有没走到的分支。"""
    gaps: list[Gap] = []
    for path, info in coverage["files"].items():
        missing = info.get("missing_branches") or []
        if not missing:
            continue
        source_path = REPO_ROOT / path
        lines = (
            source_path.read_text(encoding="utf-8").splitlines() if source_path.is_file() else []
        )
        for line, to_line in missing:
            source_line = lines[line - 1] if 0 < line <= len(lines) else ""
            # 分支体第一行：判定「这一侧是早退/循环回边/正常路径」
            body = lines[to_line - 1] if 0 < to_line <= len(lines) else ""
            kind, note = classify_arc(source_line, line, to_line, body)
            gaps.append(
                Gap(
                    file=path,
                    line=line,
                    to_line=to_line,
                    arc=f"{line}->{to_line}",
                    kind=kind,
                    note=note,
                )
            )
    gaps.sort(key=lambda gap: (gap.file, gap.line, gap.to_line))
    return gaps


def _scoped(gaps: list[Gap], only_file: str | None) -> list[Gap]:
    """按模块过滤缺口。``only_file`` 是子串匹配（``service.py`` 能命中多个文件）。"""
    if only_file is None:
        return gaps
    return [gap for gap in gaps if only_file in gap.file]


def build_report(
    coverage: dict[str, Any], gaps: list[Gap], *, only_file: str | None = None
) -> dict[str, Any]:
    """把覆盖率数据与缺口列表合成一份报告。

    ``only_file`` 给了就只看那个模块 —— **所有口径都按过滤后的算**
    （缺口数、按类型/按模块的分布、总量），不能出现「缺口列表是过滤后的、
    汇总数字还是全量的」这种同报告两个口径的情况。
    """
    totals = coverage["totals"]
    by_kind: dict[str, int] = {}
    by_file: dict[str, int] = {}

    # 过滤在这里做一次，下面所有数字都从同一份列表派生 ——
    # 不能出现「缺口列表是过滤后的、汇总数字还是全量的」这种同报告两个口径。
    gaps = _scoped(gaps, only_file)

    for gap in gaps:
        by_kind[gap.kind] = by_kind.get(gap.kind, 0) + 1
        by_file[gap.file] = by_file.get(gap.file, 0) + 1

    # 每个模块的分支覆盖率 —— 报告要自包含：不看外部覆盖率文件也能知道
    # 哪个模块还差多少。
    file_stats: dict[str, dict[str, Any]] = {}
    for path, info in coverage["files"].items():
        summary = info["summary"]
        num = summary.get("num_branches", 0)
        if not num:
            continue
        if only_file is not None and only_file not in path:
            continue
        cov = summary.get("covered_branches", 0)
        file_stats[path] = {
            "num_branches": num,
            "covered_branches": cov,
            "missing_branches": summary.get("missing_branches", 0) or 0,
            "branch_coverage": round(cov / num, 4),
            "statement_coverage": round(
                summary.get("covered_lines", 0)
                / max(1, summary.get("covered_lines", 0) + summary.get("missing_lines", 0)),
                4,
            ),
        }

    if only_file is None:
        total_branches = totals.get("num_branches", 0)
        covered = totals.get("covered_branches", 0)
        missing = totals.get("missing_branches", 0)
        statement_coverage = round(
            totals.get("covered_lines", 0)
            / max(1, totals.get("covered_lines", 0) + totals.get("missing_lines", 0)),
            4,
        )
    else:
        # 过滤后的口径：总量 = 过滤到的那些模块之和
        total_branches = sum(s["num_branches"] for s in file_stats.values())
        covered = sum(s["covered_branches"] for s in file_stats.values())
        missing = sum(s["missing_branches"] for s in file_stats.values())
        statement_coverage = (
            round(
                sum(s["statement_coverage"] * s["num_branches"] for s in file_stats.values())
                / total_branches,
                4,
            )
            if total_branches
            else 0.0
        )

    return {
        "report_type": "thinkback_branch_coverage_audit",
        "totals": {
            "num_branches": total_branches,
            "covered_branches": covered,
            "missing_branches": missing,
            "branch_coverage": round(covered / total_branches, 4) if total_branches else 0.0,
            "statement_coverage": statement_coverage,
        },
        "gaps_by_kind": by_kind,
        "gaps_by_file": by_file,
        "file_stats": file_stats,
        "gap_count": len(gaps),
        "gaps": [asdict(gap) for gap in gaps],
    }


def _load_baseline(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {gap["file"] + ":" + gap["arc"] for gap in payload.get("gaps", [])}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="分支覆盖缺口审计")
    parser.add_argument("--file", default=None, help="只看某个模块（如 service.py）")
    parser.add_argument("--baseline", default=None, help="与上次报告对比，点名新增缺口")
    parser.add_argument("--report", default=str(DEFAULT_REPORT), help="报告输出路径")
    parser.add_argument("--skip-run", action="store_true", help="不重跑套件，用现有覆盖率数据")
    args = parser.parse_args(argv)

    if not args.skip_run:
        _run_coverage()
    else:
        _warn_if_stale()

    coverage = _coverage_json()
    gaps = collect_gaps(coverage)
    report = build_report(coverage, gaps, only_file=args.file)

    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    totals = report["totals"]
    print(
        f"分支覆盖：{totals['covered_branches']}/{totals['num_branches']} = {totals['branch_coverage']:.2%}"
    )
    print(f"语句覆盖：{totals['statement_coverage']:.2%}")
    print(f"未覆盖分支：{report['gap_count']} 处")
    print()
    print("按类型：")
    for kind, count in sorted(report["gaps_by_kind"].items(), key=lambda kv: -kv[1]):
        print(f"  {kind:12} {count}")
    print()
    if args.file:
        print(f"按模块（只列 {args.file} 匹配到的）：")
    else:
        print("按模块（分支覆盖率最低的 12 个）：")
    file_stats = report.get("file_stats", {})
    ranked = sorted(
        file_stats.items(),
        key=lambda kv: (kv[1]["branch_coverage"], -kv[1]["missing_branches"]),
    )
    for file, stats in ranked[:12]:
        print(f"  {stats['branch_coverage']:>7.2%}  缺 {stats['missing_branches']:>3}  {file}")

    if args.baseline:
        baseline_path = Path(args.baseline)
        previous = _load_baseline(baseline_path)
        current = {gap.key for gap in _scoped(gaps, args.file)}
        if args.file:
            # 基线对比要同一个口径 —— 不然别的模块的缺口会被算成「已修复」
            previous = {key for key in previous if args.file in key}
        new_gaps = sorted(current - previous)
        fixed = sorted(previous - current)
        print()
        print(f"与基线 {baseline_path.name} 对比：")
        print(f"  新增缺口 {len(new_gaps)} 处")
        for item in new_gaps:
            print(f"    + {item}")
        print(f"  已修复 {len(fixed)} 处")
        for item in fixed:
            print(f"    - {item}")

    print()
    print(f"报告：{report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
