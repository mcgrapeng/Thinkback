#!/usr/bin/env python3
"""深入测试 README: 截图 + 精确验证关键 section 都可见。"""
import subprocess
import sys
import time
import re
from pathlib import Path
from playwright.sync_api import sync_playwright

REPO = Path("/Users/zhangpeng/workspace/liaohe/Thinkback")
README = REPO / "README.md"
OUT = Path("/tmp/readme_test2")
OUT.mkdir(exist_ok=True, parents=True)

def check_sections_visible(page, expected_h2):
    """用 JS 检查所有期望的 H2 在 DOM 里都存在 + 可见。"""
    result = page.evaluate("""(expected) => {
        const issues = [];
        const all_h2 = Array.from(document.querySelectorAll('h2'));
        const visible_h2 = all_h2.filter(h => {
            const rect = h.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0;
        });
        const h2_texts = visible_h2.map(h => h.textContent.replace(/^#{1,6}\s*/, '').trim());
        for (const want of expected) {
            if (!h2_texts.some(t => t.includes(want.replace(/^#{1,6}\s*/, '').trim()))) {
                issues.push(`Missing or invisible H2: ${want}`);
            }
        }
        return { visible: h2_texts, issues };
    }""", expected_h2)
    return result

def main():
    print("=" * 60)
    print(f"测试 README: {README}")
    print("=" * 60)
    
    md = README.read_text(encoding='utf-8')
    
    # 列出所有 H2 章节
    h2_sections = re.findall(r'^## (.+?)$', md, re.MULTILINE)
    print(f"\n找到 {len(h2_sections)} 个 H2 章节:")
    for s in h2_sections:
        print(f"  - {s}")
    
    # 提取 SVG
    svgs = re.findall(r'```svg\n(.*?)\n```', md, re.DOTALL)
    print(f"\n找到 {len(svgs)} 个 SVG 块")
    
    # 用 Chrome 渲染并检查章节
    GITHUB_CSS = """
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/github-markdown-css/5.5.1/github-markdown.min.css">
<style>
body { background: #fff; max-width: 1200px; margin: 0 auto; padding: 32px;
  font-family: -apple-system, system-ui, sans-serif; line-height: 1.6; color: #1f2328; }
img { max-width: 100%; height: auto; }
pre { background: #f6f8fa; padding: 16px; border-radius: 6px; overflow-x: auto; }
details > summary { cursor: pointer; padding: 8px 0; font-weight: 500; }
details[open] > summary { border-bottom: 1px solid #d0d7de; margin-bottom: 8px; }
h2 { padding-bottom: 0.3em; border-bottom: 1px solid #d0d7de; }
table { border-collapse: collapse; }
th, td { border: 1px solid #d0d7de; padding: 6px 13px; }
svg { max-width: 100%; height: auto; }
</style>
"""
    html = f'<!DOCTYPE html><html><head><meta charset="utf-8">{GITHUB_CSS}</head><body class="markdown-body">{md}</body></html>'
    
    expected_h2 = [
        "At a glance", "Why we built this", "How it works", "How it compares",
        "Why Thinkback", "Memory model", "Capabilities", "Screenshots",
        "Quickstart", "API Reference", "Architecture", "Performance",
        "Roadmap", "Configuration", "Project layout", "Extending",
        "Development", "Documentation", "Community", "FAQ", "Contributing",
        "Featured by", "Sponsors", "License", "Star History", "Contributors",
        "Acknowledgments",
    ]
    
    print(f"\n期望 {len(expected_h2)} 个 H2 全部可见...")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            headless=True,
            args=["--no-sandbox", "--disable-gpu"],
        )
        page = browser.new_page()
        page.set_viewport_size({"width": 1280, "height": 900})
        page.set_content(html, wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)
        
        # 检查所有期望的 H2 都可见
        result = check_sections_visible(page, expected_h2)
        print(f"\n实际可见的 H2 ({len(result['visible'])}):")
        for v in result['visible']:
            print(f"  ✓ {v}")
        
        print(f"\n问题:")
        if result['issues']:
            for i in result['issues']:
                print(f"  ! {i}")
        else:
            print(f"  无!所有期望的章节都可见")
        
        # 检查 SVG 渲染
        svg_count = page.evaluate("() => document.querySelectorAll('svg').length")
        print(f"\nDOM 中 <svg> 元素数: {svg_count}")
        
        # 截图(全页)
        page.screenshot(path=str(OUT / "full.png"), full_page=True)
        page.screenshot(path=str(OUT / "viewport.png"))  # 首屏
        browser.close()
    
    # SVG 单独渲染测试
    print(f"\n单独渲染 SVG:")
    import cairosvg
    for i, svg in enumerate(svgs, 1):
        out_png = OUT / f"svg_{i:02d}.png"
        try:
            cairosvg.svg2png(bytestring=svg.encode('utf-8'), write_to=str(out_png), output_width=1200)
            print(f"  ✓ SVG {i}: {out_png.stat().st_size:,} bytes")
        except Exception as e:
            print(f"  ✗ SVG {i}: {e}")
    
    # Markdown 结构检查
    print(f"\nMarkdown 结构检查:")
    fence_count = len(re.findall(r'```', md))
    print(f"  ``` fence 总数: {fence_count} (应为偶数)")
    
    # 检查 fenced code block 后直接接 </details> 的坏模式
    bad_pattern = len(re.findall(r'```[^\n]*\n[ \t]*</details>', md))
    print(f"  'closing ``` followed by </details>' 模式: {bad_pattern} (应为 0)")
    
    # 计数 SVG 块
    print(f"  ```svg ... ``` 块: {len(svgs)}")
    
    return result['issues']

if __name__ == "__main__":
    issues = main()
    sys.exit(1 if issues else 0)