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
