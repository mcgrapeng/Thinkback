"""任务状态机与保留期判定的不变量（hypothesis 属性测试）。

这两块此前只有示例测试，没有属性测试 —— 而它们恰恰是**阈值方向写反了
也不会让任何既有用例变红**的地方：

- 任务状态机：DEAD_LETTER 一旦被当成可重试，预算检查就被绕过
  （真实事故：连打 19 次，retry_count 涨到 11，而 MAX_RETRIES=5）。
- 保留期：``expires_at <= cutoff`` 写成 ``>=`` 的话，早过期晚过期正好反过来，
  记忆该留的被删、不该删的留着 —— 方向翻转只在生产上暴露。

示例测试只能覆盖「明显老」和「明显新」两个样本，离阈值几十天，判定方向翻转
也不红。属性测试用随机输入（含贴边界的偏移）钉住不变量。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from hypothesis import given
from hypothesis import strategies as st

from thinkback.domain.enums import TaskStatus
from thinkback.memory.retention import is_purgeable, purge_cutoff
from thinkback.memory.task_state import is_dead_letter, is_finalized, is_retryable

_MIN = datetime(2000, 1, 1, tzinfo=UTC)
_MAX = datetime(2100, 1, 1, tzinfo=UTC)

_moments = st.datetimes(min_value=_MIN, max_value=_MAX, timezones=st.just(UTC))
_offsets = st.integers(min_value=-(10**8), max_value=10**8)
_grace_days = st.integers(min_value=0, max_value=3650)
_statuses = st.sampled_from(list(TaskStatus))


@dataclass
class _Entry:
    """带到期时间的记忆条目（``is_purgeable`` 只看 ``expires_at``）。"""

    expires_at: datetime | None


# ---------------------------------------------------------------------------
# 任务状态机
# ---------------------------------------------------------------------------


@given(status=_statuses)
def test_only_failed_is_retryable(status: TaskStatus) -> None:
    """只有 FAILED 可重试。

    DEAD_LETTER 被加进可重试集合 = 预算检查被绕过 = 无限重试。
    """
    assert is_retryable(status) is (status is TaskStatus.FAILED)


@given(status=_statuses)
def test_dead_letter_is_never_retryable(status: TaskStatus) -> None:
    """DEAD_LETTER 不是「另一种失败」：它的预算已经用完了。"""
    if status is TaskStatus.DEAD_LETTER:
        assert is_retryable(status) is False


@given(status=_statuses)
def test_finalized_classification_is_exact(status: TaskStatus) -> None:
    """已定局 = COMPLETED / FAILED / DEAD_LETTER，其余都不定局。"""
    expected = status in {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.DEAD_LETTER,
    }
    assert is_finalized(status) is expected


@given(status=_statuses)
def test_dead_letter_is_always_finalized(status: TaskStatus) -> None:
    """DEAD_LETTER 必须已定局：CAS 重放若能改写它，「需人工介入」的信号就没了。"""
    if is_dead_letter(status):
        assert is_finalized(status) is True


@given(status=_statuses)
def test_in_progress_states_are_neither_finalized_nor_retryable(status: TaskStatus) -> None:
    """进行中（PENDING / RUNNING）既未定局也不可重试。

    把「在跑」当成可重试，会与正在跑的那个执行者抢同一份任务。
    """
    if status in {TaskStatus.PENDING, TaskStatus.RUNNING}:
        assert is_finalized(status) is False
        assert is_retryable(status) is False
        assert is_dead_letter(status) is False


@given(status=_statuses)
def test_every_status_is_either_in_progress_or_finalized(status: TaskStatus) -> None:
    """每个状态要么在跑、要么已定局：新增枚举成员不能从判定里悄悄漏出去。"""
    in_progress = status in {TaskStatus.PENDING, TaskStatus.RUNNING}
    assert in_progress or is_finalized(status)


@given(status=_statuses)
def test_no_status_is_both_dead_letter_and_retryable(status: TaskStatus) -> None:
    """「预算耗尽」与「可以重试」不能同时成立。"""
    assert not (is_dead_letter(status) and is_retryable(status))


# ---------------------------------------------------------------------------
# 保留期
# ---------------------------------------------------------------------------


@given(now=_moments, grace_days=_grace_days)
def test_purge_cutoff_is_exactly_grace_days_earlier(now: datetime, grace_days: int) -> None:
    """宽限期是「到期之后」再等 N 天：cutoff 就是 now 往前推 N 天。"""
    assert purge_cutoff(now=now, grace_days=grace_days) == now - timedelta(days=grace_days)


@given(now=_moments, grace_days=_grace_days)
def test_entry_without_expiry_is_never_purgeable(now: datetime, grace_days: int) -> None:
    """永不过期的记忆永远不清理。"""
    assert is_purgeable(_Entry(expires_at=None), now=now, grace_days=grace_days) is False


@given(now=_moments, grace_days=_grace_days, offset=_offsets)
def test_purgeability_matches_the_cutoff_comparison(
    now: datetime, grace_days: int, offset: int
) -> None:
    """判定就是「到期时刻 <= cutoff」，边界取等号（恰好到期 + 宽限期满即可清理）。"""
    expires_at = now + timedelta(seconds=offset)
    cutoff = purge_cutoff(now=now, grace_days=grace_days)
    expected = expires_at <= cutoff
    assert is_purgeable(_Entry(expires_at=expires_at), now=now, grace_days=grace_days) is expected


@given(now=_moments, grace_days=_grace_days, offset=_offsets)
def test_purgeability_is_monotone_in_now(now: datetime, grace_days: int, offset: int) -> None:
    """过了判定点的记忆不会因为时间推进又变得不可清理。

    时间只会让 cutoff 前移，所以「可清理」是单调的。反向单调（越晚越不可清）
    就是比较方向写反了。
    """
    expires_at = now + timedelta(seconds=offset)
    if not is_purgeable(_Entry(expires_at=expires_at), now=now, grace_days=grace_days):
        return
    later = now + timedelta(days=1)
    assert is_purgeable(_Entry(expires_at=expires_at), now=later, grace_days=grace_days) is True


@given(now=_moments, grace_days=_grace_days, early=_offsets, late=_offsets)
def test_earlier_expiry_is_purgeable_no_later_than_a_later_one(
    now: datetime, grace_days: int, early: int, late: int
) -> None:
    """先到期的先清：晚过期的已可清时，先到期的必然也已可清。"""
    expires_early = now + timedelta(seconds=min(early, late))
    expires_late = now + timedelta(seconds=max(early, late))
    if is_purgeable(_Entry(expires_at=expires_late), now=now, grace_days=grace_days):
        assert (
            is_purgeable(_Entry(expires_at=expires_early), now=now, grace_days=grace_days) is True
        )


@given(now=_moments, grace_days=_grace_days, offset=_offsets)
def test_naive_expiry_is_treated_as_utc(now: datetime, grace_days: int, offset: int) -> None:
    """naive 时间按 UTC 处理（asyncpg/SQLite 可能丢掉 tzinfo）。

    口径：naive 的墙上时间被解释成 UTC 时刻，因此它与「同一个墙上时间挂上 UTC」
    是同一个瞬间，判定必须一致。
    """
    aware = now + timedelta(seconds=offset)
    naive = aware.replace(tzinfo=None)
    assert is_purgeable(_Entry(expires_at=naive), now=now, grace_days=grace_days) == (
        is_purgeable(_Entry(expires_at=naive.replace(tzinfo=UTC)), now=now, grace_days=grace_days)
    )
