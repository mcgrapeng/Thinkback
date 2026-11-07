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

分类的意义：**85 处缺口里只有一部分是真缺口**，其余是「不可达的防御分支」
或者「已有契约但本处没走到」。不分类就得逐条读源码，分不出优先级。
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

    分类依据是**源码行的形状**（`if` 那行 + 分支体第一行），不是行号 ——
    换句话说，规则本身与具体模块无关，换个模块也适用。

    返回 ``(kind, note)``：

    - ``dead_defensive`` —— 不可达的防御分支（条件恒假，如「JSON 解析成功但不是 dict」）
    - ``error_path`` —— 错误 / 降级路径
    - ``cleanup`` —— 资源归还类（finally / 释放 / 归还槽位）
    - ``early_return`` —— 早退 / 短路分支
    - ``loop_back`` —— 循环回边（提前 break 的那一侧）
    - ``feature`` —— 正常功能分支
    """
    text = source_line.strip()

    # 异常处理
    if text.startswith("except") or text.startswith("finally"):
        return "error_path", "异常处理分支"

    # 资源释放 / 槽位归还
    if any(marker in text for marker in ("release", "close()", "discard", "shutdown", "free()")):
        return "cleanup", "资源归还分支"

    # 类型防御：isinstance 检查
    if text.startswith("if not isinstance(") or text.startswith("if isinstance("):
        if "raise" in text:
            return "dead_defensive", "类型防御分支（条件恒假）"
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


def build_report(coverage: dict[str, Any], gaps: list[Gap]) -> dict[str, Any]:
    totals = coverage["totals"]
    by_kind: dict[str, int] = {}
    by_file: dict[str, int] = {}
    for gap in gaps:
        by_kind[gap.kind] = by_kind.get(gap.kind, 0) + 1
        by_file[gap.file] = by_file.get(gap.file, 0) + 1
    total_branches = totals.get("num_branches", 0)
    covered = totals.get("covered_branches", 0)

    # 每个模块的分支覆盖率 —— 报告要自包含：不看外部覆盖率文件也能知道
    # 哪个模块还差多少。
    file_stats: dict[str, dict[str, Any]] = {}
    for path, info in coverage["files"].items():
        summary = info["summary"]
        num = summary.get("num_branches", 0)
        if not num:
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

    return {
        "report_type": "thinkback_branch_coverage_audit",
        "totals": {
            "num_branches": total_branches,
            "covered_branches": covered,
            "missing_branches": totals.get("missing_branches", 0),
            "branch_coverage": round(covered / total_branches, 4) if total_branches else 0.0,
            "statement_coverage": round(
                totals.get("covered_lines", 0)
                / max(
                    1,
                    totals.get("covered_lines", 0) + totals.get("missing_lines", 0),
                ),
                4,
            ),
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

    coverage = _coverage_json()
    gaps = collect_gaps(coverage)
    report = build_report(coverage, gaps)

    if args.file:
        gaps = [gap for gap in gaps if args.file in gap.file]
        report["gaps"] = [asdict(gap) for gap in gaps]
        report["gap_count"] = len(gaps)

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
    print(
        f"未覆盖分支：{totals['missing_branches']} 处（其中 {report['gap_count']} 处列在本文件内）"
    )
    print()
    print("按类型：")
    for kind, count in sorted(report["gaps_by_kind"].items(), key=lambda kv: -kv[1]):
        print(f"  {kind:12} {count}")
    print()
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
        current = {gap.key for gap in gaps}
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
