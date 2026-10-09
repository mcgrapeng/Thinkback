// 交互态截图 — 简化版,带超时保护
import puppeteer from "puppeteer-core";

const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const BASE = "http://localhost:7001";
const OUT = "/Users/zhangpeng/workspace/liaohe/Thinkback/assets/screenshots";

const browser = await puppeteer.launch({
  executablePath: CHROME,
  headless: "new",
  args: ["--no-sandbox", "--disable-gpu", "--hide-scrollbars"],
});

async function safe(fn, timeout = 5000) {
  return Promise.race([
    fn(),
    new Promise((_, rej) => setTimeout(() => rej(new Error("timeout")), timeout)),
  ]);
}

async function go() {
  const page = await browser.newPage();
  page.setDefaultNavigationTimeout(8000);
  await page.setViewport({ width: 1440, height: 900, deviceScaleFactor: 2 });
  await page.goto(`${BASE}/memories`, { waitUntil: "domcontentloaded" });
  await new Promise((r) => setTimeout(r, 1500));
  // 关 onboarding
  try {
    const skip = await page.$('button:has-text("跳过")');
    if (skip) await skip.click();
  } catch {}
  await new Promise((r) => setTimeout(r, 300));
  // 试点击第一行
  try {
    await page.locator('li[role="button"]').first().click({ timeout: 2000 });
    await new Promise((r) => setTimeout(r, 800));
  } catch {}
  await page.screenshot({ path: `${OUT}/memories-detail.png`, fullPage: false });
  console.log("✓ memories-detail.png");
  await page.close();
}

try {
  await safe(go, 25000);
} catch (e) {
  console.log("interaction timeout:", e.message);
}

await browser.close();
console.log("done");
