"""pytest 套件的防回血基线：删用例必须是个显式动作。

变异基线（``tests/eval/mutation_survivors.json``）盯的是「变异体有没有被杀掉」，
它看不见**用例本身被删**。删掉一条用例，剩下的套件照样全绿 —— 门禁不会有任何反应，
而那条用例原本钉住的行为就此失去保护。

所以这里记一份用例清单：当前收集到的用例必须**覆盖**基线里的每一条。
少一条就失败。删用例要连带更新基线文件 —— 那是一次显式的、可 review 的动作，
而不是顺手一删就没了。

只看「少没少」，不看「多不多」：新增用例随时可以加。

用法：
    python tests/eval/check_pytest_baseline.py            # 校验（缺失即失败）
    python tests/eval/check_pytest_baseline.py --update   # 重新生成基线
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE_PATH = REPO_ROOT / "tests" / "eval" / "pytest_baseline.json"
COLLECT_TARGETS = ["tests/unit"]


def collect_test_ids() -> list[str]:
    """收集当前确定性套件的用例 ID（``tests/unit/...::test_xxx``）。

    ``-o addopts=""`` 是必须的：项目 addopts 里的 ``-q`` 会让 ``--collect-only``
    只输出「每个文件几条」而不是用例 ID，基线就退化成「文件级计数」——
    少一条又补一条的替换删法就抓不到了。
    """
    result = subprocess.run(
        [
            "uv",
            "run",
            "--group",
            "dev",
            "pytest",
            "-o",
            "addopts=",
            *COLLECT_TARGETS,
            "--collect-only",
            "-q",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(result.stdout[-4000:], file=sys.stderr)
        print(result.stderr[-4000:], file=sys.stderr)
        raise SystemExit(f"用例收集失败，退出码 {result.returncode}")

    ids = []
    for line in result.stdout.splitlines():
        line = line.strip()
        # 收集输出形如 tests/unit/test_x.py::test_y[param]
        if line.startswith("tests/") and "::" in line:
            ids.append(line)
    return sorted(set(ids))


def load_baseline() -> list[str]:
    if not BASELINE_PATH.is_file():
        raise SystemExit(f"基线文件不存在：{BASELINE_PATH}")
    payload = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    return sorted(str(item) for item in payload.get("tests", []))


def write_baseline(ids: list[str]) -> None:
    BASELINE_PATH.write_text(
        json.dumps(
            {
                "note": "pytest 套件的防回血基线：删用例要连带更新本文件（显式动作）",
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
    args = parser.parse_args()

    current = collect_test_ids()
    if not current:
        print("没有收集到任何用例：收集方式有问题，基线校验中止", file=sys.stderr)
        return 2

    if args.update:
        write_baseline(current)
        print(f"基线已更新：{len(current)} 条用例")
        return 0

    baseline = set(load_baseline())
    missing = sorted(baseline - set(current))
    if missing:
        print(
            f"用例被删除了 {len(missing)} 条（基线 {len(baseline)} 条，当前 {len(current)} 条）："
        )
        for item in missing:
            print(f"  - {item}")
        print(
            "\n删用例要连带更新基线："
            "python tests/eval/check_pytest_baseline.py --update\n"
            "确认这些行为确实不再需要保护，再更新。",
            file=sys.stderr,
        )
        return 1

    print(f"用例基线一致：{len(current)} 条（基线 {len(baseline)} 条）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
