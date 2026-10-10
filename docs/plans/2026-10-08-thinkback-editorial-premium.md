# Thinkback Editorial Premium Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 Thinkback 治理台从标准 admin dashboard 升级到 Stripe Press 风格的 Editorial Premium 体验(全局 design system + 总览 + 记忆浏览器两页示范)

**Architecture:** 在 appica 现有 18 token + 6 状态色哲学基础上叠加暖白底色 + 暖纸面阴影 + 大字号 editorial 节奏,不动后端契约、不动 API、不动 4 个非示范页结构、不动 dark 模式视觉(保留 token)。

**Tech Stack:** React 19 + Vite 6 + Tailwind CSS 4 + Radix UI + Geist Sans/Mono + lucide-react + sonner + TanStack Router/Query

**Spec:** `docs/specs/2026-10-08-thinkback-editorial-premium-design.md`

## Global Constraints

- **不动后端契约 / API / mock 数据**
- **不动 4 个非示范页**(任务/治理/审计/配置)内部结构 — 它们自动继承新 token
- **仅做 light 模式视觉**,dark 模式 token 保留但本次不动
- **保留**:`tabular-nums`、`focus-visible`、`prefers-reduced-motion`、所有 a11y 实践
- **保留**:`react-error-boundary`、`useCountUp`、query 缓存策略
- **dev server**: `cd web && VITE_USE_MOCK=1 FRONTEND_PORT=7002 npm run dev`(已有进程 pid 74981,如需重启先 `kill 74981`)
- **验证命令**: `cd web && npx tsc --noEmit`(类型检查),`npm run build`(完整构建),chrome-devtools 截图
- **commit 约定**: 完成后立即 `git add <files> && git commit -m "<message>"`,message 描述改动而非文件清单

---

## Phase 1 — Design System 升级 (基础)

### Task 1: index.css — 暖色背景/边框/阴影 token 升级

**Files:**
- Modify: `web/src/index.css:15-95`(两个 `:root` + `.dark` 块)、`:152-158`(字体/阴影)、`@theme inline`(97-150)、`@layer utilities`(193+)

**Step 1.1: 修改 `:root` 暖色背景系**

把:
```css
--background: #ffffff;
--background-subtle: #f9fafb;
--background-muted: #f3f4f6;
--background-strong: #e5e7eb;
```
改为:
```css
--background: #fdfcf8;        /* 暖白主底 */
--background-subtle: #f8f6f0;  /* 暖米黄副底 */
--background-muted: #f5f2ec;   /* 卡片 hover / 装饰 */
--background-strong: #ebe7df; /* 暖棕灰分隔线 */
```

**Step 1.2: 修改 `--border` 系列为暖色**

把:
```css
--border-muted: #f3f4f6;
--border: #e5e7eb;
--border-strong: #d1d5dc;
--border-emphasis: #99a1af;
--border-intense: #6a7282;
```
改为:
```css
--border-muted: #f5f2ec;
--border: #ebe7df;
--border-strong: #d6cfbe;
--border-emphasis: #99a1af;
--border-intense: #6a7282;
```

**Step 1.3: 修改 `--card` 改为纯白(浮于暖白底制造对比)**

```css
--card: #ffffff;  /* 保留 */
--card-foreground: #101828; /* 保留 */
```

**Step 1.4: 阴影色调由冷蓝改为暖棕,并新增 `lg`**

把:
```css
--shadow-xs: 0 1px 2px rgb(16 24 40 / 0.05);
--shadow-sm: 0 2px 8px -2px rgb(16 24 40 / 0.08);
--shadow-md: 0 4px 12px -2px rgb(16 24 40 / 0.1);
```
改为:
```css
--shadow-xs: 0 1px 2px rgb(60 41 17 / 0.05);
--shadow-sm: 0 2px 12px -2px rgb(60 41 17 / 0.06);
--shadow-md: 0 6px 20px -4px rgb(60 41 17 / 0.08);
--shadow-lg: 0 12px 32px -8px rgb(60 41 17 / 0.12); /* 新增,hero 卡 */
```

**Step 1.5: `.dark` 块保持不变**(本次不动 dark 视觉)

**Step 1.6: 验证 build**

```bash
cd /Users/zhangpeng/workspace/liaohe/Thinkback/web && npx tsc --noEmit
```
期望:`TypeScript: No errors found`

**Step 1.7: Commit**

```bash
cd /Users/zhangpeng/workspace/liaohe/Thinkback && git add web/src/index.css && git commit -m "style: 升级 token 至 Editorial Premium 暖色基底(背景/边框/阴影)"
```

---

### Task 2: index.css — 新增 editorial typography / layout / 动效 utility

**Files:**
- Modify: `web/src/index.css`(在 `@layer utilities` 块内 `@keyframes fade-up` 之前新增)

**Step 2.1: 新增 `text-display-*` 类(超大数字 mono)**

```css
/* Display: 96px / 72px / 48px 数字锚点(Geist Mono) */
.text-display-xl {
  font-family: var(--font-mono);
  font-size: 6rem;
  line-height: 1;
  letter-spacing: -0.04em;
  font-weight: 500;
  font-variant-numeric: tabular-nums;
}
.text-display-lg {
  font-family: var(--font-mono);
  font-size: 4.5rem;
  line-height: 1;
  letter-spacing: -0.03em;
  font-weight: 500;
  font-variant-numeric: tabular-nums;
}
.text-display-md {
  font-family: var(--font-mono);
  font-size: 3rem;
  line-height: 1;
  letter-spacing: -0.02em;
  font-weight: 500;
  font-variant-numeric: tabular-nums;
}
```

**Step 2.2: 新增 editorial 装饰与节奏 utility**

```css
/* Editorial 装饰 */
.editorial-rule {
  border-top: 1px solid #e8e2d4;
}
.section-label {
  font-size: 0.75rem;
  font-weight: 500;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--foreground-muted);
}
.kpi-tile-hover {
  transition: transform 200ms cubic-bezier(0.16, 1, 0.3, 1),
              box-shadow 200ms cubic-bezier(0.16, 1, 0.3, 1);
}
.kpi-tile-hover:hover {
  transform: translateY(-2px);
  box-shadow: var(--shadow-md);
}
.row-hover-warm:hover {
  background-color: #fdf8ed;
  border-left-color: var(--warning-strong);
}
```

**Step 2.3: 替换/升级 `animate-fade-up` 为 `animate-editorial-fade-up`**

把:
```css
.animate-fade-up {
  animation: fade-up 360ms cubic-bezier(0.16, 1, 0.3, 1) both;
}
```
替换为(保留旧名为兼容):
```css
.animate-editorial-fade-up {
  animation: fade-up 640ms cubic-bezier(0.16, 1, 0.3, 1) both;
}
.animate-fade-up {
  animation: fade-up 360ms cubic-bezier(0.16, 1, 0.3, 1) both;
}
```

并在 `@keyframes fade-up` 里追加 `scale`:
```css
@keyframes fade-up {
  from {
    opacity: 0;
    transform: translateY(8px) scale(0.98);
  }
  to {
    opacity: 1;
    transform: translateY(0) scale(1);
  }
}
```

**Step 2.4: 升级 body 字号 14→15px / line-height 1.5→1.6**

在 `@layer base` 的 `body` 块:
```css
body {
  background-color: var(--background);
  color: var(--foreground);
  font-family: var(--font-sans);
  font-size: 1rem;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
}
```
改为:
```css
body {
  background-color: var(--background);
  color: var(--foreground);
  font-family: var(--font-sans);
  font-size: 0.9375rem; /* 15px,editorial rhythm */
  line-height: 1.6;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}
```

**Step 2.5: 验证 build**

```bash
cd /Users/zhangpeng/workspace/liaohe/Thinkback/web && npx tsc --noEmit
```
期望:`TypeScript: No errors found`

**Step 2.6: Commit**

```bash
cd /Users/zhangpeng/workspace/liaohe/Thinkback && git add web/src/index.css && git commit -m "style: 新增 editorial typography / layout / 动效 utility classes"
```

---

### Task 3: Card 组件 — 加 `elevated` variant

**Files:**
- Modify: `web/src/components/ui/card.tsx`

**Step 3.1: 用 cva 重构 Card 以支持 variant**

把 `card.tsx` 完整替换为:
```tsx
import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const cardVariants = cva("rounded-2xl border bg-card text-card-foreground", {
  variants: {
    variant: {
      default: "border-border shadow-xs",
      elevated: "rounded-3xl border-[#ebe7df] shadow-md transition-shadow hover:shadow-lg",
    },
  },
  defaultVariants: {
    variant: "default",
  },
});

function Card({
  className,
  variant,
  ...props
}: React.ComponentProps<"div"> & VariantProps<typeof cardVariants>) {
  return (
    <div
      data-slot="card"
      data-variant={variant ?? "default"}
      className={cn(cardVariants({ variant }), className)}
      {...props}
    />
  );
}

function CardHeader({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="card-header"
      className={cn("flex flex-col gap-1.5 p-6", className)}
      {...props}
    />
  );
}

function CardTitle({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="card-title"
      className={cn("text-lg font-semibold leading-snug tracking-tight", className)}
      {...props}
    />
  );
}

function CardDescription({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="card-description"
      className={cn("text-sm text-foreground-muted leading-relaxed", className)}
      {...props}
    />
  );
}

function CardContent({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div data-slot="card-content" className={cn("p-6 pt-0", className)} {...props} />
  );
}

function CardFooter({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="card-footer"
      className={cn("flex items-center p-6 pt-0", className)}
      {...props}
    />
  );
}

export {
  Card,
  CardHeader,
  CardTitle,
  CardDescription,
  CardContent,
  CardFooter,
  cardVariants,
};
```

**Step 3.2: 验证 tsc**

```bash
cd /Users/zhangpeng/workspace/liaohe/Thinkback/web && npx tsc --noEmit
```
期望:`TypeScript: No errors found`

**Step 3.3: Commit**

```bash
cd /Users/zhangpeng/workspace/liaohe/Thinkback && git add web/src/components/ui/card.tsx && git commit -m "feat(card): 加 elevated variant (rounded-3xl + 暖色边 + shadow-md)"
```

---

### Task 4: Button 组件 — rounded-xl + 暖色 hover 上浮

**Files:**
- Modify: `web/src/components/ui/button.tsx`

**Step 4.1: 把所有 size 的 `rounded-md` 改为 `rounded-xl`,主按钮加 hover 上浮**

读 `web/src/components/ui/button.tsx`,把 cva 的 `base` 里 `rounded-md` 替换为 `rounded-xl`,在 `default` variant 加 hover 上浮:

```tsx
const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 rounded-xl text-sm font-medium whitespace-nowrap transition-all duration-200 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--ring)] disabled:pointer-events-none disabled:opacity-50 active:scale-[0.98] [&_svg]:size-4 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        default:
          "bg-primary text-primary-foreground shadow-sm hover:-translate-y-px hover:shadow-md",
        destructive:
          "bg-destructive text-destructive-foreground shadow-sm hover:-translate-y-px hover:shadow-md",
        outline:
          "border border-border bg-background hover:bg-background-muted hover:-translate-y-px",
        secondary:
          "bg-secondary text-secondary-foreground hover:bg-background-strong",
        ghost: "hover:bg-background-muted",
        link: "text-primary underline-offset-4 hover:underline",
      },
      size: {
        default: "h-10 px-5",
        sm: "h-8 px-3 text-xs",
        lg: "h-12 px-7 text-base",
        icon: "size-11",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  },
);
```

(具体细节以原文件为准,核心改动是 `rounded-md` → `rounded-xl`、`hover:-translate-y-px`、active 缩放)

**Step 4.2: 验证 tsc**

```bash
cd /Users/zhangpeng/workspace/liaohe/Thinkback/web && npx tsc --noEmit
```
期望:`TypeScript: No errors found`

**Step 4.3: Commit**

```bash
cd /Users/zhangpeng/workspace/liaohe/Thinkback && git add web/src/components/ui/button.tsx && git commit -m "feat(button): rounded-xl + hover 上浮 + active 缩放"
```

---

### Task 5: Input 组件 — rounded-xl + 暖色 focus

**Files:**
- Modify: `web/src/components/ui/input.tsx`

**Step 5.1: 改圆角 + focus shadow**

读 `web/src/components/ui/input.tsx`,把 `rounded-md` 改为 `rounded-xl`,focus-visible shadow 改暖色:

```tsx
className={cn(
  "flex h-10 w-full rounded-xl border border-border bg-background px-4 py-2 text-sm transition-shadow placeholder:text-foreground-soft focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--ring)] focus-visible:shadow-[0_0_0_3px_rgb(60_41_17/0.08)] disabled:cursor-not-allowed disabled:opacity-50",
  className,
)}
```

**Step 5.2: 验证 tsc + Commit**

```bash
cd /Users/zhangpeng/workspace/liaohe/Thinkback/web && npx tsc --noEmit
cd /Users/zhangpeng/workspace/liaohe/Thinkback && git add web/src/components/ui/input.tsx && git commit -m "feat(input): rounded-xl + 暖色 focus shadow"
```

---

### Task 6: Sheet 组件 — 默认宽 480px + 圆角

**Files:**
- Modify: `web/src/components/ui/sheet.tsx`

**Step 6.1: 改默认宽度与圆角**

读 `web/src/components/ui/sheet.tsx`,把:
- 默认宽度 `w-3/4 sm:max-w-sm`(24rem)
- 改为 `w-full sm:max-w-[480px]` 并加 `rounded-l-3xl`(侧抽屉)

具体:`SheetContent` 的 `className` 默认值:
```tsx
className={cn(
  "fixed z-50 gap-4 bg-background p-6 shadow-lg transition ease-in-out data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:duration-200 data-[state=open]:duration-300",
  side === "right" && "inset-y-0 right-0 h-full w-3/4 border-l data-[state=closed]:slide-out-to-right data-[state=open]:slide-in-from-right sm:max-w-sm",
  ...
)}
```
改为:
```tsx
className={cn(
  "fixed z-50 gap-4 bg-background p-8 shadow-lg transition ease-in-out data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:duration-200 data-[state=open]:duration-300",
  side === "right" && "inset-y-0 right-0 h-full w-3/4 rounded-l-3xl border-l data-[state=closed]:slide-out-to-right data-[state=open]:slide-in-from-right sm:max-w-[480px]",
  ...
)}
```

**Step 6.2: 验证 tsc + Commit**

```bash
cd /Users/zhangpeng/workspace/liaohe/Thinkback/web && npx tsc --noEmit
cd /Users/zhangpeng/workspace/liaohe/Thinkback && git add web/src/components/ui/sheet.tsx && git commit -m "feat(sheet): 默认宽 480px + 圆角左侧 24px"
```

---

### Task 7: Tabs / Table / page-header 升级

**Files:**
- Modify: `web/src/components/ui/tabs.tsx`、`web/src/components/ui/table.tsx`、`web/src/components/page-header.tsx`

**Step 7.1: Tabs 下划线由 2px → 1px 暖色**

读 `tabs.tsx`,把 `TabsList` 的 `h-10` 改为 `h-11`(给底部下划线留空间),`TabsTrigger` 的 `data-[state=active]:bg-transparent data-[state=active]:shadow-[0_2px_0_0_var(--primary)]` 改为 `data-[state=active]:shadow-[inset_0_-1px_0_0_var(--foreground-intense)]` —— 即从 2px 顶部下划线改为 1px 暖色 inset 下划线。

`TabsTrigger` 的 active 文字:`data-[state=active]:text-foreground-intense` 保留,加 `font-semibold`。

**Step 7.2: Table 行高加大 + 暖色分隔线**

读 `table.tsx`,把 `TableRow` 的 `hover:bg-muted/50` 替换为:
```tsx
className={cn(
  "border-b border-[#f5f2ec] transition-colors hover:bg-[#fdf8ed] data-[state=selected]:bg-background-muted",
  className,
)}
```

`TableCell` 默认 `p-3` 改为 `py-4 px-4`(行高 52px 起步),`TableHead` 同。

**Step 7.3: PageHeader 加日期 + 大字版式**

读 `web/src/components/page-header.tsx`(原文件 820 字节,看一下实际内容),把 `h1` 改为 `text-4xl font-semibold tracking-tight`,并在标题下方加一个 section label 风格的"今日日期"行(用 `Intl.DateTimeFormat("zh-CN", { weekday: "long", month: "long", day: "numeric" }).format(new Date())`)。

如果原 page-header 没有 description/description 槽,加一行 metadata 显示。

**Step 7.4: 验证 tsc**

```bash
cd /Users/zhangpeng/workspace/liaohe/Thinkback/web && npx tsc --noEmit
```
期望:`TypeScript: No errors found`

**Step 7.5: Commit**

```bash
cd /Users/zhangpeng/workspace/liaohe/Thinkback && git add web/src/components/ui/tabs.tsx web/src/components/ui/table.tsx web/src/components/page-header.tsx && git commit -m "feat(components): tabs/table/page-header editorial 升级"
```

---

### Task 8: Phase 1 验证 — 截图对照 base 状态

**Step 8.1: 启动/确认 dev server 在 7002**

```bash
curl -s -o /dev/null -w '%{http_code}' http://localhost:7002/
```
若非 200,启新 server:
```bash
cd /Users/zhangpeng/workspace/liaohe/Thinkback/web && VITE_USE_MOCK=1 FRONTEND_PORT=7002 nohup npm run dev > /tmp/thinkback-dev.log 2>&1 &
sleep 4 && curl -s -o /dev/null -w '%{http_code}' http://localhost:7002/
```

**Step 8.2: 拍 4 张截图(每个示范页 + 任务 + 配置作为对照)**

```bash
# 用 chrome-devtools 工具
chrome-devtools_navigate_page(pageId, url=http://localhost:7002/)
# 等待 2s,拍 overview
chrome-devtools_take_screenshot(pageId, filePath=.shots/ds_overview.jpeg)
# 拍 memories
chrome-devtools_take_screenshot(pageId, filePath=.shots/ds_memories.jpeg)
# 拍 tasks(对照)
chrome-devtools_take_screenshot(pageId, filePath=.shots/ds_tasks.jpeg)
# 拍 config(对照)
chrome-devtools_take_screenshot(pageId, filePath=.shots/ds_config.jpeg)
```

**Step 8.3: 肉眼对比 base 截图(`.shots/p1_*.jpeg`)与新截图**

期望:
- ✅ 暖白底色 `#fdfcf8` 可见
- ✅ 卡片圆角从 12px → 16-24px
- ✅ 阴影色调从冷蓝 → 暖棕
- ✅ Button/Input 圆角加大
- ⏳ KPI 数字还未到 96px(等 Phase 2)
- ⏳ Memories 表格布局还未重做(等 Phase 3)

---

## Phase 2 — 总览页 Editorial Premium 重做

### Task 9: 重写 overview.tsx — editorial hero + system pulse

**Files:**
- Modify: `web/src/pages/overview.tsx`(整文件重写,665 行)

**Step 9.1: 顶部 hero 区(替换 4 KPI 横排)**

把 `<div className="grid grid-cols-2 gap-3 sm:grid-cols-2 lg:grid-cols-4">{[4 个 StatTile]}</div>` 替换为:

```tsx
{/* HERO: 单一大数字作为"今天的头条事实" */}
<section className="animate-editorial-fade-up">
  <p className="section-label mb-3">ACTIVE MEMORIES · 实时索引</p>
  <div className="flex items-baseline gap-6">
    <span className="text-display-xl text-foreground-intense">
      {(data.memories.ACTIVE ?? 0).toLocaleString()}
    </span>
    <div className="flex flex-col gap-1">
      <span className="flex items-center gap-1 text-base font-medium text-error tabular-nums">
        <ArrowUp aria-hidden="true" className="size-4" />
        {prevValues?.ACTIVE !== undefined ? data.memories.ACTIVE! - prevValues.ACTIVE : 0}
      </span>
      <span className="text-sm text-foreground-muted">since last sync · 30s 前</span>
    </div>
  </div>
  <p className="mt-3 max-w-2xl text-base text-foreground-muted leading-relaxed">
    当前已索引 {data.memories.ACTIVE?.toLocaleString() ?? 0} 条有效记忆,覆盖 {data.by_classification.normal?.toLocaleString() ?? 0} 条普通记忆与 {data.by_classification.personal?.toLocaleString() ?? 0} 条个人偏好。
  </p>
</section>
```

**Step 9.2: editorial-rule 分隔**

```tsx
<div className="editorial-rule my-10" />
```

**Step 9.3: System Pulse 区(4 个 64px 数字副 KPI)**

```tsx
<section className="animate-editorial-fade-up" style={{ animationDelay: "80ms" }}>
  <p className="section-label mb-5">SYSTEM PULSE · 系统脉搏</p>
  <div className="grid grid-cols-2 gap-8 lg:grid-cols-4">
    {[
      { label: "RUNNING", value: String(data.tasks.running ?? 0), suffix: "tasks", delta: prevValues?.running },
      { label: "FAILED", value: String(failed), tone: failed > 0 ? "warning" : "neutral", suffix: failed > 0 ? "needs attention" : "all clear", delta: prevValues?.failed },
      { label: "L3 QUEUE", value: `${queueUsed}/${data.l3.max_pending_tasks}`, suffix: `${queuePct}% used`, delta: undefined },
      { label: "UPTIME", value: formatUptime(health.data?.uptime_seconds ?? 0), suffix: "stable" },
    ].map((pulse, i) => (
      <div key={pulse.label} className="space-y-2 animate-editorial-fade-up" style={{ animationDelay: `${120 + i * 80}ms` }}>
        <p className="section-label">{pulse.label}</p>
        <p className={`text-display-md tabular-nums ${pulse.tone === "warning" ? "text-error" : "text-foreground-intense"}`}>
          {pulse.value}
        </p>
        <p className="text-sm text-foreground-muted flex items-center gap-2">
          {pulse.suffix}
          {pulse.delta !== undefined && pulse.delta !== 0 ? (
            <span className={`flex items-center text-xs tabular-nums ${pulse.delta > 0 ? "text-error" : "text-success"}`}>
              {pulse.delta > 0 ? <ArrowUp className="size-3" /> : <ArrowDown className="size-3" />}
              {Math.abs(pulse.delta)}
            </span>
          ) : null}
        </p>
      </div>
    ))}
  </div>
</section>
```

**Step 9.4: Needs Attention 区(纵向 editorial 列表,替换红色 alert 卡片)**

把 `<Card className="border-error-strong/40 bg-error-soft/30">` 整块替换为:

```tsx
{hasFailedTasks || failed > 0 ? (
  <section className="animate-editorial-fade-up" style={{ animationDelay: "200ms" }}>
    <div className="flex items-baseline justify-between mb-4">
      <p className="section-label">NEEDS ATTENTION · 需关注</p>
      <Link to="/tasks" search={{ tab: failedTasks[0]?.status === "dead_letter" ? "dead_letter" : "failed" }} className="text-sm text-foreground-emphasis hover:underline">
        {failed} item{failed !== 1 ? "s" : ""} →
      </Link>
    </div>
    <ul className="divide-y divide-[#f5f2ec] border-t border-b border-[#f5f2ec]">
      {failedTasks.slice(0, 5).map((task) => (
        <li key={task.task_id} className="flex items-center justify-between gap-4 py-3 group">
          <div className="flex items-center gap-4 min-w-0 flex-1">
            <span aria-hidden="true" className="block size-1.5 shrink-0 rounded-full bg-error" />
            <Link to="/tasks" search={{ tab: task.status === "dead_letter" ? "dead_letter" : "failed" }} className="font-mono text-sm text-foreground-emphasis truncate hover:underline" title={task.task_id}>
              {truncateMiddle(task.task_id, 16, 8)}
            </Link>
            <StatusBadge status={task.status} />
            <span className="text-sm text-foreground-muted">{task.op_type}</span>
          </div>
          {task.last_error ? (
            <span className="hidden md:block max-w-[40%] truncate font-mono text-xs text-error" title={task.last_error}>
              {task.last_error}
            </span>
          ) : null}
        </li>
      ))}
    </ul>
  </section>
) : null}
```

**Step 9.5: 服务身份卡片升级为 elevated**

把 `<Card className="animate-fade-up">`(服务身份)替换为:
```tsx
<Card variant="elevated" className="animate-editorial-fade-up" style={{ animationDelay: "250ms" }}>
  ...
</Card>
```

**Step 9.6: Memory Status 分布卡升级**

把 `DistributionBars` 的 `h-1.5`(进度条)改为 `h-1`(细线,editorial 感)。其余保留。

(可选:在 description 后加 `<p className="text-3xl font-semibold mt-2">{total.toLocaleString()}</p>` 大字)

**Step 9.7: L3 后台写 / 5min 吞吐卡片升级**

把这两卡也升级为 `variant="elevated"`。

**Step 9.8: 最近治理动作表格行高加大 + 暖色 hover**

`<tr className="hover:bg-background-muted/50">` → `<tr className="row-hover-warm border-l-2 border-transparent transition-colors">`

`<td className="py-2 ...">` → `<td className="py-3.5 ...">`

**Step 9.9: 全局入场动画替换**

把所有 `animate-fade-up` 替换为 `animate-editorial-fade-up`,并加上 `style={{ animationDelay: ... }}` 形成 stagger(80ms 步进)。

**Step 9.10: 验证 tsc + 截图**

```bash
cd /Users/zhangpeng/workspace/liaohe/Thinkback/web && npx tsc --noEmit
```

然后用 chrome-devtools 拍 overview 截图存为 `.shots/ep2_overview.jpeg`,对比 Phase 1 的 `ds_overview.jpeg`。

期望看到:
- ✅ 顶部 96px 大数字(有效记忆 8247)
- ✅ System Pulse 4 个 64px 副 KPI(运行中/失败/队列/运行时间)
- ✅ Needs Attention 是纵向 editorial 列表(不是红色卡片)
- ✅ 卡片圆角 24px、暖色阴影
- ✅ editorial-rule 暖色分隔线

**Step 9.11: Commit**

```bash
cd /Users/zhangpeng/workspace/liaohe/Thinkback && git add web/src/pages/overview.tsx && git commit -m "feat(overview): editorial premium 重做 - hero KPI + system pulse + needs attention 列表"
```

---

## Phase 3 — 记忆浏览器重做 + ⌘K 命令面板

### Task 10: 新增 command-palette 组件

**Files:**
- Create: `web/src/components/command-palette.tsx`
- Modify: `web/src/components/layout.tsx`

**Step 10.1: 写命令面板组件(自管理 open state + ⌘K 监听 + 自定义事件)**

```tsx
/** 全局 ⌘K 命令面板:搜索记忆 + 快速跳转。
 * 自管理 open state:监听 ⌘K / Ctrl+K 切换、监听 `open-command-palette` CustomEvent。
 * 静态数据,不做后端集成(见 spec 7.scope cut)。 */

import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "@tanstack/react-router";
import { Command, Database, ListTodo, Search, Settings, ShieldAlert, ScrollText } from "lucide-react";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";

type Item =
  | { kind: "memory"; id: string; label: string }
  | { kind: "page"; to: string; label: string; icon: typeof Database };

const PAGES: Item[] = [
  { kind: "page", to: "/", label: "总览 Overview", icon: Command },
  { kind: "page", to: "/memories", label: "记忆浏览器 Memory Browser", icon: Database },
  { kind: "page", to: "/tasks", label: "任务监控 Tasks", icon: ListTodo },
  { kind: "page", to: "/govern", label: "治理操作 Govern", icon: ShieldAlert },
  { kind: "page", to: "/audit", label: "审计日志 Audit", icon: ScrollText },
  { kind: "page", to: "/config", label: "系统配置 Config", icon: Settings },
];

const RECENT_MEMORIES = [
  { id: "mem_001", label: "用户偏好使用极简暗色主题" },
  { id: "mem_002", label: "用户对海鲜过敏" },
];

export function CommandPalette() {
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const needle = query.trim().toLowerCase();

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen((v) => !v);
      }
    };
    const onCustom = () => setOpen((v) => !v);
    window.addEventListener("keydown", onKey);
    window.addEventListener("open-command-palette", onCustom);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("open-command-palette", onCustom);
    };
  }, []);

  useEffect(() => {
    if (!open) setQuery("");
  }, [open]);

  const results = useMemo<Item[]>(() => {
    const mems: Item[] = RECENT_MEMORIES
      .filter((m) => !needle || m.id.includes(needle) || m.label.toLowerCase().includes(needle))
      .map((m) => ({ kind: "memory", id: m.id, label: `${m.id}  ${m.label}` }));
    const pages: Item[] = PAGES.filter((p) => !needle || p.label.toLowerCase().includes(needle));
    return [...mems, ...pages];
  }, [needle]);

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent className="max-w-[560px] gap-0 p-0 rounded-3xl border-[#ebe7df] shadow-lg top-[20%] translate-y-0">
        <DialogTitle className="sr-only">命令面板</DialogTitle>
        <div className="flex items-center gap-3 border-b border-[#f5f2ec] px-5 py-4">
          <Search aria-hidden="true" className="size-5 text-foreground-muted" />
          <input
            autoFocus
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索记忆 / 跳转页面..."
            className="flex-1 bg-transparent text-base outline-none placeholder:text-foreground-soft"
            aria-label="命令面板搜索"
          />
          <kbd className="hidden md:inline-flex items-center gap-1 rounded-md border border-[#ebe7df] bg-background px-2 py-0.5 text-xs text-foreground-muted">esc</kbd>
        </div>
        <ul className="max-h-80 overflow-y-auto p-2">
          {results.length === 0 ? (
            <li className="px-4 py-8 text-center text-sm text-foreground-muted">没有匹配的结果</li>
          ) : (
            results.map((item, i) => (
              <li key={i}>
                <button
                  type="button"
                  className="flex w-full items-center gap-3 rounded-xl px-4 py-3 text-left text-sm hover:bg-background-muted focus-visible:bg-background-muted focus-visible:outline-none"
                  onClick={() => {
                    if (item.kind === "page") navigate({ to: item.to });
                    setOpen(false);
                  }}
                >
                  {item.kind === "memory" ? (
                    <Database aria-hidden="true" className="size-4 text-foreground-muted" />
                  ) : (
                    <item.icon aria-hidden="true" className="size-4 text-foreground-muted" />
                  )}
                  <span className="flex-1 truncate font-mono text-xs text-foreground-emphasis">{item.label}</span>
                </button>
              </li>
            ))
          )}
        </ul>
        <div className="border-t border-[#f5f2ec] px-5 py-2.5 flex items-center justify-between text-xs text-foreground-muted">
          <span>RECENT · QUICK NAVIGATION</span>
          <span>↑↓ 选择 · ↵ 跳转</span>
        </div>
      </DialogContent>
    </Dialog>
  );
}
```

**Step 10.2: 在 layout.tsx 中挂载 CommandPalette(无 prop)**

读 `web/src/components/layout.tsx`,在 return 顶层加:

```tsx
import { CommandPalette } from "@/components/command-palette";

// ...在 Layout return 内,最外层 <div> 内最后一行加:
<CommandPalette />
```

**Step 10.3: 验证 tsc**

```bash
cd /Users/zhangpeng/workspace/liaohe/Thinkback/web && npx tsc --noEmit
```

**Step 10.4: Commit**

```bash
cd /Users/zhangpeng/workspace/liaohe/Thinkback && git add web/src/components/command-palette.tsx web/src/components/layout.tsx && git commit -m "feat: ⌘K 全局命令面板(自管理 state + 静态搜索 + 页面跳转)"
```

---

### Task 11: 重写 memories.tsx — editorial hero + chip 筛选 + card 列表

**Files:**
- Modify: `web/src/pages/memories.tsx`(整文件重写,525 行)

**Step 11.1: PageHeader 加 hero 副标题**

把原 `<PageHeader title="记忆浏览器" />` 替换为:
```tsx
<PageHeader
  title="MEMORY BROWSER · 记忆浏览器"
  subtitle={`${(data?.total ?? 0).toLocaleString()} memories · indexed for retrieval`}
/>
```

(若 page-header 不支持 subtitle,加一个 subtitle prop。)

**Step 11.2: 搜索 + 筛选区升级**

把 `<Input className="max-w-sm">` 包到圆角更大的容器 + 右侧 `⌘K` 提示:
```tsx
<div className="flex flex-wrap items-center gap-3">
  <div className="relative max-w-md flex-1">
    <Search aria-hidden="true" className="pointer-events-none absolute left-4 top-1/2 -translate-y-1/2 size-4 text-foreground-muted" />
    <Input className="pl-11 h-11 rounded-xl" placeholder="用户 ID / 记忆 ID / scope..." value={userIdInput} onChange={...} />
  </div>
  <Select value={String(limit)} onValueChange={...}>...50 / 100 / 200 ... 页</Select>
  <button onClick={() => window.dispatchEvent(new CustomEvent("open-command-palette"))} className="hidden md:flex items-center gap-1.5 rounded-xl border border-border bg-background px-3 h-11 text-xs text-foreground-muted hover:bg-background-muted">
    <span>Quick switch</span>
    <kbd className="rounded-md bg-background-muted px-1.5 py-0.5 font-mono text-[10px]">⌘K</kbd>
  </button>
</div>

<div className="flex flex-wrap gap-2">
  {["ACTIVE", "SUPERSEDED", "DELETED", "SUPPRESSED"].map((status) => {
    const active = statusesInput === status || (status === "ACTIVE" && !statusesInput);
    return (
      <button
        key={status}
        onClick={() => setStatusesInput(status)}
        className={cn(
          "relative rounded-xl border px-4 py-1.5 text-sm font-medium transition-colors",
          active
            ? "border-foreground-intense text-foreground-intense bg-background"
            : "border-border text-foreground-muted hover:text-foreground-emphasis hover:bg-background-muted",
          active && "shadow-[inset_0_-1px_0_0_var(--foreground-intense)]",
        )}
      >
        {STATUS_LABELS[status] ?? status}
      </button>
    );
  })}
  {hasActiveFilter && (
    <button onClick={reset} className="text-xs text-foreground-muted underline-offset-2 hover:underline">
      清空
    </button>
  )}
</div>
```

**Step 11.3: editorial card 列表(替换 table)**

把 `<Table>` 整块替换为:
```tsx
<ul className="divide-y divide-[#f5f2ec] border-t border-b border-[#f5f2ec]">
  {data.items.map((memory, i) => (
    <li
      key={memory.memory_id}
      className="group cursor-pointer py-6 row-hover-warm border-l-2 border-transparent pl-4 -ml-4 pr-2 animate-editorial-fade-up"
      style={{ animationDelay: `${i * 40}ms` }}
      onClick={() => setSelected(memory.memory_id)}
      onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setSelected(memory.memory_id); } }}
      tabIndex={0}
      role="button"
      aria-label={`打开记忆 ${memory.memory_id}`}
    >
      <div className="flex items-baseline gap-3 mb-2">
        <span className="section-label text-[10px]">MEMORY · {memory.memory_id.slice(-3)}</span>
        <span className="font-mono text-xs text-foreground-soft">
          {memory.last_recalled_at ? formatTime(memory.last_recalled_at) : "never"}
        </span>
        <span className="ml-auto text-sm text-foreground-muted tabular-nums">
          recalled <strong className="font-semibold text-foreground-intense">{memory.recall_count}×</strong>
        </span>
      </div>
      <p className="text-lg leading-snug text-foreground-intense mb-2 line-clamp-2">
        {memory.memory_text}
      </p>
      <div className="flex flex-wrap items-center gap-3 text-xs text-foreground-muted">
        <span className="font-mono">{memory.user_id}</span>
        <span aria-hidden="true">·</span>
        <span>{memory.memory_scope_id}</span>
        <StatusBadge status={memory.memory_status} />
        <Badge variant="neutral">{memory.data_classification}</Badge>
      </div>
    </li>
  ))}
</ul>
```

(需要 import `Search` from lucide-react,`formatTime` from utils,`cn` from utils。)

**Step 11.4: 分页器替换**

把 "加载更多" 按钮替换为:
```tsx
<div className="flex items-center justify-between mt-4 text-sm text-foreground-muted">
  <span className="tabular-nums">
    显示 {page * 50 - 49}-{Math.min(page * 50, data.total)} / 共 {data.total.toLocaleString()}
  </span>
  <div className="flex gap-2">
    <Button variant="outline" size="sm" disabled={page === 1} onClick={() => setPage((p) => p - 1)}>
      ← 上一页
    </Button>
    <Button variant="outline" size="sm" disabled={page * 50 >= data.total} onClick={() => setPage((p) => p + 1)}>
      下一页 →
    </Button>
  </div>
</div>
```

**Step 11.5: Sheet 详情升级**

把详情 Sheet 内的 `<SheetHeader>` 替换为大字版式:
```tsx
<SheetHeader className="space-y-3 p-8 pb-6">
  <p className="section-label">MEMORY · {selected.slice(-3)}</p>
  <SheetTitle className="text-3xl font-semibold tracking-tight leading-tight">
    {source?.data?.memory.memory_text ?? "记忆详情"}
  </SheetTitle>
  <SheetDescription className="font-mono text-sm">
    {source?.data?.memory.memory_id}
  </SheetDescription>
</SheetHeader>
```

**Step 11.6: 验证 tsc + 截图**

```bash
cd /Users/zhangpeng/workspace/liaohe/Thinkback/web && npx tsc --noEmit
```

拍 memories 截图存为 `.shots/ep3_memories.jpeg`。

**Step 11.7: Commit**

```bash
cd /Users/zhangpeng/workspace/liaohe/Thinkback && git add web/src/pages/memories.tsx && git commit -m "feat(memories): editorial premium 重做 - hero + chip 筛选 + card 列表 + 分页"
```

---

### Task 12: 最终验证 — 6 页截图回归

**Step 12.1: 跑 tsc + build**

```bash
cd /Users/zhangpeng/workspace/liaohe/Thinkback/web && npx tsc --noEmit
cd /Users/zhangpeng/workspace/liaohe/Thinkback/web && npm run build
```
期望:
- tsc: `TypeScript: No errors found`
- build: `built in <N>s`,只有 chunk size warning(已知 P1)

**Step 12.2: 拍 6 页截图(全 regression)**

chrome-devtools 拍:
- `.shots/final_overview.jpeg`(总览,Editorial Premium 完整版)
- `.shots/final_memories.jpeg`(记忆浏览器,Editorial Premium 完整版)
- `.shots/final_tasks.jpeg`(任务 — 对照,只继承 token,内部结构未变)
- `.shots/final_govern.jpeg`(治理 — 对照)
- `.shots/final_audit.jpeg`(审计 — 对照)
- `.shots/final_config.jpeg`(配置 — 对照)

**Step 12.3: 肉眼验证 spec 9 节验证标准**

- ✅ build 通过
- ✅ 总览 hero KPI 96px,System Pulse 4 KPI 64px,editorial 列表
- ✅ 记忆浏览器 hero,chip,editorial card,⌘K 可打开
- ✅ 视觉对照 Stripe / Linear(肉眼)
- ✅ a11y 不退化(键盘 Tab 走查 + 检查 focus ring)
- ✅ 桌面端 1280/1440/1920 三档宽度不破布局

**Step 12.4: 写总结 commit(如需要)+ 关闭 dev server**

```bash
cd /Users/zhangpeng/workspace/liaohe/Thinkback && git log --oneline -10
```

如无问题,关闭 dev server:
```bash
kill 74981  # 旧 server(如还在)
```

---

## Summary

预计 11 个 task × 5 分钟 = 55 分钟(理想);实际 ~4 小时(每个 task 含验证 + 截图 + commit)。

实施顺序严格按 Task 1-12 顺序,每完成一个 task 立即 commit,失败回滚到上一个 commit。
