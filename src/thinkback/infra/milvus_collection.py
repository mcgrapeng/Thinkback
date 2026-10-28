"""Thinkback 自持 Milvus collection 的 schema、预建与迁移（ADR-0007）。

为什么不让 mem0 建表：mem0 2.2.1 把 ``text`` 字段硬编码成不带
``analyzer_params`` 的分析器（服务端默认 standard），中文整句被当成一个词，
BM25 对中文查询恒为 0。实测同一个集合结构下查「气浮平台」：standard 无命中，
chinese 命中。Milvus 又不允许原地改分析器
（``analyzer_params does not allow update``），所以只能由 Thinkback 预建带
中文分析器的集合，mem0 发现集合已存在且字段齐全时接管读写。

集合重建按「新建 + 复制式回填」执行，不重新抽取事实；切换窗口双写，
回滚只需把配置改回旧集合名。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from loguru import logger

ID_FIELD = "id"
VECTOR_FIELD = "vectors"
METADATA_FIELD = "metadata"
TEXT_FIELD = "text"
SPARSE_FIELD = "sparse"

#: mem0 2.2.1 写与查依赖的字段，缺一个 mem0 就会退化或直接报错。
REQUIRED_FIELDS = (ID_FIELD, VECTOR_FIELD, METADATA_FIELD, TEXT_FIELD, SPARSE_FIELD)

#: 回填与双写时需要搬运的字段。``sparse`` 是 BM25 Function 的输出字段，
#: 不允许显式写入，由服务端按 text 重新生成。
COPYABLE_FIELDS = (ID_FIELD, VECTOR_FIELD, METADATA_FIELD, TEXT_FIELD)

#: Milvus 2.6 内置的中文分析器（服务端按 jieba 分词）。``jieba`` 与
#: ``multilingual`` 这两个名字在 2.6.15 上会被服务端拒绝（tokenizer 创建失败），
#: 所以用 ``chinese``。
DEFAULT_ANALYZER_TYPE = "chinese"

#: 预建结果状态。
STATE_CREATED = "created"
STATE_REUSED = "reused"
STATE_DRIFTED = "drifted"
STATE_FAILED = "failed"


@dataclass(frozen=True)
class CollectionState:
    """预建结果。

    state:
      created —— 集合不存在，本次按 Thinkback schema 建好；
      reused —— 集合已存在且 schema 与分析器都对得上；
      drifted —— 集合已存在但分析器或字段与 Thinkback schema 不符
                  （Milvus 不允许原地改，只能走迁移重建）；
      failed —— 连不上或建表失败，调用方据此降级并告警。
    """

    state: str
    analyzer: str | None = None
    detail: str = ""


@dataclass(frozen=True)
class MilvusMirror:
    """迁移窗口里的次集合：双写目标 + 双读回落源。

    切换窗口内新集合是主集合，旧集合挂在镜像位上：写同时进两边，删也同时
    删两边，召回先读新集合、命中不足时回落旧集合。回滚只需把镜像位清掉并
    把集合名改回旧集合 —— 不补数据，缺口风险为零。
    """

    url: str
    database: str
    collection: str
    token: str = ""

    def client(self) -> Any:
        from pymilvus import MilvusClient

        return MilvusClient(uri=self.url, token=self.token or None, db_name=self.database)


def build_schema(*, dim: int, analyzer_type: str = DEFAULT_ANALYZER_TYPE) -> Any:
    """按 Thinkback 的 schema 建集合定义（与 mem0 2.2.1 的字段名一致）。"""

    from pymilvus import CollectionSchema, DataType, FieldSchema, Function, FunctionType

    fields = [
        FieldSchema(name=ID_FIELD, dtype=DataType.VARCHAR, is_primary=True, max_length=512),
        FieldSchema(name=VECTOR_FIELD, dtype=DataType.FLOAT_VECTOR, dim=dim),
        FieldSchema(name=METADATA_FIELD, dtype=DataType.JSON),
        FieldSchema(
            name=TEXT_FIELD,
            dtype=DataType.VARCHAR,
            max_length=65535,
            enable_analyzer=True,
            analyzer_params={"type": analyzer_type},
        ),
        FieldSchema(name=SPARSE_FIELD, dtype=DataType.SPARSE_FLOAT_VECTOR),
    ]
    schema = CollectionSchema(fields, enable_dynamic_field=True)
    schema.add_function(
        Function(
            name="bm25",
            input_field_names=[TEXT_FIELD],
            output_field_names=[SPARSE_FIELD],
            function_type=FunctionType.BM25,
        )
    )
    return schema


def analyzer_of(description: dict[str, Any]) -> str | None:
    """从集合描述里读出 ``text`` 字段实际生效的分析器类型。"""

    for field in description.get("fields", []) or []:
        if field.get("name") != TEXT_FIELD:
            continue
        raw = (field.get("params") or {}).get("analyzer_params")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                return None
        if isinstance(raw, dict):
            value = raw.get("type")
            return str(value) if value else None
    return None


def missing_fields(description: dict[str, Any]) -> list[str]:
    """集合里缺的字段名（空列表 = 字段齐全）。"""

    present = {field.get("name") for field in description.get("fields", []) or []}
    return [name for name in REQUIRED_FIELDS if name not in present]


def ensure_collection(
    *,
    milvus_url: str,
    milvus_database: str,
    collection_name: str,
    dim: int,
    metric_type: str = "COSINE",
    analyzer_type: str = DEFAULT_ANALYZER_TYPE,
    milvus_token: str | None = None,
    consistency_level: str = "Strong",
) -> CollectionState:
    """确保集合存在且 schema 由 Thinkback 持有。

    集合不存在时按中文分析器建好；已存在时只做体检，不动它的 schema ——
    Milvus 不允许原地改分析器，改不了的情况由迁移流程处理。
    """

    try:
        from pymilvus import MilvusClient
    except ImportError as exc:
        raise RuntimeError("pymilvus is not installed; install pymilvus") from exc

    log = logger.bind(
        milvus_url=milvus_url,
        milvus_database=milvus_database,
        collection=collection_name,
        analyzer=analyzer_type,
    )
    try:
        client = MilvusClient(
            uri=milvus_url,
            token=milvus_token or None,
            db_name=milvus_database,
        )
    except Exception as exc:  # noqa: BLE001 - 连不上时降级，不挡启动
        log.bind(error_type=type(exc).__name__, error=str(exc)).warning(
            "milvus collection ensure connection failed"
        )
        return CollectionState(state=STATE_FAILED, detail=f"{type(exc).__name__}: {exc}")
    try:
        if client.has_collection(collection_name):
            description = client.describe_collection(collection_name)
            actual = analyzer_of(description)
            gaps = missing_fields(description)
            if gaps:
                return CollectionState(
                    state=STATE_DRIFTED,
                    analyzer=actual,
                    detail=f"缺字段 {gaps}",
                )
            if actual != analyzer_type:
                # 没有 analyzer_params 就是服务端默认 standard：中文整句被当成
                # 一个词，BM25 对中文查询恒为 0。
                shown = actual or "standard（未设 analyzer_params）"
                return CollectionState(
                    state=STATE_DRIFTED,
                    analyzer=actual,
                    detail=f"分析器是 {shown}，期望 {analyzer_type}",
                )
            return CollectionState(state=STATE_REUSED, analyzer=actual, detail="schema 一致")

        schema = build_schema(dim=dim, analyzer_type=analyzer_type)
        index_params = client.prepare_index_params()
        index_params.add_index(
            field_name=VECTOR_FIELD,
            metric_type=metric_type,
            index_type="AUTOINDEX",
            index_name="vector_index",
        )
        index_params.add_index(
            field_name=SPARSE_FIELD,
            index_type="SPARSE_INVERTED_INDEX",
            metric_type="BM25",
            index_name="sparse_index",
        )
        client.create_collection(
            collection_name=collection_name,
            schema=schema,
            index_params=index_params,
            consistency_level=consistency_level,
        )
        log.info("milvus collection created by thinkback")
        return CollectionState(
            state=STATE_CREATED, analyzer=analyzer_type, detail="已按 Thinkback schema 建好"
        )
    except Exception as exc:  # noqa: BLE001 - 建表失败要看得见
        log.bind(error_type=type(exc).__name__, error=str(exc)).warning(
            "milvus collection ensure failed"
        )
        return CollectionState(state=STATE_FAILED, detail=f"{type(exc).__name__}: {exc}")
    finally:
        client.close()


def copy_rows(
    *,
    client: Any,
    source: str,
    target: str,
    batch_size: int = 500,
) -> tuple[int, int]:
    """复制式回填：把 source 的行原样搬进 target，返回 (已复制行数, 源总行数)。

    向量与原文一起搬，不重新嵌入、不重新抽取事实。目标端已有同 id 的行会被
    覆盖（upsert），所以作业可以中断后重跑。
    """

    copied = 0
    total = 0
    offset = 0
    while True:
        rows = client.query(
            collection_name=source,
            filter="",
            output_fields=list(COPYABLE_FIELDS),
            limit=batch_size,
            offset=offset,
        )
        if not rows:
            break
        payload = [{key: row.get(key) for key in COPYABLE_FIELDS} for row in rows]
        client.upsert(collection_name=target, data=payload)
        # 不 flush 的话集合统计里看不到这批行，回填的「行数一致」判据会误判。
        client.flush(collection_name=target)
        copied += len(payload)
        total += len(payload)
        offset += batch_size
        if len(rows) < batch_size:
            break
    return copied, total


def mirror_rows(*, client: Any, source: str, target: str, ids: list[str]) -> int:
    """双写：把 source 里这些 id 的行搬到 target，返回搬运行数。"""

    if not ids:
        return 0
    payload: list[dict[str, Any]] = []
    for start in range(0, len(ids), 100):
        chunk = ids[start : start + 100]
        rows = client.get(collection_name=source, ids=chunk)
        for row in rows or []:
            payload.append({key: row.get(key) for key in COPYABLE_FIELDS})
    if not payload:
        return 0
    client.upsert(collection_name=target, data=payload)
    client.flush(collection_name=target)
    return len(payload)


def delete_mirror_rows(*, client: Any, target: str, ids: list[str]) -> int:
    """双写窗口内的删除同步：旧集合必须跟着删，否则回滚会把已擦除事实带回来。"""

    if not ids:
        return 0
    deleted = 0
    for start in range(0, len(ids), 100):
        chunk = ids[start : start + 100]
        client.delete(collection_name=target, ids=chunk)
        deleted += len(chunk)
    client.flush(collection_name=target)
    return deleted


def row_count(*, client: Any, collection: str, page: int = 16384) -> int:
    """集合里可见的行数。

    不用 ``get_collection_stats``：它的 row_count 把已删除但还没压实的墓碑行
    也算进去（实测老集合报 252，实际可见 240），拿它当「回填前后行数一致」
    的判据就永远对不上。
    """

    total = 0
    offset = 0
    while True:
        rows = client.query(
            collection_name=collection,
            filter="",
            output_fields=[ID_FIELD],
            limit=page,
            offset=offset,
        )
        if not rows:
            break
        total += len(rows)
        offset += page
        if len(rows) < page:
            break
    return total
