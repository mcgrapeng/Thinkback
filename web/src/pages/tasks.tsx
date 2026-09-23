/** P3 任务监控：状态 Tab + 表格（桌面）/ 卡片（移动）+ 行展开详情。 */

import { useState } from "react";
import { AlertTriangle, ChevronDown, ChevronRight, Inbox, RefreshCw } from "lucide-react";
import { useSearch } from "@tanstack/react-router";
import { toast } from "sonner";
import { useTasks } from "@/api/queries";
import type { TaskItem } from "@/api/client";
import { PageHeader } from "@/components/page-header";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { StatusBadge } from "@/components/status-badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { cn } from "@/lib/utils";

const TABS = [
  { value: "all", label: "全部", statuses: undefined },
  { value: "running", label: "运行中", statuses: "running,pending" },
  { value: "failed", label: "失败", statuses: "failed" },
  { value: "dead_letter", label: "死信", statuses: "dead_letter" },
] as const;

/** retry 色阶：0-2 默认 / 3-4 警示 / 5 死信紫。 */
function retryTone(retry: number): string {
  if (retry >= 5) return "text-violet font-medium";
  if (retry >= 3) return "text-warning font-medium";
  return "";
}

function scopeUser(scope: Record<string, unknown>): string {
  const user = scope.user_id ?? scope.session_id;
  return typeof user === "string" && user ? user : "—";
}

function TaskDetail({ task }: { task: TaskItem }) {
  return (
    <div className="space-y-2 p-3">
      {task.last_error ? (
        <div>
          <h4 className="text-xs font-medium text-muted-foreground">最后错误</h4>
          <p className="font-mono text-xs break-all text-error">{task.last_error}</p>
        </div>
      ) : null}
      <div>
        <h4 className="text-xs font-medium text-muted-foreground">任务结果（JSON）</h4>
        <pre className="max-h-60 overflow-auto rounded-xl bg-background-muted p-3 font-mono text-xs">
          {JSON.stringify(task.result ?? {}, null, 2)}
        </pre>
      </div>
      <div>
        <h4 className="text-xs font-medium text-muted-foreground">请求 ID</h4>
        <p className="font-mono text-xs break-all">{task.request_id}</p>
      </div>
      {task.last_error?.includes("reclaimed") ? (
        <p className="text-xs text-muted-foreground">
          此任务由孤儿回收机制收敛为失败（进程重启/关机竞态遗留），非业务错误。
        </p>
      ) : null}
    </div>
  );
}

function TaskRow({
  task,
  expanded,
  onToggle,
}: {
  task: TaskItem;
  expanded: boolean;
  onToggle: () => void;
}) {
  return (
    <>
      <TableRow>
        <TableCell className="w-8">
          <Button
            variant="ghost"
            size="icon"
            className="size-7"
            aria-expanded={expanded}
            aria-label={expanded ? `收起 ${task.task_id} 详情` : `展开 ${task.task_id} 详情`}
            onClick={onToggle}
          >
            {expanded ? <ChevronDown aria-hidden="true" /> : <ChevronRight aria-hidden="true" />}
          </Button>
        </TableCell>
        <TableCell className="max-w-64">
          <button
            type="button"
            className="block w-full truncate text-left font-mono text-xs text-foreground-muted hover:text-foreground-intense hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--ring)]"
            title={`${task.task_id}（点击复制）`}
            onClick={() => {
              void navigator.clipboard?.writeText(task.task_id).then(() =>
                toast.success("已复制任务 ID"),
              );
            }}
          >
            {task.task_id}
          </button>
        </TableCell>
        <TableCell className="text-xs">{scopeUser(task.scope)}</TableCell>
        <TableCell className="text-xs">{task.op_type}</TableCell>
        <TableCell>
          <StatusBadge status={task.status} />
        </TableCell>
        <TableCell
          className={cn("font-mono text-xs tabular-nums", retryTone(task.retry_count ?? 0))}
        >
          {task.retry_count ?? 0}
        </TableCell>
        <TableCell className="max-w-72">
          <span
            className={cn(
              "block truncate text-xs",
              task.last_error && "text-error",
            )}
            title={task.last_error ?? ""}
          >
            {task.last_error ?? "—"}
          </span>
        </TableCell>
      </TableRow>
      {expanded ? (
        <TableRow className="hover:bg-transparent">
          <TableCell colSpan={7} className="bg-background-subtle p-0">
            <TaskDetail task={task} />
          </TableCell>
        </TableRow>
      ) : null}
    </>
  );
}

export function TasksPage() {
  // 支持 /tasks?tab=failed 深链（总览 KPI 下钻入口）；tab 切换不回写 URL
  const search = useSearch({ strict: false }) as { tab?: string };
  const [tab, setTab] = useState<(typeof TABS)[number]["value"]>(() =>
    TABS.some((item) => item.value === search.tab) ? (search.tab as (typeof TABS)[number]["value"]) : "all",
  );
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const activeTab = TABS.find((item) => item.value === tab) ?? TABS[0];
  const { data, isPending, isError, refetch } = useTasks(activeTab.statuses);

  const toggle = (taskId: string) =>
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(taskId)) next.delete(taskId);
      else next.add(taskId);
      return next;
    });

  return (
    <div className="space-y-6">
      <PageHeader
        title="任务监控"
        description="后台任务队列实时视图 · 展开行查看错误与结果"
        actions={
          <Button variant="outline" size="sm" onClick={() => refetch()}>
            <RefreshCw aria-hidden="true" /> 刷新
          </Button>
        }
      />

      <Tabs value={tab} onValueChange={(value) => setTab(value as typeof tab)}>
        <TabsList aria-label="按任务状态筛选">
          {TABS.map((item) => (
            <TabsTrigger key={item.value} value={item.value}>
              {item.label}
            </TabsTrigger>
          ))}
        </TabsList>
      </Tabs>

      {isPending ? (
        <div aria-busy="true" className="space-y-2">
          {Array.from({ length: 5 }).map((_, index) => (
            <Skeleton key={index} className="h-10" />
          ))}
        </div>
      ) : isError ? (
        <Alert variant="destructive">
          <AlertTriangle aria-hidden="true" />
          <AlertTitle>无法加载任务列表</AlertTitle>
          <AlertDescription>请确认后端服务可达、管理 token 正确。</AlertDescription>
        </Alert>
      ) : data.length === 0 ? (
        <div className="flex flex-col items-center gap-2 py-16 text-center">
          <Inbox aria-hidden="true" className="size-8 text-foreground-soft" />
          <p className="text-sm text-foreground-muted">当前筛选下没有任务</p>
        </div>
      ) : (
        <p className="sr-only" aria-live="polite">
          {data.length} 个任务
        </p>
      )}

      {data && data.length > 0 ? (
        <>
          {/* 桌面表格 */}
          <div className="hidden md:block">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-8">
                    <span className="sr-only">展开</span>
                  </TableHead>
                  <TableHead>任务 ID</TableHead>
                  <TableHead>用户</TableHead>
                  <TableHead>操作</TableHead>
                  <TableHead>状态</TableHead>
                  <TableHead>重试</TableHead>
                  <TableHead>最后错误</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.map((task) => (
                  <TaskRow
                    key={task.task_id}
                    task={task}
                    expanded={expanded.has(task.task_id)}
                    onToggle={() => toggle(task.task_id)}
                  />
                ))}
              </TableBody>
            </Table>
          </div>

          {/* 移动卡片列表（触达面积 ≥44px） */}
          <div className="space-y-2 md:hidden">
            {data.map((task) => (
              <Card key={task.task_id}>
                <CardContent className="p-4">
                  <button
                    type="button"
                    aria-expanded={expanded.has(task.task_id)}
                    onClick={() => toggle(task.task_id)}
                    className="flex min-h-11 w-full flex-col gap-1 text-left"
                  >
                    <span className="flex items-center justify-between gap-2">
                      <span className="truncate font-mono text-xs">{task.task_id}</span>
                      <StatusBadge status={task.status} />
                    </span>
                    <span className="flex items-center gap-2 text-xs text-muted-foreground">
                      <span>{scopeUser(task.scope)}</span>
                      <span>·</span>
                      <span>{task.op_type}</span>
                      <span>·</span>
                      <span className={cn("tabular-nums", retryTone(task.retry_count ?? 0))}>
                        {`重试 ${task.retry_count ?? 0}`}
                      </span>
                    </span>
                  </button>
                  {expanded.has(task.task_id) ? <TaskDetail task={task} /> : null}
                </CardContent>
              </Card>
            ))}
          </div>
        </>
      ) : null}
    </div>
  );
}
