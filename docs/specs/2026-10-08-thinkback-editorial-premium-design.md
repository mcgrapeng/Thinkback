# Thinkback Editorial Premium 重塑设计

**日期**: 2026-10-08
**范围**: 全局 design system 升级 + 总览页 + 记忆浏览器 两页示范重做
**风格**: Editorial Premium(Stripe Press 风)
**配色**: 保留 appica 哲学(中性灰阶 + 6 状态色徽章),叠加暖白底色 + 暖纸面阴影
**明暗**: 本次仅做 light 模式视觉,dark 模式 token 保留但视觉优化推迟

---

## 1. 设计目标

把 Thinkback 治理台从"标准 admin dashboard"升级到"产品级 editorial 体验":

- **保留**: appica 18 个语义 token、6 状态色 token、WCAG AA 对比度、`tabular-nums`、`focus-visible`、`prefers-reduced-motion`、现有 TypeScript 纪律
- **升级**: 字号节奏、阴影色调、卡片圆角、字体配比、入场动效、editorial 网格、信息层级

气质关键词:**暖白纸面感、超大数字锚点、editorial 排版节奏、品牌级视觉**

---

## 2. Design System 升级

### 2.1 颜色 token(轻调,保留 appica 哲学)

| 维度 | 现状 | 升级后 |
|------|------|--------|
| 主背景 `--background` | `#ffffff` 纯白 | `#fdfcf8` 暖白(纸面感) |
| 副背景 `--background-subtle` | `#f9fafb` 冷灰 | `#f8f6f0` 暖米黄 |
| 卡片 `--card` | `#ffffff` | `#ffffff`(纯白卡片浮于暖白底,制造对比) |
| 强调边 `--border` | `#e5e7eb` 冷灰 | `#ebe7df` 暖棕灰 |
| 装饰边 `--border-muted` | `#f3f4f6` | `#f5f2ec` |
| 状态色 6 档 | 保留 | **保留**(success/error/info/warning/neutral/violet) |
| 阴影色调 | `rgb(16 24 40 / 0.05)` 冷蓝 | `rgb(60 41 17 / 0.06)` 暖棕 |
| 阴影强度 | xs/sm/md 三档 | **xs/sm/md/lg 四档**,lg 用于 hero 卡片 |

### 2.2 字体 token

| 维度 | 现状 | 升级后 |
|------|------|--------|
| 基础正文 | 14px / 1.5 | **15px / 1.6** |
| KPI 数字 | `text-4xl` 36px Geist Sans 600 | **`text-display-xl` 96px / `text-display-lg` 72px** Geist Mono 500 |
| H1 页面标题 | `text-3xl` 30px | **`text-4xl` 36px** Geist Sans 600 |
| H2 卡片标题 | `text-base` 14px | **`text-lg` 18px** Geist Sans 600 |
| H3 小标题 | `text-sm` 12px | **`text-sm` 14px** Geist Sans 500 |
| 标签 | `text-xs` 12px | **`text-xs` 12px** Geist Sans 500 + `tracking-wide` |
| 数据数字 | Geist Sans 600 | **Geist Mono 500**(tabular-nums + 字距收紧) |

### 2.3 间距 token

| 维度 | 现状 | 升级后 |
|------|------|--------|
| 卡片间距 | `gap-3/4/6` 12-24px | **`gap-6/8/10`** 24-40px |
| 卡片内 padding | `p-4/p-5` 16-20px | **`p-6/p-8`** 24-32px |
| 页面 padding | `p-4 md:p-6 lg:p-8` | **`p-6 md:p-10 lg:p-14`** |
| 主区 max-width | `max-w-[1400px]` | **`max-w-[1320px]`**(杂志页面比例) |

### 2.4 圆角 & 阴影

| 维度 | 现状 | 升级后 |
|------|------|--------|
| 卡片 | `rounded-xl` 12px | **`rounded-2xl`** 16px 主体,`rounded-3xl` 24px hero 卡 |
| 按钮 | `rounded-md` 6px | **`rounded-xl`** 12px |
| Input | `rounded-md` 6px | **`rounded-xl`** 12px |
| Sheet 抽屉 | 0(顶到边) | **`rounded-l-3xl`** 24px 左上+左下(不是顶到边) |
| shadow-xs | `0 1px 2px` | 保留,改用暖色 |
| shadow-sm | `0 2px 8px -2px` | `0 2px 12px -2px rgb(60 41 17 / 0.06)` |
| shadow-md | `0 4px 12px -2px` | `0 6px 20px -4px rgb(60 41 17 / 0.08)` |
| shadow-lg | 不存在 | **`0 12px 32px -8px rgb(60 41 17 / 0.12)`**(新增,hero 卡) |

### 2.5 新增 utility class

```css
/* typography */
.text-display-xl { font-family: var(--font-mono); font-size: 6rem; line-height: 1; letter-spacing: -0.04em; font-weight: 500; }
.text-display-lg { font-family: var(--font-mono); font-size: 4.5rem; line-height: 1; letter-spacing: -0.03em; font-weight: 500; }
.text-display-md { font-family: var(--font-mono); font-size: 3rem; line-height: 1; letter-spacing: -0.02em; font-weight: 500; }

/* layout */
.card-elevated { @apply rounded-3xl bg-card border border-border shadow-md; }
.editorial-rule { border-top: 1px solid #e8e2d4; }
.section-label { @apply text-xs font-medium tracking-wider uppercase text-foreground-muted; }

/* animation */
@keyframes editorial-fade-up {
  from { opacity: 0; transform: translateY(16px) scale(0.98); }
  to { opacity: 1; transform: translateY(0) scale(1); }
}
.animate-editorial-fade-up { animation: editorial-fade-up 640ms cubic-bezier(0.16, 1, 0.3, 1) both; }
```

---

## 3. 关键 Component 升级

| 组件 | 现状 | 升级 |
|------|------|------|
| **Card** | `rounded-xl border border-border bg-card shadow-xs` | **`card-elevated`**: `rounded-3xl border border-[#ebe7df] bg-card shadow-md` |
| **Button** | `rounded-md` 6px | `rounded-xl` 12px,主按钮加 1px inset 暖色 shadow + hover 时 `translateY(-1px)` |
| **Badge** | 6 状态色 + 中性 | 保留 + 新增 `tone="kpi"` 用于 KPI 大数字旁的 mini 徽章 |
| **Input / Select** | `rounded-md` | `rounded-xl` 12px,focus ring 改暖色 `shadow-[0_0_0_3px_rgb(60_41_17/0.08)]` |
| **Table** | `text-sm` 行高 40px | `text-[15px]` 行高 52px,分隔线改暖色 `#f5f2ec`,hover bg `#fdf8ed` |
| **Sheet** | `w-72 sm:max-w-72` 顶到边 | `w-[480px] sm:max-w-[480px] rounded-l-3xl` |
| **Tabs** | 2px 下划线 | 1px 暖色下划线,active 态文字加粗 + `translateY(1px)` |

---

## 4. 页面 1: 总览 (Editorial Premium 重塑)

### 4.1 Layout 结构

```
┌─ PageHeader ───────────────────────────────────────────────────┐
│  TUE · OCT 8 · 2026    [需关注] [12s] [刷新] [回收孤儿]       │
├─────────────────────────────────────────────────────────────────┤
│                                                                │
│  8,247  ← 96px Geist Mono 500,tabular-nums                    │
│  有效记忆 · active memories                                    │
│  ↑ 2 since last sync                                          │
│                                                                │
│  ─────────────────────────────────────────── editorial-rule ──│
│                                                                │
│  SYSTEM PULSE   ← section-label uppercase tracking-wider     │
│  ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐                          │
│  │  14  │ │  10  │ │ 47/1k│ │ 4.5d │   64px mono + 标签在下  │
│  │run.  │ │failed│ │queue │ │uptime│                          │
│  │ 32%  │ │ ↑2   │ │  5%  │ │stable│   小字状态                │
│  └──────┘ └──────┘ └──────┘ └──────┘                          │
│                                                                │
│  ─────────────────────────────────────────── editorial-rule ──│
│                                                                │
│  NEEDS ATTENTION                                       2 items│
│  ───────────                                                   │
│  ⚠ task_a1b2...  failed      memory.append     DB timeout    │
│  ⚠ task_b2c3...  dead letter                   slot conflict  │
│                                                                │
│  ─────────────────────────────────────────── editorial-rule ──│
│                                                                │
│  MEMORY STATUS  ·  8,689 indexed                              │
│  Active        ████████████████████████████  95%  8,247      │
│  Superseded    ██                                4%    312    │
│  Deleted       ▏                                 1%     89    │
│  Suppressed    ▏                                 0%     41    │
│                                                                │
└────────────────────────────────────────────────────────────────┘
```

### 4.2 关键变化清单

- **Hero KPI 区**: 删除现有 4 卡横排,改为**单一大数字 96px** 作为页面"今天的头条事实"
- **System Pulse**: 4 个 KPI 改为 64px 数字 + 标签在下 + 状态色 mini badge
- **删除"需关注"红色 alert 卡片**: 改为**纵向 editorial 列表**,左侧 1px 暖橙装饰条
- **进度条**: 1.5px → **3px 暖色**,数字右对齐 mono
- **分节线**: `border-border-muted` → **`editorial-rule`** 暖色装饰
- **服务身份卡片**: 升级为 `card-elevated`,保留 dl/dt/dd 结构,行高加大

### 4.3 滚动行为

- PageHeader sticky 不变
- Hero KPI 不固定(随滚动消失,符合 editorial 阅读节奏)
- System Pulse 进入视口时 `editorial-fade-up` 640ms 渐入 + 80ms stagger

---

## 5. 页面 2: 记忆浏览器 (Editorial Premium 重塑)

### 5.1 Layout 结构

```
┌─ PageHeader ───────────────────────────────────────────────────┐
│  MEMORY BROWSER                                    [导出 CSV ↓]│
│  8,247 memories · indexed for retrieval                       │
├────────────────────────────────────────────────────────────────┤
│                                                                │
│  [🔍 user / memory_id / scope...]        50/页   ⌘K           │
│  ┌─────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐                  │
│  │active│superseded│ deleted  │suppressed│   长条 chip       │
│  └─────┘ └─────────┘ └─────────┘ └─────────┘                  │
│                                                                │
│  ─────────────────────────────────────────── editorial-rule ──│
│                                                                │
│  ┌ MEMORY · 001 ────────────────────── today                   │
│  │                                                             │
│  │  用户偏好使用极简暗色主题的代码编辑器。                     │
│  │                                                             │
│  │  scope: u_8421 / preferences          recalled 47×         │
│  └────────────────────────────────────────────────────────────┘│
│                                                                │
│  ┌ MEMORY · 002 ────────────────────── yesterday               │
│  │  用户对海鲜过敏，特别是贝类。                                │
│  │  scope: u_1980 / dietary              recalled 12×          │
│  └────────────────────────────────────────────────────────────┘│
│                                                                │
│  ──────────── 显示 1-50 / 8,247  ←  [上一页] [下一页] ──────│
└────────────────────────────────────────────────────────────────┘
```

### 5.2 关键变化清单

- **Hero 区**: 加 "MEMORY BROWSER" + "8,247 memories · indexed for retrieval" 编辑文副本
- **筛选 chip**: 圆形按钮 → **长条 editorial chip**,active 态有顶部装饰线
- **列表布局**: 从 `<table>` → **editorial card 列表**(大标题 + 引用式正文 + 元数据三段式)
- **回忆次数**: 右对齐 mono → **inline "recalled 47×"** 与 scope 同列
- **详情 Sheet**: 320px → **480px** `rounded-l-3xl`,顶部大字 + 引用式排版
- **新增 ⌘K 命令面板**: 全局搜索,Radix Dialog 实现,编辑式布局
- **分页**: 保留 50/页 selector + 上一页/下一页(取代"加载更多")
- **总数字**: 底部加 "显示 1-50 / 8,247" 总数提示

### 5.3 ⌘K 命令面板设计

```
┌────────────────────────────────────────────────────────────┐
│  ⌘K                                                       │
│  ┌──────────────────────────────────────────────────────┐  │
│  │ 🔍 搜索记忆 / 用户 / scope...                        │  │
│  └──────────────────────────────────────────────────────┘  │
│                                                            │
│  RECENT                                          2 items  │
│  → mem_001  用户偏好使用极简暗色主题                       │
│  → u_8421   偏好与身份                                      │
│                                                            │
│  QUICK NAVIGATION                                          │
│  → 总览 (G then O)                                        │
│  → 记忆浏览器 (G then M)                                  │
│  → 任务监控 (G then T)                                    │
│                                                            │
└────────────────────────────────────────────────────────────┘
```

---

## 6. 交互与微动效

| 场景 | 现状 | 升级后 |
|------|------|--------|
| 页面入场 | `fade-up` 360ms | **`editorial-fade-up`** 640ms + 80ms stagger |
| 数字更新 | count-up 1.2s linear | **count-up 1.6s ease-out + 末尾 100ms scale 1.02 微闪烁** |
| 表格行 hover | `bg-muted/50` 冷灰 | **`bg-[#fdf8ed]` 暖色 + 左侧 1px 暖色装饰条** |
| 卡片 hover | shadow-md | **shadow-md → shadow-lg + `translateY(-2px)` 200ms** |
| 状态色变化 | 立即 | **`transition-colors duration-300 ease-out`** |
| Toast | sonner 默认 | **暖底色 + 1px 状态色边框 + rounded-xl + shadow-md** |
| 按钮点击 | active:scale-95 | **active:scale-[0.98] + inset 暖色 1px shadow** |
| 主题切换 | toggle | **保留**,不动 |

---

## 7. 不做的部分 (Scope Cut)

明确不做的部分,避免范围漂移:

- ❌ **Dark mode 视觉优化**(token 变量保留,本次不做 dark 视觉重做)
- ❌ **移动端深度优化**(本次桌面端为主,移动端保持基本可用)
- ❌ **4 个非示范页深度重做**(任务/治理/审计/配置 — 自动继承新 token 和组件升级,但内部布局结构不动)
- ❌ **后端契约 / API / 类型改动**(纯视觉层)
- ❌ **Command+K 后端集成**(只做 UI 骨架 + 静态跳转,不做后端搜索)
- ❌ **治理页危险操作 UX 升级**(在原组件上能用即可,本次不做两步提交改造)

---

## 8. 文件变更清单

### 新增
- 无新文件(在现有 token 系统上扩展)

### 修改
- `web/src/index.css` — 暖色背景/边框/阴影 token、新增 utility class、入场动画
- `web/src/components/ui/card.tsx` — 加 `variant="elevated"` (rounded-3xl + shadow-md + 暖色边),默认 variant 仍为 `rounded-2xl`
- `web/src/components/ui/button.tsx` — rounded-xl + hover 上浮 + inset shadow
- `web/src/components/ui/badge.tsx` — 新增 `kpi` variant(可选)
- `web/src/components/ui/input.tsx` — rounded-xl + 暖色 focus ring
- `web/src/components/ui/sheet.tsx` — 默认宽度 480px + rounded-l-3xl
- `web/src/components/ui/tabs.tsx` — 1px 暖色下划线
- `web/src/components/ui/table.tsx` — 行高 52px + 暖色分隔线
- `web/src/components/page-header.tsx` — 加日期 + section label 风格
- `web/src/pages/overview.tsx` — Editorial Premium 重写
- `web/src/pages/memories.tsx` — Editorial Premium 重写
- `web/src/components/layout.tsx` — sidebar active 指示器升级(可选)

### 不动
- `web/src/api/*`、`web/src/main.tsx`、`web/src/components/status-badge.tsx`、`web/src/components/error-boundary.tsx`
- 4 个非示范页(任务/治理/审计/配置)内部结构
- 所有 mock 数据

---

## 9. 验证标准

完成后必须满足:

1. ✅ `npm run build` 通过,无 tsc 错误,无 vite 警告
2. ✅ `npm run dev` 启动后:
   - 总览页 hero KPI 96px 暖白底,System Pulse 4 个 KPI 64px,editorial 列表正常
   - 记忆浏览器顶部 hero,筛选 chip 长条,editorial card 列表,⌘K 命令面板可打开
3. ✅ 视觉对照 reference(Stripe.com / Linear.app / Vercel.com):
   - 暖白底色 + 暖纸面阴影
   - 大数字作为视觉锚点
   - 编辑文风排版节奏(间距、字号、行高)
4. ✅ a11y 不退化:键盘可达、屏幕阅读器、`prefers-reduced-motion`
5. ✅ 响应式桌面端正常(1280px / 1440px / 1920px 三档宽度截图对比)

---

## 10. 实施策略 (写到 writing-plans 阶段)

预计分 3 个 phase:

1. **Phase 1 - Design System 升级**(约 1.5 小时)
   - index.css token 调整
   - 新增 utility class
   - 升级 ui/* 基础组件
   - 升级 page-header

2. **Phase 2 - 总览页重做**(约 1 小时)
   - 重写 overview.tsx layout
   - 验证视觉对照

3. **Phase 3 - 记忆浏览器重做 + ⌘K**(约 1.5 小时)
   - 重写 memories.tsx layout
   - 新增 command-palette 组件
   - 验证视觉对照

总预计 ~4 小时。
