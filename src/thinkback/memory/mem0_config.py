"""mem0 自定义配置服务 — 管理 mem0 抽取提示词等可变配置。

存储在 ins_mem0_config 表(key-value 结构)。
每次 append 操作实时读取,无需重启服务。

支持的配置项:

1. `custom_instructions` — mem0 v2 的 user prompt 追加段(事实抽取约束)
   - 挂到 v2 的 ADDITIVE_EXTRACTION_PROMPT 的「## Custom Instructions」段
   - 控制:记忆语言、输出格式、实体保留、修正处理、粒度

2. `update_memory_prompt` — 记忆更新/冲突处理提示词
   - 控制 ADD/UPDATE/DELETE/NONE 四种操作的决策规则
   - 影响:记忆合并、取代、去重策略

3. `fact_extraction_prompt` — v1 事实抽取提示词
   - v1 模式下的事实抽取系统提示
   - 控制:记忆类型识别、分类、粒度

4. `memory_answer_prompt` — 记忆召回回答提示词
   - 控制:如何基于记忆回答问题
   - 影响:回答的准确性、完整性、语气

5. `extraction_system_prompt` — v2 抽取系统提示(完整)
   - 覆盖 mem0 默认的 ADDITIVE_EXTRACTION_PROMPT
   - 高级用法:完全重写抽取逻辑

初始值来自 mem0 默认 + THINKBACK_CUSTOM_INSTRUCTIONS(硬编码默认)。
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

# ─── 默认配置(硬编码 fallback,DB 中不存在时使用) ──────────────────────

DEFAULT_CONFIGS: dict[str, tuple[str, str]] = {
    "custom_instructions": (
        THINKBACK_CUSTOM_INSTRUCTIONS,
        "mem0 v2 事实抽取约束(user prompt 追加段)。每次 append 时拼入。控制记忆语言、输出格式、实体保留、修正处理、粒度等。",
    ),
    "update_memory_prompt": (
        "Compare newly retrieved facts with the existing memory. For each new fact, decide whether to:\n"
        "- ADD: Add it to the memory as a new element\n"
        "- UPDATE: Update an existing memory element\n"
        "- DELETE: Delete an existing memory element\n"
        "- NONE: Make no change (if the fact is already present or irrelevant)\n\n"
        "Guidelines:\n"
        "1. **Add**: If the retrieved facts contain new information not present in the memory.\n"
        "2. **Update**: If the retrieved facts contain information that is already present but the information is totally different. If the fact conveys the same thing, keep the fact with the most information.\n"
        "3. **Delete**: If the retrieved facts contradict existing memories, delete the old one.\n"
        "4. **NONE**: If the fact is already present or irrelevant.",
        "记忆更新/冲突处理提示词。控制 ADD/UPDATE/DELETE/NONE 四种操作的决策规则。影响记忆合并、取代、去重策略。",
    ),
    "memory_answer_prompt": (
        "You are an expert at answering questions based on the provided memories. Your task is to provide accurate and concise answers to the questions by leveraging the information given in the memories.\n\n"
        "Guidelines:\n"
        "- Extract relevant information from the memories based on the question.\n"
        "- If no relevant information is found, make sure you don't say no information is found. Instead, accept the question and provide a general response.\n"
        "- Ensure that the answers are clear, concise, and directly address the question.",
        "记忆召回回答提示词。控制如何基于记忆回答问题。影响回答的准确性、完整性、语气。",
    ),
}

# ─── 默认配置说明(用于 UI 展示) ──────────────────────────────────────────

CONFIG_SECTIONS = {
    "custom_instructions": {
        "title": "抽取约束 (custom_instructions)",
        "category": "extraction",
        "icon": "Sparkles",
        "tips": [
            "用「## Custom Instructions」段拼入 mem0 的 user prompt",
            "控制:记忆语言、输出格式(JSON schema)、实体保留规则、修正处理、粒度",
            "常见调优:加业务术语约束、调整记忆粒度(一次最多抽 N 条)、改输出格式",
            "建议:保持简洁,每次写入记忆时都拼入此 prompt,过长会增加 token 消耗",
        ],
        "examples": [
            {
                "label": "语言约束",
                "code": "Language: write each memory text in the SAME language as the conversation (Chinese conversations → Chinese memories). Never translate Chinese names, nicknames, places, or brands into English.",
            },
            {
                "label": "输出格式",
                "code": 'Output shape: return exactly {"memory": [{"id": "<sequential string>", "text": "<one self-contained memory>"}]}. Every "text" MUST be a plain string; never nest arrays or objects inside it.',
            },
            {
                "label": "记忆粒度",
                "code": "One fact per memory. If a turn contains multiple distinct facts, emit one memory per fact. Never combine \"X is Y\" + \"X is Z\" into a single memory.",
            },
        ],
    },
    "update_memory_prompt": {
        "title": "更新策略 (update_memory_prompt)",
        "category": "update",
        "icon": "RefreshCw",
        "tips": [
            "控制记忆的 ADD/UPDATE/DELETE/NONE 四种操作决策",
            "影响:记忆合并策略、取代逻辑、去重规则",
            "常见调优:修改冲突解决策略(如「新值优先」vs「保留最完整值」)、调整去重阈值",
            "建议:默认策略已较通用,只有特定业务场景才需要修改",
        ],
        "examples": [
            {
                "label": "新值优先",
                "code": "If the retrieved fact conveys the same thing as an existing memory, prefer the NEW fact (recent information is more accurate).",
            },
            {
                "label": "保留最完整",
                "code": "If the retrieved fact conveys the same thing as an existing memory, keep the fact with the MOST information (longest, most detailed).",
            },
        ],
    },
    "memory_answer_prompt": {
        "title": "召回回答 (memory_answer_prompt)",
        "category": "recall",
        "icon": "Search",
        "tips": [
            "控制记忆召回时的 LLM 回答行为",
            "影响:回答的准确性、完整性、语气",
            "常见调优:改回答风格(如「简洁」vs「详细」)、加引用格式、调整不确定性处理",
            "建议:根据业务场景定制,如客服场景可加「如果记忆不足请主动询问」",
        ],
        "examples": [
            {
                "label": "简洁回答",
                "code": "Provide concise answers. Only use information from the provided memories. If the memory doesn't contain the answer, say so briefly.",
            },
            {
                "label": "详细回答",
                "code": "Provide detailed, comprehensive answers. Use all relevant information from the memories. If multiple memories relate to the question, synthesize them into a coherent answer.",
            },
        ],
    },
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
