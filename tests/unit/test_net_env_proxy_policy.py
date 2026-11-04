"""网络环境策略：脚本层统一关代理 + 客户端层免疫（两侧都要）。

为什么两侧都要：
- **脚本层**：脚本自己发 HTTP（驱动本机服务、拉引擎端点）→ 机器上的 SOCKS
  代理会劫持 127.0.0.1 直连（表现为 UNAVAILABLE），缺 socksio 时客户端
  构造阶段就炸。
- **客户端层**：服务里的 mem0 / embedder / LLM 客户端 → 内部端点本就不该
  走代理（``trust_env=False``）。

踩过的坑：``gate_real.py`` 自己清了代理环境变量 → 真实栈门禁一路绿；同一台
机器上正常启动的服务，抽取链整个是断的（日志里只有一条 ``warmup failed``
的 warning）。所以这里把「每个碰真实栈的脚本都要显式声明一次」钉成契约，
不靠「记得清环境变量」。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from thinkback.infra.net_env import PROXY_ENV_KEYS, disable_env_proxy

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "script"

#: 直接发网络请求的脚本（urlopen / httpx 客户端 / requests / grpc 通道）。
_DIRECT_NETWORK_RE = re.compile(
    r"urlopen|httpx\.Client|requests\.(get|post|put|delete|request)|grpc\.(insecure|secure)_channel"
)

#: 会在进程内构造**真实后端**客户端的脚本（要连内部 LLM / embedding 端点）。
#: 只用 FakeMemoryBackend / 内存仓储的自包含评测不碰 HTTP，无关代理。
_IN_PROCESS_STACK_RE = re.compile(r"get_memory_backend\(|Mem0LibraryMemoryBackend\(")

#: 客户端层的两种隔离手段：httpx/requests 的 trust_env=False、grpc 通道的
#: enable_http_proxy=0。脚本自己建客户端时用这两种同样满足政策。
_CLIENT_LEVEL_ISOLATION = ("trust_env=False", "grpc.enable_http_proxy")


def _scripts() -> list[Path]:
    return sorted(p for p in SCRIPT_DIR.rglob("*.py") if p.name != "__init__.py")


def test_disable_env_proxy_removes_the_proxy_variables() -> None:
    """代理变量被清掉：httpx / urllib / grpc 不再读它们。"""
    for key in PROXY_ENV_KEYS:
        os.environ[key] = "socks5://127.0.0.1:9/"

    disable_env_proxy()

    for key in PROXY_ENV_KEYS:
        assert key not in os.environ


def test_disable_env_proxy_sets_the_no_proxy_escape_hatch() -> None:
    """NO_PROXY=* 兜住只认 no_proxy 的客户端（httpx 的 proxy_bypass）。"""
    os.environ.pop("NO_PROXY", None)
    os.environ.pop("no_proxy", None)

    disable_env_proxy()

    assert os.environ["NO_PROXY"] == "*"
    assert os.environ["no_proxy"] == "*"


def test_disable_env_proxy_is_idempotent() -> None:
    """重复调用不报错（脚本可能被重复进入）。"""
    disable_env_proxy()
    disable_env_proxy()

    assert os.environ["NO_PROXY"] == "*"


def test_every_script_that_touches_the_stack_disables_the_env_proxy() -> None:
    """碰真实栈的脚本必须隔离代理。

    两种机制任一即可：脚本层统一 ``disable_env_proxy()``，或脚本自建客户端时
    明确 ``trust_env=False`` / ``grpc.enable_http_proxy=0``。
    """
    offenders: list[str] = []
    for path in _scripts():
        source = path.read_text(encoding="utf-8")
        if not (_DIRECT_NETWORK_RE.search(source) or _IN_PROCESS_STACK_RE.search(source)):
            continue
        if "disable_env_proxy" in source:
            continue
        if any(marker in source for marker in _CLIENT_LEVEL_ISOLATION):
            continue
        offenders.append(path.relative_to(SCRIPT_DIR).as_posix())

    assert offenders == []


def test_the_fake_only_evaluation_scripts_stay_untouched() -> None:
    """自包含评测（假后端）不需要代理处理，但也不能被误加上。"""
    source = (SCRIPT_DIR / "run_self_contained_evaluation.py").read_text(encoding="utf-8")

    assert "disable_env_proxy" not in source
