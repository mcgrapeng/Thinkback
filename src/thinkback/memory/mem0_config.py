"""mem0 自定义配置服务 — 管理 mem0 抽取提示词等可变配置。

存储在 ins_mem0_config 表(key-value 结构)。
每次 append 操作实时读取,无需重启服务。

主要配置项:
- `custom_instructions`: mem0 v2 的 user prompt 追加段(事实抽取约束)

初始值来自 THINKBACK_CUSTOM_INSTRUCTIONS(硬编码默认),首次启动自动写入 DB。
治理台可随时编辑,下次 append 时生效。
"""

from __future__ import annotations

import threading

from loguru import logger
from sqlalchemy import select

from thinkback.infra.database.models import Mem0ConfigRecord
from thinkback.memory.backends.mem0_library import THINKBACK_CUSTOM_INSTRUCTIONS

# 线程锁:防止并发写入冲突
_lock = threading.Lock()

# 进程内缓存(减少 DB 查询,但仍可被外部刷新)
_cache: dict[str, str] = {}
_cache_dirty = True

DEFAULT_CONFIGS: dict[str, tuple[str, str]] = {
    "custom_instructions": (
        THINKBACK_CUSTOM_INSTRUCTIONS,
        "mem0 v2 事实抽取约束(user prompt 追加段)。每次 append 时拼入。",
    ),
}


def _get_session_factory():
    """延迟获取 session factory,避免循环导入。"""
    from thinkback.infra.database.engine import SessionLocal

    return SessionLocal


async def init_mem0_configs() -> None:
    """启动时写入默认配置(不存在则插入,存在则跳过)。"""
    factory = _get_session_factory()
    async with factory() as session:
        for key, (default_value, description) in DEFAULT_CONFIGS.items():
            existing = await session.execute(
                select(Mem0ConfigRecord).where(Mem0ConfigRecord.config_key == key)
            )
            if existing.scalar_one_or_none() is None:
                session.add(
                    Mem0ConfigRecord(
                        config_key=key,
                        value=default_value,
                        description=description,
                        is_active=True,
                    )
                )
                logger.info(f"mem0_config.initialized key={key}")
        await session.commit()
    global _cache_dirty
    _cache_dirty = True


async def get_config_value(key: str) -> str:
    """读取配置值(带缓存)。找不到时返回硬编码默认值。"""
    global _cache_dirty
    if _cache_dirty:
        await _refresh_cache()

    value = _cache.get(key)
    if value is not None:
        return value

    default = DEFAULT_CONFIGS.get(key, ("", ""))[0]
    return default


async def _refresh_cache() -> None:
    """从 DB 加载所有 active 配置到内存缓存。"""
    global _cache_dirty
    factory = _get_session_factory()
    async with factory() as session:
        result = await session.execute(
            select(Mem0ConfigRecord).where(Mem0ConfigRecord.is_active.is_(True))
        )
        rows = result.scalars().all()
        for row in rows:
            _cache[row.config_key] = row.value
    _cache_dirty = False


async def set_config_value(key: str, value: str, description: str = "") -> None:
    """写入/更新配置值。立即刷新缓存。"""
    global _cache_dirty
    factory = _get_session_factory()
    async with factory() as session:
        existing = await session.execute(
            select(Mem0ConfigRecord).where(Mem0ConfigRecord.config_key == key)
        )
        record = existing.scalar_one_or_none()
        if record:
            record.value = value
            if description:
                record.description = description
        else:
            session.add(
                Mem0ConfigRecord(
                    config_key=key,
                    value=value,
                    description=description,
                    is_active=True,
                )
            )
        await session.commit()
    _cache_dirty = True
    logger.info(f"mem0_config.updated key={key} value_length={len(value)}")


async def list_all_configs() -> list[dict]:
    """列出所有配置项(含默认值,即使 DB 中不存在)。"""
    factory = _get_session_factory()
    async with factory() as session:
        result = await session.execute(select(Mem0ConfigRecord))
        rows = {r.config_key: r for r in result.scalars().all()}

    configs = []
    for key, (default_value, description) in DEFAULT_CONFIGS.items():
        db_row = rows.get(key)
        configs.append({
            "key": key,
            "value": db_row.value if db_row else default_value,
            "description": db_row.description if db_row else description,
            "is_active": db_row.is_active if db_row else True,
            "updated_at": db_row.updated_at.isoformat() if db_row else None,
        })
    return configs


def invalidate_cache() -> None:
    """手动失效缓存(下次读取时重新从 DB 加载)。"""
    global _cache_dirty
    _cache_dirty = True
