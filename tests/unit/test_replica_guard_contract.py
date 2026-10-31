"""多副本前置检查的契约。

配置上宣称多副本却没有把前置落到位时，服务必须在启动期硬失败 —— 静默跑起来
的后果是：幂等缓存与限流桶各算各的（重试会被当成新写入、限流额度被放大到
副本数倍）、L1 缓存的失效只在本副本、引擎侧历史还在 pod 本地文件里。

所以这里钉的是三件事：

1. 判定哪些前置、顺序固定（报告要稳定）；
2. 拒绝启动时点名缺了哪几项；
3. **补救动作写清楚了**（运维照着报错改哪个环境变量）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from thinkback.infra.replica_guard import assert_multi_replica_ready, check_multi_replica


def test_the_prerequisites_are_checked_in_a_fixed_order() -> None:
    """五项前置的顺序固定（报告要稳定），默认配置下前三项不满足。"""
    checks = check_multi_replica(SimpleNamespace())

    assert [item.name for item in checks] == [
        "幂等与限流入 PG",
        "L1 缓存改读穿透",
        "引擎侧历史外置",
        "后台任务 DB 抢占",
        "删除屏障跨副本",
    ]
    assert [item.satisfied for item in checks] == [False, False, False, True, True]
    # 每一项都要有说法 —— detail 是运维看到的那段解释，不能是空的
    assert all(item.detail for item in checks)


def test_the_refusal_lists_every_missing_prerequisite() -> None:
    """拒绝启动时报错必须点名**每一项**缺的前置，并说清缺了会出什么事。"""
    settings = SimpleNamespace(
        memory_multi_replica=True,
        memory_l1_cache_enabled=False,
        memory_history_db_shared=True,
    )

    with pytest.raises(RuntimeError) as excinfo:
        assert_multi_replica_ready(settings)

    message = str(excinfo.value)
    assert "幂等与限流入 PG" in message
    # 报错要点名是哪个环境变量 —— 运维照着改
    assert "MEMORY_MULTI_REPLICA" in message
    assert "L1 缓存改读穿透" not in message
    assert "引擎侧历史外置" not in message
    assert "后台任务 DB 抢占" not in message
    assert "删除屏障跨副本" not in message


def test_a_single_replica_deployment_is_not_checked() -> None:
    """单副本是当前的发布边界 —— 默认不做多副本前置检查。"""
    settings = SimpleNamespace(memory_multi_replica=False)

    assert_multi_replica_ready(settings)  # 不抛异常就算过


def test_a_config_without_the_multi_replica_flag_is_treated_as_single_replica() -> None:
    """没显式声明多副本（属性缺失）就按单副本处理 —— 不检查、不拒绝。

    反过来（缺省当成多副本）的话，默认部署会起不来：前置没齐 → 拒绝启动。
    """
    assert_multi_replica_ready(SimpleNamespace())  # 不抛异常就算过


def test_the_unsatisfied_detail_names_the_setting_to_change() -> None:
    """缺前置时，报错必须点名**要改哪个配置**。

    运维照着报错做就能改对配置 —— 只说「仍开着」不说设哪个变量，等于没说。
    """
    checks = check_multi_replica(
        SimpleNamespace(memory_l1_cache_enabled=True, memory_history_db_shared=False)
    )
    details = {item.name: item.detail for item in checks}

    assert "MEMORY_L1_CACHE_ENABLED" in details["L1 缓存改读穿透"]
    assert "MEMORY_HISTORY_DB_SHARED" in details["引擎侧历史外置"]


def test_the_satisfied_detail_says_the_requirement_is_met() -> None:
    """满足时的文案要说「已经关掉了」，别还在说「仍开着」。

    判据（satisfied）对了、文案说反了，运维会去改一个没问题的配置。
    """
    checks = check_multi_replica(
        SimpleNamespace(memory_l1_cache_enabled=False, memory_history_db_shared=True)
    )
    details = {item.name: item.detail for item in checks}

    assert "读穿透" in details["L1 缓存改读穿透"]
    assert "已声明" in details["引擎侧历史外置"]
