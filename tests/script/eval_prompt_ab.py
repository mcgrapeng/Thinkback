"""抽取指令 A/B：用真实评测语料量覆盖率、空抽率与语言合规率。

为什么要有这个脚本：自包含评测跑的是假后端，看不出抽取指令的效果；而真实
栈上门禁的「期望事实覆盖率」只有 0.50，一次运行分不清是抽取指令的问题还是
语料的问题。同一批语料、同一套后端，只换指令，才能把两件事分开。

跑法（需要可达的 LLM / embedding endpoint 与 Milvus）::

    MILVUS_URL=http://localhost:19540 uv run --group dev python tests/script/eval_prompt_ab.py

退出码 0 = 有变体达标（覆盖率不低于地板）；非 0 = 全都没达标。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv


def _prepare_environment(env_file: str | None) -> None:
    """先读凭据、再清代理。Settings 在导入时构造，晚了就来不及。"""

    load_dotenv(env_file)
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        os.environ.pop(key, None)
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"


_prepare_environment(
    sys.argv[sys.argv.index("--env-file") + 1] if "--env-file" in sys.argv else None
)

from thinkback.api.dependencies import get_memory_backend  # noqa: E402
from thinkback.domain.prompts import DEFAULT_EXTRACTION_INSTRUCTIONS  # noqa: E402
from thinkback.infra.config import get_settings  # noqa: E402

CORPUS_PATH = Path("tests/eval/semiconductor_cases.json")

# 覆盖率地板：与真实栈门禁同源（tests/eval/baseline_semiconductor_real.json）。
COVERAGE_FLOOR = 0.66

CJK = re.compile(r"[一-鿿]")

# ── 候选指令 ──────────────────────────────────────────────────────────

#: 现行版本：7 条规则，含输出形状与「何时返回空列表」。
V_CURRENT = "current"

#: 瘦身版：删掉输出形状（mem0 的 system prompt 已经定义）与空列表条款。
V_SLIM = """1. Write each memory in the SAME language and script as the conversation. Chinese input -> Chinese memories. Never translate names, equipment models or brands into English.
2. Keep entity values verbatim (设备型号、工艺节点、机台编号、品牌名逐字保留). Do not transliterate, do not explain them in parentheses.
3. One fact per memory. Split a turn that carries several distinct facts into several memories.
4. Record the user's own facts: responsibilities, equipment, tools, process parameters, targets, constraints, deadlines, customer information, numeric specifics."""

#: 瘦身版 + 两个示例。
V_SLIM_FEWSHOT = (
    V_SLIM
    + """

Examples:
Input: 我负责 28nm 逻辑工艺的整合，主要做前道 CMP 段的优化。
Output: {"memory": [{"id": "0", "text": "用户负责 28nm 逻辑工艺的整合，主要做前道 CMP 段的优化。"}]}

Input: 我们这条线用 193nm 浸没式光刻做 14nm 逻辑。
Output: {"memory": [{"id": "0", "text": "用户产线使用 193nm 浸没式光刻做 14nm 逻辑。"}]}"""
)

#: 只留示例，规则压到最少。
V_FEWSHOT_ONLY = """Extract the user's own facts from each message. Keep entity values verbatim and write in the message's own language.

Examples:
Input: 我负责 28nm 逻辑工艺的整合，主要做前道 CMP 段的优化。
Output: {"memory": [{"id": "0", "text": "用户负责 28nm 逻辑工艺的整合，主要做前道 CMP 段的优化。"}]}

Input: 我平时用 Neovim 写 Python，报表口径按下季度。
Output: {"memory": [{"id": "0", "text": "用户平时用 Neovim 写 Python。"}, {"id": "1", "text": "用户的报表口径是按下季度。"}]}"""

VARIANTS: dict[str, str] = {
    V_CURRENT: DEFAULT_EXTRACTION_INSTRUCTIONS,
    V_SLIM: V_SLIM,
    V_SLIM_FEWSHOT: V_SLIM_FEWSHOT,
    V_FEWSHOT_ONLY: V_FEWSHOT_ONLY,
}


@dataclass
class VariantResult:
    name: str
    covered: int = 0
    expected: int = 0
    rounds: int = 0
    empty_rounds: int = 0
    chinese_hits: int = 0
    total_hits: int = 0
    seconds: float = 0.0
    per_case: list[dict[str, object]] = field(default_factory=list)

    @property
    def coverage(self) -> float:
        return self.covered / self.expected if self.expected else 0.0

    @property
    def empty_rate(self) -> float:
        return self.empty_rounds / self.rounds if self.rounds else 0.0

    @property
    def chinese_rate(self) -> float:
        return self.chinese_hits / self.total_hits if self.total_hits else 0.0


def _user_rounds(case: dict[str, object]) -> list[str]:
    rounds = case.get("rounds") or []
    texts: list[str] = []
    for item in rounds:
        if isinstance(item, dict):
            user = item.get("user")
            if isinstance(user, str) and user.strip():
                texts.append(user)
    return texts


def run_variant(
    name: str,
    prompt: str,
    cases: list[dict[str, object]],
    *,
    client: object,
) -> VariantResult:
    result = VariantResult(name=name)
    started = time.monotonic()
    for case in cases:
        expected = [str(term) for term in (case.get("expected_terms") or [])]
        texts: list[str] = []
        for payload in _user_rounds(case):
            outcome = client.add(  # type: ignore[attr-defined]
                [{"role": "user", "content": payload}],
                user_id=f"ab-{uuid4().hex[:10]}",
                agent_id="thinkback",
                metadata={"memory_scope_id": "thinkback"},
                infer=True,
                prompt=prompt,
            )
            items = outcome.get("results") if isinstance(outcome, dict) else outcome
            for item in items or []:
                if isinstance(item, dict):
                    texts.append(str(item.get("memory") or item.get("text") or ""))
            result.rounds += 1
            if not texts:
                result.empty_rounds += 1
        hits = [term for term in expected if any(term in text for text in texts)]
        result.covered += len(hits)
        result.expected += len(expected)
        result.total_hits += len(texts)
        result.chinese_hits += sum(1 for text in texts if CJK.search(text))
        result.per_case.append(
            {
                "case": case.get("name"),
                "hit": hits,
                "expected": expected,
                "memories": len(texts),
            }
        )
    result.seconds = round(time.monotonic() - started, 1)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=int, default=8, help="用前 N 条语料")
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--corpus", default=str(CORPUS_PATH))
    parser.add_argument("--out", default="docs/memory/report/real/prompt-ab.json")
    args = parser.parse_args()

    corpus = json.loads(Path(args.corpus).read_text(encoding="utf-8"))
    cases = list(corpus["cases"])[: args.cases]

    settings = get_settings()
    backend = get_memory_backend(settings)
    client = backend.memory_client

    results = []
    for name, prompt in VARIANTS.items():
        print(f"\n===== 变体 {name} =====")
        result = run_variant(name, prompt, cases, client=client)
        print(
            f"覆盖率 {result.coverage:.2f}（{result.covered}/{result.expected}）"
            f"｜空抽率 {result.empty_rate:.2f}"
            f"｜中文率 {result.chinese_rate:.2f}"
            f"｜耗时 {result.seconds}s"
        )
        for item in result.per_case:
            print(f"  {item['case']}: 命中 {item['hit']} / 期望 {item['expected']}")
        results.append(result)

    payload = {
        "report_type": "thinkback_extraction_prompt_ab",
        "generated_at": datetime.now(UTC).isoformat(),
        "corpus": args.corpus,
        "cases_used": len(cases),
        "coverage_floor": COVERAGE_FLOOR,
        "variants": [
            {
                "name": item.name,
                "coverage": item.coverage,
                "covered": item.covered,
                "expected": item.expected,
                "empty_rate": item.empty_rate,
                "chinese_rate": item.chinese_rate,
                "seconds": item.seconds,
                "per_case": item.per_case,
            }
            for item in results
        ],
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n===== 汇总 =====")
    for item in results:
        flag = "达标" if item.coverage >= COVERAGE_FLOOR else "未达标"
        print(
            f"{item.name:<16} 覆盖率 {item.coverage:.2f}｜空抽率 {item.empty_rate:.2f}"
            f"｜中文率 {item.chinese_rate:.2f}｜{flag}"
        )
    print(f"报告: {out_path}")

    best = max(results, key=lambda item: item.coverage)
    if best.coverage < COVERAGE_FLOOR:
        print(f"没有变体达到地板 {COVERAGE_FLOOR}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
