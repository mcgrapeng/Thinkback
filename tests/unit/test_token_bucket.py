"""令牌桶原语：HTTP 面与 gRPC 面共用的那一份。

两个面共用一个原语，就得共用一份行为 —— 尤其「桶数有上限」这条：分桶键是
调用方可控的（namespace + user_id），不封顶的话这张表就是一个能无限长大的
内存洞。
"""

from __future__ import annotations

from thinkback.infra.token_bucket import BucketRegistry, TokenBucket


def test_a_new_bucket_starts_with_the_full_burst() -> None:
    registry = BucketRegistry(rate_limit_per_minute=60, burst=5)

    ok, wait = registry.consume("a")

    assert ok is True
    assert wait == 0.0
    assert registry.remaining("a") == 4.0


def test_consuming_past_the_burst_is_refused() -> None:
    registry = BucketRegistry(rate_limit_per_minute=60, burst=2)

    assert registry.consume("a")[0] is True
    assert registry.consume("a")[0] is True
    ok, wait = registry.consume("a")

    assert ok is False
    assert wait > 0


def test_different_keys_have_separate_buckets() -> None:
    registry = BucketRegistry(rate_limit_per_minute=60, burst=1)

    assert registry.consume("a")[0] is True
    assert registry.consume("a")[0] is False
    assert registry.consume("b")[0] is True


def test_the_bucket_map_is_bounded() -> None:
    """桶数有硬上限 —— 轮换 key 不能把表撑爆。"""
    registry = BucketRegistry(rate_limit_per_minute=60, burst=5, max_buckets=10)

    for index in range(100):
        registry.consume(f"key-{index}")

    assert registry.bucket_count() <= 10


def test_the_evicted_bucket_is_the_least_recently_used_one() -> None:
    """淘汰的是最久没用上的那个，不是随机的。"""
    registry = BucketRegistry(rate_limit_per_minute=60, burst=5, max_buckets=2)
    registry.consume("old")
    registry.consume("new")
    registry.consume("new")  # 让 "new" 变成最近用过

    registry.consume("third")  # 超上限 → 淘汰最久没用的 "old"

    assert registry.bucket_count() == 2
    assert registry.remaining("old") == 0.0
    assert registry.remaining("new") > 0.0


def test_the_token_bucket_reports_how_long_to_wait() -> None:
    import time

    import pytest

    bucket = TokenBucket(tokens=0.0, last_refill=time.time(), rate_per_second=1.0, burst=10)

    ok, wait = bucket.consume(1)

    assert ok is False
    assert wait == pytest.approx(1.0, abs=0.01)
