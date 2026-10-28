"""Milvus 集合迁移：预建中文分词集合 + 复制式回填 + 切换判据（ADR-0007）。

为什么需要迁移：Milvus 不允许原地改 ``text`` 字段的分析器
（``analyzer_params does not allow update``），而 mem0 自建的集合没有
analyzer_params，中文 BM25 恒为 0。想让中文检索生效只能新建集合再把行搬过去。

不重新抽取事实：向量与原文一起搬，搬家不改变事实内容。

用法（需要能连上 Milvus，凭据从环境变量或 .env 读）::

    # 1. 看两个集合的体检结果
    uv run python script/migrate_milvus_collection.py status \
        --source agent_semantic_memory_v1 --target agent_semantic_memory_v2

    # 2. 建好目标集合并把行搬过去（可重复执行，同 id 覆盖）
    uv run python script/migrate_milvus_collection.py backfill \
        --source agent_semantic_memory_v1 --target agent_semantic_memory_v2

    # 3. 切换判据：行数一致 + 抽样召回通过
    uv run python script/migrate_milvus_collection.py verify \
        --source agent_semantic_memory_v1 --target agent_semantic_memory_v2

切换与回滚按 ADR-0007：把 MEMORY_MILVUS_COLLECTION 指到新集合，
MEMORY_MILVUS_MIRROR_COLLECTION 指到旧集合（双写 + 双读回落）。
回滚只需把两个值换回去，不用补数据。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv  # noqa: E402


def _load_env() -> None:
    """先读凭据再清代理：内网端点在 10.x 网段，mac 的本地代理会劫持 httpx。"""

    load_dotenv()
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


_load_env()

from pymilvus import MilvusClient  # noqa: E402

from thinkback.infra.config import get_settings  # noqa: E402
from thinkback.infra.milvus_collection import (  # noqa: E402
    STATE_DRIFTED,
    analyzer_of,
    copy_rows,
    ensure_collection,
    missing_fields,
    row_count,
)


def _client(url: str, database: str, token: str) -> MilvusClient:
    return MilvusClient(uri=url, token=token or None, db_name=database)


def _describe(client: MilvusClient, name: str) -> str:
    if not client.has_collection(name):
        return f"集合 {name} 不存在"
    description = client.describe_collection(name)
    analyzer = analyzer_of(description) or "standard（未设 analyzer_params）"
    gaps = missing_fields(description)
    rows = row_count(client=client, collection=name)
    suffix = f"，缺字段 {gaps}" if gaps else ""
    return f"集合 {name}：行数 {rows}，分析器 {analyzer}{suffix}"


def cmd_status(args: argparse.Namespace) -> int:
    settings = get_settings()
    client = _client(settings.milvus_url, settings.milvus_database, settings.milvus_token)
    try:
        print(_describe(client, args.source))
        print(_describe(client, args.target))
    finally:
        client.close()
    return 0


def cmd_backfill(args: argparse.Namespace) -> int:
    settings = get_settings()
    dim = args.dim or settings.memory_embedding_dims
    state = ensure_collection(
        milvus_url=settings.milvus_url,
        milvus_database=settings.milvus_database,
        collection_name=args.target,
        dim=dim,
        milvus_token=settings.milvus_token,
        analyzer_type=args.analyzer,
    )
    print(f"目标集合预建结果：{state}")
    if state.state == STATE_DRIFTED:
        print("目标集合的分析器不对，先处理掉它再回填", file=sys.stderr)
        return 1
    client = _client(settings.milvus_url, settings.milvus_database, settings.milvus_token)
    try:
        if not client.has_collection(args.source):
            print(f"源集合 {args.source} 不存在", file=sys.stderr)
            return 1
        copied, total = copy_rows(
            client=client, source=args.source, target=args.target, batch_size=args.batch_size
        )
        print(f"回填完成：搬运 {copied} 行，源端 {total} 行")
    finally:
        client.close()
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """切换判据：行数一致 + 抽样召回通过。"""

    settings = get_settings()
    client = _client(settings.milvus_url, settings.milvus_database, settings.milvus_token)
    try:
        source_rows = row_count(client=client, collection=args.source)
        target_rows = row_count(client=client, collection=args.target)
        print(f"源 {args.source}：{source_rows} 行；目标 {args.target}：{target_rows} 行")
        if source_rows != target_rows:
            print("行数不一致，不能切换", file=sys.stderr)
            return 1
        for query in args.probe:
            hits = client.search(
                collection_name=args.target,
                data=[query],
                anns_field="sparse",
                limit=3,
                output_fields=["text"],
            )
            top = hits[0][0] if hits and hits[0] else None
            score = float(top["distance"]) if top else 0.0
            print(f"目标集合 BM25 抽样 {query!r} -> {top['id'] if top else None}（{score:.3f}）")
            if not top:
                print("抽样没有命中，不能切换", file=sys.stderr)
                return 1
    finally:
        client.close()
    print("切换判据通过：行数一致，抽样召回有命中")
    print(
        "切换配置：MEMORY_MILVUS_COLLECTION="
        f"{args.target}\n"
        f"          MEMORY_MILVUS_MIRROR_COLLECTION={args.source}"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["status", "backfill", "verify"])
    parser.add_argument("--source", required=True, help="旧集合名")
    parser.add_argument("--target", required=True, help="新集合名")
    parser.add_argument("--analyzer", default="chinese", help="目标集合的分析器类型")
    parser.add_argument("--dim", type=int, default=0, help="向量维度（默认取配置值）")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument(
        "--probe",
        action="append",
        default=None,
        help="verify 时的 BM25 抽样词，可重复传",
    )
    args = parser.parse_args()
    if args.probe is None:
        args.probe = ["气浮平台", "MA6000"]
    if args.command == "status":
        return cmd_status(args)
    if args.command == "backfill":
        return cmd_backfill(args)
    return cmd_verify(args)


if __name__ == "__main__":
    sys.exit(main())
