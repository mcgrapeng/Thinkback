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

#: 覆盖率版：保留「不许返回空列表」这条硬约束，补「一条消息里的每件事实都要
#: 抽」，并用示例把「输出形状」讲清楚（现行版本的输出形状写成一个元素的
#: 模板，模型就照着只给一条）。
V_COVERAGE = """Additional requirements:
1. Language: write each memory text in the SAME language and script as the conversation (Chinese conversation -> Chinese memories). Never translate names, equipment models, process nodes or brands into English.
2. Entities: keep entity values verbatim (device model, process node, tool id, brand). Do not transliterate them and do not add a parenthetical translation.
3. Coverage: extract EVERY distinct fact in the message. One message often carries two facts (which tool and which metrology tool, which process step and which defect); emit one memory per fact and never drop one. If the message contains ANY work fact (responsibility, equipment, tool, process parameter, target, constraint, deadline, customer, number), extract at least one memory about it - returning an empty list for such a message is WRONG. Return an empty list ONLY for pure greetings or acknowledgements.
4. Shape: {"memory": [{"id": "0", "text": "..."}, {"id": "1", "text": "..."}]} - one entry per fact, ids numbered from 0.
5. Corrections: when the user changes a value ("不叫 X，叫 Y"), record only the new value, never re-state the old one.

Examples:
Input: 刻蚀机台我们用 TEL 的 Divinity，量测用 KLA 的 2930。
Output: {"memory": [{"id": "0", "text": "用户的刻蚀机台使用 TEL 的 Divinity。"}, {"id": "1", "text": "用户的量测设备使用 KLA 的 2930。"}]}

Input: 3nm 良率卡在 CMP 段，缺陷密度偏高，主要是颗粒和针孔。
Output: {"memory": [{"id": "0", "text": "用户的 3nm 良率卡在 CMP 段。"}, {"id": "1", "text": "用户的缺陷密度偏高，主要是颗粒和针孔。"}]}"""

VARIANTS: dict[str, str] = {
    "current": DEFAULT_EXTRACTION_INSTRUCTIONS,
    "slim": V_SLIM,
    "slim_fewshot": V_SLIM_FEWSHOT,
    "fewshot_only": V_FEWSHOT_ONLY,
    "coverage": V_COVERAGE,
}


def _without_rules(*rule_numbers: int) -> str:
    """从现役指令里去掉某几条规则。

    现役指令的第一行是 "Additional requirements:"，后面每行一条规则，
    规则 N 在第 N 行。消融用：哪条规则真正承重，看去掉它之后空抽率与
    覆盖率怎么动，而不是凭感觉删。
    """
    lines = DEFAULT_EXTRACTION_INSTRUCTIONS.split("\n")
    dropped = set(rule_numbers)
    kept = [
        line
        for index, line in enumerate(lines)
        if index == 0 or (index - 1) not in dropped
    ]
    return "\n".join(kept)


#: 逐条消融：现行指令的 7 条规则各自承多少重。
ABLATIONS: dict[str, str] = {
    "no_rule1_language": _without_rules(1),
    "no_rule2_shape": _without_rules(2),
    "no_rule3_entity": _without_rules(3),
    "no_rule4_correction": _without_rules(4),
    "no_rule5_one_fact": _without_rules(5),
    "no_rule6_no_assistant": _without_rules(6),
    "no_rule7_empty_clause": _without_rules(7),
}

#: 在现役指令上补「一条消息里的每件事实都要抽」：消融显示语言、输出形状、
#: 一事一记忆、无 assistant 轮次这四条承重，不能删；缺口在第五条不够狠 ——
#: 一条消息带两件事时模型常只给一条。
V_MULTI_RULE = DEFAULT_EXTRACTION_INSTRUCTIONS + """
8. One message often carries two or more facts (which tool and which metrology tool, which process step and which defect, target and current value). Extract ALL of them: one memory per fact, never stop after the first."""

#: 再加一个两事实的示例，把输出形状落到具体条目上。
V_MULTI_EXAMPLE = V_MULTI_RULE + """

Example:
Input: 刻蚀机台我们用 TEL 的 Divinity，量测用 KLA 的 2930。
Output: {"memory": [{"id": "0", "text": "用户的刻蚀机台使用 TEL 的 Divinity。"}, {"id": "1", "text": "用户的量测设备使用 KLA 的 2930。"}]}"""

#: 中性示例：内容与评测语料、门禁语料都不重叠（物流场景，不是半导体）。
#: 用语料里的内容做示例等于把评测题抄进提示词，覆盖率会虚高 —— 那版只作
#: 对照，不作结论。
V_MULTI_EXAMPLE_NEUTRAL = V_MULTI_RULE + """

Example:
Input: 我负责华东仓的库存周转，目标是每季度压到 4 轮。
Output: {"memory": [{"id": "0", "text": "用户负责华东仓的库存周转。"}, {"id": "1", "text": "用户的库存周转目标是每季度压到 4 轮。"}]}"""

EXTRA_VARIANTS: dict[str, str] = {
    "multi_rule": V_MULTI_RULE,
    "multi_example_corpus": V_MULTI_EXAMPLE,
    "multi_example_neutral": V_MULTI_EXAMPLE_NEUTRAL,
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
        # 同一则语料的多个轮次共用一个作用域：mem0 会拿已有记忆做关联，
        # 拆开就测不到「一条消息里含几件事实」这种真实情形。
        user_id = f"ab-{uuid4().hex[:10]}"
        for payload in _user_rounds(case):
            outcome = client.add(  # type: ignore[attr-defined]
                [{"role": "user", "content": payload}],
                user_id=user_id,
                agent_id="thinkback",
                metadata={"memory_scope_id": "thinkback"},
                infer=True,
                prompt=prompt,
            )
            items = outcome.get("results") if isinstance(outcome, dict) else outcome
            round_texts = [
                str(item.get("memory") or item.get("text") or "")
                for item in items or []
                if isinstance(item, dict)
            ]
            texts.extend(round_texts)
            result.rounds += 1
            # 空抽按「这一轮有没有抽出东西」算，不是按累计。
            if not round_texts:
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
    parser.add_argument(
        "--ablations",
        action="store_true",
        help="额外跑逐条消融（现行指令去掉某一条规则）",
    )
    parser.add_argument(
        "--extra",
        action="store_true",
        help="额外跑候选增强版（在现行指令上加规则/示例）",
    )
    parser.add_argument("--out", default="docs/memory/report/real/prompt-ab.json")
    args = parser.parse_args()

    corpus = json.loads(Path(args.corpus).read_text(encoding="utf-8"))
    cases = list(corpus["cases"])[: args.cases]

    settings = get_settings()
    backend = get_memory_backend(settings)
    client = backend.memory_client

    pool: dict[str, str] = dict(VARIANTS)
    if args.ablations:
        pool.update(ABLATIONS)
    if args.extra:
        pool.update(EXTRA_VARIANTS)

    results = []
    for name, prompt in pool.items():
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
