"""网络环境策略：内部端点与本机服务不该走机器上的 HTTP/SOCKS 代理。

两个具体危害：

- **劫持直连**：httpx / urllib / grpc 默认读 ``http_proxy`` 环境变量，机器上
  配了个人代理时，对 127.0.0.1 与内网网关的直连会被送进代理（表现为
  UNAVAILABLE，而不是「连不上」这种能指错方向的报错）。
- **构造期就炸**：SOCKS 代理要求装 ``socksio``，缺了的话客户端在
  **构造阶段**就抛 ImportError。预热只记一条 warning，用户看到的现象是
  「抽取一直为空、召回 0 条」，日志里没有任何指向代理的线索。

这个坑踩过一次：``gate_real.py`` 自己清了代理环境变量，所以真实栈门禁一路
绿；而同一台机器上正常启动的服务，抽取链整个是断的。

所以统一在这里：**每个碰真实栈的脚本启动时都显式声明一次**，别再依赖
「记得清环境变量」。客户端层（mem0 / embedder / LLM）另有 ``trust_env=False``
的兜底，两处都要：脚本层管脚本自己的 HTTP 调用，客户端层管后端调用。
"""

from __future__ import annotations

import os

#: 这些变量会让 httpx / urllib / grpc 走代理。
PROXY_ENV_KEYS: tuple[str, ...] = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


def disable_env_proxy() -> None:
    """关掉环境代理，并让 ``NO_PROXY=*`` 兜住只认 no_proxy 的客户端。

    在 ``load_dotenv()`` **之后**调用：``.env`` 里也可能带代理设置。
    """
    for key in PROXY_ENV_KEYS:
        os.environ.pop(key, None)
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"
