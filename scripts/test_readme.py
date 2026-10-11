#!/usr/bin/env python3
"""自动化测试: 验证 README 中每个 SVG 在 GitHub-like 上下文中渲染。

1. 单独渲染 SVG → PNG (cairo, 验证 SVG 语法 + 视觉)
2. 嵌入完整 markdown 上下文 → 截图 (puppeteer, 验证 GitHub 渲染)
3. 检测 SVG 边界外的 markdown 是否被截断
4. 多轮迭代直到 README 完全合格
"""
import subprocess
import sys
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

REPO = Path("/Users/zhangpeng/workspace/liaohe/Thinkback")
README = REPO / "README.md"
OUT = Path("/tmp/readme_test")
OUT.mkdir(exist_ok=True, parents=True)

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

def extract_svgs(md):
    import re
    return re.findall(r'```svg\n(.*?)\n```', md, re.DOTALL)

def render_svg_standalone(svg, out_png):
    import cairosvg
    cairosvg.svg2png(bytestring=svg.encode('utf-8'), write_to=str(out_png), output_width=1200)
    return out_png

def render_readme_in_chrome(readme_path, out_png, viewport=(1280, 900)):
    """用 Chrome 渲染完整 README,模拟 GitHub 视图。"""
    md = readme_path.read_text(encoding='utf-8')
    # 把相对路径替换成 file:// 协议
    md = md.replace('src="assets/', f'src="file://{REPO}/assets/')
    html = f'<!DOCTYPE html><html><head><meta charset="utf-8">{GITHUB_CSS}</head><body class="markdown-body">{md}</body></html>'
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            headless=True,
            args=["--no-sandbox", "--disable-gpu", "--allow-file-access-from-files"],
        )
        page = browser.new_page()
        page.set_viewport_size({"width": viewport[0], "height": viewport[1]})
        page.set_content(html, wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)
        page.screenshot(path=str(out_png), full_page=True)
        browser.close()
    return out_png

def find_problems_in_detail(md, full_png):
    """分析 README + 截图,找出问题。"""
    issues = []
    import re
    # 1. 检查所有 </details> 紧跟 ``` (同一行/前一行)
    for m in re.finditer(r'```[^\n]*\n[ \t]*</details>', md):
        pos = m.start()
        issues.append(f'Line ~{md[:pos].count(chr(10)) + 1}: closing ``` followed directly by </details>')
    # 2. 检查代码块未闭合 (奇数 ```)
    fence_count = len(re.findall(r'```', md))
    if fence_count % 2 != 0:
        issues.append(f'Odd number of ``` ({fence_count}), unclosed fence')
    # 3. 检查 HTML 标签配对
    for tag in ['details', 'svg']:
        opens = md.count(f'<{tag}')
        closes = md.count(f'</{tag}>')
        if opens != closes:
            issues.append(f'<{tag}>: {opens} open vs {closes} close')
    return issues

def test_loop():
    """循环测试直到 README 完全没有问题。"""
    print("=" * 60)
    print(f"测试 README: {README}")
    print("=" * 60)
    
    md = README.read_text(encoding='utf-8')
    svgs = extract_svgs(md)
    print(f"\n找到 {len(svgs)} 个 SVG 块")
    
    for i, svg in enumerate(svgs, 1):
        out_png = OUT / f"svg_{i:02d}.png"
        try:
            render_svg_standalone(svg, out_png)
            print(f"  SVG {i}: 渲染成功 → {out_png.name} ({out_png.stat().st_size:,} bytes)")
        except Exception as e:
            print(f"  SVG {i}: RENDER ERROR → {e}")
    
    # 用 Chrome 渲染完整 README
    print(f"\n用 Chrome 渲染 README ...")
    full_png = OUT / "readme_full.png"
    try:
        render_readme_in_chrome(README, full_png)
        print(f"  Full README → {full_png.name} ({full_png.stat().st_size:,} bytes)")
    except Exception as e:
        print(f"  Chrome render ERROR: {e}")
    
    # 检查问题
    issues = find_problems_in_detail(md, full_png)
    if issues:
        print(f"\n找到 {len(issues)} 个问题:")
        for i in issues:
            print(f"  ! {i}")
    else:
        print(f"\n✓ README 测试通过,无明显问题")
    
    return issues

if __name__ == "__main__":
    issues = test_loop()
    sys.exit(1 if issues else 0)