# Admin 总览页（/）

## 信息层级（自上而下）

1. **KPI 4 卡**（最顶端） — 一眼看到核心指标，其它所有都从属于它
2. **需关注** — 仅在 unhealthy 时显示（失败+死信 > 0 或 L3 队列已满）
3. **服务身份** — 版本 / 环境 / 关键开关（与 .env 比对），2 列紧凑布局
4. **分布图 4 张**（任务状态 | 数据分类 | 记忆来源 | 记忆状态 hero） — 记忆状态占满底部整行作为 hero
5. **L3 后台写 + 5min 吞吐** — 拆成两张独立卡（之前挤一块太密）
6. **最近治理动作** — Top-5 紧凑表 + 「查看全部」入口

## 设计原则

- KPI 用 text-4xl 大数字 + tabular-nums；倒计时 `<=3s` 时 pulse-soft
- 「需关注」卡用 destructive tone + 内嵌实心徽章（failed red / dead_letter violet）
- 分布图颜色用基础 token（`bg-info` / `bg-success`），不用 `*-strong`，避免饱和
- 进场动画：每张卡 `animate-fade-up` + 50ms 递增延迟（KPI 0/50/100/150ms，需关注 200ms，服务身份 250ms，分布 300-450ms，L3/5min 550-600ms，治理动作 650ms）
- KPI 数字用 `useCountUp` hook，数值变化时 700ms easeOutCubic 滚动

## 自动刷新

`REFETCH_SECONDS = 30`：
- `useOverview` 每 30s 重拉
- `dataUpdatedAt` 触发 `setPrevValues` 快照，供 KPI 角标显示 delta
- `secondsToNext` 倒计时实时刷新，最后 3s 触发 pulse-soft

## v2 视觉修订（2026-09-30）

- KPI 4 卡升级到 text-4xl，原 text-3xl 显得平淡
- 「需关注」从「独立长卡」改为「折叠 1 行 summary」，只在 unhealthy 时显示
- 服务身份卡：2 列紧凑布局，关键开关用徽章组
- 分布图从 4 列 → 3+1（记忆状态 hero 占整行）
- L3 后台写 + 5min 吞吐：拆成两张独立卡（之前挤一块太密）
- 治理动作表：target_id 截断到 28 字符，timestamp 紧凑显示
- 零状态（5min 无 append/recall）显示友好提示而非四个 "0"
- 顶部工具栏合并：状态徽章 + 倒计时靠左，两个操作按钮靠右
