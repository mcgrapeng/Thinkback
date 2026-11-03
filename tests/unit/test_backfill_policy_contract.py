"""回填路径的策略一致性契约。

背景：回填（P0 槽位回填）把原始轮次里的关键事实直接物化成一条**本地索引行**。
它与「引擎抽取 → 本地索引」是两条并行的写入路径，两边必须按**同一份已解析
的策略**判定：类型准入、数据分类、TTL 到期。

踩过的坑（一次根因、三个症状）：``_publish_append_round_locally`` 把调用方
原样的 ``request.metadata`` 传给回填路径，而不是 append 路径上解析出来的
``effective_metadata``。于是回填行：

1. 丢 TTL —— 引擎侧到期隐藏了事实，本地行却永远 ACTIVE，过期事实回流；
2. 分类被降级 —— 类型策略声明 personal、调用方声明 normal，回填行记成
   normal（调用方能借回填路径把策略声明的分类降下来）；
3. 漏掉 per-type 指令的类型 —— 回填路径看不到解析后的类型名，照样写出
   不属于该类型语义的事实。

这三个症状同源，因此放在一个文件里钉住：任何一条被改回去都会红。

第三条（类型自带指令时回填整条跳过）已随「回填对带指令的类型照常参与」
一起翻过来：引擎产出的本来就是 P0 模板文本，冲突键也按 P0 槽位算，清单外
的 P0 事实（昵称、宠物名、沟通偏好、睡眠提醒）只能靠回填兜底。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from thinkback.domain.memory_types import MemoryTypeRegistry, MemoryTypeSpec
from thinkback.memory.repositories import InMemoryMemoryRepository
from thinkback.memory.schemas import AppendMemoryRequest, MessageRole
from thinkback.memory.service import MemoryService


class _NoEventBackend:
    """引擎返回空事件：只有回填路径会写本地索引行。

    真实链路上 mem0 抽不到东西（空抽率、被白名单挡掉）就是这个形状。
    """

    def add(self, messages: list[dict[str, str]], **kwargs: Any) -> list[dict[str, Any]]:
        return []

    def search(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    def update(self, memory_id: str, data: str) -> None:
        return None

    def delete(self, memory_id: str) -> None:
        return None

    def delete_many(self, memory_ids: list[str]) -> int:
        return 0

    def get_all(self, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    def get(self, memory_id: str) -> dict[str, Any] | None:
        return None

    def delete_all(self, **kwargs: Any) -> list[str]:
        return []

    def purge_history(self, **kwargs: Any) -> dict[str, int]:
        return {"history": 0, "messages": 0, "supported": False}


def _append(
    round_id: str, content: str, metadata: dict[str, Any] | None = None
) -> AppendMemoryRequest:
    moment = datetime(2026, 5, 4, 10, 0, 0, tzinfo=UTC)
    return AppendMemoryRequest(
        request_id=f"req-{round_id}",
        user_id="user-1",
        session_id="session-1",
        round_id=round_id,
        messages=[
            {
                "message_id": f"{round_id}-u",
                "role": MessageRole.USER,
                "content": content,
                "timestamp": moment,
            }
        ],
        source_timestamp=moment,
        namespace="thinkback",
        metadata=metadata or {},
    )


def _service(registry: MemoryTypeRegistry | None = None) -> MemoryService:
    return MemoryService(
        repository=InMemoryMemoryRepository(),
        backend=_NoEventBackend(),
        l3_write_mode="sync",
        registry_provider=(lambda _namespace: registry) if registry else None,
    )


def test_backfilled_row_carries_the_ttl_of_its_memory_type() -> None:
    """TTL 类型（state，90 天）的回填行必须带到期日。

    不带的后果：引擎侧按 expiration_date 隐藏了这条事实，本地索引行却
    永远 ACTIVE —— 过期事实从本地召回/列表回流，且永远不被保留期作业清理。
    """
    service = _service()

    service.append(_append("r-ttl", "我现在住在杭州", metadata={"memory_type": "state"}))

    rows = service.repository.list_memories("user-1", "thinkback")
    assert len(rows) == 1, "回填行应当只有一条"
    assert rows[0].expires_at is not None, "TTL 类型的回填行必须有到期日"
    # 到期日按「写入时刻 + ttl_days」算，落在 90 天之内。
    assert datetime.now(UTC) < rows[0].expires_at <= datetime.now(UTC) + timedelta(days=91)


def test_backfilled_row_keeps_the_strictest_classification() -> None:
    """类型策略声明的分类是下限，回填行不得低于它。

    state 的策略分类是 personal；调用方声明 normal（更宽松的一级）也不能
    把回填行记成 normal —— 否则分类声明在回填路径上被绕过。
    """
    service = _service()

    service.append(
        _append(
            "r-class",
            "我现在住在杭州",
            metadata={"memory_type": "state", "data_classification": "normal"},
        )
    )

    rows = service.repository.list_memories("user-1", "thinkback")
    assert len(rows) == 1
    assert rows[0].data_classification == "personal"


def test_backfill_runs_for_a_type_that_owns_an_extraction_prompt() -> None:
    """类型自带抽取指令时，回填照常参与。

    曾经整条跳过，理由是「P0 的英文模板文本跟类型指令冲突，混进来只会产出
    既不属于该类型语义、又算不出冲突键的条目」。两条前提都不成立：引擎自己
    产出的就是这套模板文本（mem0 先 ADD 属性形态、再 UPDATE 成模板形态），
    冲突键现在也按 P0 槽位算（属性名形态经 ATTRIBUTE_SLOTS 映射回槽位）。

    不放开的后果：类型指令清单外的 P0 事实（昵称、宠物名、沟通偏好、睡眠提醒）
    只能靠引擎那句「照常抽成一句话」—— 不可靠：同一句话这轮抽到、下轮抽不到
    （真实栈实测 2026-11-03：狗名/睡眠提醒/沟通偏好三轮抽取为空，召回 0 条），
    抽不到时事实静默消失。回填是这些槽位唯一的确定性兜底。
    """
    registry = MemoryTypeRegistry(
        namespace="thinkback",
        version="1",
        types={
            "profile": MemoryTypeSpec(
                name="profile",
                extractor="mem0",
                source_roles=("user",),
                conflict="overwrite",
                retention="forever",
                classification="personal",
                extraction_prompt="prompts/x/profile.v1.md",
                extraction_prompt_text="按 <属性名>:<值> 产出。",
            )
        },
    )
    service = _service(registry)

    # 不显式给 memory_type：append 解析出的默认类型是 profile（带指令）。
    service.append(_append("r-prompt", "我现在住在杭州"))

    rows = service.repository.list_memories("user-1", "thinkback")
    assert len(rows) == 1, "带指令的类型也要有回填行"
    assert rows[0].memory_text == "User lives in 杭州"


def test_backfill_runs_for_a_type_without_an_extraction_prompt() -> None:
    """对照组：没有 per-type 指令的类型照常回填。

    上一条钉的是「跳过」，这一条钉的是「只在该跳的时候跳」—— 否则修
    TTL 时把回填整个关掉，这两条会一起绿。
    """
    registry = MemoryTypeRegistry(
        namespace="thinkback",
        version="1",
        types={
            "profile": MemoryTypeSpec(
                name="profile",
                extractor="mem0",
                source_roles=("user",),
                conflict="overwrite",
                retention="forever",
                classification="personal",
            )
        },
    )
    service = _service(registry)

    service.append(_append("r-no-prompt", "我现在住在杭州"))

    rows = service.repository.list_memories("user-1", "thinkback")
    assert len(rows) == 1
    assert rows[0].memory_text == "User lives in 杭州"


def test_backfilled_row_records_the_source_time_as_valid_at() -> None:
    """回填行的事实生效时刻取来源时间，不是写入时刻。

    冲突仲裁（valid_at 最新的胜出）与召回期的槽位去重都按 valid_at 比；
    回填行与引擎抽取行必须是同一个口径，否则同一条事实的两条记录会
    按不同时间轴仲裁。
    """
    service = _service()
    source_timestamp = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

    service.append(
        AppendMemoryRequest(
            request_id="req-valid",
            user_id="user-1",
            session_id="session-1",
            round_id="r-valid",
            messages=[
                {
                    "message_id": "m-valid",
                    "role": MessageRole.USER,
                    "content": "我现在住在杭州",
                    "timestamp": source_timestamp,
                }
            ],
            source_timestamp=source_timestamp,
            namespace="thinkback",
            metadata={"memory_type": "profile"},
        )
    )

    rows = service.repository.list_memories("user-1", "thinkback")
    assert len(rows) == 1
    assert rows[0].valid_at == source_timestamp
