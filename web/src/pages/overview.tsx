/** P1 总览：KPI 快照 + 状态分布 + L3 容量（30s 轮询）。
 *
 * 数据形态决策（dataviz 口径）：overview 只有当前快照，无历史序列端点，
 * 不画编造的时序图 —— 状态分布用直接标注的 HTML 条形（标签承担身份，
 * 颜色仅强化状态语义），时序图待指标历史端点补齐。
 */

import { AlertTriangle, RefreshCw } from "lucide-react";
import { Link } from "@tanstack/react-router";
import { useOverview } from "@/api/queries";
import { PageHeader } from "@/components/page-header";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

const TASK_STATUS_ORDER = [
  { key: "running", label: "运行中", bar: "bg-info-strong" },
  { key: "pending", label: "等待中", bar: "bg-neutral-strong" },
  { key: "completed", label: "已完成", bar: "bg-success-strong" },
  { key: "failed", label: "失败", bar: "bg-error-strong" },
  { key: "dead_letter", label: "死信", bar: "bg-violet-strong" },
] as const;

const MEMORY_STATUS_ORDER = [
  { key: "ACTIVE", label: "有效", bar: "bg-success-strong" },
  { key: "SUPERSEDED", label: "已取代", bar: "bg-neutral-strong" },
  { key: "DELETED", label: "已删除", bar: "bg-neutral-strong" },
  { key: "SUPPRESSED", label: "已抑制", bar: "bg-neutral-strong" },
] as const;

function StatTile({
  label,
  value,
  hint,
  tone = "default",
  to,
}: {
  label: string;
  value: string;
  hint?: string;
  tone?: "default" | "error";
  /** 下钻目标：点击 KPI 卡直达对应过滤视图。 */
  to?: string;
}) {
  const card = (
    <Card className={cn("h-full", to && "transition-shadow hover:shadow-md")}>
      <CardContent className="p-5">
        <dl className="space-y-2">
          <dt className="text-sm text-foreground-muted">{label}</dt>
          <dd
            className={cn(
              "text-3xl font-semibold tracking-tight tabular-nums",
              tone === "error" ? "text-error" : "text-foreground-intense",
            )}
          >
            {value}
          </dd>
          {hint ? (
            <dd
              className={cn(
                "text-sm",
                tone === "error" ? "text-error" : "text-foreground-muted",
              )}
            >
              {hint}
            </dd>
          ) : null}
        </dl>
      </CardContent>
    </Card>
  );
  if (!to) return card;
  return (
    <Link
      to={to}
      aria-label={`查看${label}详情`}
      className="block rounded-xl focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--ring)]"
    >
      {card}
    </Link>
  );
}

function DistributionBars({
  title,
  description,
  rows,
  counts,
}: {
  title: string;
  description: string;
  rows: readonly { key: string; label: string; bar: string }[];
  counts: Record<string, number>;
}) {
  const max = Math.max(1, ...rows.map((row) => counts[row.key] ?? 0));
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
        <CardDescription>{description}</CardDescription>
      </CardHeader>
      <CardContent>
        <ul className="space-y-3">
          {rows.map((row) => {
            const value = counts[row.key] ?? 0;
            return (
              <li key={row.key} className="flex items-center gap-3">
                <span className="w-16 shrink-0 text-sm text-foreground-muted">{row.label}</span>
                <span
                  aria-hidden="true"
                  className="h-2 flex-1 overflow-hidden rounded-full bg-background-strong"
                >
                  <span
                    className={cn("block h-full rounded-full", row.bar)}
                    style={{ width: `${(value / max) * 100}%` }}
                  />
                </span>
                <span className="w-12 text-right font-mono text-sm font-medium tabular-nums text-foreground-emphasis">
                  {value}
                </span>
              </li>
            );
          })}
        </ul>
      </CardContent>
    </Card>
  );
}

export function OverviewPage() {
  const { data, isPending, isError, refetch, dataUpdatedAt } = useOverview();

  if (isPending) {
    return (
      <div aria-busy="true" className="space-y-6">
        <PageHeader title="总览" />
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
          {Array.from({ length: 4 }).map((_, index) => (
            <Skeleton key={index} className="h-28 rounded-xl" />
          ))}
        </div>
        <Skeleton className="h-64 rounded-xl" />
      </div>
    );
  }

  if (isError) {
    return (
      <div className="space-y-6">
        <PageHeader title="总览" />
        <Alert variant="destructive">
          <AlertTriangle aria-hidden="true" />
          <AlertTitle>无法加载总览数据</AlertTitle>
          <AlertDescription>
            请确认后端服务可达、管理 token 正确。
            <Button variant="outline" size="sm" className="ml-2" onClick={() => refetch()}>
              <RefreshCw aria-hidden="true" /> 重试
            </Button>
          </AlertDescription>
        </Alert>
      </div>
    );
  }

  const failed = (data.tasks.failed ?? 0) + (data.tasks.dead_letter ?? 0);
  const queueUsed = data.l3.max_pending_tasks - data.l3.available_capacity;
  const queuePct = Math.round((queueUsed / Math.max(1, data.l3.max_pending_tasks)) * 100);
  const unhealthy = failed > 0 || data.l3.available_capacity === 0;

  return (
    <div className="space-y-6">
      <PageHeader
        title="总览"
        actions={
          <p className="flex items-center gap-2 text-xs">
            {/* 播报只绑健康态文本：30s 轮询刷新时间不再打扰读屏（WCAG 4.1.3） */}
            <span className="sr-only" aria-live="polite">
              {unhealthy ? "存在需关注项" : "状态正常"}
            </span>
            {unhealthy ? <Badge variant="warning">需关注</Badge> : null}
            <span
              className={cn(
                "flex items-center gap-1.5",
                unhealthy ? "text-warning" : "text-success",
              )}
              aria-hidden="true"
            >
              <span
                className={cn(
                  "size-1.5 rounded-full",
                  unhealthy ? "bg-warning-strong" : "bg-success-strong",
                )}
              />
              {unhealthy ? "存在需关注项" : "状态正常"}
            </span>
            <span className="text-foreground-muted">
              · 更新于{" "}
              {new Date(dataUpdatedAt).toLocaleTimeString("zh-CN", { hour12: false })}
            </span>
          </p>
        }
      />

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatTile
          label="有效记忆"
          value={String(data.memories.ACTIVE ?? 0)}
          to="/memories"
        />
        <StatTile
          label="运行中任务"
          value={String(data.tasks.running ?? 0)}
          to="/tasks?tab=running"
        />
        <StatTile
          label="失败 + 死信任务"
          value={String(failed)}
          hint={failed > 0 ? "需要关注" : undefined}
          tone={failed > 0 ? "error" : "default"}
          to="/tasks?tab=failed"
        />
        <StatTile
          label="L3 队列水位"
          value={`${queueUsed}/${data.l3.max_pending_tasks}`}
          hint={`${queuePct}% · ${data.l3.write_mode} 模式`}
          tone={data.l3.available_capacity === 0 ? "error" : "default"}
        />
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <DistributionBars
          title="任务状态分布"
          description="各状态任务计数（实时快照）"
          rows={TASK_STATUS_ORDER}
          counts={data.tasks}
        />
        <DistributionBars
          title="记忆状态分布"
          description="本地索引各状态计数（实时快照）"
          rows={MEMORY_STATUS_ORDER}
          counts={data.memories}
        />
      </div>

      <Card>
        <CardHeader>
          <CardTitle>L3 后台写</CardTitle>
          <CardDescription>执行器与清理队列快照</CardDescription>
        </CardHeader>
        <CardContent>
          <dl className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
            {[
              ["写模式", data.l3.write_mode],
              ["worker 数", String(data.l3.executor_workers)],
              ["待写任务", String(data.l3.pending_write_tasks)],
              ["清理任务", String(data.l3.cleanup_tasks)],
              ["剩余容量", String(data.l3.available_capacity)],
            ].map(([label, value]) => (
              <div key={label}>
                <dt className="text-sm text-foreground-muted">{label}</dt>
                <dd className="font-mono text-sm font-medium tabular-nums text-foreground-emphasis">
                  {value}
                </dd>
              </div>
            ))}
          </dl>
        </CardContent>
      </Card>
    </div>
  );
}
