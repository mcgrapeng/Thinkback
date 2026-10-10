#!/usr/bin/env node
// README 全面浏览器审计 — 渲染 + 截图 + 问题检测
const puppeteer = require("puppeteer-core");
const fs = require("fs");
const path = require("path");

const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const BASE = "/Users/zhangpeng/workspace/liaohe/Thinkback";
const OUT = "/tmp/readme_audit";

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const md = fs.readFileSync(path.join(BASE, "README.md"), "utf-8");
  
  // 用 GitHub 风格 CSS 渲染
  const html = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/github-markdown-css/5.5.1/github-markdown.min.css">
<style>
body { background: #fff; max-width: 980px; margin: 0 auto; padding: 32px 16px;
  font-family: -apple-system, system-ui, sans-serif; line-height: 1.6; color: #1f2328; }
.markdown-body img { max-width: 100%; height: auto; border-radius: 6px; }
.markdown-body h1 { padding-bottom: 0.3em; border-bottom: 1px solid #d0d7de; }
.markdown-body h2 { padding-bottom: 0.3em; border-bottom: 1px solid #d0d7de; }
.markdown-body table { border-collapse: collapse; width: 100%; }
.markdown-body th, .markdown-body td { border: 1px solid #d0d7de; padding: 6px 13px; }
.markdown-body pre { background: #f6f8fa; padding: 16px; border-radius: 6px; overflow-x: auto; }
.markdown-body code { background: rgba(175,184,193,0.2); padding: 0.2em 0.4em; border-radius: 3px; font-size: 85%; }
.markdown-body pre code { background: transparent; padding: 0; }
details > summary { cursor: pointer; padding: 8px 0; font-weight: 500; }
details[open] > summary { border-bottom: 1px solid #d0d7de; margin-bottom: 8px; }
img { max-width: 100%; }
img[alt*="banner"], img[alt*="Banner"] { width: 100%; }
svg { max-width: 100%; height: auto; }
</style>
</head>
<body class="markdown-body">
${md.replace(/src="assets\//g, `src="file://${BASE}/assets/`)}
</body>
</html>`;

  const browser = await puppeteer.launch({
    executablePath: CHROME,
    headless: "new",
    args: ["--no-sandbox", "--disable-gpu", "--hide-scrollbars", "--allow-file-access-from-files"],
  });

  const page = await browser.newPage();
  await page.setViewport({ width: 1200, height: 900, deviceScaleFactor: 1 });
  await page.setContent(html, { waitUntil: "networkidle0", timeout: 30000 });
  await new Promise(r => setTimeout(r, 3000));

  // === 1. 检测图片加载 ===
  const imgIssues = await page.evaluate(() => {
    const issues = [];
    document.querySelectorAll("img").forEach(img => {
      if (!img.complete || img.naturalWidth === 0) {
        issues.push({ type: "broken-image", src: img.src.slice(0, 100), alt: img.alt?.slice(0, 50) });
      }
    });
    return issues;
  });

  // === 2. 检测 SVG 渲染(inline) ===
  const svgIssues = await page.evaluate(() => {
    const issues = [];
    document.querySelectorAll("pre > code").forEach(code => {
      const text = code.textContent || "";
      if (text.includes("<svg") || text.includes("<?xml")) {
        const parent = code.parentElement;
        const rendered = parent.querySelector("svg");
        if (!rendered) {
          issues.push({ type: "svg-not-rendered", preview: text.slice(0, 60) });
        }
      }
    });
    return issues;
  });

  // === 3. 检测 mermaid ===
  const mermaidIssues = await page.evaluate(() => {
    const issues = [];
    document.querySelectorAll("pre > code").forEach(code => {
      const text = code.textContent || "";
      if (text.includes("flowchart") || text.includes("graph LR")) {
        const parent = code.parentElement;
        const rendered = parent.querySelector("svg");
        if (!rendered) {
          issues.push({ type: "mermaid-not-rendered", preview: text.slice(0, 60) });
        }
      }
    });
    return issues;
  });

  // === 4. 检测死链 ===
  const linkIssues = await page.evaluate(() => {
    const issues = [];
    document.querySelectorAll("a[href]").forEach(a => {
      const href = a.getAttribute("href") || "";
      if (href.startsWith("#")) {
        // 内部锚点
        const id = href.slice(1);
        const target = document.getElementById(id) || document.querySelector(`a[name="${id}"]`);
        if (!target) {
          issues.push({ type: "dead-anchor", href, text: a.textContent.slice(0, 30) });
        }
      } else if (href.startsWith("assets/") || href.includes(".github/ISSUE_TEMPLATE") || href.includes(".github/CONTRIBUTING")) {
        // 相对链接 — 检查文件是否存在
        const p = path.join(BASE, href.replace(/^\.\.\//, "").replace(/^\.\//, ""));
        if (!fs.existsSync(p)) {
          issues.push({ type: "dead-relative-link", href, text: a.textContent.slice(0, 30) });
        }
      }
    });
    return issues;
  });

  // === 5. 检测重复章节 ===
  const headingIssues = await page.evaluate(() => {
    const issues = [];
    const headings = [];
    document.querySelectorAll("h2").forEach(h => {
      const t = (h.textContent || "").trim();
      if (headings.includes(t)) {
        issues.push({ type: "duplicate-h2", heading: t });
      }
      headings.push(t);
    });
    return issues;
  });

  // === 6. 检测空章节 ===
  const emptyIssues = await page.evaluate(() => {
    const issues = [];
    const allH2 = Array.from(document.querySelectorAll("h2"));
    for (let i = 0; i < allH2.length; i++) {
      const next = allH2[i + 1] || document.body.lastElementChild;
      const start = allH2[i];
      // 找到下一个 h2 或 body 末尾之间的内容
      let content = "";
      let node = start.nextElementSibling;
      while (node && node.tagName !== "H2") {
        content += (node.textContent || "").trim();
        node = node.nextElementSibling;
      }
      if (content.length < 10) {
        issues.push({ type: "empty-section", heading: start.textContent.slice(0, 50), contentLen: content.length });
      }
    }
    return issues;
  });

  // === 7. 检测 TOC 完整性 ===
  const tocIssues = await page.evaluate(() => {
    const issues = [];
    // 找 TOC 表格里的链接
    const tocTable = Array.from(document.querySelectorAll("table")).find(t =>
      (t.textContent || "").includes("What you'll find") || (t.textContent || "").includes("内容")
    );
    if (tocTable) {
      const tocLinks = Array.from(tocTable.querySelectorAll("a[href^='#']")).map(a => (a.textContent || "").trim());
      // 所有 h2
      const allH2 = Array.from(document.querySelectorAll("h2")).map(h => (h.textContent || "").trim());
      // 检查哪些 h2 不在 TOC 里
      for (const h of allH2) {
        if (!tocLinks.some(toc => h.toLowerCase().includes(toc.toLowerCase()) || toc.toLowerCase().includes(h.toLowerCase()))) {
          issues.push({ type: "toc-missing-section", heading: h });
        }
      }
    }
    return issues;
  });

  // === 8. 截图(分 3 段) ===
  const totalH = await page.evaluate(() => document.body.scrollHeight);
  console.log("README total height:", totalH, "px");
  for (let i = 0; i < 3; i++) {
    const y = Math.floor(totalH * i / 3);
    await page.evaluate(yy => window.scrollTo(0, yy), y);
    await new Promise(r => setTimeout(r, 300));
    await page.screenshot({ path: path.join(OUT, `readme_seg_${i + 1}.png`), clip: { x: 0, y: 0, width: 1200, height: 900 } });
  }

  // === 汇总 ===
  const allIssues = [
    ...imgIssues, ...svgIssues, ...mermaidIssues,
    ...linkIssues, ...headingIssues, ...emptyIssues, ...tocIssues,
  ];
  
  console.log("\n=== README AUDIT ===");
  console.log("images broken:", imgIssues.length);
  console.log("SVG not rendered:", svgIssues.length);
  console.log("mermaid not rendered:", mermaidIssues.length);
  console.log("dead links/anchors:", linkIssues.length);
  console.log("duplicate H2:", headingIssues.length);
  console.log("empty sections:", emptyIssues.length);
  console.log("TOC gaps:", tocIssues.length);
  console.log("\n--- ALL ISSUES ---");
  allIssues.forEach(i => console.log(JSON.stringify(i)));

  await browser.close();
})();
