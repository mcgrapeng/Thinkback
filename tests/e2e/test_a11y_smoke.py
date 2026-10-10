"""a11y 冒烟：axe-core 扫描核心页面，严重/致命违规为零。

axe v4.10.3（tests/e2e/vendor/axe.min.js）注入页面本地执行，无网络。
口径只卡 serious/critical（moderate/minor 列 backlog），对应 WCAG 2.1 AA。
"""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import Page

AXE_PATH = Path(__file__).resolve().parent / "vendor" / "axe.min.js"

_CORE_PAGES = ("/", "/memories", "/integration")


def test_a11y_no_serious_violations(page: Page, frontend_url: str) -> None:
    offenders: dict[str, list[str]] = {}
    for path in _CORE_PAGES:
        page.goto(frontend_url + path)
        page.wait_for_timeout(500)
        # 入场淡入动画中途的瞬时透明度会拉低计算对比度；扫描前禁动画。
        page.add_style_tag(content="*{animation:none!important;transition:none!important}")
        page.add_script_tag(path=str(AXE_PATH))
        results = page.evaluate("async () => (await axe.run(document)).violations")
        serious = [
            violation["id"]
            for violation in results
            if violation.get("impact") in ("serious", "critical")
        ]
        if serious:
            offenders[path] = serious

    assert offenders == {}, f"serious/critical a11y violations: {offenders}"
