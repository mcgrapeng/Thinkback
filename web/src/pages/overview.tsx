/** 总览页：Hero KPI → System Pulse → Needs Attention → 服务身份 → 4 张分布 → L3 + 5min 吞吐 → 治理快览。
 * 设计原则与历史决策见 docs/admin/overview.md */

import { useEffect, useRef, useState } from "react";
import { Link } from "@tanstack/react-router";
import {
  Activity,
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  Clock,
  Database,
  ListTodo,
  RefreshCw,
  Server,
  ShieldAlert,
} from "lucide-react";
import { toast } from "sonner";
import { useHealthDetail, useOverview, useReclaimOrphanTasks } from "@/api/queries";
import { PageHeader } from "@/components/page-header";
import { StatusBadge } from "@/components/status-badge";
import { Sparkline } from "@/components/sparkline";
import { Heatmap } from "@/components/heatmap";
import { StaleIndicator } from "@/components/stale-indicator";
import { EmptyState } from "@/components/empty-state";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import { useCountUp } from "@/lib/use-count-up";

type OverviewSnapshot = { ACTIVE: number; running: number; failed: number };

const TASK_STATUS_ORDER = [
  { key: "running", label: "运行中", bar: "bg-info" },
  { key: "pending", label: "等待中", bar: "bg-neutral-strong" },
  { key: "completed", label: "已完成", bar: "bg-success" },
  { key: "failed", label: "失败", bar: "bg-error" },
  { key: "dead_letter", label: "死信", bar: "bg-violet" },
] as const;

const MEMORY_STATUS_ORDER = [
  { key: "ACTIVE", label: "有效", bar: "bg-success" },
  { key: "SUPERSEDED", label: "已取代", bar: "bg-neutral-strong" },
  { key: "DELETED", label: "已删除", bar: "bg-neutral-strong" },
  { key: "SUPPRESSED", label: "已抑制", bar: "bg-neutral-strong" },
] as const;

const CLASSIFICATION_ORDER = [
  { key: "normal", label: "普通", bar: "bg-success" },
  { key: "personal", label: "个人", bar: "bg-info" },
  { key: "sensitive", label: "敏感", bar: "bg-warning" },
  { key: "restricted", label: "受限", bar: "bg-error" },
] as const;

const SOURCE_TYPE_ORDER = [
  { key: "chat_round", label: "对话抽取", bar: "bg-info" },
  { key: "manual_fix", label: "人工修正", bar: "bg-warning" },
  { key: "session_rebuild", label: "会话重建", bar: "bg-success" },
  { key: "import", label: "导入", bar: "bg-neutral-strong" },
  { key: "system_migration", label: "系统迁移", bar: "bg-violet" },
] as const;

function DistributionBars({
  title,
  description,
  rows,
  counts,
  className,
}: {
  title: string;
  description: string;
  rows: readonly { key: string; label: string; bar: string }[];
  counts: Record<string, number>;
  className?: string;
}) {
  const known = rows.reduce((sum, row) => sum + (counts[row.key] ?? 0), 0);
  const unknown = Object.entries(counts).reduce(
    (sum, [k, v]) => (rows.some((r) => r.key === k) ? sum : sum + v),
    0,
  );
  const total = known + unknown;
  const max = Math.max(1, ...rows.map((row) => counts[row.key] ?? 0), unknown);
  return (
    <Card className={className}>
      <CardHeader className="pb-2">
        <div className="flex items-baseline justify-between gap-3">
          <CardTitle className="text-sm">{title}</CardTitle>
          <p className="text-2xl font-semibold tabular-nums text-foreground-intense">
            {total.toLocaleString()}
          </p>
        </div>
        <CardDescription className="text-xs">{description}</CardDescription>
      </CardHeader>
      <CardContent>
        <ul className="space-y-2.5">
          {rows.map((row) => {
            const value = counts[row.key] ?? 0;
            const pct = total > 0 ? Math.round((value / total) * 100) : 0;
            return (
              <li key={row.key} className="flex items-center gap-3">
                <span className="w-24 shrink-0 text-xs text-foreground-muted">{row.label}</span>
                <span
                  aria-hidden="true"
                  className="h-1 flex-1 overflow-hidden rounded-full bg-background-strong"
                >
                  <span
                    className={cn("block h-full rounded-full", row.bar)}
                    style={{ width: `${(value / max) * 100}%` }}
                  />
                </span>
                <span className="w-20 text-right font-mono text-xs tabular-nums text-foreground-emphasis">
                  {value} <span className="text-foreground-muted">({pct}%)</span>
                </span>
              </li>
            );
          })}
          {unknown > 0 ? (
            <li className="flex items-center gap-3">
              <span className="w-24 shrink-0 text-xs text-foreground-muted">其他</span>
              <span
                aria-hidden="true"
                className="h-1 flex-1 overflow-hidden rounded-full bg-background-strong"
              >
                <span
                  className="block h-full rounded-full bg-neutral-strong"
                  style={{ width: `${(unknown / max) * 100}%` }}
                />
              </span>
              <span className="w-20 text-right font-mono text-xs tabular-nums text-foreground-emphasis">
                {unknown}
              </span>
            </li>
          ) : null}
        </ul>
      </CardContent>
    </Card>
  );
}

function formatUptime(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86400) return `${(seconds / 3600).toFixed(1)}h`;
  return `${(seconds / 86400).toFixed(1)}d`;
}

function truncateMiddle(value: string, head: number, tail: number): string {
  if (value.length <= head + tail + 3) return value;
  return `${value.slice(0, head)}…${value.slice(-tail)}`;
}

const REFETCH_SECONDS = 30;

export function OverviewPage() {
  const { data, isPending, isError, error, refetch, dataUpdatedAt } = useOverview();
  const health = useHealthDetail();
  const reclaim = useReclaimOrphanTasks();
  const [secondsToNext, setSecondsToNext] = useState(REFETCH_SECONDS);
  // delta 计算：每次 fetch 把"上次成功的 snapshot"作为 prev，下一次 fetch 时再换。
  // 用 ref 保存"上次成功值"，state 保存"当前 prev(供渲染读取)"，避免 prev 永远是当前值的 bug。
  const lastSnapshotRef = useRef<OverviewSnapshot | null>(null);
  const [prevValues, setPrevValues] = useState<OverviewSnapshot | null>(null);

  useEffect(() => {
    if (!data) return;
    const snapshot: OverviewSnapshot = {
      ACTIVE: data.memories.ACTIVE ?? 0,
      running: data.tasks.running ?? 0,
      failed: (data.tasks.failed ?? 0) + (data.tasks.dead_letter ?? 0),
    };
    setPrevValues(lastSnapshotRef.current);
    lastSnapshotRef.current = snapshot;
  }, [dataUpdatedAt, data]);

  useEffect(() => {
    const startedAt = dataUpdatedAt || Date.now();
    const tick = () => {
      const elapsed = Math.floor((Date.now() - startedAt) / 1000);
      setSecondsToNext(Math.max(0, REFETCH_SECONDS - elapsed));
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [dataUpdatedAt]);

  // Hero KPI count-up：data 不可达时用 0 占位,加载完后从 0 缓动到真实值
  const activeMemories = data?.memories.ACTIVE ?? 0;
  const animatedActive = useCountUp(activeMemories);

  if (isPending) {
    return (
      <div aria-busy="true" className="space-y-4">
        <PageHeader title="总览" />
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          {Array.from({ length: 4 }).map((_, index) => (
            <Skeleton key={index} className="h-24 rounded-xl" />
          ))}
        </div>
        <Skeleton className="h-48 rounded-xl" />
      </div>
    );
  }

  if (isError) {
    const errMsg = error instanceof Error ? error.message : String(error);
    return (
      <div className="space-y-4">
        <PageHeader title="总览" />
        <EmptyState
          variant="error"
          icon={AlertTriangle}
          title="无法加载总览数据"
          description="请确认后端服务可达。"
          detail={errMsg.slice(0, 240)}
          action={{ label: "重试", onClick: () => refetch() }}
        />
      </div>
    );
  }

  // Hooks 必须在 early return 之前调用(Rules of Hooks)
  // data 不可达时 useCountUp 用 0 占位,数据加载后会从 0 缓动到真实值

  const failed = (data.tasks.failed ?? 0) + (data.tasks.dead_letter ?? 0);
  const failedTasks = data.recent_failed_tasks ?? [];
  const queueUsed = data.l3.max_pending_tasks - data.l3.available_capacity;
  const queuePct = Math.round((queueUsed / Math.max(1, data.l3.max_pending_tasks)) * 100);
  const hasFailedTasks = failedTasks.length > 0;
  const unhealthy = failed > 0 || data.l3.available_capacity === 0 || hasFailedTasks;
  const throughput = data.throughput_5min;
  const totalOps = throughput.append_ok.count + throughput.append_fail.count + throughput.recall_ok.count + throughput.recall_fail.count;

  const activeDelta = prevValues?.ACTIVE !== undefined ? activeMemories - prevValues.ACTIVE : 0;

  const pulses = [
    {
      label: "RUNNING",
      value: String(data.tasks.running ?? 0),
      suffix: "tasks",
      tone: "neutral" as const,
      delta: prevValues?.running !== undefined ? (data.tasks.running ?? 0) - prevValues.running : undefined,
      trend: data.trends?.running_tasks_24h ?? [],
    },
    {
      label: "FAILED",
      value: String(failed),
      tone: failed > 0 ? ("warning" as const) : ("neutral" as const),
      suffix: failed > 0 ? "needs attention" : "all clear",
      delta: prevValues ? failed - prevValues.failed : undefined,
      trend: data.trends?.failed_24h ?? [],
    },
    {
      label: "L3 QUEUE",
      value: `${queueUsed}/${data.l3.max_pending_tasks}`,
      suffix: `${queuePct}% used`,
      tone: "neutral" as const,
      delta: undefined,
      trend: data.trends?.l3_queue_24h ?? [],
    },
    {
      label: "UPTIME",
      value: formatUptime(health.data?.uptime_seconds ?? 0),
      suffix: health.data ? "stable" : "—",
      tone: "neutral" as const,
      delta: undefined,
      trend: data.trends?.recall_24h ?? [],
    },
  ];

  return (
    <div className="space-y-6">
      <PageHeader
        title="总览"
        actions={
          <div className="flex items-center gap-2 text-xs">
            <span className="sr-only" aria-live="polite">
              {unhealthy ? "存在需关注项" : "状态正常"}
            </span>
            {unhealthy ? <Badge variant="warning">需关注</Badge> : <Badge variant="secondary">正常</Badge>}
            <span
              className={cn(
                "flex items-center text-foreground-muted tabular-nums",
                secondsToNext <= 3 && "animate-pulse-soft text-warning",
              )}
            >
              <Clock aria-hidden="true" className="-mt-0.5 mr-1 inline size-3" />
              {secondsToNext}s
            </span>
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                refetch();
                void health.refetch();
              }}
              aria-label="立即刷新"
            >
              <RefreshCw aria-hidden="true" /> 刷新
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() =>
                reclaim.mutate(undefined, {
                  onSuccess: (result) => {
                    toast.success(
                      result.reclaimed_count === 0
                        ? "没有需要回收的孤儿任务"
                        : `回收了 ${result.reclaimed_count} 个孤儿任务`,
                    );
                  },
                  onError: (err) =>
                    toast.error(`回收失败: ${err instanceof Error ? err.message : String(err)}`),
                })
              }
              disabled={reclaim.isPending}
              aria-label="回收孤儿 running 任务"
            >
              <ShieldAlert aria-hidden="true" /> 回收孤儿
            </Button>
          </div>
        }
      />

      {/* HERO: 单一 96px 数字作为"今天的头条事实" + 24h sparkline 趋势 */}
      <section className="animate-editorial-fade-up pt-2">
        <p className="section-label mb-3">ACTIVE MEMORIES · 实时索引</p>
        <div className="flex items-end gap-6 flex-wrap">
          <span className="text-display-xl text-foreground-intense leading-none">
            {animatedActive.toLocaleString()}
          </span>
          <div className="flex-1 min-w-[180px] max-w-[280px] pb-2">
            <Sparkline
              data={data.trends?.active_memories_24h ?? []}
              width={260}
              height={48}
              fill
              smooth
              strokeClass="stroke-foreground-intense"
              endClass="fill-foreground-intense"
              ariaLabel="过去 24 小时有效记忆数趋势"
            />
            <p className="mt-1 flex items-center justify-between text-xs text-foreground-muted">
              <span>过去 24h</span>
              <span>
                {data.trends?.active_memories_24h?.[0] !== undefined
                  ? `起 ${data.trends.active_memories_24h[0].toLocaleString()}`
                  : "—"}
              </span>
            </p>
          </div>
          <div className="flex flex-col gap-1 pb-2">
            {activeDelta !== 0 ? (
              <span
                className={cn(
                  "flex items-center gap-1 text-base font-medium tabular-nums",
                  activeDelta > 0 ? "text-error" : "text-success",
                )}
                aria-label={activeDelta > 0 ? `增加 ${activeDelta}` : `减少 ${-activeDelta}`}
              >
                {activeDelta > 0 ? (
                  <ArrowUp aria-hidden="true" className="size-4" />
                ) : (
                  <ArrowDown aria-hidden="true" className="size-4" />
                )}
                {Math.abs(activeDelta)}
              </span>
            ) : (
              <span className="text-base text-foreground-muted">stable</span>
            )}
            <span className="text-sm text-foreground-muted">
              next refresh · {secondsToNext}s
            </span>
            <StaleIndicator updatedAt={dataUpdatedAt} prefix="last sync" />
          </div>
        </div>
        <p className="mt-5 max-w-2xl text-base text-foreground-muted leading-relaxed">
          当前已索引 {activeMemories.toLocaleString()} 条有效记忆，覆盖{" "}
          {data.by_classification.normal?.toLocaleString() ?? 0} 条普通记忆与{" "}
          {data.by_classification.personal?.toLocaleString() ?? 0} 条个人偏好。
        </p>
      </section>

      <div className="editorial-rule my-10" />

      {/* SYSTEM PULSE: 4 个 48px 副 KPI */}
      <section className="animate-editorial-fade-up" style={{ animationDelay: "80ms" }}>
        <p className="section-label mb-5">SYSTEM PULSE · 系统脉搏</p>
        <div className="grid grid-cols-2 gap-8 lg:grid-cols-4">
          {pulses.map((pulse, i) => (
            <div
              key={pulse.label}
              className="space-y-3 animate-editorial-fade-up"
              style={{ animationDelay: `${120 + i * 80}ms` }}
            >
              <p className="section-label">{pulse.label}</p>
              <p
                className={cn(
                  "text-display-md tabular-nums leading-none",
                  pulse.tone === "warning" ? "text-error" : "text-foreground-intense",
                )}
              >
                {pulse.value}
              </p>
              {pulse.trend && pulse.trend.length > 1 ? (
                <Sparkline
                  data={pulse.trend}
                  width={180}
                  height={28}
                  fill
                  smooth
                  strokeClass={pulse.tone === "warning" ? "stroke-error" : "stroke-foreground-emphasis"}
                  endClass={pulse.tone === "warning" ? "fill-error" : "fill-foreground-intense"}
                  ariaLabel={`${pulse.label} 过去 24h 趋势`}
                />
              ) : null}
              <p className="flex items-center gap-2 text-sm text-foreground-muted">
                <span>{pulse.suffix}</span>
                {pulse.delta !== undefined && pulse.delta !== 0 ? (
                  <span
                    className={cn(
                      "flex items-center text-xs tabular-nums",
                      pulse.delta > 0 ? "text-error" : "text-success",
                    )}
                    aria-label={pulse.delta > 0 ? `增加 ${pulse.delta}` : `减少 ${-pulse.delta}`}
                  >
                    {pulse.delta > 0 ? (
                      <ArrowUp aria-hidden="true" className="size-3" />
                    ) : (
                      <ArrowDown aria-hidden="true" className="size-3" />
                    )}
                    {Math.abs(pulse.delta)}
                  </span>
                ) : null}
              </p>
            </div>
          ))}
        </div>
      </section>

      {/* NEEDS ATTENTION: 纵向 editorial 列表 */}
      {hasFailedTasks || failed > 0 ? (
        <section
          className="animate-editorial-fade-up rounded-r-xl border-l-[3px] border-error bg-error-soft/15 py-4 pl-5 pr-4"
          style={{ animationDelay: "200ms" }}
        >
          <div className="mb-4 flex items-baseline justify-between">
            <p className="section-label text-error">NEEDS ATTENTION · 需关注</p>
            <Link
              to="/tasks"
              search={{
                tab: failedTasks[0]?.status === "dead_letter" ? "dead_letter" : "failed",
              }}
              className="text-sm text-foreground-emphasis hover:underline"
            >
              {failed} item{failed !== 1 ? "s" : ""} →
            </Link>
          </div>
          <ul className="divide-y divide-[#f5f2ec] border-y border-[#f5f2ec]">
            {failedTasks.slice(0, 5).map((task) => (
              <li
                key={task.task_id}
                className="flex items-center justify-between gap-4 py-3 group"
              >
                <div className="flex min-w-0 flex-1 items-center gap-4">
                  <span
                    aria-hidden="true"
                    className="block size-1.5 shrink-0 rounded-full bg-error"
                  />
                  <Link
                    to="/tasks"
                    search={{
                      tab: task.status === "dead_letter" ? "dead_letter" : "failed",
                    }}
                    className="truncate font-mono text-sm text-foreground-emphasis hover:underline"
                    title={task.task_id}
                  >
                    {truncateMiddle(task.task_id, 16, 8)}
                  </Link>
                  <StatusBadge status={task.status} />
                  <span className="text-sm text-foreground-muted">{task.op_type}</span>
                </div>
                {task.last_error ? (
                  <span
                    className="hidden max-w-[40%] truncate font-mono text-xs text-error md:block"
                    title={task.last_error}
                  >
                    {task.last_error}
                  </span>
                ) : null}
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {/* 服务身份 */}
      <Card variant="elevated" className="animate-editorial-fade-up" style={{ animationDelay: "250ms" }}>
        <CardHeader className="border-b border-border-muted pb-3">
          <CardTitle className="flex items-center gap-2 text-base">
            <Server aria-hidden="true" className="size-4" /> 服务身份
          </CardTitle>
          <CardDescription className="text-xs">版本 / 环境 / 关键开关（运行时只读视图）</CardDescription>
        </CardHeader>
        <CardContent>
          {health.isPending ? (
            <div aria-busy="true" className="space-y-2">
              <Skeleton className="h-4 w-1/2" />
              <Skeleton className="h-4 w-1/3" />
            </div>
          ) : health.isError ? (
            <p className="text-xs text-foreground-muted">无法加载服务身份</p>
          ) : health.data ? (
            <dl className="grid grid-cols-1 gap-x-6 gap-y-3 text-sm sm:grid-cols-2">
              <Row label="应用版本">
                <span className="font-mono">{health.data.app.version}</span>
                <Badge variant="secondary" className="ml-2">{health.data.app.environment}</Badge>
              </Row>
              <Row label="运行时间">{formatUptime(health.data.uptime_seconds)}</Row>
              <Row label="Alembic">
                {health.data.alembic_current ? (
                  <>
                    <span className="font-mono">{health.data.alembic_current}</span>
                    <Badge variant="secondary" className="ml-2">head</Badge>
                  </>
                ) : (
                  <span className="text-foreground-muted">—</span>
                )}
              </Row>
              <Row label="DB 连接池">
                <Database aria-hidden="true" className="mr-1 inline size-3" />
                <span className="font-mono tabular-nums">
                  {health.data.db_pool.checked_out}/{health.data.db_pool.size}
                </span>
                {health.data.db_pool.max_overflow !== null ? (
                  <span className="ml-1 text-foreground-muted">
                    +{health.data.db_pool.max_overflow}
                  </span>
                ) : null}
              </Row>
              <Row label="gRPC">
                <span className="font-mono tabular-nums">
                  {health.data.grpc.enabled
                    ? `${health.data.grpc.host}:${health.data.grpc.port}`
                    : "未启用"}
                </span>
              </Row>
              <Row label="关键开关">
                <div className="flex flex-wrap gap-1">
                  <Badge variant={health.data.flags.memory_l2_llm_enabled ? "secondary" : "outline"}>
                    L2 LLM {health.data.flags.memory_l2_llm_enabled ? "开" : "关"}
                  </Badge>
                  <Badge variant={health.data.flags.memory_decay_enabled ? "secondary" : "outline"}>
                    Decay {health.data.flags.memory_decay_enabled ? "开" : "关"}
                  </Badge>
                  <Badge variant="outline">
                    抽取 {health.data.flags.memory_infer_facts ? "开" : "关"}
                  </Badge>
                </div>
              </Row>
              <Row label="P0 白名单">
                {health.data.flags.memory_p0_slots.length > 0 ? (
                  <div className="flex flex-wrap gap-1">
                    {health.data.flags.memory_p0_slots.map((slot) => (
                      <Badge key={slot} variant="warning">
                        {slot}
                      </Badge>
                    ))}
                  </div>
                ) : (
                  <span className="text-foreground-muted">空 · 全部 slot 可见</span>
                )}
              </Row>
            </dl>
          ) : null}
        </CardContent>
      </Card>

      <div className="grid grid-cols-1 gap-6 sm:grid-cols-2 xl:grid-cols-3">
        {[
          { title: "任务状态", description: "实时快照", rows: TASK_STATUS_ORDER, counts: data.tasks, className: undefined as string | undefined },
          { title: "数据分类", description: "合规审计", rows: CLASSIFICATION_ORDER, counts: data.by_classification, className: undefined },
          { title: "记忆来源", description: "人工干预量", rows: SOURCE_TYPE_ORDER, counts: data.by_source_type, className: undefined },
          { title: "记忆状态", description: "本地索引 · 长期趋势观察对象", rows: MEMORY_STATUS_ORDER, counts: data.memories, className: "sm:col-span-2 xl:col-span-3" },
        ].map((cfg, i) => (
          <div
            key={cfg.title}
            className="animate-editorial-fade-up"
            style={{ animationDelay: `${300 + i * 60}ms` }}
          >
            <DistributionBars {...cfg} />
          </div>
        ))}
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
        <Card variant="elevated" className="animate-editorial-fade-up" style={{ animationDelay: "550ms" }}>
          <CardHeader className="border-b border-border-muted pb-3">
            <CardTitle className="flex items-center gap-2 text-base">
              <ListTodo aria-hidden="true" className="size-4" /> L3 后台写
            </CardTitle>
            <CardDescription className="text-xs">执行器与队列</CardDescription>
          </CardHeader>
          <CardContent>
            <dl className="grid grid-cols-2 gap-3">
              <Mini label="写模式" value={data.l3.write_mode} mono />
              <Mini label="worker" value={String(data.l3.executor_workers)} />
              <Mini label="待写" value={String(data.l3.pending_write_tasks)} warn={data.l3.pending_write_tasks > 0} />
              <Mini label="清理" value={String(data.l3.cleanup_tasks)} warn={data.l3.cleanup_tasks > 0} />
              <Mini label="剩余容量" value={String(data.l3.available_capacity)} />
              <Mini label="队列" value={`${queueUsed}/${data.l3.max_pending_tasks}`} />
            </dl>
          </CardContent>
        </Card>

        <Card variant="elevated" className="animate-editorial-fade-up lg:col-span-2" style={{ animationDelay: "600ms" }}>
          <CardHeader className="border-b border-border-muted pb-3">
            <div className="flex items-start justify-between">
              <div>
                <CardTitle className="flex items-center gap-2 text-base">
                  <Activity aria-hidden="true" className="size-4" /> 5min 吞吐
                </CardTitle>
                <CardDescription className="text-xs">
                  滑动窗口（{totalOps} 次操作）· 失败率仅统计 append
                </CardDescription>
              </div>
              <dl className="hidden sm:grid grid-cols-4 gap-3 text-right">
                <div>
                  <dt className="text-[10px] text-foreground-muted">append ok</dt>
                  <dd className="font-mono text-sm tabular-nums text-success">
                    {throughput.append_ok.count}
                  </dd>
                </div>
                <div>
                  <dt className="text-[10px] text-foreground-muted">append fail</dt>
                  <dd className={cn("font-mono text-sm tabular-nums", throughput.append_fail.count > 0 ? "text-error" : "text-foreground-emphasis")}>
                    {throughput.append_fail.count}
                  </dd>
                </div>
                <div>
                  <dt className="text-[10px] text-foreground-muted">recall ok</dt>
                  <dd className="font-mono text-sm tabular-nums text-foreground-emphasis">
                    {throughput.recall_ok.count}
                  </dd>
                </div>
                <div>
                  <dt className="text-[10px] text-foreground-muted">recall fail</dt>
                  <dd className={cn("font-mono text-sm tabular-nums", throughput.recall_fail.count > 0 ? "text-error" : "text-foreground-emphasis")}>
                    {throughput.recall_fail.count}
                  </dd>
                </div>
              </dl>
            </div>
          </CardHeader>
          <CardContent>
            {totalOps === 0 ? (
              <p className="py-6 text-center text-sm text-foreground-muted">
                过去 5min 无 append / recall 调用
              </p>
            ) : (
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                <div>
                  <p className="section-label mb-2">append 写入</p>
                  <Heatmap
                    data={throughput.append_ok.timeline}
                    rows={5}
                    cols={12}
                    ariaLabel="过去 5 分钟 append 写入分布"
                    cellClassName="bg-success"
                  />
                </div>
                <div>
                  <p className="section-label mb-2">recall 检索</p>
                  <Heatmap
                    data={throughput.recall_ok.timeline}
                    rows={5}
                    cols={12}
                    ariaLabel="过去 5 分钟 recall 检索分布"
                    cellClassName="bg-info"
                  />
                </div>
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      {data.recent_audit_actions?.length ? (
        <Card variant="elevated" className="animate-editorial-fade-up" style={{ animationDelay: "650ms" }}>
          <CardHeader className="border-b border-border-muted pb-3">
            <div className="flex items-center justify-between">
              <CardTitle className="text-base">最近治理动作</CardTitle>
              <Link to="/audit" className="text-xs text-foreground-intense hover:underline">
                查看全部 →
              </Link>
            </div>
            <CardDescription className="text-xs flex items-center gap-2">
              Top-5
              <StaleIndicator updatedAt={dataUpdatedAt} prefix="updated" />
            </CardDescription>
          </CardHeader>
          <CardContent className="p-0">
            <ol className="relative pl-6 pr-4">
              {data.recent_audit_actions.map((action, i) => {
                const tone = action.action === "delete" ? "bg-error" : action.action === "update" ? "bg-info" : "bg-violet";
                const label = action.action === "delete" ? "删除" : action.action === "update" ? "更新" : "重建";
                return (
                  <li
                    key={action.audit_id}
                    className="relative flex items-center gap-3 py-3 animate-editorial-fade-up"
                    style={{ animationDelay: `${i * 40}ms` }}
                  >
                    <div className="absolute -left-3.5 top-1/2 -translate-y-1/2 flex flex-col items-center">
                      <span className={cn("z-10 size-2.5 rounded-full ring-4 ring-background", tone)} />
                      {i < data.recent_audit_actions.length - 1 ? (
                        <span className="absolute top-2.5 h-[calc(100%+0.5rem)] w-px bg-border" />
                      ) : null}
                    </div>
                    <Badge variant={action.action === "delete" ? "error" : action.action === "update" ? "info" : "violet"}>
                      {label}
                    </Badge>
                    <Link
                      to="/audit"
                      className="flex-1 truncate font-mono text-xs text-foreground-emphasis hover:text-foreground-intense hover:underline"
                      title={action.target}
                    >
                      {truncateMiddle(action.target, 18, 6)}
                    </Link>
                    <span className="text-xs text-foreground-muted tabular-nums">
                      {action.created_at
                        ? new Date(action.created_at).toLocaleString("zh-CN", {
                            month: "2-digit",
                            day: "2-digit",
                            hour: "2-digit",
                            minute: "2-digit",
                            hour12: false,
                          }).replace(/\//g, "-")
                        : "—"}
                    </span>
                  </li>
                );
              })}
            </ol>
          </CardContent>
        </Card>
      ) : null}
    </div>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center gap-3 overflow-hidden">
      <dt className="w-24 shrink-0 text-xs text-foreground-muted">{label}</dt>
      <dd className="min-w-0 flex-1 overflow-hidden text-foreground-emphasis">{children}</dd>
    </div>
  );
}

function Mini({
  label,
  value,
  suffix,
  mono = false,
  warn = false,
  error = false,
}: {
  label: string;
  value: string;
  suffix?: string;
  mono?: boolean;
  warn?: boolean;
  error?: boolean;
}) {
  return (
    <div>
      <dt className="text-xs text-foreground-muted">{label}</dt>
      <dd
        className={cn(
          "mt-0.5 text-base font-medium tabular-nums",
          mono && "font-mono",
          warn && "text-warning",
          error && "text-error",
        )}
      >
        {value}
        {suffix ? (
          <span className="ml-1.5 text-xs font-normal text-foreground-muted">{suffix}</span>
        ) : null}
      </dd>
    </div>
  );
}