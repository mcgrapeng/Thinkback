"""变异存活基线门禁：出现**新增**存活 / 无测试变异体即失败。

mutmut 3.8 即使有存活变异体也退出 0，`mutmut results` 只是一张给人看的快照——
光跑 mutmut 不构成门禁。本脚本把当前结果与已验收基线
（``tests/eval/mutation_survivors.json``）比对：

- 出现基线之外的存活 / 无测试变异体 → 退出 1（新增盲区：补测试，或登记理由）
- 基线里的变异体已被杀死 → 只提示，不失败（是进步，可顺手清理基线）

用法：``uv run --group dev python tests/eval/check_mutation_survivors.py``
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE_PATH = Path(__file__).with_name("mutation_survivors.json")
# mutmut results 的输出形如 "    <变异体名>: <状态>"
RESULT_LINE = re.compile(r"^\s*(?P<name>\S+):\s+(?P<status>.+?)\s*$")
SURVIVED = "survived"
NO_TESTS = "no tests"
# 超时也是「没被杀掉」：测试没跑完，可能是变异体让代码死循环，也可能是
# 墙钟估算偏了。**两种都不该被静默放过** —— 门禁以前只认 survived / no tests，
# timeout 直接从门禁视野里消失了，等于「跑不完的用例」这一类盲区无人看守。
TIMEOUT = "timeout"


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


def main() -> int:
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    accepted_survivors = set(baseline["accepted_survivors"])
    accepted_no_tests = set(baseline["accepted_no_tests"])
    accepted_timeouts = set(baseline.get("accepted_timeouts", []))

    results = read_results()
    survivors = {name for name, status in results.items() if status == SURVIVED}
    no_tests = {name for name, status in results.items() if status == NO_TESTS}
    timeouts = {name for name, status in results.items() if status == TIMEOUT}

    new_survivors = sorted(survivors - accepted_survivors)
    new_no_tests = sorted(no_tests - accepted_no_tests)
    new_timeouts = sorted(timeouts - accepted_timeouts)
    fixed = sorted(
        (accepted_survivors - survivors)
        | (accepted_no_tests - no_tests)
        | (accepted_timeouts - timeouts)
    )

    print(
        f"变异存活基线：当前存活 {len(survivors)} 个 / 无测试 {len(no_tests)} 个 / "
        f"超时 {len(timeouts)} 个；已验收 {len(accepted_survivors)} 个 / "
        f"{len(accepted_no_tests)} 个 / {len(accepted_timeouts)} 个"
    )
    if fixed:
        print(f"已被杀死（可从基线删除，共 {len(fixed)} 个）：")
        for name in fixed:
            print(f"  - {name}")
    if new_survivors or new_no_tests or new_timeouts:
        print(
            f"新增存活变异体 {len(new_survivors)} 个，"
            f"新增无测试变异体 {len(new_no_tests)} 个，"
            f"新增超时变异体 {len(new_timeouts)} 个："
        )
        for name in new_survivors:
            print(f"  + survived: {name}")
        for name in new_no_tests:
            print(f"  + no tests: {name}")
        for name in new_timeouts:
            print(f"  + timeout: {name}")
        print(
            "补测试把它们杀掉，或在 tests/eval/mutation_survivors.json 里登记验收理由"
            "（超时登记到 accepted_timeouts）。"
        )
        return 1
    print("无新增存活变异体，基线一致。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
