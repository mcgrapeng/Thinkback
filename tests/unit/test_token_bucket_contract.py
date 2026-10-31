"""令牌桶原语的**精确值**契约。

``infra/token_bucket.py`` 是 HTTP 面与 gRPC 面共用的速率上限原语 ——
回填算快了一点等于限流形同虚设（洪水打穿），算慢了等于用户被限得比配置的
狠（而且没人知道为什么）。所以这里的用例都断言**精确值**（令牌数、等待秒数、
桶数），而不是「没报错」。

时钟是冻住的：模块里是 ``import time`` 后调 ``time.time()``，所以把模块里的
``time`` 换掉即可 —— 不然回填会随真实时间漂移，断言就成了概率事件。
"""

from __future__ import annotations

import pytest

from thinkback.infra import token_bucket as token_bucket_module
from thinkback.infra.token_bucket import BucketRegistry, TokenBucket


class _Clock:
    """可冻结的时钟。"""

    def __init__(self, now: float) -> None:
        self.now = now

    def time(self) -> float:
        return self.now


def test_the_bucket_refills_at_exactly_the_configured_rate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回填速度必须精确等于「每秒回填数 × 经过的秒数」。

    60/分钟 → 每秒 1 个；这里取每秒 2 个，是为了让「乘」与「除」两种写法
    的结果差得开（每秒 1 个时两种写法撞成同一个数，断言就白写了）。
    """
    clock = _Clock(1_000.0)
    monkeypatch.setattr(token_bucket_module, "time", clock)
    bucket = TokenBucket(tokens=0.0, last_refill=clock.now, rate_per_second=2.0, burst=10)

    clock.now += 1.0
    ok, wait = bucket.consume(1)

    assert ok is True, "1 秒后没有回填出 2 个令牌"
    assert wait == 0.0
    assert bucket.tokens == pytest.approx(1.0, abs=1e-6)  # 回填 2 个，扣掉 1 个


def test_the_refill_never_exceeds_the_burst(monkeypatch: pytest.MonkeyPatch) -> None:
    """回填不能超过突发额度 —— 否则「停一会儿」就攒出一把更大的桶。"""
    clock = _Clock(1_000.0)
    monkeypatch.setattr(token_bucket_module, "time", clock)
    bucket = TokenBucket(tokens=0.0, last_refill=clock.now, rate_per_second=1.0, burst=5)

    clock.now += 100.0
    ok, wait = bucket.consume(1)

    assert ok is True
    assert wait == 0.0
    # 回填到突发上限 5，扣掉 1 → 4
    assert bucket.tokens == pytest.approx(4.0, abs=1e-6)


def test_consuming_exactly_the_remaining_tokens_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """桶里正好剩 1 个时取 1 个必须成功（不是「不够 1 个」）。"""
    clock = _Clock(1_000.0)
    monkeypatch.setattr(token_bucket_module, "time", clock)
    bucket = TokenBucket(tokens=1.0, last_refill=clock.now, rate_per_second=1.0, burst=10)

    ok, wait = bucket.consume(1)

    assert ok is True, "正好够却被拒了"
    assert wait == 0.0
    assert bucket.tokens == pytest.approx(0.0, abs=1e-6)


def test_the_wait_counts_the_tokens_that_are_still_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """等待秒数 = 还差几个令牌 ÷ 每秒回填数 —— 调用方按它退避。

    这里桶里还剩 1 个（不是空的）：还差 3 个就还差 2 个，
    「还差的个数」算错（加号写成减号）才会看得出差别。
    """
    clock = _Clock(1_000.0)
    monkeypatch.setattr(token_bucket_module, "time", clock)
    bucket = TokenBucket(tokens=1.0, last_refill=clock.now, rate_per_second=2.0, burst=10)

    ok, wait = bucket.consume(3)

    assert ok is False
    assert wait == pytest.approx(1.0, abs=1e-6)  # (3 - 1) / 2


def test_consuming_without_a_count_takes_exactly_one_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不传个数时默认取 1 个 —— 默认值写错就是「一次请求扣两份额度」。"""
    clock = _Clock(1_000.0)
    monkeypatch.setattr(token_bucket_module, "time", clock)
    bucket = TokenBucket(tokens=2.0, last_refill=clock.now, rate_per_second=1.0, burst=10)

    ok, wait = bucket.consume()

    assert ok is True
    assert wait == 0.0
    assert bucket.tokens == pytest.approx(1.0, abs=1e-6)


def test_a_bucket_that_was_never_used_has_no_balance() -> None:
    """没见过的键余额是 0 —— 不是 1，更不是报错。"""
    registry = BucketRegistry(rate_limit_per_minute=60, burst=5)

    assert registry.remaining("never-seen") == 0.0


def test_changing_the_limit_does_not_hand_out_a_free_quota(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """限额变了**不能**重置余额 —— 改配置不该给每个桶发一次免费额度。

    桶里的余额只该「按新的突发上限裁一刀」，不能反过来被抬高。
    """
    clock = _Clock(1_000.0)
    monkeypatch.setattr(token_bucket_module, "time", clock)
    registry = BucketRegistry(rate_limit_per_minute=60, burst=5)
    for _ in range(5):
        assert registry.consume("u1")[0] is True
    assert registry.remaining("u1") == 0.0

    # 配置热更新：限额与突发都变了
    registry.burst = 100
    registry.rate_limit_per_minute = 6000

    ok, wait = registry.consume("u1")

    assert ok is False, "改配置给了这个桶一份免费额度"
    assert wait > 0


def test_the_registry_refills_at_the_configured_rate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """注册表里的桶也按「每分钟配额 ÷ 60」回填。

    每分钟 60 个 → 每秒 1 个 → 桶空后再等一个令牌要等 1 秒。
    限流口径两个面必须一致，这条错了就是「HTTP 面 60/分钟、gRPC 面别的数」。
    """
    clock = _Clock(1_000.0)
    monkeypatch.setattr(token_bucket_module, "time", clock)
    registry = BucketRegistry(rate_limit_per_minute=60, burst=1)

    assert registry.consume("u1")[0] is True  # 新桶是满的
    ok, wait = registry.consume("u1")  # 桶空了

    assert ok is False
    assert wait == pytest.approx(1.0, abs=1e-6)


def test_a_zero_burst_reports_the_exact_wait_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """突发额度为 0 时，等待秒数精确等于「一个令牌要多久」。

    这是**唯一**能看见新桶构造时那份回填速度的配置：桶非空时第一次取令牌
    必然成功，而第二次访问这个桶时回填速度已经被重新赋值覆盖掉了。
    """
    clock = _Clock(1_000.0)
    monkeypatch.setattr(token_bucket_module, "time", clock)
    registry = BucketRegistry(rate_limit_per_minute=60, burst=0)

    ok, wait = registry.consume("u1")

    assert ok is False
    assert wait == pytest.approx(1.0, abs=1e-6)  # 1 个令牌 ÷ 每秒 1 个


def test_the_least_recently_used_bucket_is_the_one_evicted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """桶满了要淘汰时，淘汰的必须是**最久没用上**的那把。

    键故意不用字典序：最早用的叫 ``z``，字典序最小的叫 ``a`` ——
    两种口径挑出来的不是同一把桶，断言才看得出差别。
    """
    clock = _Clock(1_000.0)
    monkeypatch.setattr(token_bucket_module, "time", clock)
    registry = BucketRegistry(rate_limit_per_minute=60, burst=5, max_buckets=2)

    clock.now += 1.0
    registry.consume("z")  # 最先用上的
    clock.now += 1.0
    registry.consume("a")
    clock.now += 1.0
    registry.consume("m")  # 桶满了 → 淘汰 z

    assert registry.bucket_count() == 2
    assert registry.remaining("z") == 0.0, "淘汰的不是最久没用上的那把桶"
    assert registry.remaining("a") > 0.0


def test_the_bucket_count_never_exceeds_the_limit() -> None:
    """桶数有硬上限 —— 轮换 key 不能把表撑爆（这是内存洞的防线）。"""
    registry = BucketRegistry(rate_limit_per_minute=60, burst=5, max_buckets=2)

    registry.consume("a")
    registry.consume("b")
    registry.consume("c")

    assert registry.bucket_count() == 2


def test_the_bucket_count_stays_capped_after_an_evicted_key_comes_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """被淘汰过的键再回来时，桶数仍然不能超过上限。

    淘汰要把「桶」和「最后使用时间」两处都清掉；只清一处的话，这个键还
    留在使用时间表里，下一轮淘汰又挑中它 —— 桶数就涨过上限了。
    """
    clock = _Clock(1_000.0)
    monkeypatch.setattr(token_bucket_module, "time", clock)
    registry = BucketRegistry(rate_limit_per_minute=60, burst=5, max_buckets=2)

    registry.consume("a")
    registry.consume("b")
    registry.consume("c")  # 淘汰 a
    assert registry.bucket_count() == 2

    clock.now += 10.0
    registry.consume("a")  # a 回来了

    assert registry.bucket_count() == 2, "桶数涨过了上限"


def test_the_default_bucket_cap_is_ten_thousand() -> None:
    """默认上限就是一万把桶 —— 这是「轮换 key 把内存撑爆」那道防线的值。"""
    registry = BucketRegistry(rate_limit_per_minute=60, burst=5)

    assert registry.max_buckets == 10_000
