"""中文记忆回放评测（P2#8）。

目的：给召回质量一个**可重复、可对比**的中文基线（无权威中文长期记忆基准，
见 RESEARCH.md #8）。评测口径对齐学界共识：分数必须与成本成对报告。

形态：每个场景 = 一组真实感中文多轮 append + 一组召回问题（期望命中实体）。
对运行中的服务（HTTP）回放，输出：通过率、p50/p95 延迟、每场景 token 注入量。

用法：
    # 服务需已启动（no_proxy='*' 环境）
    .venv/bin/python script/eval/zh_replay_eval.py [--base http://127.0.0.1:8000]

退出码：全部通过=0；任何断言失败=1（可挂 CI）。

判分说明：期望答案是"必须出现在召回 L3 内容里的实体/短语"（substring）。
这是保守口径——衡量"该记得的有没有记对"，不衡量排序质量（后续可加
LLM-judge 升级为分级判分）。
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass
class Question:
    query: str
    expected_in_answer: list[str]


@dataclass
class Scenario:
    name: str
    rounds: list[tuple[str, str]]  # (user_text, assistant_text)
    questions: list[Question]
    results: list[bool] = field(default_factory=list)
    latencies_ms: list[float] = field(default_factory=list)
    injected_chars: int = 0


SCENARIOS: list[Scenario] = [
    Scenario(
        name="宠物与昵称",
        rounds=[
            ("我养了一只猫，名字叫麻薯，是只英短，特别黏人。", "麻薯听起来很可爱！"),
            ("以后请叫我小朋吧，这是我的昵称。", "好的，小朋！"),
        ],
        questions=[
            Question("我的猫叫什么名字？", ["麻薯"]),
            Question("你应该怎么称呼我？", ["小朋"]),
        ],
    ),
    Scenario(
        name="地点与工作变迁",
        rounds=[
            ("我最近搬到杭州工作了，在滨江上班。", "杭州滨江很不错，通勤方便吗？"),
            ("我不喜欢被催睡觉，这会让我更焦虑。", "记住了，不会催你睡觉。"),
        ],
        questions=[
            Question("我现在住在哪个城市？", ["杭州"]),
            Question("关于睡觉我有什么偏好？", ["睡觉"]),
        ],
    ),
    Scenario(
        name="饮食偏好",
        rounds=[
            ("我最喜欢喝美式咖啡，拿铁不爱喝。", "记住了，美式咖啡。"),
            ("我对花生过敏，吃东西要注意。", "花生过敏很重要，我会注意。"),
        ],
        questions=[
            Question("我最喜欢喝什么？", ["美式"]),
            Question("我有什么过敏？", ["花生"]),
        ],
    ),
    Scenario(
        name="事实更新（新事实优先）",
        rounds=[
            ("我养了一只狗，名字叫旺财。", "旺财很乖！"),
            ("我的狗不叫旺财了，现在叫来福。", "好的，来福！"),
        ],
        questions=[
            Question("我的狗现在叫什么名字？", ["来福"]),
        ],
    ),
]


def _append(
    client: httpx.Client,
    base: str,
    run: str,
    user: str,
    session: str,
    round_id: str,
    index: int,
    user_text: str,
    assistant_text: str,
) -> None:
    response = client.post(
        f"{base}/memory/append",
        json={
            "request_id": f"req-{run}-{round_id}",
            "user_id": user,
            "session_id": session,
            "round_id": f"{run}-{round_id}",
            "round_index": index,
            "messages": [
                {
                    "message_id": f"{round_id}-u",
                    "role": "user",
                    "content": user_text,
                    "timestamp": "2026-09-08T10:00:00+00:00",
                },
                {
                    "message_id": f"{round_id}-a",
                    "role": "assistant",
                    "content": assistant_text,
                    "timestamp": "2026-09-08T10:00:01+00:00",
                },
            ],
            "source_timestamp": "2026-09-08T10:00:01+00:00",
        },
        timeout=30,
    )
    response.raise_for_status()


def _recall(client: httpx.Client, base: str, user: str, session: str, query: str) -> dict[str, Any]:
    response = client.post(
        f"{base}/memory/recall",
        json={
            "user_id": user,
            "session_id": session,
            "query": query,
            "intent": "chat",
            "l3_limit": 8,
            "l3_score_threshold": 0.3,
            "token_budget": 2000,
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def _wait_l3(client: httpx.Client, base: str, task_ids: list[str], timeout: float = 420.0) -> None:
    deadline = time.monotonic() + timeout
    pending = set(task_ids)
    while pending and time.monotonic() < deadline:
        for task_id in list(pending):
            response = client.get(f"{base}/memory/tasks/{task_id}", timeout=15)
            if response.status_code == 200:
                status = response.json().get("status")
                if status in {"completed", "failed", "dead_letter"}:
                    pending.discard(task_id)
        if pending:
            time.sleep(2)


def _percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(ratio * len(ordered)))
    return ordered[index]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    base = args.base
    run = uuid.uuid4().hex[:8]

    client = httpx.Client(trust_env=False, timeout=30.0)
    ready = client.get(f"{base}/health/ready", timeout=15)
    if ready.status_code != 200:
        print(f"service not ready: {ready.status_code} {ready.text[:120]}")
        return 2

    total_questions = 0
    total_passed = 0
    all_latencies: list[float] = []
    total_injected_chars = 0

    print(f"== 中文回放评测 run={run} base={base} ==")
    for scenario in SCENARIOS:
        user = f"eval-{run}-{scenario.name}"
        session = f"session-{run}"
        task_ids: list[str] = []
        for index, (user_text, assistant_text) in enumerate(scenario.rounds, start=1):
            round_id = f"s{len(SCENARIOS)}-{uuid.uuid4().hex[:6]}"
            _append(client, base, run, user, session, round_id, index, user_text, assistant_text)
            # append 响应里有 task_id，但为简化直接按命名约定轮询
            task_ids.append(f"memory-extract:{run}-{round_id}")
        _wait_l3(client, base, task_ids)

        for question in scenario.questions:
            started = time.monotonic()
            payload = _recall(client, base, user, session, question.query)
            latency_ms = (time.monotonic() - started) * 1000
            scenario.latencies_ms.append(latency_ms)
            all_latencies.append(latency_ms)
            contents = " ".join(item.get("content", "") for item in payload.get("items", []))
            scenario.injected_chars += len(contents)
            total_injected_chars += len(contents)
            passed = all(expected in contents for expected in question.expected_in_answer)
            scenario.results.append(passed)
            total_questions += 1
            total_passed += int(passed)
            mark = "PASS" if passed else "FAIL"
            missing = [e for e in question.expected_in_answer if e not in contents]
            print(
                f"  [{mark}] {scenario.name} | q={question.query!r}"
                + (f" | 缺失={missing}" if missing else "")
                + f" | {latency_ms:.0f}ms"
            )

    rate = total_passed / total_questions if total_questions else 0.0
    print(
        f"\n== 通过率 {total_passed}/{total_questions} ({rate:.0%}) | "
        f"召回延迟 p50={_percentile(all_latencies, 0.5):.0f}ms "
        f"p95={_percentile(all_latencies, 0.95):.0f}ms | "
        f"注入总量 {total_injected_chars} chars =="
    )
    client.close()
    return 0 if total_passed == total_questions else 1


if __name__ == "__main__":
    sys.exit(main())
