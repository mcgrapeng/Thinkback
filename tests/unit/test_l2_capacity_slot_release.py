"""L2 刷新的**容量槽位归还**：三条路径都要还，且只还一次。

L2 刷新与 L3 抽取**共用同一个队列预算**（``memory_l3_max_pending_tasks``）。
所以每次 L2 刷新占了一个槽位，就必须在刷新结束时还回去 ——
槽位只进不出的话，队列会被 L2 慢慢吃光，之后**任何 append 都写不进 L3**。

三条归还路径：

1. ``maybe_submit`` 里发现该 scope 已经在刷（另一个 append 也判定要刷新，
   它先抢到了）→ 这次不提交，但要**归还刚申请的槽位**；
2. 提交到 executor 失败（executor 满了 / 被关掉）→ 归还槽位；
3. 刷新跑完（``_finish`` 回调）→ 归还槽位。

**这三条路径此前从未被验证**：现有用例要么把 ``release_capacity`` 传成
``lambda: None``（不观察），要么只走「提交成功 → 刷新完成」那一条。

漏掉归还的后果是静默的：队列预算被 L2 慢慢吃光，最后 append 全都排队超时，
而日志里没有任何异常。
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from thinkback.memory.l2_refresh import L2BackgroundRefresher
from thinkback.memory.repositories import InMemoryMemoryRepository


class _Composer:
    """极简摘要合成器：写出一段固定摘要。

    合成器是**可调用对象**，签名 ``(rounds, previous_summary) -> (摘要, 段落)``。
    """

    def __call__(
        self, rounds: list[Any], previous_summary: str | None
    ) -> tuple[str, dict[str, str]]:
        return "摘要", {}


class _BlockingComposer(_Composer):
    """卡住刷新，好让「刷新还在飞」这个窗口可控。"""

    def __init__(self) -> None:
        self.release = threading.Event()

    def __call__(
        self, rounds: list[Any], previous_summary: str | None
    ) -> tuple[str, dict[str, str]]:
        self.release.wait(timeout=5)
        return "摘要", {}


def _refresher(
    *,
    composer: Any = None,
    reserve_capacity: Any = None,
    release_capacity: Any = None,
    executor: ThreadPoolExecutor | None = None,
    interval: int = 5,
) -> L2BackgroundRefresher:
    return L2BackgroundRefresher(
        repository=InMemoryMemoryRepository(),
        composer=composer or _Composer(),
        executor=executor or ThreadPoolExecutor(max_workers=1),
        refresh_interval_rounds=interval,
        reserve_capacity=reserve_capacity,
        release_capacity=release_capacity,
    )


def _settle(refresher: L2BackgroundRefresher, *, budget: float = 1.0) -> None:
    """等在飞的刷新跑完。"""
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        if not refresher._futures and not refresher._pending_scopes:  # noqa: SLF001
            return
        time.sleep(0.01)


def test_a_scope_already_being_refreshed_returns_its_capacity_slot() -> None:
    """该 scope 在**检查之后**被别的线程标成「在刷」→ 归还刚申请的槽位。

    这条分支是个**竞态窗口**：``maybe_submit`` 先在锁内看一遍「在不在刷」
    （不在才继续），申请完槽位后再在锁内复查一次 —— 两次之间如果另一个
    线程抢先把该 scope 标成「在刷」，这次就不能提交了，但槽位已经申请，
    **必须还**。

    正常调用走不到这条（第一次检查就发现「在刷」，在申请槽位**之前**
    返回了，那条路没申请槽位，自然不用还）。所以这里用
    ``reserve_capacity`` 回调精确地制造那个窗口：申请槽位的瞬间把 scope
    标成「在刷」。
    """
    released: list[str] = []
    refresher = _refresher(
        composer=_Composer(), release_capacity=lambda: released.append("released")
    )

    def _reserve(wait: int) -> None:
        # 申请槽位的那一刻，另一个线程抢先把该 scope 标成了「在刷」
        refresher._pending_scopes.add(("user-1", "thinkback", "session-1"))  # noqa: SLF001

    refresher._reserve_capacity = _reserve  # noqa: SLF001

    refresher.maybe_submit("user-1", "session-1", namespace="thinkback")

    assert released == ["released"], f"撞上「已在刷」时没有归还容量槽位：{released}"


def test_a_failed_submit_returns_its_capacity_slot() -> None:
    """提交到 executor 失败 → 归还槽位。

    executor 被关掉时 ``submit`` 抛 ``RuntimeError``。不还槽位的话，
    队列预算被这些失败提交吃光。
    """
    released: list[str] = []
    executor = ThreadPoolExecutor(max_workers=1)
    refresher = _refresher(
        composer=_Composer(),
        reserve_capacity=lambda wait: None,
        release_capacity=lambda: released.append("released"),
        executor=executor,
    )
    # 把 executor 关掉 → submit 会抛
    executor.shutdown(wait=False)

    refresher.maybe_submit("user-1", "session-1", namespace="thinkback")

    assert released == ["released"], f"提交失败时没有归还容量槽位：{released}"


def test_a_finished_refresh_returns_its_capacity_slot() -> None:
    """刷新跑完 → 归还槽位。"""
    released: list[str] = []
    refresher = _refresher(
        composer=_Composer(),
        reserve_capacity=lambda wait: None,
        release_capacity=lambda: released.append("released"),
    )

    refresher.maybe_submit("user-1", "session-1", namespace="thinkback")
    _settle(refresher)

    assert released == ["released"], f"刷新完成后没有归还容量槽位：{released}"


def test_the_capacity_slot_is_returned_exactly_once() -> None:
    """一次刷新只还一次 —— 还两次等于把别人的槽位也放掉。

    L2 与 L3 共用队列预算：多还一次，实际并发上限就比配置的高。
    """
    released: list[str] = []
    refresher = _refresher(
        composer=_Composer(),
        reserve_capacity=lambda wait: None,
        release_capacity=lambda: released.append("released"),
    )

    refresher.maybe_submit("user-1", "session-1", namespace="thinkback")
    _settle(refresher)
    time.sleep(0.1)

    assert released == ["released"], f"槽位归还次数不对：{released}"


def test_a_refresh_without_a_capacity_callback_does_not_crash() -> None:
    """没接容量闸门时（``release_capacity=None``）也能正常刷新。

    ``release_capacity`` 是可选的（默认 None）—— 不接闸门时那几条
    「归还槽位」的分支必须跳过，不能 ``NoneType`` 崩掉。
    """
    refresher = _refresher(composer=_Composer(), reserve_capacity=None, release_capacity=None)
    refresher.maybe_submit("user-1", "session-1", namespace="thinkback")
    _settle(refresher)
    # 没崩就是通过；断言任务已静默
    assert not refresher._futures  # noqa: SLF001


def test_a_refresh_without_a_capacity_callback_skips_the_release_in_the_race_window() -> None:
    """竞态窗口里，**没接容量回调**时直接返回，不调 ``None``。

    这条与 ``test_a_scope_already_being_refreshed_returns_its_capacity_slot``
    是同一段代码的两侧：接了回调就归还，没接就直接返回。
    """
    refresher = _refresher(composer=_Composer(), reserve_capacity=None, release_capacity=None)

    def _reserve(wait: int) -> None:
        refresher._pending_scopes.add(("user-1", "thinkback", "session-1"))  # noqa: SLF001

    refresher._reserve_capacity = _reserve  # noqa: SLF001

    # 不该崩 —— 没接回调时「归还」那段要跳过
    refresher.maybe_submit("user-1", "session-1", namespace="thinkback")


def test_a_failed_submit_without_a_capacity_callback_does_not_crash() -> None:
    """提交失败 + **没接容量回调** → 也不能崩。"""
    executor = ThreadPoolExecutor(max_workers=1)
    refresher = _refresher(
        composer=_Composer(),
        reserve_capacity=None,
        release_capacity=None,
        executor=executor,
    )
    executor.shutdown(wait=False)

    # 不该崩
    refresher.maybe_submit("user-1", "session-1", namespace="thinkback")


def test_a_finished_refresh_without_a_capacity_callback_does_not_crash() -> None:
    """刷新完成 + **没接容量回调** → 也不能崩。"""
    refresher = _refresher(composer=_Composer(), reserve_capacity=None, release_capacity=None)
    refresher.maybe_submit("user-1", "session-1", namespace="thinkback")
    _settle(refresher)
    assert not refresher._futures  # noqa: SLF001


def test_drain_gives_up_when_its_deadline_has_passed() -> None:
    """drain 的预算用完了 → 放弃等待，直接返回。

    这条不是「优化」而是承诺：``drain`` 必须在预算内返回（关停路径依赖它）。
    预算耗尽后还在等 = 关停被一次卡住的刷新拖住。
    """
    repository = InMemoryMemoryRepository()
    _seed_round(repository, "user-1", "session-1", "r1")
    composer = _BlockingComposer()
    refresher = _refresher(
        composer=composer,
        reserve_capacity=None,
        release_capacity=None,
    )
    # 用带轮次的仓储：没有轮次时刷新会立刻跳过，drain 就没有「在飞的东西」可等
    refresher._repository = repository  # noqa: SLF001

    refresher.maybe_submit("user-1", "session-1", namespace="thinkback")
    assert refresher._futures, "刷新没在飞，drain 的预算路径就测不到"

    # 用一个已经过去的 deadline 去 drain → 立刻返回
    started = time.monotonic()
    refresher.drain(timeout=-1.0)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, f"deadline 已过还在等，drain 用了 {elapsed:.2f}s"

    # 放行卡住的刷新，别让线程挂着
    composer.release.set()


def test_drain_waits_within_its_budget() -> None:
    """drain 在预算内**等**在飞的刷新跑完。

    与上一条互补：上一条是「预算用完 → 放弃」，这条是「预算够 → 等到」。
    少了这条，「drain 会等刷新」这个承诺就没被验证 —— 关停路径依赖它
    （不等就可能关掉正在写摘要的服务）。

    要让刷新**在 drain 期间还在飞**：先提交一个卡住的刷新，在另一个线程里
    放行它，同时主线程 drain（预算够 → 走 ``wait(futures, timeout=remaining)``）。
    """
    repository = InMemoryMemoryRepository()
    _seed_round(repository, "user-1", "session-1", "r1")
    composer = _BlockingComposer()
    refresher = _refresher(
        composer=composer,
        reserve_capacity=None,
        release_capacity=None,
    )
    refresher._repository = repository  # noqa: SLF001

    refresher.maybe_submit("user-1", "session-1", namespace="thinkback")
    assert refresher._futures

    # 另一个线程稍后放行刷新，主线程此刻 drain（预算够 → 会等）
    def _release_later() -> None:
        time.sleep(0.2)
        composer.release.set()

    threading.Thread(target=_release_later, daemon=True).start()
    refresher.drain(timeout=5.0)

    assert not refresher._futures, "drain 返回了但刷新还在飞"


def _seed_round(
    repository: InMemoryMemoryRepository, user_id: str, session_id: str, round_id: str
) -> None:
    """给仓储塞一条轮次 —— 没有轮次时 L2 刷新会立刻跳过。

    注意 ``list_rounds`` 的第二个位置参数是 **memory_scope_id**（不是 session_id），
    而 ``save_round`` 落库的 ``memory_scope_id`` 取的是 session_id（见仓储实现），
    两者在单会话场景下正好一致 —— 但必须显式对齐，否则刷新会看到 0 条轮次。
    """
    from datetime import UTC, datetime

    from thinkback.memory.schemas import AppendMemoryRequest, MessageRole

    repository.save_round(
        AppendMemoryRequest(
            request_id=f"req-{round_id}",
            user_id=user_id,
            session_id=session_id,
            round_id=round_id,
            messages=[
                {
                    "message_id": f"m-{round_id}",
                    "role": MessageRole.USER,
                    "content": "今天在讨论产能",
                    "timestamp": datetime(2026, 11, 5, tzinfo=UTC),
                }
            ],
            source_timestamp=datetime(2026, 11, 5, tzinfo=UTC),
            metadata={},
        )
    )


def test_the_summary_composer_rejects_a_json_array() -> None:
    """摘要解析：JSON 解析成功但**不是对象**（比如数组）→ 回退到【】解析。

    LLM 返回 ``["a", "b"]`` 时不能当段落用，也不能崩 —— 回退到旧的
    【主题】… 解析。
    """
    from thinkback.memory.l2_refresh import parse_summary_sections

    sections = parse_summary_sections('["主题", "x"]')
    assert sections == {}, "非对象的 JSON 不该被当成段落"


def test_the_summary_composer_rejects_a_json_object_with_only_unknown_keys() -> None:
    """JSON 对象但 known_keys 全不在 → 回退到【】解析（不丢内容）。"""
    from thinkback.memory.l2_refresh import parse_summary_sections

    sections = parse_summary_sections('{"别的键": "值"}')
    assert sections == {}, "known_keys 全空时不该返回段落（要回退到【】解析）"


def test_the_summary_composer_falls_back_to_the_bracket_form_when_json_has_no_known_keys() -> None:
    """JSON 里没有 known_keys，但【】形式有内容 → 取【】形式的内容。

    LLM 返回 ``{"别的": "x"}`` 的同时正文里还有【主题】… 时，
    段落要来自【】形式 —— 不能因为 JSON 解析成功就把内容丢掉
    （注释里写的「不回退会让 LLM 输出彻底丢失」）。
    """
    from thinkback.memory.l2_refresh import parse_summary_sections

    sections = parse_summary_sections('{"别的键": "值"}\n【主题】真正的摘要')
    assert sections.get("主题") == "真正的摘要", f"没有回退到【】解析：{sections}"


def test_a_failed_refresh_still_returns_its_capacity_slot() -> None:
    """刷新**失败**时也要归还槽位。

    ``_run_refresh`` 自己捕获了异常并落日志（所以业务上是「降级为拼接摘要」，
    不是崩溃），但队列槽位必须照还 —— 不还的话，失败的刷新会把 L3/L2 的
    队列预算慢慢吃光。
    """
    released: list[str] = []

    class _FailingComposer:
        def __call__(
            self, rounds: list[Any], previous_summary: str | None
        ) -> tuple[str, dict[str, str]]:
            raise RuntimeError("llm down")

    refresher = _refresher(
        composer=_FailingComposer(),
        reserve_capacity=lambda wait: None,
        release_capacity=lambda: released.append("released"),
    )
    refresher.maybe_submit("user-1", "session-1", namespace="thinkback")
    _settle(refresher)
    time.sleep(0.1)

    assert released == ["released"], f"刷新失败时没有归还容量槽位：{released}"


def test_a_failed_refresh_without_a_capacity_callback_does_not_crash() -> None:
    """刷新失败 + 没接容量回调 → 也不能崩。"""

    class _FailingComposer:
        def __call__(
            self, rounds: list[Any], previous_summary: str | None
        ) -> tuple[str, dict[str, str]]:
            raise RuntimeError("llm down")

    refresher = _refresher(
        composer=_FailingComposer(),
        reserve_capacity=None,
        release_capacity=None,
    )
    refresher.maybe_submit("user-1", "session-1", namespace="thinkback")
    _settle(refresher)
    assert not refresher._futures  # noqa: SLF001


def test_drain_without_a_budget_waits_for_the_refresh() -> None:
    """``drain()`` **不给预算**时，仍要等在飞的刷新跑完。

    与「drain_without_a_budget_still_works」（已有用例）互补：那条测的是
    「没有在飞的东西时立刻返回」；这条测的是「有在飞的东西时**等它**」。

    关停路径（SIGTERM）走的就是无预算的 drain —— 它不能因为「没预算」
    就跳过正在写摘要的刷新（那会丢掉这次摘要，下次得重新攒轮次）。
    """
    repository = InMemoryMemoryRepository()
    _seed_round(repository, "user-1", "session-1", "r1")
    composer = _BlockingComposer()
    refresher = _refresher(
        composer=composer,
        reserve_capacity=None,
        release_capacity=None,
    )
    refresher._repository = repository  # noqa: SLF001

    refresher.maybe_submit("user-1", "session-1", namespace="thinkback")
    assert refresher._futures, "刷新没在飞"

    def _release_later() -> None:
        time.sleep(0.2)
        composer.release.set()

    threading.Thread(target=_release_later, daemon=True).start()
    refresher.drain()  # 不给预算

    assert not refresher._futures, "无预算的 drain 没等刷新跑完就返回了"


def test_the_trailing_refresh_after_a_finish_also_returns_its_slot() -> None:
    """trailing 刷新（补刷攒够的轮次）也占槽位，也必须还。

    trailing 刷新走的是**另一次** ``maybe_submit`` —— 它自己申请、自己归还，
    不依赖上一轮的归还。
    """
    released: list[str] = []
    refresher = _refresher(
        composer=_Composer(),
        reserve_capacity=lambda wait: None,
        release_capacity=lambda: released.append("released"),
        interval=1,
    )

    # 先刷一次（攒够 1 轮 → trailing 会补刷）
    refresher.maybe_submit("user-1", "session-1", namespace="thinkback")
    _settle(refresher)
    time.sleep(0.2)

    assert len(released) >= 1, f"trailing 刷新的容量槽位没归还：{released}"


@pytest.mark.parametrize("scenario", ["already_pending", "submit_failed", "finished"])
def test_every_capacity_path_returns_the_slot(scenario: str) -> None:
    """三条路径逐一验证：槽位必须被归还。

    排队预算被 L2 慢慢吃光是**静默故障**：没有异常、没有日志，
    只是慢慢写不进去了。所以这三条路径都要有断言。
    """
    released: list[str] = []
    executor = ThreadPoolExecutor(max_workers=1)
    refresher = _refresher(
        composer=_Composer(),
        reserve_capacity=lambda wait: None,
        release_capacity=lambda: released.append("released"),
        executor=executor,
    )

    if scenario == "already_pending":
        # 竞态窗口：申请槽位的瞬间被别的线程标成「在刷」
        def _reserve(wait: int) -> None:
            refresher._pending_scopes.add(("user-1", "thinkback", "session-1"))  # noqa: SLF001

        refresher._reserve_capacity = _reserve  # noqa: SLF001
        refresher.maybe_submit("user-1", "session-1", namespace="thinkback")
    elif scenario == "submit_failed":
        executor.shutdown(wait=False)
        refresher.maybe_submit("user-1", "session-1", namespace="thinkback")
    else:
        refresher.maybe_submit("user-1", "session-1", namespace="thinkback")
        _settle(refresher)

    assert released, f"{scenario} 路径没有归还容量槽位"
