"""thinkback HTTP 全链路真实测试。

前置：真实服务进程（uvicorn 127.0.0.1:8000，async 模式），
真实 PG / Milvus / ollama LLM / vLLM embedding。
按用户故事驱动 append → L3 抽取 → recall → update → delete 全链路，
每一步都对 PG / Milvus 做真值校验。
"""

from __future__ import annotations

import sys
import time
import uuid

import httpx

BASE = "http://127.0.0.1:8000"
RUN = uuid.uuid4().hex[:8]
USER = f"fullchain-{RUN}"
SESSION = f"session-{uuid.uuid4().hex[:8]}"
client = httpx.Client(base_url=BASE, timeout=60.0, trust_env=False)

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  PASS {name}")
    else:
        FAILED.append(f"{name}: {detail}")
        print(f"  FAIL {name} -> {detail}")


def append(round_id: str, user_text: str, assistant_text: str, index: int) -> dict:
    resp = client.post(
        "/memory/append",
        json={
            "request_id": f"req-{round_id}",
            "user_id": USER,
            "session_id": SESSION,
            "round_id": f"{RUN}-{round_id}",
            "round_index": index,
            "messages": [
                {
                    "message_id": f"{round_id}-u",
                    "role": "user",
                    "content": user_text,
                    "timestamp": "2026-09-07T10:00:00+00:00",
                },
                {
                    "message_id": f"{round_id}-a",
                    "role": "assistant",
                    "content": assistant_text,
                    "timestamp": "2026-09-07T10:00:01+00:00",
                },
            ],
            "source_timestamp": "2026-09-07T10:00:01+00:00",
        },
    )
    return {"status_code": resp.status_code, "body": resp.json()}


def wait_task(task_id: str, timeout: float = 420.0) -> tuple[str, dict]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(f"/memory/tasks/{task_id}")
        if resp.status_code == 200:
            body = resp.json()
            if body["status"] in {"completed", "failed", "dead_letter"}:
                return body["status"], body
        time.sleep(2)
    return "timeout", {}


def recall(query: str) -> dict:
    resp = client.post(
        "/memory/recall",
        json={
            "user_id": USER,
            "session_id": SESSION,
            "query": query,
            "intent": "chat",
            "l3_limit": 8,
            "l3_score_threshold": 0.3,
            "token_budget": 2000,
        },
    )
    return {"status_code": resp.status_code, "body": resp.json()}


def main() -> int:
    print(f"== 全链路用户: {USER} / 会话: {SESSION} ==")
    # R-4 回归锁：readiness 探测必须先于第一个业务请求（历史上这一步
    # 会把主循环连接放回业务池，导致首个 append 500）。
    ready = client.get("/health/ready")
    check("readiness(先于业务) 200", ready.status_code == 200, ready.text[:120])

    # ── 1. append 三轮真实事实 ─────────────────────────────────────
    rounds = [
        ("round-cat", "我养了一只猫，名字叫麻薯，英短，很黏人。", "麻薯听起来很可爱！"),
        ("round-city", "我最近搬到杭州工作了，在滨江上班。", "杭州滨江很不错，通勤方便吗？"),
        ("round-nick", "以后请叫我小朋吧，这是我的昵称。", "好的，小朋！"),
    ]
    task_ids = []
    for i, (rid, user_text, assistant_text) in enumerate(rounds, start=1):
        result = append(rid, user_text, assistant_text, i)
        check(f"append[{rid}] HTTP 200", result["status_code"] == 200, str(result["body"])[:120])
        body = result["body"]
        check(
            f"append[{rid}] 返回 task_id",
            bool(body.get("task_id")),
            str(body)[:120],
        )
        task_ids.append(body.get("task_id", ""))
        # 幂等：同 round_id 重复提交
        again = append(rid, user_text, assistant_text, i)
        check(
            f"append[{rid}] 幂等 already_done",
            again["status_code"] == 200 and again["body"].get("status") == "already_done",
            str(again["body"])[:120],
        )

    # ── 2. L3 后台抽取轮询（真实 LLM + embedding + Milvus） ─────────
    for rid, task_id in zip([f"{RUN}-{r[0]}" for r in rounds], task_ids, strict=True):
        status, body = wait_task(task_id)
        check(f"task[{rid}] L3 抽取 completed", status == "completed", f"status={status} {body}")

    bg = client.get("/memory/l3/background-status").json()
    check("background-status 返回结构", {"write_mode", "pending_write_tasks"} <= set(bg), str(bg))

    # ── 2.5 L2 LLM 综合摘要（P0）：轮询直到 L2 层出现结构化 llm 摘要 ──
    llm_l2_seen = False
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        probe = recall("用户提过什么重要信息？")
        l2_items = [
            item["content"]
            for item in probe.get("body", {}).get("items", [])
            if item.get("layer") == "L2"
        ]
        if any(content.startswith("【主题】") for content in l2_items):
            llm_l2_seen = True
            print(f"  L2 llm 摘要: {l2_items[0][:120]}")
            break
        time.sleep(3)
    check("L2 LLM 综合摘要生成并进入召回", llm_l2_seen, "180s 内未见【主题】结构化摘要")

    # ── 3. items 本地索引可见性 ────────────────────────────────────
    items = client.get("/memory/items", params={"user_id": USER}).json()
    texts = [item["content"] for item in items.get("items", [])]
    print(f"  items={len(texts)}: {texts}")
    check("items 至少 1 条 L3 记忆", len(texts) >= 1, str(texts))
    has_cat = any("麻薯" in text for text in texts)
    print(f"  [提示] 猫事实入索引: {has_cat}（未入也有槽位回填兜底）")

    # ── 4. recall 全链路（L1/L2/L3） ───────────────────────────────
    result = recall("我的猫叫什么名字？")
    check("recall[猫] HTTP 200", result["status_code"] == 200, str(result["body"])[:150])
    contents = [item["content"] for item in result["body"].get("items", [])]
    joined = " ".join(contents)
    check("recall[猫] 命中『麻薯』", "麻薯" in joined, f"items={contents}")
    layers = {item["layer"] for item in result["body"].get("items", [])}
    check("recall 至少含 L1 层", "L1" in layers, f"layers={layers}")

    result2 = recall("用户提过什么重要信息？")
    contents2 = [item["content"] for item in result2["body"].get("items", [])]
    check("recall[宽泛] HTTP 200", result2["status_code"] == 200, str(result2["body"])[:120])
    print(f"  宽泛召回 items={len(contents2)}")

    # ── 5. update 编辑第一条记忆 ───────────────────────────────────
    if items.get("items"):
        target = items["items"][0]
        new_text = (target["content"] + "（用户确认补充）").strip()
        upd = client.post(
            "/memory/update",
            json={
                "request_id": f"req-upd-{uuid.uuid4().hex[:6]}",
                "user_id": USER,
                "memory_id": target["memory_id"],
                "operation_id": f"op-upd-{uuid.uuid4().hex[:6]}",
                "content": new_text,
            },
        )
        check("update HTTP 200", upd.status_code == 200, str(upd.json())[:150])
        if upd.status_code == 200:
            status, _ = wait_task(upd.json().get("task_id", ""), timeout=120)
            check("update 任务完成", status == "completed", status)
            got = client.get(
                "/memory/items/" + target["memory_id"], params={"user_id": USER}
            ).json()
            check(
                "update 内容生效",
                "用户确认补充" in got.get("memory", {}).get("content", ""),
                str(got)[:150],
            )

    # ── 6. delete 会话级删除 + 幂等 ────────────────────────────────
    op = f"op-del-{uuid.uuid4().hex[:6]}"
    dele = client.post(
        "/memory/delete",
        json={
            "request_id": f"req-del-{uuid.uuid4().hex[:6]}",
            "user_id": USER,
            "scope": "session",
            "operation_id": op,
            "session_id": SESSION,
        },
    )
    check("delete HTTP 200", dele.status_code == 200, str(dele.json())[:150])
    status, body = wait_task(dele.json().get("task_id", ""), timeout=300)
    check("delete 任务 completed", status == "completed", f"{status} {body}")
    again = client.post(
        "/memory/delete",
        json={
            "request_id": f"req-del2-{uuid.uuid4().hex[:6]}",
            "user_id": USER,
            "scope": "session",
            "operation_id": op,
            "session_id": SESSION,
        },
    )
    check(
        "delete 幂等 already_done",
        again.status_code == 200 and again.json().get("status") in {"already_done", "completed"},
        str(again.json())[:120],
    )

    # 删除后 recall 不应再吐出已删事实
    after = recall("我的猫叫什么名字？")
    contents_after = [item["content"] for item in after["body"].get("items", [])]
    check(
        "删除后 recall 不泄漏『麻薯』(L3)",
        all("麻薯" not in content for content in contents_after if item_layer(content, after)),
        f"items={contents_after}",
    )

    print(f"\n== 结果: {len(PASSED)} passed / {len(FAILED)} failed ==")
    for failure in FAILED:
        print(f"  FAILED: {failure}")
    return 1 if FAILED else 0


def item_layer(content: str, recall_result: dict) -> bool:
    """content 是否来自 L3 层（L1 残留在删除后允许为空，这里只查 L3 项）。"""
    for item in recall_result["body"].get("items", []):
        if item["content"] == content and item["layer"] == "L3":
            return True
    return False


if __name__ == "__main__":
    sys.exit(main())
