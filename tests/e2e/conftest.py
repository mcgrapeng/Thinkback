"""e2e 共享夹具：mock 模式 vite dev + Playwright（系统 Chrome 优先）。"""

from __future__ import annotations

import os
import socket
import subprocess
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page, sync_playwright

WEB_DIR = Path(__file__).resolve().parents[2] / "web"
AXE_PATH = Path(__file__).resolve().parent / "vendor" / "axe.min.js"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_http(url: str, timeout_seconds: float = 60.0) -> None:
    deadline = time.time() + timeout_seconds
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310
                if response.status == 200:
                    return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(0.5)
    raise RuntimeError(f"frontend did not serve {url} within {timeout_seconds}s: {last_error}")


@pytest.fixture(scope="module")
def frontend_url() -> Iterator[str]:
    port = _free_port()
    env = {**os.environ, "VITE_USE_MOCK": "1"}
    process = subprocess.Popen(
        ["npm", "run", "dev", "--", "--host", "127.0.0.1", "--port", str(port), "--strictPort"],
        cwd=WEB_DIR,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        _wait_http(url)
        yield url
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as playwright:
        try:
            instance = playwright.chromium.launch(channel="chrome", headless=True)
        except Exception:  # noqa: BLE001
            instance = playwright.chromium.launch(headless=True)
        try:
            yield instance
        finally:
            instance.close()


@pytest.fixture()
def page(browser: Browser, frontend_url: str) -> Iterator[Page]:
    context = browser.new_context()
    # 首次访问弹的引导对话框会挡导航，预置完成标记。
    context.add_init_script("localStorage.setItem('thinkback-onboarding-done', '1')")
    page = context.new_page()
    try:
        yield page
    finally:
        context.close()
