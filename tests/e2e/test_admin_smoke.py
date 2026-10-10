"""管理台端到端冒烟（mock API 模式）。

用 venv 自带的 Playwright 驱动本机 Chrome（无 Chrome 时回退 Playwright
自带 Chromium），对 ``VITE_USE_MOCK=1`` 的 vite dev 走通关键页面渲染与导航。
守护对象是 UI 集成：路由、组件树、API 客户端路径接线；真实链路
（真 PG / 真 Milvus / 真 LLM）由 ``tests/script/realchain`` 覆盖，
确定性套件里不引入外部服务。

运行：``make test-e2e``（自动起 vite、装了 Chrome 即免下载浏览器）。
"""

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


def test_overview_renders(page: Page, frontend_url: str) -> None:
    page.goto(frontend_url)
    heading = page.get_by_role("heading", name="总览").first
    heading.wait_for(timeout=15_000)
    assert heading.is_visible()


def test_navigation_walk_memories_and_integration(page: Page, frontend_url: str) -> None:
    page.goto(frontend_url)

    page.get_by_role("link", name="记忆浏览器").click()
    page.get_by_role("heading", name="MEMORY BROWSER · 记忆浏览器").first.wait_for(timeout=15_000)
    assert "/memories" in page.url

    page.get_by_role("link", name="接入协议").click()
    page.get_by_text("API Keys").first.wait_for(timeout=15_000)
    assert "/integration" in page.url
