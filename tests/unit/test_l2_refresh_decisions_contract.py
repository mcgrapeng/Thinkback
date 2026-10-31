"""``memory/l2_refresh.py`` 的**精确值**契约（去抖 / trailing / 失败语义）。

L2 刷新器决定「什么时候再跑一次 LLM 综合」。三个方向写错，后果各不相同：

- 去抖阈值写成 ``>`` → 每 N+1 轮才更新一次摘要，用户以为系统没记住；
- 提交后不清零计数 → 每次 append 都刷一次，LLM 账单爆炸；
- 失败不算「已尝试」 → LLM 挂掉期间每个 append 都重试一次，故障被放大成
  调用风暴 —— 与本模块「无重试风暴」的降级语义正好相反。

所以这里的用例都断言**精确值**（提交次数、计数、缓存顺序、命名空间），
而不是「刷过了没有」。
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

import pytest

from thinkback.memory.backends import FakeMemoryBackend
from thinkback.memory.l2_refresh import (
    L2BackgroundRefresher,
    build_default_composer_with,
)
from thinkback.memory.repositories import InMemoryMemoryRepository
from thinkback.memory.schemas import AppendMemoryRequest
from thinkback.memory.service import MemoryService

OTHER_NAMESPACE = "other"


class _RecordingComposer:
    """记录调用次数的 fake composer。"""

    def __init__(self, output: str = "【主题】测试摘要", *, fail: bool = False) -> None:
        self.output = output
        self.fail = fail
        self.calls: list[tuple[int, str | None]] = []
        self.thread_ids: list[int] = []

    def __call__(
        self, rounds: list[Any], previous_summary: str | None
    ) -> tuple[str, dict[str, str]]:
        self.calls.append((len(rounds), previous_summary))
        self.thread_ids.append(threading.get_ident())
        if self.fail:
            raise RuntimeError("simulated llm failure")
        return self.output, {"主题": self.output}


def _append(
    round_id: str, content: str, *, index: int = 1, namespace: str = "thinkback"
) -> AppendMemoryRequest:
    return AppendMemoryRequest(
        request_id=f"req-{round_id}",
        user_id="user-1",
        session_id="session-1",
        round_id=round_id,
        round_index=index,
        namespace=namespace,
        messages=[
            {
                "message_id": f"{round_id}-u",
                "role": "user",
                "content": content,
                "timestamp": "2026-09-07T10:00:00+00:00",
            },
            {
                "message_id": f"{round_id}-a",
                "role": "assistant",
                "content": "好的。",
                "timestamp": "2026-09-07T10:00:01+00:00",
            },
        ],
        source_timestamp=datetime(2026, 9, 7, 10, 0, 1, tzinfo=UTC),
    )


def _refresher(
    *,
    composer: _RecordingComposer,
    repository: InMemoryMemoryRepository | None = None,
    interval: int = 5,
    max_tracked_scopes: int = 8,
    reserve_capacity: Any = None,
    release_capacity: Any = None,
    on_refreshed: Any = None,
) -> L2BackgroundRefresher:
    return L2BackgroundRefresher(
        repository=repository or InMemoryMemoryRepository(),
        composer=composer,
        executor=ThreadPoolExecutor(max_workers=1),
        refresh_interval_rounds=interval,
        max_tracked_scopes=max_tracked_scopes,
        reserve_capacity=reserve_capacity,
        release_capacity=release_capacity,
        on_refreshed=on_refreshed,
    )


def _settle(refresher: L2BackgroundRefresher, *, budget: float = 0.5) -> None:
    """等在飞的刷新跑完。

    **两个预算都给得很短**，而且是有理由的短：

    - drain 的预算 0.2 秒：有些变异体让 drain 永远不静默，
      而它空转时一直占着 GIL —— 整轮测试会被拖到超时（mutmut 把那种情况判成
      timeout 而不是 killed，盲区反而更大）。短预算让这类变异体**快速失败**，
      而不是把审计拖垮。
    - 轮询的预算 0.5 秒：**这一条是被 mutmut 的墙钟预算逼出来的**。
      正常情况下刷新几毫秒就完事（轮询不占 GIL）；但变异体让刷新不发生时，
      每个用到它的用例都要把这份预算等满 —— 本文件有二十多条用例，
      于是「变异体那一轮」的整份文件要跑几十秒，撞上 mutmut 的墙钟上限，
      变异体被记成 **timeout 而不是 killed**（又一次「挂死/太慢 → 免检」）。
      0.5 秒对正常路径有 50 倍余量，对变异体那一轮把整份文件压到十几秒。

    刷新本身用轮询等：正常情况下它几毫秒就完事，轮询不占 GIL。
    """
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        if refresher.pending_count == 0 and not refresher._pending_scopes:
            break
        time.sleep(0.01)
    refresher.drain(timeout=0.2)


# ---------------------------------------------------------------------------
# 构造参数的边界
# ---------------------------------------------------------------------------


def test_the_refresh_interval_must_be_at_least_one() -> None:
    """间隔为 0 是非法配置：它会让「每轮都刷新」变成默认行为。"""
    with pytest.raises(ValueError, match="refresh_interval_rounds must be >= 1"):
        L2BackgroundRefresher(
            repository=InMemoryMemoryRepository(),
            composer=_RecordingComposer(),
            executor=ThreadPoolExecutor(max_workers=1),
            refresh_interval_rounds=0,
        )


def test_a_refresh_interval_of_one_is_allowed() -> None:
    """间隔 1 是合法值（每轮都刷新）。"""
    refresher = _refresher(composer=_RecordingComposer(), interval=1)

    assert refresher.refresh_interval_rounds == 1


def test_the_default_refresh_interval_is_five_rounds() -> None:
    """默认间隔与默认 scope 上界就是这两个数 —— 改默认值等于改生产行为。

    这里**不**走 ``_refresher``（它总是显式传参），否则测的是调用方传的值，
    不是类自己的默认值。
    """
    refresher = L2BackgroundRefresher(
        repository=InMemoryMemoryRepository(),
        composer=_RecordingComposer(),
        executor=ThreadPoolExecutor(max_workers=1),
    )

    assert refresher.refresh_interval_rounds == 5
    assert refresher._max_tracked_scopes == 4096


def test_a_zero_scope_budget_still_keeps_one_scope() -> None:
    """上界是 0 时仍要留住 1 个 scope。

    全 0 → 每次 append 都把计数清掉 → 去抖永远不生效 → 每轮都刷 LLM。
    """
    refresher = _refresher(composer=_RecordingComposer(), max_tracked_scopes=0)

    for index in range(3):
        refresher.maybe_submit(f"user-{index}", "session-1")

    assert len(refresher._append_counters) == 1, "scope 上界被算成了 0"

    _settle(refresher)


# ---------------------------------------------------------------------------
# 去抖：阈值必须精确到「第 N 轮」
# ---------------------------------------------------------------------------


def test_the_debounce_threshold_is_exact() -> None:
    """攒够 N 轮才刷新；第 N-1 轮不刷新。

    阈值写成 ``>`` → 每 N+1 轮才更新一次摘要；计数写成 ``+2`` → 不到 N 轮
    就刷了。两个方向都让 LLM 调用量与摘要新鲜度对不上。
    """
    composer = _RecordingComposer()
    repository = InMemoryMemoryRepository()
    repository.save_round(_append("r1", "第一轮"))
    refresher = _refresher(composer=composer, repository=repository, interval=3)

    refresher.maybe_submit("user-1", "session-1")
    _settle(refresher)
    assert len(composer.calls) == 1, "首次出现就该刷新"

    for index in (2, 3):
        repository.save_round(_append(f"r{index}", f"第{index}轮", index=index))
        refresher.maybe_submit("user-1", "session-1")
    _settle(refresher)
    assert len(composer.calls) == 1, "还没攒够 3 轮就刷新了"

    repository.save_round(_append("r4", "第4轮", index=4))
    refresher.maybe_submit("user-1", "session-1")
    _settle(refresher)
    assert len(composer.calls) == 2, "攒够 3 轮必须刷新"


def test_one_append_after_a_refresh_does_not_trigger_another_refresh() -> None:
    """提交后计数清零：再来 1 轮（还没到 N 轮）不刷新。

    计数不清零 → 提交那一轮本身也计数 → 下一轮立刻又刷一次。
    """
    composer = _RecordingComposer()
    repository = InMemoryMemoryRepository()
    repository.save_round(_append("r1", "第一轮"))
    refresher = _refresher(composer=composer, repository=repository, interval=2)

    refresher.maybe_submit("user-1", "session-1")
    _settle(refresher)
    assert len(composer.calls) == 1

    repository.save_round(_append("r2", "第二轮", index=2))
    refresher.maybe_submit("user-1", "session-1")
    _settle(refresher)

    assert len(composer.calls) == 1, "一次 append 就又刷了一次：提交后计数没清零"


def test_a_failed_refresh_still_counts_as_attempted() -> None:
    """失败也算「已尝试」：LLM 挂掉时不能每个 append 都重试。

    否则「还没刷过」的条件恒成立，故障被放大成 LLM 调用风暴 ——
    与本模块「无重试风暴」的降级语义正好相反。
    """
    composer = _RecordingComposer(fail=True)
    repository = InMemoryMemoryRepository()
    repository.save_round(_append("r1", "第一轮"))
    refresher = _refresher(composer=composer, repository=repository, interval=5)

    refresher.maybe_submit("user-1", "session-1")
    _settle(refresher)
    assert len(composer.calls) == 1

    for _ in range(4):
        refresher.maybe_submit("user-1", "session-1")
    _settle(refresher)

    assert len(composer.calls) == 1, "失败后每个 append 都重试了：故障被放大成调用风暴"


# ---------------------------------------------------------------------------
# 容量闸门：申请槽位的方式与延后语义
# ---------------------------------------------------------------------------


def test_the_capacity_gate_is_asked_for_a_zero_wait() -> None:
    """容量闸门必须以「不等待」的方式申请槽位。

    append 请求线程里等几秒 = 慢请求；L2 刷新拿不到槽位就延后。
    """
    waits: list[int] = []
    refresher = _refresher(
        composer=_RecordingComposer(),
        reserve_capacity=waits.append,
        release_capacity=lambda: None,
    )

    refresher.maybe_submit("user-1", "session-1")

    assert waits == [0], f"容量闸门的等待时长不对：{waits}"

    _settle(refresher)


def test_a_scope_deferred_by_the_capacity_gate_is_retried_on_the_next_append() -> None:
    """队列满时延后，但**不算已尝试**：下一轮 append 还要再试。

    算成已尝试 → 队列恢复后这个 scope 再也刷不出摘要，只能等下一个会话。
    """
    attempts: list[int] = []

    def _reserve(wait: int) -> None:
        attempts.append(wait)
        if len(attempts) == 1:
            raise RuntimeError("l3 background queue full")

    refresher = _refresher(
        composer=_RecordingComposer(),
        reserve_capacity=_reserve,
        release_capacity=lambda: None,
    )

    refresher.maybe_submit("user-1", "session-1")
    assert len(attempts) == 1

    refresher.maybe_submit("user-1", "session-1")
    assert len(attempts) == 2, "容量闸门挡过一次后就不再尝试了"

    _settle(refresher)


# ---------------------------------------------------------------------------
# 进程内 scope 状态：LRU 顺序与上界
# ---------------------------------------------------------------------------


def test_the_refreshed_scope_cache_keeps_the_most_recently_refreshed_scope() -> None:
    """``_llm_refreshed_scopes`` 是 LRU：刚刷过的 scope 不能先被淘汰。

    淘汰错对象 → 被淘汰的 scope 下次 append 又触发一次「首次刷新」，
    LLM 调用量翻倍。
    """
    refresher = _refresher(composer=_RecordingComposer(), max_tracked_scopes=2)

    refresher._remember_scope(("user-1", "thinkback", "s1"))
    refresher._remember_scope(("user-2", "thinkback", "s2"))
    refresher._remember_scope(("user-1", "thinkback", "s1"))  # 刷新 user-1 → 它变最新
    refresher._remember_scope(("user-3", "thinkback", "s3"))  # 超上界 → 淘汰最旧的

    assert ("user-1", "thinkback", "s1") in refresher._llm_refreshed_scopes
    assert ("user-2", "thinkback", "s2") not in refresher._llm_refreshed_scopes


def test_the_append_counter_cache_keeps_the_most_recently_used_scope() -> None:
    """``_append_counters`` 也是 LRU：刚用过的 scope 不能先被淘汰。

    淘汰错对象 → 那个 scope 的去抖计数被清零 → 下次 append 立刻触发刷新。
    """
    refresher = _refresher(composer=_RecordingComposer(), max_tracked_scopes=2)

    refresher._append_counters[("user-1", "thinkback", "s1")] = 1
    refresher._append_counters[("user-2", "thinkback", "s2")] = 1
    refresher._remember_scope(("user-1", "thinkback", "s1"))
    refresher._append_counters[("user-3", "thinkback", "s3")] = 1
    refresher._trim_scopes()

    assert ("user-1", "thinkback", "s1") in refresher._append_counters
    assert ("user-2", "thinkback", "s2") not in refresher._append_counters


def test_the_scope_cache_evicts_the_least_recently_used_scope() -> None:
    """超上界时淘汰**最久未用**的那个，不是最新的那个。

    淘汰最新的 → 刚刷过的 scope 被当成「还没刷过」→ 立刻再刷一次。
    """
    refresher = _refresher(composer=_RecordingComposer(), max_tracked_scopes=2)

    refresher._llm_refreshed_scopes[("user-1", "thinkback", "s1")] = None
    refresher._llm_refreshed_scopes[("user-2", "thinkback", "s2")] = None
    refresher._llm_refreshed_scopes[("user-3", "thinkback", "s3")] = None
    refresher._trim_scopes()

    assert ("user-1", "thinkback", "s1") not in refresher._llm_refreshed_scopes
    assert ("user-3", "thinkback", "s3") in refresher._llm_refreshed_scopes


# ---------------------------------------------------------------------------
# 刷新本身：轮次、命名空间、降级
# ---------------------------------------------------------------------------


def test_a_refresh_with_no_active_rounds_writes_no_summary() -> None:
    """没有轮次 → 本次刷新放弃，不写摘要。

    写成空摘要会把已有摘要清掉 —— 用户看到「记忆被抹掉」。
    """
    composer = _RecordingComposer()
    repository = InMemoryMemoryRepository()
    refresher = _refresher(composer=composer, repository=repository)

    refresher.maybe_submit("user-1", "session-1")
    _settle(refresher)

    assert repository.get_summary("user-1", "session-1") is None, "没有轮次却写了摘要"


def test_an_empty_summary_from_the_composer_keeps_the_concat_fallback() -> None:
    """composer 返回空白 → 本次刷新放弃，摘要保持拼接版。

    空白摘要会把上一版摘要清空 —— 用户看到「记忆被抹掉」。
    """
    composer = _RecordingComposer("   ")
    repository = InMemoryMemoryRepository()
    repository.save_round(_append("r1", "第一轮"))
    repository.upsert_summary_from_rounds(
        "user-1",
        "session-1",
        repository.list_rounds("user-1", "session-1"),
        summary_text="拼接版",
        summary_kind="concat",
    )
    refresher = _refresher(composer=composer, repository=repository)

    refresher.maybe_submit("user-1", "session-1")
    _settle(refresher)

    summary = repository.get_summary("user-1", "session-1")
    assert summary is not None
    assert summary.summary_kind == "concat", "空白摘要覆盖了拼接版"
    assert summary.summary_text == "拼接版"


def test_the_refresh_reads_and_writes_under_the_given_namespace() -> None:
    """命名空间必须贯穿「读轮次」与「写摘要」。

    同一 session 在不同 namespace 下是两份记忆：读错 namespace → 拿不到
    本会话的轮次；写错 → 摘要落进别人的命名空间。
    """
    composer = _RecordingComposer()
    repository = InMemoryMemoryRepository()
    repository.save_round(_append("r1", "第一轮", namespace=OTHER_NAMESPACE))
    refresher = _refresher(composer=composer, repository=repository)

    refresher.maybe_submit("user-1", "session-1", namespace=OTHER_NAMESPACE)
    _settle(refresher)

    summary = repository.get_summary("user-1", "session-1", namespace=OTHER_NAMESPACE)
    assert summary is not None, "摘要没写到指定的命名空间"
    assert summary.summary_kind == "llm"


def test_every_repository_call_carries_the_target_namespace() -> None:
    """命名空间必须贯穿**每一次**仓储调用，不只是读轮次与写摘要。

    同一 user+session 在两个命名空间下各有一份摘要。少传一次 namespace，
    读回来的就是**另一个命名空间**的东西：

    - 乐观锁基准读错 → 版本对不上 → 本次刷新被当成「别人改过」而丢弃；
    - 上一版摘要读错 → 拿另一个命名空间的历史去综合，摘要内容串味。

    两种写错都不报错，只是「静默刷不出来 / 刷成别的样子」。
    """
    composer = _RecordingComposer()
    repository = InMemoryMemoryRepository()

    default_round = repository.save_round(_append("r0", "默认命名空间那一轮"))
    other_round = repository.save_round(_append("r1", "第一轮", namespace=OTHER_NAMESPACE))

    # 默认命名空间：一份旧摘要（版本 0）
    repository.upsert_summary_from_rounds(
        "user-1",
        "session-1",
        [default_round],
        summary_text="默认命名空间的旧摘要",
        summary_kind="llm",
    )
    # 目标命名空间：另一份旧摘要（写两次 → 版本 1，与默认命名空间不同）
    for _ in range(2):
        repository.upsert_summary_from_rounds(
            "user-1",
            "session-1",
            [other_round],
            summary_text="另一个命名空间的旧摘要",
            summary_kind="llm",
            namespace=OTHER_NAMESPACE,
        )

    refresher = _refresher(composer=composer, repository=repository)
    refresher.maybe_submit("user-1", "session-1", namespace=OTHER_NAMESPACE)
    _settle(refresher)

    assert [previous for _count, previous in composer.calls] == ["另一个命名空间的旧摘要"], (
        "喂回 composer 的上一版摘要取错了命名空间"
    )

    summary = repository.get_summary("user-1", "session-1", namespace=OTHER_NAMESPACE)
    assert summary is not None
    assert summary.summary_text == "【主题】测试摘要", (
        "乐观锁基准取错了命名空间 → 刷新被当成「别人改过」而丢弃"
    )


# ---------------------------------------------------------------------------
# composer 适配层：识别不到段落时用原文
# ---------------------------------------------------------------------------


def test_the_default_composer_uses_the_raw_text_when_no_section_is_recognized() -> None:
    """LLM 没给任何【】标记 → 用原文当摘要文本。

    段落识别不到就整段丢弃 = 这一轮刷新白跑。
    """
    compose = build_default_composer_with(lambda _system, _user: "  这是一段没有标记的摘要  ")

    text, sections = compose([], None)

    assert sections == {}
    assert text == "这是一段没有标记的摘要"


def test_the_default_composer_renders_the_recognized_sections() -> None:
    """识别到段落 → 文本视图由段落拼成（而不是原文）。"""
    compose = build_default_composer_with(lambda _system, _user: "【主题】猫\n【行为偏好】简洁")

    text, sections = compose([], None)

    assert sections == {"主题": "猫", "行为偏好": "简洁"}
    assert text == "【主题】猫\n【行为偏好】简洁"


def test_the_refresh_runs_off_the_append_thread() -> None:
    """刷新在后台线程里跑：append 调用本身不能被 LLM 拖住。

    这条钉的是「提交后立刻返回」这个契约 —— 提交动作在调用线程里同步完成，
    但刷新本体是后台线程。
    """
    composer = _RecordingComposer()
    repository = InMemoryMemoryRepository()
    repository.save_round(_append("r1", "第一轮"))
    refresher = _refresher(composer=composer, repository=repository)

    refresher.maybe_submit("user-1", "session-1")
    _settle(refresher)

    assert composer.thread_ids, "刷新没有跑起来"
    assert composer.thread_ids[0] != threading.get_ident(), "刷新在 append 调用线程里同步跑了"


# ---------------------------------------------------------------------------
# drain：在飞刷新与预算
# ---------------------------------------------------------------------------


def test_drain_respects_its_budget_even_when_a_refresh_is_still_running() -> None:
    """drain 必须按自己的预算返回，不能被在飞的刷新拖住。

    关停时 drain 是最后一道闸：它超预算 = 关停流程被一次慢 LLM 拖垮。
    守护线程 + 用例自带 5 秒上限：预算失效的实现会让 drain 一直等下去 ——
    那时用例要**失败**，而不是把整轮审计挂死。
    """
    started = threading.Event()
    release = threading.Event()

    def _blocking_composer(rounds: list[Any], previous: str | None) -> tuple[str, dict[str, str]]:
        started.set()
        if not release.wait(timeout=2):
            raise RuntimeError("test release timeout")
        return "【主题】x", {"主题": "x"}

    repository = InMemoryMemoryRepository()
    repository.save_round(_append("r1", "第一轮"))
    refresher = L2BackgroundRefresher(
        repository=repository,
        composer=_blocking_composer,
        executor=ThreadPoolExecutor(max_workers=1),
        refresh_interval_rounds=5,
    )
    refresher.maybe_submit("user-1", "session-1")
    assert started.wait(timeout=2), "刷新没有在飞"

    returned = threading.Event()

    def _drain() -> None:
        refresher.drain(timeout=0.2)
        returned.set()

    began = time.monotonic()
    threading.Thread(target=_drain, daemon=True).start()
    try:
        assert returned.wait(timeout=0.6), "drain 超过了它自己的预算"
        assert time.monotonic() - began >= 0.05, "drain 提前放弃了预算"
    finally:
        release.set()


def test_drain_without_a_budget_still_works() -> None:
    """``drain()`` 不给预算时也要能用 —— 关停路径走的就是这条。

    预算算不出来（timeout 是 None）时不能抛异常：抛了就是关停直接失败，
    比「等久一点」糟得多。

    **drain 放在守护线程里跑**：没有预算的 drain 一旦碰上「退不出去」的状态
    就会**一直空转**（每个变异体都可能把它变成那样）。放在主线程里 = 用例
    永远挂着 → mutmut 判成 timeout 而不是 killed（挂死型变异体免检）；
    放在守护线程里 = 主线程照常收尾，用例**失败**（= 变异体被杀）。
    """
    refresher = _refresher(composer=_RecordingComposer())
    returned = threading.Event()

    def _drain() -> None:
        refresher.drain()
        returned.set()

    began = time.monotonic()
    threading.Thread(target=_drain, daemon=True).start()
    assert returned.wait(timeout=5), "无预算的 drain 没有立刻返回"
    assert time.monotonic() - began < 1.0, "无预算的 drain 没有立刻返回"


def test_drain_tolerates_a_pending_scope_without_an_in_flight_future() -> None:
    """「有 pending scope、没有在飞 future」是刷新器真实存在的瞬间状态。

    future 跑完、完成回调还没跑的那个窗口里就是这个状态（drain 里那 5 毫秒
    的让步就是为它准备的）。drain 必须收得住它 —— 在预算内返回，而不是
    抛异常把关停流程打断。
    """
    refresher = _refresher(composer=_RecordingComposer())
    refresher._pending_scopes.add(("user-1", "thinkback", "session-1"))
    try:
        began = time.monotonic()
        refresher.drain(timeout=0.05)
        assert time.monotonic() - began < 1.0, "drain 在那个窗口里没有收住"
    finally:
        refresher._pending_scopes.discard(("user-1", "thinkback", "session-1"))


def test_the_finishing_counter_is_back_to_zero_after_the_drain() -> None:
    """在飞回调的计数必须归零。

    归零不了 → drain 永远认为「还没静默」→ 关停流程永远等下去。
    """
    composer = _RecordingComposer()
    repository = InMemoryMemoryRepository()
    repository.save_round(_append("r1", "第一轮"))
    refresher = _refresher(composer=composer, repository=repository)

    refresher.maybe_submit("user-1", "session-1")
    _settle(refresher)

    assert refresher._finishing == 0, "在飞回调计数没归零：drain 会一直以为还没静默"


# ---------------------------------------------------------------------------
# 刷新成功之后：状态、回调、段落
# ---------------------------------------------------------------------------


def test_a_successful_refresh_marks_the_scope_as_refreshed() -> None:
    """刷成功之后，该作用域要记成「已有 llm 摘要」。

    记不上 → 下一次 append 又触发一次「首次刷新」→ 每轮都刷一次 LLM。
    """
    composer = _RecordingComposer()
    repository = InMemoryMemoryRepository()
    repository.save_round(_append("r1", "第一轮"))
    refresher = _refresher(composer=composer, repository=repository)

    refresher.maybe_submit("user-1", "session-1")
    _settle(refresher)

    assert ("user-1", "thinkback", "session-1") in refresher._llm_refreshed_scopes


def test_the_refresh_callback_receives_the_user_and_the_scope() -> None:
    """刷新成功后要回调服务层，参数就是那个用户与作用域。

    回调是服务层失效摘要缓存的钩子；参数错了 → 缓存没被失效 → 召回读到旧摘要。
    """
    seen: list[tuple[str, str]] = []
    composer = _RecordingComposer()
    repository = InMemoryMemoryRepository()
    repository.save_round(_append("r1", "第一轮"))
    refresher = _refresher(
        composer=composer,
        repository=repository,
        on_refreshed=lambda user_id, scope_id: seen.append((user_id, scope_id)),
    )

    refresher.maybe_submit("user-1", "session-1")
    _settle(refresher)

    assert seen == [("user-1", "session-1")]


def test_the_structured_sections_are_stored_with_the_summary() -> None:
    """结构化画像要和摘要一起落库。

    不落 → 下游拿不到那 4 段，只能拿到一坨文本（G4 的机器消费链路断掉）。
    """
    composer = _RecordingComposer("【主题】猫")
    repository = InMemoryMemoryRepository()
    repository.save_round(_append("r1", "第一轮"))
    refresher = _refresher(composer=composer, repository=repository)

    refresher.maybe_submit("user-1", "session-1")
    _settle(refresher)

    summary = repository.get_summary("user-1", "session-1")
    assert summary is not None
    assert summary.structured_sections == {"主题": "【主题】猫"}


def test_the_previous_llm_summary_is_fed_back_to_the_composer() -> None:
    """上一版 **llm** 摘要要喂回 composer 做增量综合。

    不喂 → 每次刷新都从零开始，前面几轮说过的话被综合丢掉。
    """
    composer = _RecordingComposer()
    repository = InMemoryMemoryRepository()
    repository.save_round(_append("r1", "第一轮"))
    refresher = _refresher(composer=composer, repository=repository, interval=1)

    refresher.maybe_submit("user-1", "session-1")
    _settle(refresher)
    assert composer.calls[0][1] is None, "首次不该有上一版"

    repository.save_round(_append("r2", "第二轮", index=2))
    refresher.maybe_submit("user-1", "session-1")
    _settle(refresher)

    assert len(composer.calls) == 2
    assert composer.calls[1][1] == "【主题】测试摘要", "上一版 llm 摘要没有喂回去"


# ---------------------------------------------------------------------------
# 并发：删除与并发写
# ---------------------------------------------------------------------------


def test_a_concurrent_write_to_the_summary_is_not_overwritten() -> None:
    """刷新期间有人改了摘要行 → 旧快照不得覆盖它（乐观锁）。

    版本号被绕过 → 别人的内容被旧快照覆盖；用户刚说过的话被抹掉。
    """

    class _ConcurrentWriter(InMemoryMemoryRepository):
        def upsert_summary_from_rounds(
            self, user_id: str, memory_scope_id: str, rounds: list[Any], **kwargs: Any
        ) -> Any:
            existing = self.get_summary(
                user_id, memory_scope_id, namespace=kwargs.get("namespace", "thinkback")
            )
            if existing is not None:
                # 另一写者先提交：版本 +1、内容被换掉。
                existing.row_version += 1
                existing.summary_text = "别人写的摘要"
            return super().upsert_summary_from_rounds(user_id, memory_scope_id, rounds, **kwargs)

    composer = _RecordingComposer()
    repository = _ConcurrentWriter()
    repository.save_round(_append("r1", "第一轮"))
    refresher = _refresher(composer=composer, repository=repository, interval=1)

    refresher.maybe_submit("user-1", "session-1")
    _settle(refresher)

    repository.save_round(_append("r2", "第二轮", index=2))
    refresher.maybe_submit("user-1", "session-1")
    _settle(refresher)

    summary = repository.get_summary("user-1", "session-1")
    assert summary is not None
    assert summary.summary_text == "别人写的摘要", "旧快照覆盖了别人的写入"


def test_a_long_term_memory_deleted_during_the_refresh_stops_the_summary() -> None:
    """刷新期间被删除的 L3 记忆，其来源轮次不得进摘要。

    排除集是**写之前**再查一次的：composer 跑的那几秒里用户删掉了一条
    长期记忆，那条记忆的原文就不能进摘要 —— 否则删除被撤销了。
    """
    deleted = threading.Event()
    texts: list[str] = []

    def _composer(rounds: list[Any], previous: str | None) -> tuple[str, dict[str, str]]:
        if not deleted.is_set():
            deleted.set()
            for memory in repository.list_memories("user-1", "thinkback"):
                repository.mark_memory_deleted(memory.memory_id)
        text = "【主题】RESURRECTED-MARKER"
        texts.append(text)
        return text, {"主题": text}

    repository = InMemoryMemoryRepository()
    service = MemoryService(
        repository=repository,
        backend=FakeMemoryBackend(),
        l3_write_mode="sync",
        l2_summary_composer=_composer,
        l2_refresh_interval_rounds=1,
    )
    service.append(_append("r1", "我养了一只猫叫麻薯"))
    service.drain_l3_background_tasks(timeout=0.5)

    summary = repository.get_summary("user-1", "session-1")
    assert summary is not None
    assert "RESURRECTED-MARKER" not in summary.summary_text, "已删除的来源被写回了摘要"


# ---------------------------------------------------------------------------
# composer 适配层：上一版进 prompt、文本视图由段落重拼
# ---------------------------------------------------------------------------


def test_the_previous_summary_is_included_in_the_user_prompt() -> None:
    """上一版摘要要进 user prompt —— 增量综合靠它。"""
    seen: list[tuple[str, str]] = []

    def _complete(system: str, user: str) -> str:
        seen.append((system, user))
        return "【主题】x"

    compose = build_default_composer_with(_complete)
    compose([], "上一版摘要内容")

    assert seen, "complete 没被调用"
    assert "上一版摘要内容" in seen[0][1], "上一版摘要没进 user prompt"


def test_the_composer_text_view_is_rebuilt_from_the_sections() -> None:
    """文本视图由识别到的段落重拼，不是原文照抄。

    原文照抄 → 噪声（前言、重复、无效段落）全进了摘要正文。
    """
    compose = build_default_composer_with(lambda _s, _u: "前言噪声\n【主题】猫\n【行为偏好】无")

    text, sections = compose([], None)

    assert sections == {"主题": "猫", "行为偏好": "无"}
    assert text == "【主题】猫", "文本视图应该由段落重拼，而不是原文"


# ---------------------------------------------------------------------------
# 进程内状态：已尝试缓存的 LRU 顺序
# ---------------------------------------------------------------------------


def test_the_attempted_scope_cache_keeps_the_most_recently_used_scope() -> None:
    """``_llm_attempted_scopes`` 也是 LRU：刚提交过的 scope 不能先被淘汰。

    淘汰错对象 → 那个 scope 的「已尝试」标记没了 → 下次 append 又触发
    一次刷新（LLM 挂掉时这正是调用风暴的来源）。
    """
    composer = _RecordingComposer()
    refresher = _refresher(composer=composer, interval=1, max_tracked_scopes=2)

    refresher.maybe_submit("user-1", "session-1")
    _settle(refresher)
    refresher.maybe_submit("user-2", "session-2")
    _settle(refresher)
    refresher.maybe_submit("user-1", "session-1")  # 再提交 → 它变成最新
    _settle(refresher)
    refresher.maybe_submit("user-3", "session-3")  # 超上界 → 淘汰最旧的
    _settle(refresher)
    # 淘汰发生在下一次 submit 的开头（_trim_scopes 早于本次的登记），
    # 所以再来一次提交，把上界这步逼出来。
    refresher.maybe_submit("user-1", "session-1")
    _settle(refresher)

    assert ("user-1", "thinkback", "session-1") in refresher._llm_attempted_scopes
    assert ("user-2", "thinkback", "session-2") not in refresher._llm_attempted_scopes
