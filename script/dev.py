"""本地开发启动器：后端 7002 / 前端 7001 起步，被占用自动顺延到下一空闲端口。

用法：
    uv run python script/dev.py           # 默认 7002 后端 + 7001 前端
    BACKEND_PORT=8000 FRONTEND_PORT=5173 uv run python script/dev.py

注意：TCP 端口上限 65535（70001/70002 这类五位数非法，启动器会直接报错）。

行为：
- 后端：从 BACKEND_PORT（默认 70002）起逐个探测，取第一个空闲端口，
  以 uvicorn 启动（lifespan 自动跑迁移 / Milvus lazy-create / 孤儿任务回收）。
- 前端：vite dev（FRONTEND_PORT 默认 70001）；vite 原生行为即端口占用自动 +1，
  实际端口以 vite 输出为准；代理目标自动指向本次后端实际端口。
- Ctrl+C 同时回收两个子进程。
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def free_port(start: int, tries: int = 20) -> int:
    """从 start 起找第一个未被监听的端口（含 start 本身）。"""
    if not 1 <= start <= 65535:
        raise SystemExit(f"invalid port {start}: TCP 端口范围是 1-65535")
    for port in range(start, min(start + tries, 65536)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise SystemExit(f"no free port in [{start}, {start + tries})")


def wait_port(port: int, timeout_seconds: float = 45.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.5)
    return False


def main() -> None:
    backend_port = free_port(int(os.environ.get("BACKEND_PORT", "7002")))
    frontend_port = int(os.environ.get("FRONTEND_PORT", "7001"))

    procs: list[subprocess.Popen] = []

    def shutdown(*_: object) -> None:
        for proc in procs:
            proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    backend = subprocess.Popen(
        [
            "uv",
            "run",
            "uvicorn",
            "thinkback.api.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(backend_port),
        ],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": "src"},
    )
    procs.append(backend)

    if not wait_port(backend_port):
        print(f"[dev] backend failed to listen on :{backend_port}, see uvicorn output", flush=True)
        shutdown()

    frontend = subprocess.Popen(
        ["npm", "run", "dev", "--", "--port", str(frontend_port)],
        cwd=ROOT / "web",
        env={**os.environ, "VITE_API_TARGET": f"http://127.0.0.1:{backend_port}"},
    )
    procs.append(frontend)

    print(
        f"\n[dev] 后端  http://127.0.0.1:{backend_port}  (docs: /docs, admin: /admin/api)\n"
        f"[dev] 前端  http://localhost:{frontend_port} 起步（如被占用 vite 自动顺延，以 vite 输出为准）\n",
        flush=True,
    )

    try:
        while True:
            for proc in procs:
                if proc.poll() is not None:
                    shutdown()
            time.sleep(1)
    except KeyboardInterrupt:
        shutdown()


if __name__ == "__main__":
    main()
