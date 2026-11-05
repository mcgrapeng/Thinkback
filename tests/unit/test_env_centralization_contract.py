"""环境变量集中管理的契约：**每个可配置的项都要在 ``.env.example`` 里露面**。

配置分散的后果不是「不好看」，是**运维改不动、也看不见**：

- ``Settings`` 里有 22 个字段不在 ``.env.example`` 里（新增字段时忘了同步）。
  运维读 ``.env.example`` 根本不知道有这些旋钮，想调只能去翻 Python 源码。
- k8s 的 ConfigMap 里写了一个**已经不存在的变量名**
  （``MEMORY_BACKEND_MAX_CONCURRENT_CALLS``，真名是
  ``MEMORY_BACKEND_MAX_CONCURRENT_WRITES`` / ``_READS``）。
  ``Settings`` 是 ``extra="ignore"``，所以这个名字被**静默丢弃** ——
  生产环境想调后端并发，改 ConfigMap 一点用都没有，而且没有任何报错。

所以这里把三条约定钉成契约：

1. ``Settings`` 的每个字段都有对应的 ``.env.example`` 条目（值可以留空，
   但**名字必须在**）—— 名字是 pydantic-settings 的默认映射（字段名大写）。
2. ``.env.example`` 里的每个 ``XXX=`` 都要能被 ``Settings`` 认领 ——
   反向漂移（``.env.example`` 写了服务不认的名字）同样是静默失效。
3. k8s ConfigMap 里的每个变量名要么是 ``Settings`` 字段，要么是**显式**
   属于脚本/工具链的一组（``DATABASE_URL`` 是别名，另有原因）。

``DATABASE_URL`` 是唯一的别名例外：字段本名是 ``raw_database_url``，
``DATABASE_URL`` 通过 ``AliasChoices`` 一起认领（见 ``config.py`` 的 R-0 修复）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from thinkback.infra.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = REPO_ROOT / ".env.example"
K8S_CONFIGMAP = REPO_ROOT / "k8s" / "configmap.yaml"

#: ``.env.example`` 里出现、但不属于服务配置的名字。
#:
#: 全部是**工具链**变量（本地启动脚本 / 前端 / 真实栈脚本），不是服务配置 ——
#: 它们由各自的脚本直接读 ``os.environ``，不经 ``Settings``。
#:
#: 之所以显式列出而不是「允许任意未知名字」：那就是「随便加个名字也不报错」，
#: 正是本文件要防的那类静默失效。
_TOOLCHAIN_VARS = frozenset(
    {
        # 本地启动脚本（script/dev.py）与前端 vite
        "BACKEND_PORT",
        "FRONTEND_PORT",
        "VITE_USE_MOCK",
        "VITE_API_TARGET",
        # MCP 客户端（src/thinkback/mcp/__main__.py 直读 os.environ）
        "THINKBACK_MCP_ALLOW_UNAUTHENTICATED",
        "THINKBACK_MCP_HOST",
        "THINKBACK_MCP_NAMESPACE",
        "THINKBACK_MCP_PORT",
        "THINKBACK_MCP_TRANSPORT",
        "THINKBACK_MCP_USER_ID",
        # 真实栈脚本（tests/script/realchain/）驱动运行中的服务
        "THINKBACK_MEMORY_API_URL",
    }
)

#: ``Settings`` 字段的**别名**（字段名大写之外还能用的名字）。
#:
#: ``DATABASE_URL`` 是云平台通用变量名，字段本名是 ``raw_database_url``，
#: 两者都生效（``config.py`` 的 R-0 修复：此前只有当 pymilvus 的 import
#: 副作用把 ``.env`` 灌进 ``os.environ`` 时才读得到）。
#:
#: ``RAW_DATABASE_URL`` 是字段本名，``AliasChoices`` 把它也算作一个可用的
#: 环境变量名 —— 所以它同样要在「服务认得的名字」这一集合里。
_ALIASES = frozenset({"DATABASE_URL", "RAW_DATABASE_URL"})


def _settings_field_names() -> set[str]:
    """``Settings`` 的字段名（大写形式 = 环境变量名）。

    pydantic-settings 的默认映射是「字段名」→ 环境变量名，且
    ``case_sensitive=False``，所以 ``memory_llm_model`` 对应的环境变量是
    ``MEMORY_LLM_MODEL``。
    """
    return {name.upper() for name in Settings.model_fields}


def _env_example_entries() -> set[str]:
    """``.env.example`` 里出现的所有变量名（含被注释掉的）。"""
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    return set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)\s*=", text, re.MULTILINE))


def _k8s_configmap_entries() -> set[str]:
    """k8s ConfigMap ``data:`` 段里的变量名。"""
    text = K8S_CONFIGMAP.read_text(encoding="utf-8")
    in_data = False
    names: set[str] = set()
    for line in text.splitlines():
        if line.startswith("data:"):
            in_data = True
            continue
        if in_data:
            match = re.match(r"\s{2}([A-Z][A-Z0-9_]+):", line)
            if match:
                names.add(match.group(1))
    return names


def test_the_env_example_file_exists() -> None:
    """``.env.example`` 是「所有环境变量的清单」，它必须在。"""
    assert ENV_EXAMPLE.is_file(), f"{ENV_EXAMPLE} 不存在"


def test_the_env_example_file_is_tracked() -> None:
    """``.env.example`` 必须进版本库 —— 它是清单，不是本地配置。"""
    import subprocess

    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", ".env.example"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, ".env.example 没进版本库"


def test_the_local_env_file_is_never_tracked() -> None:
    """``.env`` 装的是真凭据，**绝不能**进版本库。"""
    import subprocess

    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", ".env"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0, ".env 被跟踪了 —— 凭据会进版本库！"

    ignored = subprocess.run(
        ["git", "check-ignore", ".env"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert ignored.returncode == 0, ".env 没进 .gitignore"


def test_every_settings_field_is_listed_in_the_env_example() -> None:
    """``Settings`` 的每个字段都要在 ``.env.example`` 里露面。

    漏一个 = 运维看不见这个旋钮。本文件写下时漏了 22 个（gRPC 并发、
    限流、L2 摘要、衰减、准入白名单……），全都只能靠翻源码才知道存在。
    """
    listed = _env_example_entries()
    missing = sorted(_settings_field_names() - listed - _ALIASES)
    assert missing == [], f"这些 Settings 字段没进 .env.example（运维看不见、调不了）：{missing}"


def test_the_env_example_only_lists_names_the_service_recognizes() -> None:
    """反向漂移：``.env.example`` 里的名字要么是 ``Settings`` 字段，要么是工具链变量。

    写错名字 = 那个配置**静默不生效**。``.env.example`` 是运维照着填的清单，
    清单上的名字填了没用，比清单上没这个项更糟。
    """
    known = _settings_field_names() | _ALIASES | _TOOLCHAIN_VARS
    unknown = sorted(_env_example_entries() - known)
    assert unknown == [], (
        f".env.example 里的这些名字服务不认（填了静默失效）：{unknown}。"
        f"要么加进 Settings，要么加进 _TOOLCHAIN_VARS（工具链变量）。"
    )


def test_the_k8s_configmap_only_lists_names_the_service_recognizes() -> None:
    """k8s ConfigMap 里的变量名必须被服务认领。

    这条是**生产事故级**的防线：``Settings`` 是 ``extra="ignore"``，
    写错的名字被静默丢弃 —— 运维改了 ConfigMap，重启后配置没变，
    而日志里什么都没有。

    真实踩过：ConfigMap 写的是 ``MEMORY_BACKEND_MAX_CONCURRENT_CALLS``，
    真名是 ``MEMORY_BACKEND_MAX_CONCURRENT_WRITES`` / ``_READS``
    （读写分开两把信号量，见 ``config.py`` 的注释）。当时它碰巧等于
    默认值，所以没暴露 —— 隐患是「将来改了默认值，生产不会跟着变」。
    """
    known = _settings_field_names() | _ALIASES | _TOOLCHAIN_VARS
    unknown = sorted(_k8s_configmap_entries() - known)
    assert unknown == [], f"k8s ConfigMap 里的这些名字服务不认（改了静默失效）：{unknown}"


def test_the_env_example_does_not_carry_real_credentials() -> None:
    """``.env.example`` 里不能有「看起来像真凭据」的值。

    判据：key 类变量必须为空或占位符。写了真值 = 凭据进版本库
    （gitleaks 未必认得出自建端点的 key）。
    """
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    sensitive = (
        "MEMORY_LLM_KEY",
        "MEMORY_EMBEDDING_API_KEY",
        "POSTGRES_PASSWORD",
        "MILVUS_PASSWORD",
        "THINKBACK_API_KEY",
    )
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        for name in sensitive:
            if stripped.startswith(f"{name}="):
                value = stripped.split("=", 1)[1].strip()
                assert value in ("", "<local-postgres-password>"), (
                    f".env.example 的 {name} 有真实值 {value!r} —— 凭据会进版本库"
                )


def test_the_env_example_has_no_internal_hostnames() -> None:
    """``.env.example`` 里不能出现内网/私网地址。

    清单是公开仓库的一部分；内网地址泄漏等于给扫描器送地图。
    允许的是 ``localhost`` 与 RFC-5737 文档用地址段。
    """
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    allowed = ("localhost", "127.0.0.1", "0.0.0.0", "example.internal", "example.com")
    for match in re.finditer(r"https?://([^\s/:\"]+)", text):
        host = match.group(1)
        if any(token in host for token in allowed):
            continue
        # 私网段（RFC 1918）与链路本地地址
        if re.match(r"^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|169\.254\.)", host):
            pytest.fail(f".env.example 里出现私网地址：{host}")


def test_the_local_env_file_mirrors_the_example() -> None:
    """本地 ``.env`` 的变量集合与 ``.env.example`` **一致**。

    ``.env`` 不进版本库，所以它与清单的漂移没人看得见 —— 后果是
    「清单里有这个旋钮，本地却没有」或反过来：
    本地跑得起来，换台机器/换个部署就少一个配置。

    这条只在**本地存在 ``.env``** 时才检查（CI 里没有 ``.env``，
    跳过而不是红 —— CI 没有本地配置可比）。
    """
    local_env = REPO_ROOT / ".env"
    if not local_env.is_file():
        pytest.skip("本地没有 .env（CI 环境），跳过")

    local_vars = set(
        re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)\s*=", local_env.read_text(encoding="utf-8"), re.M)
    )
    example_vars = _env_example_entries()
    assert local_vars == example_vars, (
        f"本地 .env 与 .env.example 不一致："
        f"清单有本地没有 {sorted(example_vars - local_vars)}，"
        f"本地有清单没有 {sorted(local_vars - example_vars)}"
    )
