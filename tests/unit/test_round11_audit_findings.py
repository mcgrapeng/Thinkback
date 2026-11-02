"""第七轮对抗性审计发现的缺陷 —— 每条一个回归钉子。

这一轮修的是**八条已复现的缺陷**（全部先用探针脚本复现出错误行为，再改源码）：

1. ``MEMORY_L1_CACHE_ENABLED=false`` 时 L1 召回通道整个消失（读穿透拿到的
   轮次被丢掉）—— 多副本部署正是这个配置。
2. 正文去重键把 ``1.5`` 与 ``15`` 归一成同一个键，召回时一条被静默丢弃。
3. gRPC 侧的未知 ``memory_type`` 枚举被 ``.get()`` 悄悄丢掉，编辑照常成功。
4. decay 按 ``valid_at`` 判龄，导入的旧会话在写入后第一次清扫就被抑制 ——
   它从落地到被抑制之间一次都没被召回过。
5. HTTP 的 GET 读按对端 IP 分桶，gRPC 的同一个读按租户分桶：同一租户换个
   协议就换了一份额度。
6. L1 缓存窗口按写入顺序而不是回合时间保留：晚到的旧轮次把窗口里最新的
   那条挤掉。
7. SQL 仓储用真值判「有没有传」过滤条件，``user_id=""`` 会丢掉租户过滤 ——
   与内存实现分叉，且分叉方向是「少一道隔离」。
8. ``list_expired_memories`` 排序时未归一时区，naive 与 aware 混排抛 TypeError。

每条用例都对着**它修的那个行为**，不是「多测一点」。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from thinkback.api.app import create_app
from thinkback.api.ratelimit import RateLimitMiddleware
from thinkback.domain.dedup import content_dedup_key, normalize_text
from thinkback.domain.entities import JournalEntry, MemoryIndexEntry
from thinkback.domain.enums import MemoryStatus
from thinkback.domain.recall_policy import dedupe_and_clip
from thinkback.memory.backends.fake import FakeMemoryBackend
from thinkback.memory.decay import MemoryDecaySweeper
from thinkback.memory.repositories import InMemoryMemoryRepository
from thinkback.memory.schemas import (
    AppendMemoryRequest,
    MemoryItem,
    MessageRole,
    RecallMemoryRequest,
)
from thinkback.memory.service import MemoryService
from thinkback.rpc import memory_pb2 as pb
from thinkback.rpc.converters import to_update_request


def _append(round_id: str, content: str, metadata: dict[str, Any] | None = None) -> Any:
    moment = datetime(2026, 5, 4, 10, 0, 0, tzinfo=UTC)
    return AppendMemoryRequest(
        request_id=f"req-{round_id}",
        user_id="user-1",
        session_id="session-1",
        round_id=round_id,
        messages=[
            {
                "message_id": f"m-{round_id}",
                "role": MessageRole.USER,
                "content": content,
                "timestamp": moment,
            }
        ],
        source_timestamp=moment,
        namespace="thinkback",
        metadata=metadata or {},
    )


# ── 1. 缓存关掉时 L1 读穿透必须仍然拿得到轮次 ─────────────────────────


def test_l1_recall_survives_when_the_cache_is_disabled() -> None:
    """多副本部署必须关掉进程内缓存（``MEMORY_L1_CACHE_ENABLED=false``）。

    关掉之后读穿透去 journal 取最近轮次 —— 但取回来的轮次被丢在中间：
    ``update_l1`` 是 no-op、``get_l1`` 恒返回空，于是 L1 整层从召回里消失。
    线上表现是「多副本部署后助手看不到最近几轮对话」。
    """
    repository = InMemoryMemoryRepository()
    repository.l1_cache_enabled = False
    service = MemoryService(
        repository=repository, backend=FakeMemoryBackend(), l3_write_mode="sync"
    )

    service.append(_append("r-cache-off", "我养了一只猫叫麻薯"))

    response = service.recall(
        RecallMemoryRequest(
            user_id="user-1", session_id="session-1", query="猫", namespace="thinkback"
        )
    )

    l1_items = [item for item in response.items if item.layer == "L1"]
    assert l1_items, "缓存关掉时 L1 读穿透必须仍然返回最近轮次"
    assert "麻薯" in l1_items[0].content


# ── 2. 去重键不能把 1.5 与 15 归成同一个 ───────────────────────────────


def test_a_decimal_point_keeps_two_different_numbers_apart() -> None:
    """``1.5 万`` 与 ``15 万`` 是两条不同的事实。

    归一化把非实义字符全滤掉，小数点也在其中 —— 两条事实落到同一个键，
    召回时其中一条被当成重复**静默丢掉**。丢掉一条真实事实比重复更严重。
    """
    assert content_dedup_key("预算 1.5 万") != content_dedup_key("预算 15 万")
    # 标点仍然滤掉（归一只用于比较）。
    assert normalize_text("User lives in Shanghai.") == "userlivesinshanghai"


def test_only_a_decimal_point_between_two_digits_is_kept() -> None:
    """保留的是「夹在两个数字之间的小数点」，不是所有点。

    - ``1.5`` → ``1.5``（小数点保留）
    - ``1.5.5`` 里两个点都夹在数字之间 → 都保留
    - ``...``（不夹在数字之间）→ 滤掉
    - ``1.``（后面不是数字）→ 滤掉
    - ``.5``（前面不是数字）→ 滤掉

    判据写宽一点（把任何点都保留）就会让 ``user.`` 与 ``user`` 分成两个键 ——
    同一句话的两种写法不再去重。
    """
    assert normalize_text("1.5") == "1.5"
    assert normalize_text("1.5.5") == "1.5.5"
    assert normalize_text("...") == ""
    assert normalize_text("1.") == "1"
    assert normalize_text(".5") == "5"
    # 句末句号不保留：同一句话的两种写法仍落到同一个键。
    assert normalize_text("user.") == normalize_text("user")


def test_recall_keeps_both_amounts_when_they_differ_only_by_a_decimal_point() -> None:
    items = [
        MemoryItem(layer="L3", content="预算 1.5 万", memory_id="m1", score=0.9),
        MemoryItem(layer="L3", content="预算 15 万", memory_id="m2", score=0.9),
    ]

    kept = dedupe_and_clip(items, 1000)

    assert {item.content for item in kept} == {"预算 1.5 万", "预算 15 万"}


# ── 3. gRPC 的未知枚举必须报错而不是被丢掉 ────────────────────────────


def test_grpc_rejects_an_unknown_memory_type_enum() -> None:
    """客户端版本比服务端新时会发来服务端不认的枚举值。

    ``.get()`` 把它悄悄变成 None：编辑照常成功、类型保持旧值，调用方以为
    改了类型而实际没改。本文件里其它枚举都用 ``_enum_value``（它存在的理由
    正是这一条），这里漏了。
    """
    request = pb.UpdateMemoryRequest(
        request_id="r",
        user_id="u",
        operation_id="o",
        memory_id="m",
        content="x",
        memory_type=99,
    )

    with pytest.raises(ValueError, match="unknown memory type enum value"):
        to_update_request(request)


def test_the_recall_budget_survives_the_round_trip_to_proto() -> None:
    """``recall_budget``（类型的 token 预算上限）要原样传到 gRPC 调用方。

    它是 proto 里的 ``optional int32``：没配就是「没设」，配了就是那个值。
    转换层把它丢掉或写成 0，接入方就按「不限」或「0」理解 —— 两种都错。
    """
    from thinkback.memory.schemas import ListMemoryTypesResponse, MemoryTypeInfo
    from thinkback.rpc.converters import types_response_to_pb

    response = ListMemoryTypesResponse(
        status="ok",
        namespace="acme",
        policy_version="1",
        types=[
            MemoryTypeInfo(
                name="profile",
                description="",
                extractor="mem0",
                source_roles=["user"],
                conflict="overwrite",
                retention="forever",
                classification="personal",
                review_required=False,
                extraction_prompt_version="default",
                recall_budget=200,
            ),
            MemoryTypeInfo(
                name="state",
                description="",
                extractor="mem0",
                source_roles=["user"],
                conflict="overwrite",
                retention="ttl",
                ttl_days=30,
                classification="personal",
                review_required=False,
                extraction_prompt_version="default",
            ),
        ],
    )

    converted = types_response_to_pb(response)

    by_name = {item.name: item for item in converted.types}
    assert by_name["profile"].HasField("recall_budget")
    assert by_name["profile"].recall_budget == 200
    # 没配 recall_budget 的类型：那一位就是不设（不是 0）。
    assert not by_name["state"].HasField("recall_budget")
    # ttl_days 同理：配了就是那个值。
    assert by_name["state"].HasField("ttl_days")
    assert by_name["state"].ttl_days == 30


# ── 4. decay 按「我们持有多久」判龄，不是「事实多久前发生」 ─────────────


def _sweeper(repository: InMemoryMemoryRepository) -> MemoryDecaySweeper:
    return MemoryDecaySweeper(
        repository=repository,
        executor=ThreadPoolExecutor(max_workers=1),
        min_age_days=90,
        unrecalled_days=60,
    )


def test_decay_ages_by_when_we_recorded_the_memory_not_when_the_fact_happened() -> None:
    """导入 200 天前的会话：事实很旧，但我们**刚刚**记下它。

    按 ``valid_at`` 判龄的话，这条记忆在写入后的第一次清扫就被置
    SUPPRESSED —— 它从落地到被抑制之间一次都没被召回过，「久未被召回」这个
    前提根本不成立。判龄要看 ``recorded_at``（我们持有它多久还没用到）；
    「内容有多旧」由 TTL 负责。
    """
    repository = InMemoryMemoryRepository()
    old_fact = datetime.now(UTC) - timedelta(days=200)
    repository.add_memory_index(
        backend_memory_id="imported-1",
        user_id="user-1",
        memory_scope_id="thinkback",
        source_refs=[{"session_id": "session-1", "round_id": "r-imported"}],
        memory_text="用户去年在读航海书",
        valid_at=old_fact,
        recorded_at=datetime.now(UTC),
    )

    suppressed = _sweeper(repository).sweep_now("user-1", "thinkback")

    assert suppressed == 0, "刚记下的记忆不能因为内容久远就被抑制"
    assert len(repository.active_memories("user-1", "thinkback")) == 1


def test_decay_still_suppresses_a_memory_we_have_held_for_a_long_time() -> None:
    """对照组：持有很久且从未被召回的长尾记忆照常衰减。

    上一条钉的是「不该衰减时不衰减」，这一条钉的是「该衰减时仍衰减」——
    否则把判龄改成永不过期也能让上一条变绿。
    """
    repository = InMemoryMemoryRepository()
    long_held = datetime.now(UTC) - timedelta(days=200)
    repository.add_memory_index(
        backend_memory_id="tail-1",
        user_id="user-1",
        memory_scope_id="thinkback",
        source_refs=[{"session_id": "session-1", "round_id": "r-tail"}],
        memory_text="用户在准备一场马拉松",
        valid_at=long_held,
        recorded_at=long_held,
    )

    suppressed = _sweeper(repository).sweep_now("user-1", "thinkback")

    assert suppressed == 1
    assert repository.active_memories("user-1", "thinkback") == []


def test_decay_falls_back_to_valid_at_when_the_memory_was_never_timestamped() -> None:
    """``recorded_at`` 缺失的行回退到 ``valid_at`` 判龄。

    迁移前的遗留行没有 ``recorded_at``（列是后来加的）。回退路径要走通：
    否则那一批行要么永远不衰减、要么被当成「不可判老」全部跳过。
    """
    repository = InMemoryMemoryRepository()
    long_held = datetime.now(UTC) - timedelta(days=200)
    entry = repository.add_memory_index(
        backend_memory_id="legacy-1",
        user_id="user-1",
        memory_scope_id="thinkback",
        source_refs=[{"session_id": "session-1", "round_id": "r-legacy"}],
        memory_text="用户在准备一场马拉松",
        valid_at=long_held,
    )
    entry.recorded_at = None  # 模拟迁移前的遗留行

    suppressed = _sweeper(repository).sweep_now("user-1", "thinkback")

    assert suppressed == 1, "没有 recorded_at 的老行应当回退到 valid_at 判龄并被衰减"


def test_decay_skips_a_row_with_neither_timestamp() -> None:
    """两个时钟都没有的行不可判老，保守跳过。"""
    repository = InMemoryMemoryRepository()
    entry = repository.add_memory_index(
        backend_memory_id="no-clock",
        user_id="user-1",
        memory_scope_id="thinkback",
        source_refs=[{"session_id": "session-1", "round_id": "r-none"}],
        memory_text="用户在准备一场马拉松",
    )
    entry.valid_at = None
    entry.recorded_at = None

    suppressed = _sweeper(repository).sweep_now("user-1", "thinkback")

    assert suppressed == 0


def test_the_unrecalled_threshold_is_inclusive_at_the_boundary() -> None:
    """``last_recalled_at`` 恰好等于阈值时算候选（差一秒则不算）。

    判定是闭区间：``<=``。写成 ``<`` 时，恰好卡在边界上的记忆永远不被衰减，
    而离边界几十天的样本看不出差别。

    同上：从 ``sweep_now`` 走测不到边界（清扫器内部取自己的 ``now``），
    这里直接调判定函数，把阈值显式传进去。
    """
    repository = InMemoryMemoryRepository()
    moment = datetime(2026, 5, 4, 10, 0, 0, tzinfo=UTC)
    entry = repository.add_memory_index(
        backend_memory_id="edge-stale",
        user_id="user-1",
        memory_scope_id="thinkback",
        source_refs=[],
        memory_text="用户提过某个长尾话题",
        valid_at=moment,
        recorded_at=moment,
    )
    entry.last_recalled_at = moment
    sweeper = _sweeper(repository)

    assert sweeper._is_decay_candidate(entry, moment, moment) is True, (
        "恰好久未达阈值的记忆应当算候选"
    )
    entry.last_recalled_at = moment + timedelta(seconds=1)
    assert sweeper._is_decay_candidate(entry, moment, moment) is False, (
        "窗口内被召回过的记忆必须跳过"
    )


def test_the_age_threshold_is_inclusive_at_the_boundary() -> None:
    """判龄阈值是闭区间：``recorded_at`` 恰好等于阈值时算候选。

    从 ``sweep_now`` 走**测不到**这个边界：清扫器在 ``_decay_scope`` 内部
    取 ``datetime.now(UTC)``，测试设的值一定比它早几微秒，阈值永远差那么
    一点点。所以这里直接调判定函数，把阈值显式传进去 ——
    「恰好等于阈值」与「差一秒」两种输入要给出不同答案。
    """
    repository = InMemoryMemoryRepository()
    moment = datetime(2026, 5, 4, 10, 0, 0, tzinfo=UTC)
    entry = repository.add_memory_index(
        backend_memory_id="edge-aged",
        user_id="user-1",
        memory_scope_id="thinkback",
        source_refs=[],
        memory_text="用户提过某个长尾话题",
        valid_at=moment,
        recorded_at=moment,
    )
    sweeper = _sweeper(repository)

    assert sweeper._is_decay_candidate(entry, moment, moment) is True, "恰好到龄应当算候选"
    assert sweeper._is_decay_candidate(entry, moment - timedelta(seconds=1), moment) is False, (
        "未到龄的记忆必须跳过"
    )


def test_a_memory_recalled_at_the_boundary_is_still_protected() -> None:
    """窗口内（差一秒）被召回过的记忆必须留下。"""
    repository = InMemoryMemoryRepository()
    now = datetime.now(UTC)
    old = datetime.now(UTC) - timedelta(days=200)
    within = repository.add_memory_index(
        backend_memory_id="edge-recent",
        user_id="user-1",
        memory_scope_id="thinkback",
        source_refs=[],
        memory_text="用户提过另一个长尾话题",
        valid_at=old,
        recorded_at=old,
    )
    within.last_recalled_at = now - timedelta(days=60) + timedelta(seconds=1)

    suppressed = _sweeper(repository).sweep_now("user-1", "thinkback")

    assert suppressed == 0, "窗口内被召回过的记忆必须留下"


# ── 5. HTTP 与 gRPC 的读操作必须按同一个口径分桶 ────────────────────────


def _get_request(path: str, query: bytes = b"") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "query_string": query,
            "headers": [],
            "client": ("10.0.0.9", 1234),
            "server": ("test", 80),
            "scheme": "http",
        }
    )


@pytest.mark.asyncio
async def test_a_get_request_buckets_by_tenant_not_by_peer_ip() -> None:
    """``GET /v2/memory/items?user_id=alice`` 的 user_id 在查询串里。

    按对端 IP 分桶等于「一个网关后面几十个租户共用一份额度」—— 正是限流
    模块开头说明要避免的那件事。gRPC 面的同一个读操作按 namespace +
    user_id 分桶，两面口径必须一致。
    """
    request = _get_request("/v2/memory/items", b"user_id=tenant-a")

    client_id = await RateLimitMiddleware._resolve_client_id(request)

    assert client_id == "thinkback:tenant-a"


@pytest.mark.asyncio
async def test_a_get_request_without_a_user_id_still_falls_back_to_the_peer_ip() -> None:
    """取不到调用方标识时仍退回对端 IP（无标识的端点不能被一起限流成一把桶）。"""
    request = _get_request("/v2/memory/l3/background-status")

    client_id = await RateLimitMiddleware._resolve_client_id(request)

    assert client_id == "10.0.0.9"


# ── 6. L1 缓存窗口按回合时间保留，不是按写入顺序 ────────────────────────


def _journal_entry(round_id: str, moment: datetime, round_index: int | None = None) -> JournalEntry:
    return JournalEntry(
        journal_id=f"journal-{round_id}",
        round_fingerprint=f"fp-{round_id}",
        user_id="user-1",
        memory_scope_id="session-1",
        session_id="session-1",
        round_id=round_id,
        messages=[{"role": "user", "content": f"content-{round_id}"}],
        source_timestamp=moment,
        round_index=round_index,
    )


def test_rounds_with_the_same_timestamp_are_ordered_by_round_index() -> None:
    """时间相同的回合按 ``round_index`` 排（会话内序号）。

    同一轮里产生的多个回合时间戳可能完全相同，这时序号是唯一的次序依据。
    排错的话窗口里的轮次顺序与实际会话顺序不一致。
    """
    repository = InMemoryMemoryRepository()
    moment = datetime.now(UTC)

    repository.update_l1(_journal_entry("second", moment, round_index=2), limit=10)
    repository.update_l1(_journal_entry("first", moment, round_index=1), limit=10)
    repository.update_l1(_journal_entry("no-index", moment, round_index=None), limit=10)

    window = repository.get_l1("user-1", "session-1", "session-1")

    assert [entry.round_id for entry in window] == ["no-index", "first", "second"]


def test_a_late_arriving_old_round_does_not_evict_a_newer_one() -> None:
    """晚到的旧轮次（重试 / 重放 / 导入）不该把窗口里最新的那条挤掉。

    按写入顺序排的话，一条 30 天前的旧轮次会留在窗口里，而 2 小时前的那条
    被淘汰 —— 窗口名不副实，且 L1 条目到模型手里时不是时间顺序。
    """
    repository = InMemoryMemoryRepository()
    now = datetime.now(UTC)
    for index in range(10):
        repository.update_l1(
            _journal_entry(f"t-{index}", now - timedelta(minutes=10 - index)), limit=10
        )

    repository.update_l1(_journal_entry("late-30d", now - timedelta(days=30)), limit=10)

    window = repository.get_l1("user-1", "session-1", "session-1")
    round_ids = [entry.round_id for entry in window]
    assert "late-30d" not in round_ids, "30 天前的旧轮次不该挤掉窗口里更新的轮次"
    assert "t-0" in round_ids, "最新的那条轮次必须在窗口里"


def test_the_l1_cache_window_holds_exactly_the_limit() -> None:
    """窗口保留最近 N 条 —— 超过上限的旧轮次要真的被淘汰。

    排序改成按时间之后，截断仍然必须按 limit 生效：否则窗口会无界增长。
    """
    repository = InMemoryMemoryRepository()
    now = datetime.now(UTC)

    for index in range(5):
        repository.update_l1(
            _journal_entry(f"t-{index}", now - timedelta(minutes=5 - index)), limit=3
        )

    window = repository.get_l1("user-1", "session-1", "session-1")
    assert len(window) == 3, f"窗口必须恰好保留 limit 条，实际 {len(window)}"
    assert [entry.round_id for entry in window] == ["t-2", "t-3", "t-4"]


def test_read_through_fills_the_cache_when_it_is_enabled() -> None:
    """缓存开着时，读穿透要把回源结果**灌进缓存**并从缓存读回。

    灌完不读回（只灌不取）的话，下一次读还是会回源 —— 缓存形同虚设，
    而 L1 读路径的延迟全压在 PG 上。
    """
    repository = InMemoryMemoryRepository()
    repository.l1_cache_enabled = True
    now = datetime.now(UTC)
    repository.save_round(
        AppendMemoryRequest(
            request_id="req-1",
            user_id="user-1",
            session_id="session-1",
            round_id="r-1",
            messages=[
                {
                    "message_id": "m-1",
                    "role": MessageRole.USER,
                    "content": "我养了一只猫叫麻薯",
                    "timestamp": now,
                }
            ],
            source_timestamp=now,
            namespace="thinkback",
        )
    )
    # 清掉缓存，强制走读穿透
    repository.clear_l1("user-1", "session-1", namespace="thinkback")

    first = repository.get_l1("user-1", "session-1", "session-1")

    assert first, "读穿透必须返回回源的轮次"
    # 读穿透之后缓存被填上了：直接摸缓存键（非空即已填）。
    assert repository.l1_cache, "读穿透必须把回源结果灌进缓存"


# ── 7. 空过滤参数不能被当成「没传」 ────────────────────────────────────


def test_an_empty_user_id_filter_is_not_treated_as_absent() -> None:
    """``user_id=""`` 是「传了一个空租户」，不是「没传」。

    SQL 侧原先用真值判（``if user_id:``），空串会**丢掉整个租户过滤** ——
    查询返回全部租户的记忆。内存侧用 ``is None`` 判，两者分叉，且分叉的
    方向是「少一道隔离」。
    """
    repository = InMemoryMemoryRepository()
    for suffix in ("a", "b"):
        repository.add_memory_index(
            backend_memory_id=f"b-{suffix}",
            user_id=f"tenant-{suffix}",
            memory_scope_id="thinkback",
            source_refs=[],
            memory_text="x",
        )

    rows = repository.list_expired_memories(
        before=datetime.now(UTC) + timedelta(days=1), limit=10, user_id=""
    )

    assert rows == [], "空 user_id 不能被当成「不过滤」"


# ── 8. 到期列表排序不能混排 naive 与 aware ─────────────────────────────


def test_listing_expired_memories_handles_mixed_naive_and_aware_timestamps() -> None:
    """asyncpg 可能返回 naive datetime，过滤时归一了、排序时没归一。

    混合的 naive 与 aware 直接比较会抛 TypeError —— 保留期作业在真实 PG 上
    会整段失败。
    """
    repository = InMemoryMemoryRepository()
    repository.memories["a"] = MemoryIndexEntry(
        memory_id="a",
        backend_memory_id="b-a",
        user_id="user-1",
        memory_scope_id="thinkback",
        source_refs=[],
        memory_text="x",
        memory_status=MemoryStatus.ACTIVE,
        expires_at=datetime(2020, 1, 1),  # naive
        metadata={},
    )
    repository.memories["b"] = MemoryIndexEntry(
        memory_id="b",
        backend_memory_id="b-b",
        user_id="user-1",
        memory_scope_id="thinkback",
        source_refs=[],
        memory_text="x",
        memory_status=MemoryStatus.ACTIVE,
        expires_at=datetime(2021, 1, 1, tzinfo=UTC),  # aware
        metadata={},
    )

    rows = repository.list_expired_memories(before=datetime.now(UTC), limit=10)

    assert {row.memory_id for row in rows} == {"a", "b"}


# ── 交叉验证：HTTP 面仍能正常起 ─────────────────────────────────────────


def test_the_http_surface_still_serves_after_the_audit_fixes() -> None:
    """改动落在限流中间件与仓储上，用一条真实请求确认面没坏。"""
    app = create_app(run_startup_tasks=False)
    with TestClient(app) as client:
        response = client.get("/v2/memory/types", params={"namespace": "thinkback"})

    assert response.status_code == 200


# ── 源文本交叉校验（防捏造闸门）────────────────────────────────────────────


def test_a_source_that_names_a_pet_of_the_same_kind_supports_the_memory() -> None:
    """源文本自己也说了一只猫时，记忆里那条猫就算有来源 —— 即使名字不同。

    闸门的判定有两条路：① 源文本自己抽出了一只同种类的宠物 → 直接支持；
    ② 否则按 marker + 名字逐项核对。① 是短路，去掉它（或把源文本读错）
    的话，「猫叫小白」这种源文本会让「User has a cat named 麻薯」被拒 ——
    记忆明明有来源却被当捏造丢掉。
    """
    from thinkback.domain.slots.support import memory_supported_by_source

    assert memory_supported_by_source("User has a cat named 麻薯", {"source_text": "猫叫小白"}), (
        "源文本已说了一只猫，记忆里的猫就该算有来源"
    )


def test_a_source_that_never_mentions_the_pet_kind_rejects_the_memory() -> None:
    """源文本根本没提那种宠物时，记忆必须被拒（防捏造）。

    上一条钉的是「该放行时放行」，这一条钉的是「该拒绝时拒绝」——
    否则把闸门改成恒放行也能让上一条变绿。
    """
    from thinkback.domain.slots.support import memory_supported_by_source

    assert not memory_supported_by_source(
        "User has a cat named 麻薯", {"source_text": "今天天气不错"}
    )


def test_a_source_with_the_kind_but_no_matching_name_rejects_the_memory() -> None:
    """源文本提了猫，但记忆说的名字在源文本里找不到 → 拒。

    走的是 marker + 名字那条路（源文本没抽出完整的宠物名）：
    marker 过了，但 ``name in source_text`` 不成立。
    """
    from thinkback.domain.slots.support import memory_supported_by_source

    assert not memory_supported_by_source(
        "User has a cat named 麻薯", {"source_text": "我养了一只猫"}
    )
