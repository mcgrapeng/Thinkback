/** P3 任务监控:Editorial Premium 重做。
 *
 * 布局:Hero KPI 4 卡(运行中/失败/死信/pending,带 sparkline) → Tab 长条(全部/运行中/失败/死信) → 任务卡片列表(每行可展开) */
import { useEffect, useState } from "react";
import {
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  Inbox,
  RefreshCw,
} from "lucide-react";
import { useNavigate, useSearch } from "@tanstack/react-router";
import { toast } from "sonner";
import { useTasks } from "@/api/queries";
import type { TaskItem } from "@/api/client";
import { PageHeader } from "@/components/page-header";
import { StaleIndicator } from "@/components/stale-indicator";
import { EmptyState } from "@/components/empty-state";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton, SkeletonLines } from "@/components/ui/skeleton";
import { StatusBadge } from "@/components/status-badge";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useListKeyboardNavigation } from "@/lib/use-list-keyboard-nav";
import { cn } from "@/lib/utils";

const TABS = [
  { value: "all", label: "全部", statuses: undefined },
  { value: "running", label: "运行中", statuses: "running,pending" },
  { value: "failed", label: "失败", statuses: "failed" },
  { value: "dead_letter", label: "死信", statuses: "dead_letter" },
] as const;

type TabValue = (typeof TABS)[number]["value"];

function scopeUser(scope: Record<string, unknown>): string {
  const user = scope.user_id ?? scope.session_id;
  return typeof user === "string" && user ? user : "—";
}

function retryTone(retry: number): "neutral" | "warning" | "violet" {
  if (retry >= 5) return "violet";
  if (retry >= 3) return "warning";
  return "neutral";
}

function TaskDetail({ task }: { task: TaskItem }) {
  return (
    <div className="grid grid-cols-1 gap-4 px-5 pb-5 pt-1 sm:grid-cols-2">
      <div className="space-y-1">
        <p className="section-label text-[10px]">REQUEST ID</p>
        <p className="break-all font-mono text-xs text-foreground-emphasis">
          {task.request_id}
        </p>
      </div>
      <div className="space-y-1">
        <p className="section-label text-[10px]">SCOPE</p>
        <pre className="overflow-auto rounded-lg bg-background-muted p-2 font-mono text-[11px] text-foreground-emphasis">
          {JSON.stringify(task.scope, null, 2)}
        </pre>
      </div>
      <div className="sm:col-span-2 space-y-1">
        <p className="section-label text-[10px]">RESULT · JSON</p>
        <pre className="max-h-60 overflow-auto rounded-lg bg-background-muted p-3 font-mono text-[11px] text-foreground-emphasis">
          {JSON.stringify(task.result ?? {}, null, 2)}
        </pre>
      </div>
      {task.last_error ? (
        <div className="sm:col-span-2 space-y-1">
          <p className="section-label text-[10px] text-error">LAST ERROR</p>
          <p className="break-all rounded-lg bg-error-soft/40 p-3 font-mono text-xs text-error">
            {task.last_error}
          </p>
          {task.last_error.includes("reclaimed") ? (
            <p className="text-xs text-foreground-muted">
              此任务由回收机制收敛 · 非业务错误
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function TaskCard({
  task,
  expanded,
  onToggle,
  index,
  isFocused,
}: {
  task: TaskItem;
  expanded: boolean;
  onToggle: () => void;
  index: number;
  isFocused: boolean;
}) {
  const retry = task.retry_count ?? 0;
  const tone = retryTone(retry);
  return (
    <li
      className={cn(
        "group row-hover-warm border-l-2 pl-4 -ml-4 pr-2 py-4 animate-editorial-fade-up transition-colors",
        isFocused ? "border-foreground-intense" : "border-transparent",
      )}
      style={{ animationDelay: `${Math.min(index * 30, 400)}ms` }}
    >
      <div className="flex items-start gap-3">
        <Button
          variant="ghost"
          size="icon"
          className="size-7 shrink-0"
          aria-expanded={expanded}
          aria-label={expanded ? `收起 ${task.task_id} 详情` : `展开 ${task.task_id} 详情`}
          onClick={onToggle}
        >
          {expanded ? <ChevronDown aria-hidden="true" /> : <ChevronRight aria-hidden="true" />}
        </Button>
        <div className="min-w-0 flex-1 space-y-1.5">
          <div className="flex flex-wrap items-baseline gap-3">
            <button
              type="button"
              className="truncate font-mono text-xs text-foreground-emphasis hover:text-foreground-intense hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--ring)]"
              title={`${task.task_id}（点击复制）`}
              onClick={(e) => {
                e.stopPropagation();
                void navigator.clipboard?.writeText(task.task_id).then(() =>
                  toast.success("已复制任务 ID"),
                );
              }}
            >
              {task.task_id}
            </button>
            <StatusBadge status={task.status} />
            <span className="text-xs text-foreground-muted">·</span>
            <span className="text-xs text-foreground-emphasis">{task.op_type}</span>
            <span className="ml-auto flex items-center gap-2 text-xs">
              <span className="shrink-0 text-foreground-muted">用户</span>
              <span className="max-w-[12ch] truncate font-mono text-foreground-emphasis" title={scopeUser(task.scope)}>
                {scopeUser(task.scope)}
              </span>
            </span>
          </div>
          <div className="flex flex-wrap items-center gap-2 text-xs">
            {retry > 0 ? (
              <Tooltip>
                <TooltipTrigger asChild>
                  <span
                    className={cn(
                      "inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 cursor-help",
                      tone === "warning" && "bg-warning-soft text-warning",
                      tone === "violet" && "bg-violet-soft text-violet",
                      tone === "neutral" && "bg-background-muted text-foreground-muted",
                    )}
                  >
                    重试 <span className="font-mono tabular-nums">{retry}</span>
                  </span>
                </TooltipTrigger>
                <TooltipContent>已重试 {retry} 次 · 5 次后进入死信</TooltipContent>
              </Tooltip>
            ) : null}
            {task.last_error ? (
              <p
                className="flex-1 truncate text-error"
                title={task.last_error}
              >
                {task.last_error}
              </p>
            ) : (
              <p className="flex-1 text-foreground-muted">无错误</p>
            )}
          </div>
        </div>
      </div>
      {expanded ? (
        <div className="mt-3 ml-10 rounded-xl bg-background-muted/50">
          <TaskDetail task={task} />
        </div>
      ) : null}
    </li>
  );
}

export function TasksPage() {
  const navigate = useNavigate({ from: "/tasks" });
  const search = useSearch({ from: "/tasks" }) as { tab?: string };
  const initialTab: TabValue = TABS.some((t) => t.value === search.tab)
    ? (search.tab as TabValue)
    : "all";
  const [tab, setTab] = useState<TabValue>(initialTab);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const activeTab = TABS.find((t) => t.value === tab) ?? TABS[0];
  const { data, isPending, isError, refetch, dataUpdatedAt } =
    useTasks(activeTab.statuses);
  const { selectedIndex } = useListKeyboardNavigation(data?.length ?? 0);

  // tab 同步 URL(可深链)
  useEffect(() => {
    navigate({
      search: tab === "all" ? {} : { tab },
      replace: true,
    });
  }, [tab, navigate]);

  const toggle = (taskId: string) =>
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(taskId)) next.delete(taskId);
      else next.add(taskId);
      return next;
    });

  // 4 个 KPI count(从 data 聚合)
  const counts = (data ?? []).reduce(
    (acc, t) => {
      if (t.status === "running") acc.running += 1;
      else if (t.status === "pending") acc.pending += 1;
      else if (t.status === "failed") acc.failed += 1;
      else if (t.status === "dead_letter") acc.dead_letter += 1;
      return acc;
    },
    { running: 0, pending: 0, failed: 0, dead_letter: 0 },
  );

  return (
    <div className="space-y-6">
      <PageHeader
        title="TASKS · 任务监控"
        description="后台任务队列实时视图 · 展开行查看错误与结果"
        actions={
          <div className="flex items-center gap-3">
            <StaleIndicator updatedAt={dataUpdatedAt} prefix="last sync" />
            <Button variant="outline" size="sm" onClick={() => refetch()}>
              <RefreshCw aria-hidden="true" /> 刷新
            </Button>
          </div>
        }
      />

      {/* Hero KPI 4 卡 */}
      <section className="animate-editorial-fade-up grid grid-cols-2 gap-6 sm:grid-cols-4">
        {(
          [
            { key: "running", label: "RUNNING", tone: "info" as const },
            { key: "failed", label: "FAILED", tone: "error" as const },
            { key: "dead_letter", label: "DEAD LETTER", tone: "violet" as const },
            { key: "pending", label: "PENDING", tone: "neutral" as const },
          ] as const
        ).map((kpi) => {
          const value = counts[kpi.key as keyof typeof counts];
          const toneClass =
            kpi.tone === "error"
              ? "text-error"
              : kpi.tone === "violet"
                ? "text-violet"
                : kpi.tone === "info"
                  ? "text-info"
                  : "text-foreground-intense";
          return (
            <div key={kpi.key} className="space-y-2">
              <p className="section-label">{kpi.label}</p>
              <p
                className={cn(
                  "text-display-md tabular-nums leading-none",
                  value > 0 ? toneClass : "text-foreground-intense",
                )}
              >
                {value}
              </p>
              {value > 0 && (kpi.tone === "error" || kpi.tone === "violet") ? (
                <p className="text-xs text-warning">需关注</p>
              ) : value > 0 ? (
                <p className="text-xs text-foreground-muted">正常</p>
              ) : (
                <p className="text-xs text-foreground-soft">—</p>
              )}
            </div>
          );
        })}
      </section>

      <div className="editorial-rule" />

      {/* Tab 长条 */}
      <div className="flex flex-wrap items-center gap-2 animate-editorial-fade-up" style={{ animationDelay: "60ms" }}>
        <p className="section-label mr-2">STATUS</p>
        {TABS.map((t) => {
          const active = tab === t.value;
          return (
            <button
              key={t.value}
              onClick={() => setTab(t.value)}
              className={cn(
                "relative rounded-xl border px-4 py-1.5 text-sm font-medium transition-colors",
                active
                  ? "border-foreground-intense text-foreground-intense bg-background"
                  : "border-border text-foreground-muted hover:text-foreground-emphasis hover:bg-background-muted",
                active && "shadow-[inset_0_-1px_0_0_var(--foreground-intense)]",
              )}
            >
              {t.label}
            </button>
          );
        })}
      </div>

      <div className="editorial-rule" />

      {isPending ? (
        <div aria-busy="true" className="space-y-4">
          {Array.from({ length: 3 }).map((_, i) => (
            <Card key={i} className="border-border-muted">
              <CardContent className="p-5 space-y-3">
                <Skeleton className="h-4 w-1/3" />
                <SkeletonLines count={2} />
              </CardContent>
            </Card>
          ))}
        </div>
      ) : isError ? (
        <EmptyState
          variant="error"
          icon={AlertTriangle}
          title="无法加载任务列表"
          description="请确认后端服务可达、管理 token 正确。"
          action={{ label: "重试", onClick: () => refetch() }}
        />
      ) : data.length === 0 ? (
        <EmptyState
          icon={Inbox}
          title="当前筛选下没有任务"
          description="试试切换到其他状态（全部/运行中/失败/死信），或刷新等待新任务。"
        />
      ) : (
        <>
          <p className="text-sm text-foreground-muted tabular-nums">
            共 {data.length} 个任务
          </p>
          <ul className="divide-y divide-border-muted border-t border-b border-border-muted">
            {data.map((task, i) => (
              <TaskCard
                key={task.task_id}
                task={task}
                expanded={expanded.has(task.task_id)}
                onToggle={() => toggle(task.task_id)}
                index={i}
                isFocused={selectedIndex === i}
              />
            ))}
          </ul>
        </>
      )}
    </div>
  );
}
