"""真实栈门禁：一条命令跑完七类检查，结果带 commit 号归档。

为什么单独做：自包含评测全绿只证明 fake 编排契约。真实栈上才谈得上抽取
质量、擦除残留、混合检索与幂等 —— 这些是产品承诺的底座。

门禁项与地板（地板以真实基线为下限，只许升不许降；见
``tests/eval/baseline_semiconductor_real.json``）：

1. 抽取非空率          1.00
2. 期望事实覆盖率      0.66
3. 期望事实召回率      0.66
4. 禁存词泄漏          0 条
5. 擦除残留            0 条（Milvus / Mem0 history / Mem0 messages / PG 索引）
6. 幂等重放一致        完全一致
7. BM25 与 filter      英文型号词命中 + filter 正负向正确
                        中文 BM25 在 jieba 上线前只报告不阻断

跑法（需要真实 Milvus + 可达的 LLM/embedding endpoint + 可写的 PG）：

    docker compose -f deploy/milvus-local/docker-compose.yml up -d
    MILVUS_URL=http://localhost:19540 uv run --group dev python tests/script/gate_real.py

退出码 0 = 全绿；非 0 = 有阻断项失败。报告写到 ``docs/memory/report/real/``。
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv


def _prepare_environment() -> None:
    """先读凭据、再清代理。

    必须在导入 thinkback 之前做：``Settings`` 在模块导入时就构造好了，
    之后再 ``load_dotenv`` 就来不及了 —— 端点会回落成默认地址，表现为
    「连接错误」而不是「缺凭据」。

    凭据文件可以显式指定（``--env-file``）：门禁在别的机器或别的 worktree
    上跑时，用哪份部署凭据由调用方说了算，脚本只在原处读，不复制凭据文件。
    """
    argv = sys.argv[1:]
    env_file = None
    if "--env-file" in argv:
        index = argv.index("--env-file")
        env_file = argv[index + 1] if index + 1 < len(argv) else None
        if not env_file:
            raise SystemExit("--env-file 需要一个路径")
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
    # mac 本地代理会劫持内网 httpx（LLM/embedding 端点在 10.x 网段）。
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"


_prepare_environment()

from thinkback.api.dependencies import build_registry_provider, get_memory_backend  # noqa: E402
from thinkback.domain.enums import MemoryStatus  # noqa: E402
from thinkback.infra.config import Settings, get_settings  # noqa: E402
from thinkback.infra.milvus import ensure_milvus_database  # noqa: E402
from thinkback.memory.repositories import SqlAlchemyMemoryRepository  # noqa: E402
from thinkback.memory.schemas import (  # noqa: E402
    DEFAULT_NAMESPACE,  # noqa: E402
    DeleteMemoryRequest,
    DeleteScope,
    MemoryMessage,
    MessageRole,
    RecallMemoryRequest,
)
from thinkback.memory.service import MemoryService  # noqa: E402

REPORT_DIR = Path("docs/memory/report/real")

# 地板：与真实基线一致，只许升不许降。
FLOOR_COVERAGE = 0.66
FLOOR_RECALL = 0.66

PASSED: list[str] = []
FAILED: list[str] = []
NOTES: list[str] = []


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""
    blocking: bool = True
    value: float | None = None
    floor: float | None = None
    skipped: bool = False


RESULTS: list[CheckResult] = []


def record(
    name: str,
    passed: bool,
    detail: str = "",
    *,
    blocking: bool = True,
    value: float | None = None,
    floor: float | None = None,
    skipped: bool = False,
) -> None:
    RESULTS.append(
        CheckResult(
            name=name,
            passed=passed,
            detail=detail,
            blocking=blocking,
            value=value,
            floor=floor,
            skipped=skipped,
        )
    )
    if skipped:
        NOTES.append(f"{name}: {detail}")
        print(f"  SKIP {name} -> {detail}")
    elif passed:
        PASSED.append(name)
        print(f"  PASS {name}")
    else:
        FAILED.append(f"{name}: {detail}")
        print(f"  FAIL {name} -> {detail}")


def git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def build_service(settings: Settings) -> tuple[MemoryService, SqlAlchemyMemoryRepository]:
    repository = SqlAlchemyMemoryRepository()
    backend = get_memory_backend(settings)
    service = MemoryService(
        repository=repository,
        backend=backend,
        registry_provider=build_registry_provider(settings),
        l3_write_mode="sync",
    )
    return service, repository


def append_event(
    service: MemoryService,
    *,
    user_id: str,
    session_id: str,
    event_id: str,
    payload: str,
    role: str = "user",
    metadata: dict | None = None,
) -> None:
    service.append(
        _append_request(
            user_id=user_id,
            session_id=session_id,
            event_id=event_id,
            payload=payload,
            role=role,
            metadata=metadata,
        )
    )


def _append_request(
    *,
    user_id: str,
    session_id: str,
    event_id: str,
    payload: str,
    role: str = "user",
    metadata: dict | None = None,
):
    from thinkback.memory.schemas import AppendMemoryRequest

    now = datetime.now(UTC)
    return AppendMemoryRequest(
        request_id=f"req-{event_id}",
        user_id=user_id,
        session_id=session_id,
        round_id=event_id,
        messages=[
            MemoryMessage(
                message_id=event_id,
                role=MessageRole(role),
                content=payload,
                timestamp=now,
            )
        ],
        source_timestamp=now,
        metadata=metadata or {},
    )


def history_residue(history_db_path: str, marker: str) -> tuple[int, int]:
    """引擎侧残留：Mem0 的 history（事实原文）与 messages（对话原文）。"""
    path = Path(history_db_path)
    if not path.is_file():
        return 0, 0
    connection = sqlite3.connect(path, timeout=10.0)
    try:
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        history_hits = 0
        message_hits = 0
        if "history" in tables:
            history_hits = connection.execute(
                "SELECT COUNT(*) FROM history WHERE old_memory LIKE ? OR new_memory LIKE ?",
                (f"%{marker}%", f"%{marker}%"),
            ).fetchone()[0]
        if "messages" in tables:
            message_hits = connection.execute(
                "SELECT COUNT(*) FROM messages WHERE content LIKE ?", (f"%{marker}%",)
            ).fetchone()[0]
        return int(history_hits), int(message_hits)
    finally:
        connection.close()


def _milvus_rows(settings: Settings, user_id: str) -> int:
    """该用户在 Milvus 主 collection 里还剩多少行。"""
    try:
        from pymilvus import MilvusClient

        client = MilvusClient(
            uri=settings.milvus_url,
            token=settings.milvus_token or "",
            db_name=settings.milvus_database,
        )
        rows = client.query(
            collection_name=settings.memory_milvus_collection,
            filter=f'metadata["user_id"] == "{user_id}"',
            output_fields=["id"],
        )
        return len(rows)
    except Exception as exc:  # noqa: BLE001 - 残留检查失败本身要报出来
        print(f"  WARN Milvus 残留检查失败: {type(exc).__name__}: {exc}")
        return -1


def _write_report(settings: Settings) -> Path:
    sha = git_sha()
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "report_type": "thinkback_real_stack_gate",
        "commit": sha,
        "generated_at": datetime.now(UTC).isoformat(),
        "environment": settings.environment,
        "checks": [
            {
                "name": result.name,
                "passed": result.passed,
                "blocking": result.blocking,
                "skipped": result.skipped,
                "value": result.value,
                "floor": result.floor,
                "detail": result.detail,
            }
            for result in RESULTS
        ],
        "passed": len(PASSED),
        "failed": len(FAILED),
        "notes": NOTES,
    }
    path = REPORT_DIR / f"gate-{sha}-{stamp}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main() -> int:
    settings = get_settings()
    ensure_milvus_database(
        milvus_url=settings.milvus_url,
        milvus_database=settings.milvus_database,
        milvus_token=settings.milvus_token,
    )

    service, repository = build_service(settings)
    backend = service.backend

    marker = f"GATE{uuid4().hex[:8]}"
    user_id = f"gate-{uuid4().hex[:10]}"
    session_id = f"gate-session-{uuid4().hex[:6]}"
    # 作用域是 namespace（请求里的 namespace），不是 Milvus collection 名。
    scope = DEFAULT_NAMESPACE

    # 1–3：抽取与召回质量
    facts = [
        (f"我在{marker}项目负责光刻机 MA6000 的日常点检。", ["MA6000"]),
        (f"{marker}的良率敏感点是气浮平台，工艺节点是 28nm。", ["气浮平台", "28nm"]),
        ("我平时用 Neovim 写 Python，报表口径按下季度。", ["Neovim"]),
    ]
    expected_total = 0
    covered = 0
    recalled = 0
    extracted_texts: list[str] = []
    for index, (payload, markers) in enumerate(facts):
        append_event(
            service,
            user_id=user_id,
            session_id=session_id,
            event_id=f"{user_id}-ev-{index}",
            payload=payload,
        )
        indexed = repository.active_memories(user_id, scope)
        texts = [memory.memory_text for memory in indexed]
        extracted_texts.extend(texts)
        expected_total += len(markers)
        covered += sum(1 for item in markers if any(item in text for text in texts))

        response = service.recall(
            RecallMemoryRequest(
                user_id=user_id,
                session_id=session_id,
                query=markers[0],
            )
        )
        recalled += sum(
            1 for item in markers if any(item in memory.content for memory in response.items)
        )

    coverage = covered / expected_total if expected_total else 0.0
    recall_rate = recalled / expected_total if expected_total else 0.0
    record(
        "extraction_non_empty",
        bool(extracted_texts),
        f"抽取到 {len(extracted_texts)} 条事实",
        value=1.0 if extracted_texts else 0.0,
        floor=1.0,
    )
    record(
        "expected_fact_coverage",
        coverage >= FLOOR_COVERAGE,
        f"覆盖率 {coverage:.2f}，地板 {FLOOR_COVERAGE}",
        value=coverage,
        floor=FLOOR_COVERAGE,
    )
    record(
        "expected_fact_recall",
        recall_rate >= FLOOR_RECALL,
        f"召回率 {recall_rate:.2f}，地板 {FLOOR_RECALL}",
        value=recall_rate,
        floor=FLOOR_RECALL,
    )

    # 4：禁存词不泄漏（restricted 分类）
    rejected = False
    try:
        append_event(
            service,
            user_id=user_id,
            session_id=session_id,
            event_id=f"{user_id}-ev-restricted",
            payload=f"{marker} 的客户密钥是 sk-{marker}-SECRET",
            metadata={"data_classification": "restricted"},
        )
    except ValueError:
        # 分类准入是 fail-closed：restricted 内容根本不进 L1/L2/L3。
        rejected = True
    restricted_rows = [
        memory
        for memory in repository.active_memories(user_id, scope)
        if "SECRET" in memory.memory_text
    ]
    restricted_response = service.recall(
        RecallMemoryRequest(
            user_id=user_id,
            session_id=session_id,
            query=f"{marker} 密钥",
        )
    )
    leaked = sum(1 for memory in restricted_response.items if "SECRET" in memory.content)
    record(
        "forbidden_term_not_leaked",
        rejected and not restricted_rows and leaked == 0,
        f"拒收={rejected}，索引残留 {len(restricted_rows)} 条，召回泄漏 {leaked} 条",
        value=float(leaked),
        floor=0.0,
    )

    # 6：幂等重放一致（放在 erase 之前，erase 会清掉这批事实）
    replay_event_id = f"{user_id}-ev-replay"
    append_event(
        service,
        user_id=user_id,
        session_id=session_id,
        event_id=replay_event_id,
        payload=f"{marker} 的重复提交必须只落一条事实。",
    )
    first_count = len(repository.active_memories(user_id, scope))
    append_event(
        service,
        user_id=user_id,
        session_id=session_id,
        event_id=replay_event_id,
        payload=f"{marker} 的重复提交必须只落一条事实。",
    )
    second_count = len(repository.active_memories(user_id, scope))
    record(
        "idempotent_replay_consistent",
        first_count == second_count,
        f"重放前后事实条数 {first_count} → {second_count}",
        value=float(second_count),
        floor=float(first_count),
    )

    # 7：BM25（英文型号词必须命中；中文在 jieba 上线前只报告不阻断）
    try:
        results = backend.search(
            "MA6000",
            user_id=user_id,
            memory_scope_id=scope,
            limit=5,
            explain=True,
        )
        details = [item.get("score_details") or {} for item in results if isinstance(item, dict)]
        bm25_ok = any(float(detail.get("bm25_score") or 0) > 0 for detail in details)
        bm25_detail = f"bm25_scores={[round(float(d.get('bm25_score') or 0), 3) for d in details]}"
    except Exception as exc:  # noqa: BLE001 - 门禁要看到真实错误
        bm25_ok = False
        bm25_detail = f"{type(exc).__name__}: {exc}"
    record("bm25_ascii_hit", bm25_ok, bm25_detail, value=1.0 if bm25_ok else 0.0, floor=1.0)

    try:
        cn_results = backend.search(
            "气浮平台",
            user_id=user_id,
            memory_scope_id=scope,
            limit=5,
            explain=True,
        )
        cn_bm25 = [
            float((item.get("score_details") or {}).get("bm25_score") or 0)
            for item in cn_results
            if isinstance(item, dict)
        ]
        record(
            "bm25_chinese_hit",
            True,
            f"中文 bm25_scores={[round(value, 3) for value in cn_bm25]}（jieba 上线前不阻断）",
            blocking=False,
            skipped=True,
        )
    except Exception as exc:  # noqa: BLE001
        record(
            "bm25_chinese_hit", True, f"{type(exc).__name__}: {exc}", blocking=False, skipped=True
        )

    # 5：erase 残留
    service.erase(
        DeleteMemoryRequest(
            request_id=f"req-erase-{user_id}",
            user_id=user_id,
            scope=DeleteScope.ALL,
            operation_id=f"op-erase-{user_id}",
        )
    )
    # 墓碑行（DELETED）是删除屏障的一部分，不是残留：它要留下来阻止旧事实复活。
    index_rows = sum(
        1
        for memory in repository.list_memories(user_id, scope)
        if memory.memory_status != MemoryStatus.DELETED
    )
    history_hits, message_hits = history_residue(settings.mem0_history_db_path, marker)
    milvus_rows = _milvus_rows(settings, user_id)
    residue = index_rows + history_hits + message_hits + max(milvus_rows, 0)
    record(
        "erase_no_residue",
        residue == 0 and milvus_rows >= 0,
        f"残留 {residue} 条（索引 {index_rows}、history {history_hits}、messages {message_hits}、"
        f"Milvus {milvus_rows}）",
        value=float(residue),
        floor=0.0,
    )

    report = _write_report(settings)
    print(f"\n报告: {report}")
    if FAILED:
        print(f"阻断项失败 {len(FAILED)} 个：")
        for item in FAILED:
            print(f"  - {item}")
        return 1
    print(f"全部通过（{len(PASSED)} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
