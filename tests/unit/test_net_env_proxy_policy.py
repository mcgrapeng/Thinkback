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

import ast
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


def _module_level_call_lines(source: str) -> list[int]:
    """模块级（顶层）执行到的调用语句行号。

    只看**顶层**语句：模块级执行的语句是按行顺序跑的，所以「先调用、
    后 import」＝ 那个名字在调用那一刻还没绑定 → NameError。
    函数体里的调用不在此列 —— 它在函数被调用时才执行。
    """
    tree = ast.parse(source)
    lines: list[int] = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.ClassDef, ast.FunctionDef)):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            lines.append(node.lineno)
    return lines


def test_every_script_binds_disable_env_proxy_before_calling_it() -> None:
    """调用 ``disable_env_proxy()`` 之前，那个名字必须已经 import 好。

    上一轮（2026-11-04，提交 3d88788）把内联的代理清理收敛到
    ``thinkback.infra.net_env.disable_env_proxy()`` 时，三个脚本把
    ``from thinkback.infra.net_env import disable_env_proxy`` 放在了
    **模块级调用之后**：

        def _prepare_environment() -> None:
            ...
            disable_env_proxy()      # ← 调用

        _prepare_environment()       # ← 模块级执行到这里

        from thinkback.infra.net_env import disable_env_proxy  # ← import 在这之后

    Python 在模块级执行到 ``_prepare_environment()`` 那一刻，函数体里的
    ``disable_env_proxy`` 还没绑定 → **NameError，脚本起不来，退出码 1**。

    受影响的是三个真实栈脚本：``gate_real.py``（真实栈门禁）、
    ``eval_prompt_ab.py``、``eval/run_semiconductor_evaluation.py``
    （``make eval`` 跑它 → ``make check`` 跟着红）。

    为什么之前没被发现：``test_every_script_that_touches_the_stack_disables_the_env_proxy``
    只检查「源码里出现过 ``disable_env_proxy`` 这个字符串」，
    看不到 import 的**位置**。字符串在 ≠ 名字在调用那一刻已绑定。
    """
    offenders: list[str] = []
    for path in _scripts():
        source = path.read_text(encoding="utf-8")
        if "disable_env_proxy" not in source:
            continue

        tree = ast.parse(source)
        # 绑定的位置：顶层 import 语句行号
        import_lines = [
            node.lineno
            for node in tree.body
            if isinstance(node, (ast.Import, ast.ImportFrom))
            and any(alias.name == "disable_env_proxy" for alias in node.names)
        ]
        if not import_lines:
            offenders.append(f"{path.relative_to(SCRIPT_DIR)}: 没有 import disable_env_proxy")
            continue

        # 调用的位置：函数定义体里的调用 + 模块级执行的调用
        call_lines: list[int] = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "disable_env_proxy"
            ):
                call_lines.append(node.lineno)
        call_lines.extend(_module_level_call_lines(source))

        earliest_import = min(import_lines)
        late = [line for line in call_lines if line < earliest_import]
        if late:
            offenders.append(
                f"{path.relative_to(SCRIPT_DIR)}: 调用在第 {sorted(late)} 行，"
                f"import 在第 {earliest_import} 行"
            )

    assert offenders == [], (
        "disable_env_proxy 在 import 之前就被调用了（NameError）：\n" + "\n".join(offenders)
    )
