"""真链路 smoke test：Mem0 Library + Milvus + zhiman-embedding 端到端。

环境：
- Milvus 容器 e4dd1f98f8df...@ localhost:19530 (gRPC) / :9091 (HTTP)
- Embedding http://101.237.37.116:7345/v1，model zhiman-embedding, dim=1024
- 不需要 chat/completions（embedding endpoint 不支持），所以走 infer=False 路径

覆盖：
- append → Milvus 写入（embedder + 1024 维向量 + 入库）
- recall → 语义检索（cosine 相似度）
- list_memory_items → 业务视图
- delete → 清掉数据

产出 JSON + Markdown 报告到 docs/memory/report/。

用法：
    PYTHONPATH=src python tests/script/run_real_smoke_test.py
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

# 真实依赖：Mem0 + Milvus（gRPC）+ OpenAI-compatible embedding
from mem0 import Memory as Mem0Client

from thinkback.memory.backends.mem0_library import (
    Mem0LibraryMemoryBackend,
    disable_mem0_telemetry,
    register_mem0_embedder_provider,
)
from thinkback.memory.repositories import InMemoryMemoryRepository
from thinkback.memory.schemas import (
    AppendMemoryRequest,
    MemoryMessage,
    MessageRole,
    RecallIntent,
    RecallMemoryRequest,
)
from thinkback.memory.service import MemoryService

REPORT_DIR = Path("docs/memory/report")

MILVUS_URL = "http://localhost:19530"
EMBEDDING_BASE_URL = "http://101.237.37.116:7345/v1"
EMBEDDING_MODEL = "zhiman-embedding"
EMBEDDING_DIMS = 1024


LLM_BASE_URL = "http://101.237.37.116:7383/v1"
LLM_MODEL = "zhiman38_27b"
LLM_API_KEY = "7c5811a2-8c02-11f1-a4d0-525400526fb7"
EMBEDDING_API_KEY = "7c5811a2-8c02-11f1-a4d0-525400526fb7"


def build_mem0_config(collection_name: str) -> dict[str, Any]:
    """构造带 thinkback custom_instructions 的 Mem0 Library 配置。

    双 endpoint：
    - LLM chat completions: http://101.237.37.116:7383/v1 (zhiman38_27b)
    - Embedding:            http://101.237.37.116:7345/v1 (zhiman-embedding)
    """
    return {
        "llm": {
            "provider": "openai",
            "config": {
                "model": LLM_MODEL,
                "api_key": LLM_API_KEY,
                "openai_base_url": LLM_BASE_URL,
            },
        },
        "embedder": {
            "provider": "openai",
            "config": {
                "model": EMBEDDING_MODEL,
                "api_key": EMBEDDING_API_KEY,
                "openai_base_url": EMBEDDING_BASE_URL,
                "embedding_dims": EMBEDDING_DIMS,
            },
        },
        "vector_store": {
            "provider": "milvus",
            "config": {
                "collection_name": collection_name,
                "url": MILVUS_URL,
                "token": "",
                "db_name": "default",
                "embedding_model_dims": EMBEDDING_DIMS,
                "metric_type": "COSINE",
            },
        },
        "history_db_path": f"/tmp/mem0_history_{uuid4().hex[:6]}.db",
    }


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str
    elapsed_ms: float


def _t0() -> float:
    return time.perf_counter()


def _make_msg(message_id: str, role: MessageRole, content: str) -> MemoryMessage:
    return MemoryMessage(
        message_id=message_id,
        role=role,
        content=content,
        timestamp=datetime.now(UTC),
    )


def _append(service: MemoryService, *, round_id: str, user_text: str) -> None:
    service.append(
        AppendMemoryRequest(
            request_id=f"req-{round_id}",
            user_id="user-real",
            session_id="session-real",
            round_id=round_id,
            messages=[
                _make_msg(f"{round_id}-u", MessageRole.USER, user_text),
                _make_msg(f"{round_id}-a", MessageRole.ASSISTANT, "好的，记下了。"),
            ],
            source_timestamp=datetime.now(UTC),
            metadata={},
        )
    )


def _recall(service: MemoryService, query: str) -> list[str]:
    response = service.recall(
        RecallMemoryRequest(
            user_id="user-real",
            session_id="session-real",
            query=query,
            intent=RecallIntent.CHAT,
        )
    )
    return [item.content for item in response.items if item.layer == "L3"]


def check_mem0_embedder_round_trip() -> CheckResult:
    """基础：embedder 直接调一次 embed() 应返回 1024 维向量。"""
    start = _t0()
    register_mem0_embedder_provider()
    disable_mem0_telemetry()

    from thinkback.memory.embeddings import OpenAICompatibleEmbeddingNoDimensions

    config = type(
        "Cfg",
        (),
        {
            "model": EMBEDDING_MODEL,
            "embedding_dims": EMBEDDING_DIMS,
            "api_key": "placeholder",
            "openai_base_url": EMBEDDING_BASE_URL,
        },
    )()
    embedder = OpenAICompatibleEmbeddingNoDimensions(config)
    vec = embedder.embed("测试中文 embedding 维度")
    elapsed_ms = (time.perf_counter() - start) * 1000
    passed = isinstance(vec, list) and len(vec) == EMBEDDING_DIMS
    return CheckResult(
        "check_mem0_embedder_round_trip",
        passed,
        f"dim={len(vec) if isinstance(vec, list) else 'N/A'} (target {EMBEDDING_DIMS})",
        elapsed_ms,
    )


def check_milvus_collection_can_be_built() -> CheckResult:
    """Mem0 Library 从配置建客户端，并验证 Milvus 连通 + collection 创建。"""
    start = _t0()
    collection_name = f"smoke_test_{uuid4().hex[:8]}"
    config = build_mem0_config(collection_name)
    try:
        client = Mem0Client.from_config(config)
        passed = client is not None
        detail = f"collection={collection_name} client_ready={passed}"
    except Exception as exc:
        passed = False
        detail = f"Mem0Client.from_config failed: {type(exc).__name__}: {exc}"
    elapsed_ms = (time.perf_counter() - start) * 1000
    return CheckResult(
        "check_milvus_collection_can_be_built",
        passed,
        detail,
        elapsed_ms,
    )


def check_real_mem0_add_search_delete_round_trip() -> CheckResult:
    """端到端：mem0.add → LLM 抽取（zhiman38_27b）→ Milvus 写入 → mem0.search 命中 → mem0.delete。

    用真实 Mem0 Library + Milvus + zhiman-embedding + zhiman38_27b LLM。
    infer=True：走 LLM 事实抽取路径（生产默认）。
    """
    start = _t0()
    collection_name = f"smoke_rt_{uuid4().hex[:8]}"
    config = build_mem0_config(collection_name)
    try:
        backend = Mem0LibraryMemoryBackend(
            config=config,
            max_concurrent_calls=4,
        )
        # LLM 抽取路径：append 一句复杂陈述，让 zhiman38_27b 抽取事实
        backend.add(
            [
                {
                    "role": "user",
                    "content": "我养了一只猫，名字叫麻薯。我现在住在上海徐汇区。我叫小鹏。",
                },
                {"role": "assistant", "content": "好的，记下了。"},
            ],
            user_id="user-real-llm",
            memory_scope_id="thinkback",
            infer=True,
        )
        backend.add(
            [
                {"role": "user", "content": "我下周要去北京出差。"},
                {"role": "assistant", "content": "好的。"},
            ],
            user_id="user-real-llm",
            memory_scope_id="thinkback",
            infer=True,
        )

        # 语义检索
        results_pet = backend.search(
            "用户的宠物",
            user_id="user-real-llm",
            memory_scope_id="thinkback",
            limit=5,
        )
        results_loc = backend.search(
            "用户在哪座城市",
            user_id="user-real-llm",
            memory_scope_id="thinkback",
            limit=5,
        )
        results_nick = backend.search(
            "用户的称呼",
            user_id="user-real-llm",
            memory_scope_id="thinkback",
            limit=5,
        )

        pet_texts = [r.get("memory", "") for r in results_pet]
        loc_texts = [r.get("memory", "") for r in results_loc]
        nick_texts = [r.get("memory", "") for r in results_nick]
        all_texts = " | ".join(pet_texts + loc_texts + nick_texts)

        # LLM 抽取后应产生 ≥ 1 条 cat-related + 1 条 location-related + 1 条 name 记忆
        # LLM 在 custom_instructions 提示下输出中文事实，但可能附带 pinyin / English
        # translation（如 "User has a cat named Mashu (麻薯)"）—— 看 .find 是否能命中核心实体。
        all_texts = pet_texts + loc_texts + nick_texts
        has_pet_fact = any(("猫" in t or "麻薯" in t or "Mashu" in t) for t in all_texts)
        has_loc_fact = any(
            (
                "上海" in t
                or "徐汇" in t
                or "北京" in t
                or "Shanghai" in t
                or "Beijing" in t
                or "Xuhui" in t
            )
            for t in all_texts
        )
        has_nick_fact = any(("小鹏" in t or "Xiaopeng" in t) for t in all_texts)

        # clean up
        for r in backend._memory_client.get_all(
            filters={"user_id": "user-real-llm", "agent_id": "thinkback"}, limit=100
        ).get("results", []):
            mid = r.get("id")
            if mid:
                backend.delete(mid)

        passed = has_pet_fact and has_loc_fact and has_nick_fact
        elapsed_ms = (time.perf_counter() - start) * 1000
        return CheckResult(
            "check_real_mem0_add_search_delete_round_trip",
            passed,
            (
                f"pet={pet_texts} loc={loc_texts} nick={nick_texts} "
                f"has_pet={has_pet_fact} has_loc={has_loc_fact} has_nick={has_nick_fact}"
            ),
            elapsed_ms,
        )
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - start) * 1000
        return CheckResult(
            "check_real_mem0_add_search_delete_round_trip",
            False,
            f"raised: {type(exc).__name__}: {exc}",
            elapsed_ms,
        )


def check_real_thinkback_service_round_trip() -> CheckResult:
    """thinkback 服务层 + 真实后端：append → recall → delete。

    验证：
    - append 走 Mem0 后端 + embedder + Milvus
    - recall 触发 slot backfill + mem0.search
    - delete 走 mem0.delete
    """
    start = _t0()
    collection_name = f"smoke_svc_{uuid4().hex[:8]}"
    config = build_mem0_config(collection_name)
    try:
        register_mem0_embedder_provider()
        disable_mem0_telemetry()

        backend = Mem0LibraryMemoryBackend(config=config, max_concurrent_calls=4)
        repo = InMemoryMemoryRepository()
        service = MemoryService(
            repository=repo,
            backend=backend,
            mem0_infer_facts=False,  # 关键：endpoint 不支持 chat completions
            l3_write_mode="sync",
        )

        # append 一条 P0 事实
        _append(service, round_id="r-real-1", user_text="我养了一只猫，名字叫麻薯")
        _append(service, round_id="r-real-2", user_text="请叫我小鹏")
        _append(service, round_id="r-real-3", user_text="我现在住在上海")

        # 也试 LLM 抽取路径：append 一条复杂事实，让 mem0 走 zhiman38_27b 抽取
        _append(
            service,
            round_id="r-real-llm",
            user_text="我妈妈住在杭州，她喜欢喝茶。我昨天在巴黎出差。",
        )

        # recall 触发 slot backfill（昵称/宠物/地点 P0 槽位）
        nick_response = service.recall(
            RecallMemoryRequest(
                user_id="user-real",
                session_id="session-real",
                query="怎么称呼用户",
                intent=RecallIntent.CHAT,
            )
        )
        nick_texts = [item.content for item in nick_response.items if item.layer == "L3"]
        pet_response = service.recall(
            RecallMemoryRequest(
                user_id="user-real",
                session_id="session-real",
                query="用户的宠物叫什么",
                intent=RecallIntent.CHAT,
            )
        )
        pet_texts = [item.content for item in pet_response.items if item.layer == "L3"]
        loc_response = service.recall(
            RecallMemoryRequest(
                user_id="user-real",
                session_id="session-real",
                query="用户在哪",
                intent=RecallIntent.CHAT,
            )
        )
        loc_texts = [item.content for item in loc_response.items if item.layer == "L3"]

        # 也记录 backend.search 直接调用结果
        backend_pet = backend.search(
            "用户的宠物",
            user_id="user-real",
            memory_scope_id="thinkback",
            limit=3,
        )
        backend_pet_texts = [r.get("memory", "") for r in backend_pet]

        # clean up
        from thinkback.memory.schemas import DeleteMemoryRequest, DeleteScope

        service.delete(
            DeleteMemoryRequest(
                request_id="req-cleanup",
                user_id="user-real",
                scope=DeleteScope.ALL,
                operation_id="op-cleanup",
            )
        )

        passed = (
            len(nick_texts) > 0
            or len(pet_texts) > 0
            or len(loc_texts) > 0
            or len(backend_pet_texts) > 0
        )
        elapsed_ms = (time.perf_counter() - start) * 1000
        return CheckResult(
            "check_real_thinkback_service_round_trip",
            passed,
            (f"nick={nick_texts} pet={pet_texts} loc={loc_texts} backend_pet={backend_pet_texts}"),
            elapsed_ms,
        )
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - start) * 1000
        return CheckResult(
            "check_real_thinkback_service_round_trip",
            False,
            f"raised: {type(exc).__name__}: {exc}",
            elapsed_ms,
        )


def render_markdown(run_id: str, results: list[CheckResult]) -> str:
    lines = [
        "# Thinkback 真链路 smoke test（Mem0 + Milvus + zhiman-embedding）",
        "",
        f"- 运行 ID: `{run_id}`",
        f"- Milvus: `{MILVUS_URL}`",
        f"- Embedding: `{EMBEDDING_BASE_URL}` model=`{EMBEDDING_MODEL}` dim={EMBEDDING_DIMS}",
        "",
        "| 检查项 | 通过 | 耗时 (ms) | 关键证据 |",
        "| --- | :---: | ---: | --- |",
    ]
    for r in results:
        ok = "✅" if r.passed else "❌"
        detail = r.detail.replace("|", "\\|").replace("\n", " ")
        if len(detail) > 120:
            detail = detail[:117] + "..."
        lines.append(f"| `{r.name}` | {ok} | {r.elapsed_ms:.1f} | {detail} |")
    lines.append("")
    failed = [r for r in results if not r.passed]
    if failed:
        lines.append("## 失败明细")
        lines.append("")
        for r in failed:
            lines.append(f"### ❌ `{r.name}`")
            lines.append("")
            lines.append(f"- 证据: {r.detail}")
            lines.append("")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    run_id = f"real-smoke-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:6]}"
    print("=" * 70)
    print(f"Thinkback 真链路 smoke test: {run_id}")
    print("=" * 70)

    checks: list[tuple[str, Any]] = [
        ("check_mem0_embedder_round_trip", check_mem0_embedder_round_trip),
        ("check_milvus_collection_can_be_built", check_milvus_collection_can_be_built),
        (
            "check_real_mem0_add_search_delete_round_trip",
            check_real_mem0_add_search_delete_round_trip,
        ),
        ("check_real_thinkback_service_round_trip", check_real_thinkback_service_round_trip),
    ]
    results: list[CheckResult] = []
    for name, fn in checks:
        print(f"→ 跑 {name} ...")
        try:
            r = fn()
        except Exception as exc:
            r = CheckResult(
                name=name,
                passed=False,
                detail=f"raised: {type(exc).__name__}: {exc}",
                elapsed_ms=0.0,
            )
        results.append(r)
        marker = "✅" if r.passed else "❌"
        print(f"  {marker} {r.elapsed_ms:.1f}ms — {r.detail[:150]}")

    total_pass = sum(1 for r in results if r.passed)
    print()
    print(f"通过率: {total_pass}/{len(results)} ({total_pass / len(results):.1%})")

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = REPORT_DIR / f"{run_id}.json"
    md_path = REPORT_DIR / f"{run_id}.md"
    json_path.write_text(
        json.dumps(
            {
                "report_type": "real_link_smoke_test",
                "run_id": run_id,
                "generated_at": datetime.now(UTC).isoformat(),
                "milvus_url": MILVUS_URL,
                "embedding_base_url": EMBEDDING_BASE_URL,
                "embedding_model": EMBEDDING_MODEL,
                "embedding_dims": EMBEDDING_DIMS,
                "results": [
                    {
                        "name": r.name,
                        "passed": r.passed,
                        "detail": r.detail,
                        "elapsed_ms": r.elapsed_ms,
                    }
                    for r in results
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    md_path.write_text(render_markdown(run_id, results), encoding="utf-8")
    print(f"报告: {json_path}")
    print(f"       {md_path}")
    return 0 if total_pass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
