"""管理台端到端冒烟（mock API 模式）。

用 venv 自带的 Playwright 驱动本机 Chrome（无 Chrome 时回退 Playwright
自带 Chromium），对 ``VITE_USE_MOCK=1`` 的 vite dev 走通关键页面渲染与导航。
守护对象是 UI 集成：路由、组件树、API 客户端路径接线；真实链路
（真 PG / 真 Milvus / 真 LLM）由 ``tests/script/realchain`` 覆盖，
确定性套件里不引入外部服务。

运行：``make test-e2e``（自动起 vite、装了 Chrome 即免下载浏览器）。
"""

from __future__ import annotations

from playwright.sync_api import Page


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


def test_memories_journey_lists_mock_rows(page: Page, frontend_url: str) -> None:
    """记忆浏览器旅程：进入列表 → mock 数据渲染出行。"""
    page.goto(frontend_url + "/memories")
    page.get_by_role("heading", name="MEMORY BROWSER · 记忆浏览器").first.wait_for(timeout=15_000)
    # 列表有内容（mock MEMORIES 至少一行），空态标题不应出现
    page.wait_for_timeout(800)
    assert page.get_by_text("暂无记忆").count() == 0


def test_tasks_journey_renders_status(page: Page, frontend_url: str) -> None:
    """任务监控旅程：进入页面 → 状态徽章渲染（mock 任务数据）。"""
    page.goto(frontend_url + "/tasks")
    page.wait_for_timeout(800)
    assert page.get_by_text("任务").first.is_visible()
    assert page.locator("[data-slot=badge]").count() > 0


def test_govern_journey_renders(page: Page, frontend_url: str) -> None:
    """治理操作旅程：进入页面 → 操作区渲染。"""
    page.goto(frontend_url + "/govern")
    page.wait_for_timeout(500)
    assert "/govern" in page.url
    assert page.locator("button").count() > 0
