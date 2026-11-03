"""真实栈评测/探针脚本的集合隔离。

为什么要这个契约：评测数据落进生产集合会污染 ADR-0007 的切换判据 ——
新集合比旧集合多出评测行，「新旧行数一致」永远对不上，回滚时的数据缺口
也算不清。生产集合只留真实用户作用域。
"""

from __future__ import annotations

from pathlib import Path

from thinkback.infra.config import Settings
from thinkback.infra.milvus_collection import EVAL_COLLECTION, eval_settings

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "script"

#: 门禁脚本验的就是生产集合（含双写镜像与 analyzer），所以它留在生产集合。
PRODUCTION_SCRIPTS = frozenset({"gate_real.py"})


def test_eval_settings_redirects_collection_and_clears_mirror() -> None:
    production = Settings(
        memory_milvus_collection="agent_semantic_memory_v2",
        memory_milvus_mirror_collection="agent_semantic_memory_v1",
    )

    redirected = eval_settings(production)

    assert redirected.memory_milvus_collection == EVAL_COLLECTION
    assert redirected.memory_milvus_mirror_collection == ""
    # 原对象不能被改：生产配置是别的进程在用的
    assert production.memory_milvus_collection == "agent_semantic_memory_v2"
    assert production.memory_milvus_mirror_collection == "agent_semantic_memory_v1"


def test_eval_collection_is_not_the_production_collection() -> None:
    assert EVAL_COLLECTION != "thinkback"
    assert EVAL_COLLECTION != "agent_semantic_memory_v2"


def test_in_process_real_stack_scripts_route_through_eval_settings() -> None:
    """进程内直连 Milvus 的评测脚本必须把集合指向评测集合。"""

    offenders: list[str] = []
    for path in sorted(SCRIPT_DIR.rglob("*.py")):
        if path.name in PRODUCTION_SCRIPTS:
            continue
        source = path.read_text(encoding="utf-8")
        if "get_memory_backend(" not in source:
            continue
        if "eval_settings(" not in source:
            offenders.append(path.relative_to(SCRIPT_DIR).as_posix())

    assert offenders == []
