"""Milvus infrastructure helpers."""

from loguru import logger


def ensure_milvus_database(
    *,
    milvus_url: str,
    milvus_database: str,
    milvus_token: str | None = None,
) -> None:
    """Make sure the configured Milvus database exists before mem0 tries to use it.

    mem0's Milvus vector store 会把 ``db_name`` 原样传给 Milvus，但不会自动
    ``CREATE DATABASE``；若 db 不存在，首次写就会抛
    ``MilvusException: (code=800, message=database not found)`` 并把 append 接口
    拖到 502。这条 lazy-create 钩子在服务启动时建好 db，让 mem0 可以照常落盘。

    失败时只 warning，不抛异常——Milvus 鉴权或 db 名非法时仍然能 boot，
    业务侧会在 append 时看到 802（db 名非法）或 800（不存在）的明确错误。
    """

    if not milvus_database:
        return
    try:
        from pymilvus import MilvusClient
    except ImportError as exc:
        raise RuntimeError("pymilvus is not installed; install pymilvus") from exc

    ensure_log = logger.bind(
        milvus_url=milvus_url,
        milvus_database=milvus_database,
    )
    try:
        client = MilvusClient(uri=milvus_url, token=milvus_token or None)
    except Exception as exc:
        ensure_log.bind(error_type=type(exc).__name__, error=str(exc)).warning(
            "milvus lazy-create connection failed"
        )
        return
    try:
        existing = client.list_databases()
    except Exception as exc:
        ensure_log.bind(error_type=type(exc).__name__, error=str(exc)).warning(
            "milvus lazy-create list databases failed"
        )
        client.close()
        return
    if milvus_database in existing:
        ensure_log.info("milvus database already exists")
        client.close()
        return
    try:
        client.create_database(milvus_database)
        ensure_log.info("milvus database created")
    except Exception as exc:
        ensure_log.bind(error_type=type(exc).__name__, error=str(exc)).warning(
            "milvus lazy-create create database failed"
        )
    finally:
        client.close()


def ensure_milvus_collection_consistency(
    *,
    milvus_url: str,
    milvus_database: str,
    collection_name: str,
    level: str = "Strong",
    milvus_token: str | None = None,
) -> str:
    """把集合的一致性级别提到 ``level``（默认 Strong），返回实际级别。

    为什么需要：Milvus 集合默认是 Bounded —— 写入后立刻用同一连接查，
    新向量**可能还看不见**。对记忆服务这是硬伤：用户刚说过的话，紧接着
    问「我刚说的什么」，召回却查不到（实测写后立刻 recall 命中率明显低于
    延时 recall）。Strong 让读看到最新写入。

    集合由 mem0 首次写入时创建，这里只能事后改属性（Milvus 支持
    ``alter_collection_properties``）；集合还不存在时返回 "skipped"，
    调用方在首次写入后再调一次。
    """
    try:
        from pymilvus import MilvusClient
    except ImportError as exc:
        raise RuntimeError("pymilvus is not installed; install pymilvus") from exc

    consistency_log = logger.bind(
        milvus_url=milvus_url,
        milvus_database=milvus_database,
        collection=collection_name,
        level=level,
    )
    client = MilvusClient(uri=milvus_url, token=milvus_token or None)
    try:
        if not client.has_collection(collection_name):
            consistency_log.info("milvus collection not created yet, consistency level skipped")
            return "skipped"
        current = str(client.describe_collection(collection_name).get("consistency_level_name", ""))
        if current.lower() == level.lower():
            return current
        client.alter_collection_properties(collection_name, {"consistency_level": level})
        consistency_log.bind(previous=current).info("milvus collection consistency level raised")
        return level
    except Exception as exc:
        consistency_log.bind(error_type=type(exc).__name__, error=str(exc)).warning(
            "milvus collection consistency level update failed"
        )
        return "failed"
    finally:
        client.close()
