"""保留期物理清理作业（ADR-0010 第三段）。

到期的事实先不可达（引擎隐藏 + 本地过滤），过了宽限期才由这个作业物理
删除：向量、引擎侧事实历史、治理行一起删。审计只留计数。

用法（凭据从环境变量或 .env 读）::

    # 先看清楚要删哪些，不动数据
    uv run python script/run_retention_cleanup.py --dry-run

    # 真删
    uv run python script/run_retention_cleanup.py

    # 只清某个 namespace
    uv run python script/run_retention_cleanup.py --namespace thinkback

作业默认关闭（MEMORY_RETENTION_ENABLED=false）：要跑先在配置里打开，或加
--force 临时放行 —— 忘了开作业而数据一直堆着，和误开作业把还在宽限期内的
事实删掉，都是不容易发现的错。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv  # noqa: E402


def _load_env(env_file: str | None) -> None:
    """先读凭据再清代理：内网端点在 10.x 网段，mac 的本地代理会劫持 httpx。"""

    load_dotenv(env_file)
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        os.environ.pop(key, None)
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"


_load_env(sys.argv[sys.argv.index("--env-file") + 1] if "--env-file" in sys.argv else None)

from thinkback.api.dependencies import get_memory_backend  # noqa: E402
from thinkback.infra.config import get_settings  # noqa: E402
from thinkback.memory.repositories import SqlAlchemyMemoryRepository  # noqa: E402
from thinkback.memory.retention import RetentionCleaner  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="只统计不删")
    parser.add_argument("--namespace", default=None, help="只清这个 namespace")
    parser.add_argument("--user-id", default=None, help="只清这个用户")
    parser.add_argument("--grace-days", type=int, default=None, help="覆盖配置里的宽限天数")
    parser.add_argument("--batch-size", type=int, default=None)
    parser.add_argument("--force", action="store_true", help="作业没打开时也跑")
    parser.add_argument("--env-file", default=None, help="凭据文件路径")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.memory_retention_enabled and not args.force:
        print(
            "保留期作业未启用（MEMORY_RETENTION_ENABLED=false）。确认要跑就加 --force。",
            file=sys.stderr,
        )
        return 1

    repository = SqlAlchemyMemoryRepository()
    backend = get_memory_backend(settings)
    cleaner = RetentionCleaner(
        repository=repository,
        backend=backend,
        grace_days=(
            args.grace_days if args.grace_days is not None else settings.memory_retention_grace_days
        ),
        batch_size=(
            args.batch_size if args.batch_size is not None else settings.memory_retention_batch_size
        ),
        memory_scope_id=args.namespace or settings.memory_retention_namespace or None,
        user_id=args.user_id,
    )

    report = cleaner.run(dry_run=args.dry_run)
    if not args.dry_run:
        cleaner.record_audit(report)
    print(
        f"扫描 {report.scanned} 条｜删除 {report.purged} 条｜失败 {report.failed} 条"
        f"｜引擎历史行 {report.history_rows_deleted}"
        f"｜{'试运行，未删任何数据' if args.dry_run else '已物理清理'}"
    )
    if report.failed_memory_ids:
        print(f"失败条目（下一轮会重试）：{report.failed_memory_ids[:10]}", file=sys.stderr)
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
