"""多副本前置检查（规格 §8.1）。

单副本是当前的发布边界。多副本要 HA，五项前置必须先落地；配置上宣称多副本
却没有把前置落到位时，服务必须在启动期硬失败 —— 静默跑起来的后果是：

- 幂等缓存与限流桶都在进程内：多副本各算各的，重试会被当成新写入、
  单客户端的限流额度被放大到副本数倍；
- L1 缓存的失效只在本副本：别的副本追加的轮次本副本看不见；
- 引擎侧历史在 pod 本地文件里：一次擦除只清当前副本，别的副本里还留着原文；
- 后台任务由进程内 executor 执行：副本挂掉后任务要等重启才被回收；
- 删除屏障靠进程内锁：在飞写入挡不住。

这五项里，删除屏障与任务抢占已经是跨副本的（墓碑与任务行都在 PG 里），
L1 读穿透是配置项，引擎侧历史靠共享卷，剩下幂等与限流还没有落 PG。
"""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger


@dataclass(frozen=True)
class Prerequisite:
    """一项前置的检查结果。"""

    name: str
    satisfied: bool
    detail: str


def check_multi_replica(settings: object) -> list[Prerequisite]:
    """逐项检查多副本前置，返回结果（顺序固定，便于报告稳定）。"""

    def _get(name: str, default: object = None) -> object:
        return getattr(settings, name, default)

    l1_read_through = not bool(_get("memory_l1_cache_enabled", True))
    history_shared = bool(_get("memory_history_db_shared", False))
    return [
        Prerequisite(
            "幂等与限流入 PG",
            False,
            "幂等缓存与限流桶仍在进程内：多副本各算各的（重试会被当成新写入，"
            "限流额度被放大到副本数倍）。需要把幂等存储与限流桶迁到跨副本共享的存储（仓库当前不引入额外的队列与缓存依赖，见「明确不做」清单）。",
        ),
        Prerequisite(
            "L1 缓存改读穿透",
            l1_read_through,
            "已关掉进程内 L1 缓存（读穿透，每次回源 PG）"
            if l1_read_through
            else "进程内 L1 缓存仍开着：别的副本追加的轮次本副本看不见。"
            "多副本部署要设 MEMORY_L1_CACHE_ENABLED=false。",
        ),
        Prerequisite(
            "引擎侧历史外置",
            history_shared,
            "已声明 mem0 的 history.db 在共享卷上"
            if history_shared
            else "mem0 的 history.db 仍是 pod 本地文件：一次擦除只清当前副本。"
            "要把它挂到共享卷，并设 MEMORY_HISTORY_DB_SHARED=true。",
        ),
        Prerequisite(
            "后台任务 DB 抢占",
            True,
            "任务行在 PG（ins_memory_task），认领靠主键插入，"
            "同一任务不会被两个副本同时执行；副本挂掉后由回收流程接手。",
        ),
        Prerequisite(
            "删除屏障跨副本",
            True,
            "擦除墓碑落在 PG 索引行里，append 路径回源 PG 校验，跨副本生效。",
        ),
    ]


def assert_multi_replica_ready(settings: object) -> None:
    """多副本配置下，前置没齐就拒绝启动。

    只在 ``MEMORY_MULTI_REPLICA=true`` 时生效；默认（单副本）不做检查。
    报错信息要能直接告诉运维缺哪几项、每项不满足会出什么事。
    """

    if not bool(getattr(settings, "memory_multi_replica", False)):
        return
    checks = check_multi_replica(settings)
    missing = [item for item in checks if not item.satisfied]
    if not missing:
        logger.bind().info("multi-replica prerequisites satisfied")
        return
    lines = [f"  - {item.name}: {item.detail}" for item in missing]
    raise RuntimeError(
        "多副本部署的前置未齐，拒绝启动（MEMORY_MULTI_REPLICA=true）：\n"
        + "\n".join(lines)
        + "\n单副本部署请把 MEMORY_MULTI_REPLICA 改回 false。"
    )
