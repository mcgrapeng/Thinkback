// 用 puppeteer-core + Chrome 截图
import puppeteer from "puppeteer-core";
import { writeFile } from "node:fs/promises";

const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const BASE = "http://localhost:7001";
const OUT = "/Users/zhangpeng/workspace/liaohe/Thinkback/assets/screenshots";

const SHOTS = [
  // 总览 - viewport (above the fold)
  { name: "overview-hero.png", path: "/", viewport: [1440, 900], fullPage: false },
  // 总览 - full
  { name: "overview-full.png", path: "/", viewport: [1440, 900], fullPage: true },
  // 记忆浏览器
  { name: "memories-list.png", path: "/memories", viewport: [1440, 900], fullPage: false },
  { name: "memories-full.png", path: "/memories", viewport: [1440, 900], fullPage: true },
  // 任务
  { name: "tasks-list.png", path: "/tasks", viewport: [1440, 900], fullPage: false },
  { name: "tasks-full.png", path: "/tasks", viewport: [1440, 900], fullPage: true },
  // 治理
  { name: "govern.png", path: "/govern", viewport: [1440, 900], fullPage: false },
  { name: "govern-full.png", path: "/govern", viewport: [1440, 900], fullPage: true },
  // 审计
  { name: "audit.png", path: "/audit", viewport: [1440, 900], fullPage: false },
  { name: "audit-full.png", path: "/audit", viewport: [1440, 900], fullPage: true },
  // 配置
  { name: "config.png", path: "/config", viewport: [1440, 900], fullPage: false },
  { name: "config-full.png", path: "/config", viewport: [1440, 900], fullPage: true },
  // 集成
  { name: "integration-keys.png", path: "/integration", viewport: [1440, 900], fullPage: false },
  { name: "integration-prompts.png", path: "/integration", viewport: [1440, 900], fullPage: true },
  // 移动端
  { name: "overview-mobile.png", path: "/", viewport: [414, 900], fullPage: false },
  { name: "overview-mobile-full.png", path: "/", viewport: [414, 900], fullPage: true },
];

const browser = await puppeteer.launch({
  executablePath: CHROME,
  headless: "new",
  args: [
    "--no-sandbox",
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--hide-scrollbars",
    "--font-render-hinting=none",
  ],
});

const results = [];
for (const shot of SHOTS) {
  const page = await browser.newPage();
  await page.setViewport({
    width: shot.viewport[0],
    height: shot.viewport[1],
    deviceScaleFactor: 2, // 高清
  });

  try {
    await page.goto(`${BASE}${shot.path}`, {
      waitUntil: "networkidle2",
      timeout: 15000,
    });
  } catch (e) {
    // networkidle2 经常超时(因为有 30s 轮询),回退到 domcontentloaded
    await page.goto(`${BASE}${shot.path}`, {
      waitUntil: "domcontentloaded",
      timeout: 15000,
    });
  }

  // 等数据加载(API 解析)
  await new Promise((r) => setTimeout(r, 1500));

  // 关闭 onboarding dialog 如果存在
  try {
    const skipBtn = await page.$('button:has-text("跳过")');
    if (skipBtn) await skipBtn.click();
  } catch {}
  try {
    const closeBtn = await page.$('button[aria-label="关闭引导"]');
    if (closeBtn) await closeBtn.click();
  } catch {}
  await new Promise((r) => setTimeout(r, 400));

  // 等入场动画(stagger 0-400ms)
  await new Promise((r) => setTimeout(r, 1200));

  const file = `${OUT}/${shot.name}`;
  await page.screenshot({
    path: file,
    fullPage: shot.fullPage,
    type: "png",
  });

  const fs = await import("node:fs");
  const size = fs.statSync(file).size;
  results.push({ name: shot.name, ok: true, size });
  console.log(`  ✓ ${shot.name} (${(size / 1024).toFixed(1)} KB)`);
  await page.close();
}

await browser.close();
console.log(`\n=== Done: ${results.length} screenshots ===`);
results.forEach((r) => console.log(`  ${r.ok ? "✓" : "✗"} ${r.name}`));
