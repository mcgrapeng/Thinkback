"""L2 刷新的 drain 静默性：drain 返回后不得再有刷新在途或即将开始。

回归缺陷：``_finish`` 在锁内把 future 与 pending scope 摘掉，**出锁后**才补
trailing 刷新（在飞期间攒够的轮次要靠它落进摘要）。而 ``drain`` 只看
「有没有 future / 有没有 pending scope」，于是可能在这个窗口里判定已静默并
返回 —— trailing 刷新随后才提交。

生产后果（关停顺序：drain → executor shutdown(wait=False)）：那段 trailing
刷新撞上已关闭的 executor 被直接丢掉，摘要停在上一轮，第 2、3 轮不在摘要里。

修法：记下正在跑的 ``_finish`` 回调数；回调没结束，drain 就还不能判定静默
（trailing 是在那个回调里提交的）。

这里把 ``release_capacity`` 注入成慢操作来放大那个窗口 —— 天然窗口只有微秒级，
不放大就测不出来。
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event, Thread
from typing import Any

from thinkback.memory.l2_refresh import L2BackgroundRefresher
from thinkback.memory.repositories import InMemoryMemoryRepository
from thinkback.memory.schemas import AppendMemoryRequest


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


def test_drain_waits_for_the_trailing_refresh_it_is_about_to_start() -> None:
    """drain 返回时，trailing 刷新必须已经跑完。"""
    started = Event()
    release = Event()
    in_window = Event()
    calls: list[int] = []

    def composer(rounds: list[Any], previous_summary: str | None) -> tuple[str, dict[str, str]]:
        calls.append(len(rounds))
        if len(calls) == 1:
            started.set()
            if not release.wait(timeout=2):
                raise RuntimeError("test release timeout")
        text = f"【主题】刷新{len(calls)}"
        return text, {"主题": text}

    def _slow_release() -> None:
        # _finish 在「已摘掉 future/pending」与「提交 trailing」之间调它。
        # 放慢它就是把那个窗口放大到 drain 看得见。
        in_window.set()
        time.sleep(0.3)

    repository = InMemoryMemoryRepository()
    repository.save_round(_append("r1", "第一轮"))
    refresher = L2BackgroundRefresher(
        repository=repository,
        composer=composer,
        executor=ThreadPoolExecutor(max_workers=1),
        refresh_interval_rounds=2,
        release_capacity=_slow_release,
    )

    refresher.maybe_submit("user-1", "session-1")
    assert started.wait(timeout=2)

    # 在飞期间又来两轮：计数达到阈值 → 刷新结束后必须补一次 trailing 刷新。
    repository.save_round(_append("r2", "第二轮", index=2))
    repository.save_round(_append("r3", "第三轮", index=3))
    refresher.maybe_submit("user-1", "session-1")
    refresher.maybe_submit("user-1", "session-1")

    release.set()
    assert in_window.wait(timeout=2), "没有进入「已摘掉 future/pending」的窗口"

    refresher.drain(timeout=1)
    calls_at_drain = len(calls)

    assert calls_at_drain >= 2, "drain 必须等到 trailing 刷新跑完才返回"

    time.sleep(0.4)
    assert len(calls) == calls_at_drain, "drain 返回后仍有刷新启动"


def test_the_trailing_refresh_keeps_the_namespace_of_its_scope() -> None:
    """补的那次 trailing 刷新必须还是同一个命名空间。

    trailing 刷新是在完成回调里提交的；命名空间传丢 → 它跑到默认命名空间
    去了：目标命名空间的轮次永远等不到这次刷新（摘要停在旧版本），
    另一个命名空间被无端改写。
    """
    started = Event()
    release = Event()
    calls: list[tuple[int, str | None]] = []

    def composer(rounds: list[Any], previous_summary: str | None) -> tuple[str, dict[str, str]]:
        calls.append((len(rounds), previous_summary))
        if len(calls) == 1:
            started.set()
            if not release.wait(timeout=2):
                raise RuntimeError("test release timeout")
        text = f"【主题】刷新{len(calls)}"
        return text, {"主题": text}

    repository = InMemoryMemoryRepository()
    repository.save_round(_append("d0", "默认命名空间那一轮"))
    repository.save_round(_append("r1", "第一轮", namespace="other"))
    refresher = L2BackgroundRefresher(
        repository=repository,
        composer=composer,
        executor=ThreadPoolExecutor(max_workers=1),
        refresh_interval_rounds=2,
    )

    refresher.maybe_submit("user-1", "session-1", namespace="other")
    assert started.wait(timeout=2)

    # 在飞期间又来两轮：计数达到阈值 → 刷新结束后补一次 trailing 刷新。
    repository.save_round(_append("r2", "第二轮", index=2, namespace="other"))
    repository.save_round(_append("r3", "第三轮", index=3, namespace="other"))
    refresher.maybe_submit("user-1", "session-1", namespace="other")
    refresher.maybe_submit("user-1", "session-1", namespace="other")

    release.set()
    refresher.drain(timeout=1)

    assert len(calls) >= 2, "trailing 刷新没有发生"
    # trailing 刷新必须看到**目标命名空间**的三轮（默认命名空间只有一轮）。
    assert calls[1][0] == 3, "trailing 刷新跑到了别的命名空间"


def test_drain_returns_when_nothing_is_pending() -> None:
    """没有在飞刷新时 drain 立即返回：修复不能把 drain 变成阻塞等待。"""
    refresher = L2BackgroundRefresher(
        repository=InMemoryMemoryRepository(),
        composer=lambda rounds, previous: ("【主题】x", {"主题": "x"}),
        executor=ThreadPoolExecutor(max_workers=1),
        refresh_interval_rounds=2,
    )

    began = time.monotonic()
    refresher.drain(timeout=0.5)

    assert time.monotonic() - began < 1.0, "无在飞刷新时 drain 不该等待"


def test_a_failed_submit_releases_the_scope_so_drain_can_still_quiesce() -> None:
    """提交失败必须把 scope 从 _pending_scopes 里摘掉，并把队列槽位还回去。

    漏了摘 scope 有两处硬伤：

    - drain 永远等不到静默（pending 为真 → 循环不退出）→ **关停卡死**；
    - 该作用域此后再也刷不出摘要（在飞去重把它一直挡在外面）。

    漏了还槽位则更隐蔽：队列预算被这次失败的提交吃掉，后续 append 全部
    被容量闸门挡在门外 —— 队列看起来「满了」，其实什么都没在跑。
    """

    class _RejectingExecutor:
        def submit(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("executor is shut down")

    released: list[int] = []
    refresher = L2BackgroundRefresher(
        repository=InMemoryMemoryRepository(),
        composer=lambda rounds, previous: ("【主题】x", {"主题": "x"}),
        executor=_RejectingExecutor(),  # type: ignore[arg-type]
        refresh_interval_rounds=2,
        reserve_capacity=lambda _wait: None,
        release_capacity=lambda: released.append(1),
    )

    refresher.maybe_submit("user-1", "session-1")

    assert refresher._pending_scopes == set(), "scope 卡在 pending：drain 会永远等下去"
    assert released, "队列槽位没归还：后续 append 会被容量闸门挡住"

    began = time.monotonic()
    refresher.drain(timeout=0.5)
    assert time.monotonic() - began < 1.0, "提交失败后 drain 没能归位"


def test_drain_waits_for_a_refresh_that_is_being_submitted() -> None:
    """drain 返回时，**提交中**的刷新必须已经跑完。

    ``maybe_submit`` 先把 scope 记进 ``_pending_scopes``，**之后**才把 future
    登记进 ``_futures``。这两步之间有一个窗口：刷新已经在提交、但还看不到
    future。那个窗口里 drain 若按「没有 future、没有 pending」判定静默就会
    立刻返回 —— 而刷新其实还没提交完。

    生产后果跟 trailing 那个窗口一样：关停顺序是 drain → executor
    shutdown(wait=False)，提前返回的那段刷新撞上已关闭的 executor 被丢掉，
    摘要停在上一轮。

    天然窗口只有微秒级（提交一个 future 很快），所以这里用一个会卡住的
    executor 把窗口放大到 drain 看得见。
    """

    class _BlockingExecutor:
        """submit 会卡住 —— 把「pending 已登记、future 未登记」放大成可观测窗口。"""

        def __init__(self) -> None:
            self.entered = Event()
            self.release = Event()
            self._inner = ThreadPoolExecutor(max_workers=1)

        def submit(self, fn: Any, *args: object, **kwargs: object) -> Any:
            self.entered.set()
            if not self.release.wait(timeout=5):
                raise RuntimeError("test release timeout")
            return self._inner.submit(fn, *args, **kwargs)

        def shutdown(self, wait: bool = True) -> None:
            self._inner.shutdown(wait=wait)

    repository = InMemoryMemoryRepository()
    repository.save_round(_append("r1", "第一轮"))
    executor = _BlockingExecutor()
    refresher = L2BackgroundRefresher(
        repository=repository,
        composer=lambda rounds, previous: ("【主题】x", {"主题": "x"}),
        executor=executor,  # type: ignore[arg-type]
        refresh_interval_rounds=2,
    )

    submitter = Thread(target=lambda: refresher.maybe_submit("user-1", "session-1"))
    submitter.start()
    assert executor.entered.wait(timeout=2), "没有进入「pending 已登记、future 未登记」的窗口"

    drained = Event()
    drainer = Thread(target=lambda: (refresher.drain(timeout=5), drained.set()))
    drainer.start()

    returned_early = drained.wait(timeout=1.0)
    executor.release.set()
    assert drained.wait(timeout=5), "drain 没有等到刷新提交完"
    submitter.join(timeout=5)

    assert not returned_early, "drain 在刷新还没提交完时就返回了 —— 那段刷新会被丢掉"
