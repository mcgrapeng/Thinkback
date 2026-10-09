/** P5 审计日志:Editorial Premium 时间轴视图。
 *
 * 每条记录是一条时间轴节点:左侧时间(竖向刻度) + 右侧操作内容(操作者/动作/目标/详情)。 */

import { useState } from "react";
import { useSearch } from "@tanstack/react-router";
import { AlertTriangle, ScrollText } from "lucide-react";
import { toast } from "sonner";
import { useAudit } from "@/api/queries";
import { PageHeader } from "@/components/page-header";
import { StaleIndicator } from "@/components/stale-indicator";
import { Skeleton, SkeletonLines } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/empty-state";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { Info } from "lucide-react";
import { cn } from "@/lib/utils";

const ACTION_OPTIONS = [
  { value: "all", label: "全部动作" },
  { value: "delete", label: "删除" },
  { value: "update", label: "更新" },
  { value: "rebuild", label: "重建" },
] as const;

const ACTION_LABELS: Record<string, string> = {
  delete: "删除",
  update: "更新",
  rebuild: "重建",
};

const ACTION_TONE: Record<string, "error" | "info" | "violet"> = {
  delete: "error",
  update: "info",
  rebuild: "violet",
};

const PAGE_SIZE = 50;

function FieldHint({ text }: { text: string }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          aria-label="字段说明"
          className="inline-flex size-3.5 items-center justify-center rounded-full text-foreground-soft hover:text-foreground-emphasis hover:bg-background-muted transition-colors focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[var(--ring)]"
        >
          <Info aria-hidden="true" className="size-3" />
        </button>
      </TooltipTrigger>
      <TooltipContent className="max-w-xs">{text}</TooltipContent>
    </Tooltip>
  );
}

function TimelineNode({
  createdAt,
  action,
  target,
  operator,
  detail,
  isLast,
  index,
}: {
  createdAt: string;
  action: string;
  target: string;
  operator: string;
  detail: Record<string, unknown>;
  isLast: boolean;
  index: number;
}) {
  const tone = ACTION_TONE[action] ?? "neutral";
  const label = ACTION_LABELS[action] ?? action;
  return (
    <li
      className="relative flex gap-4 pb-6 animate-editorial-fade-up"
      style={{ animationDelay: `${Math.min(index * 30, 400)}ms` }}
    >
      {/* 左侧:时间 + 时间轴竖线 */}
      <div className="flex w-20 shrink-0 flex-col items-end pt-0.5">
        <time className="font-mono text-xs tabular-nums text-foreground-emphasis" dateTime={createdAt}>
          {new Date(createdAt).toLocaleTimeString("zh-CN", {
            hour: "2-digit",
            minute: "2-digit",
            second: "2-digit",
            hour12: false,
          })}
        </time>
        <time className="text-[10px] text-foreground-muted tabular-nums" dateTime={createdAt}>
          {new Date(createdAt).toLocaleDateString("zh-CN", {
            month: "2-digit",
            day: "2-digit",
          })}
        </time>
      </div>
      {/* 中心:圆点 + 竖线 */}
      <div className="relative flex flex-col items-center">
        <span
          className={cn(
            "z-10 mt-1.5 size-3 rounded-full ring-4 ring-background",
            tone === "error" && "bg-error",
            tone === "info" && "bg-info",
            tone === "violet" && "bg-violet",
          )}
        />
        {isLast ? null : (
          <span className="absolute top-4 h-full w-px bg-border" />
        )}
      </div>
      {/* 右侧:内容卡 */}
      <div className="flex-1 min-w-0">
        <div className="rounded-2xl border border-[#ebe7df] bg-card p-4 hover:shadow-md transition-shadow">
          <div className="flex flex-wrap items-center gap-2 mb-2">
          <Badge variant={tone as "error" | "info" | "violet"}>
              {label}
            </Badge>
            <span className="text-xs text-foreground-muted">
              由 <span className="font-mono text-foreground-emphasis">{operator}</span> 执行
            </span>
            <span className="ml-auto">
              <FieldHint text="审计日志是只读留痕,所有治理操作都会自动记录到这里,不可篡改。" />
            </span>
          </div>
          <button
            type="button"
            className="block w-full truncate text-left font-mono text-sm text-foreground-emphasis hover:text-foreground-intense hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--ring)]"
            title={`${target}（点击复制）`}
            onClick={() => {
              void navigator.clipboard?.writeText(target).then(() =>
                toast.success("已复制目标 ID"),
              );
            }}
          >
            {target}
          </button>
          {Object.keys(detail).length > 0 ? (
            <details className="mt-2 group">
              <summary className="cursor-pointer text-xs text-foreground-muted hover:text-foreground-emphasis select-none">
                详情 <span className="text-foreground-soft">({Object.keys(detail).length} 字段)</span>
              </summary>
              <pre className="mt-2 max-h-60 overflow-auto rounded-lg bg-background-muted p-3 font-mono text-[11px] text-foreground-emphasis">
                {JSON.stringify(detail, null, 2)}
              </pre>
            </details>
          ) : null}
        </div>
      </div>
    </li>
  );
}

export function AuditPage() {
  const search = useSearch({ from: "/audit" }) as { action?: string };
  const [action, setAction] = useState<string>(search.action ?? "all");
  const [limit, setLimit] = useState(PAGE_SIZE);
  const { data, isPending, isError, isFetching, dataUpdatedAt, refetch } = useAudit({
    action: action === "all" ? undefined : action,
    limit,
  });
  const isFull = (data?.length ?? 0) >= limit;

  return (
    <div className="space-y-6">
      <PageHeader
        title="AUDIT · 审计日志"
        description="治理台操作留痕 · 只读不可篡改"
        actions={
          <div className="flex items-center gap-3">
            <StaleIndicator updatedAt={dataUpdatedAt} prefix="last sync" />
            <Select value={action} onValueChange={setAction}>
              <SelectTrigger className="w-40" aria-label="动作筛选">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {ACTION_OPTIONS.map((opt) => (
                  <SelectItem key={opt.value} value={opt.value}>
                    {opt.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Button variant="outline" size="sm" onClick={() => refetch()}>
              刷新
            </Button>
          </div>
        }
      />

      <section className="animate-editorial-fade-up flex flex-wrap items-baseline gap-3">
        <p className="section-label">FILTERED BY</p>
        <p className="text-sm text-foreground-emphasis">
          {action === "all" ? "全部动作" : ACTION_LABELS[action] ?? action}
        </p>
      </section>

      <div className="editorial-rule" />

      {isError ? (
        <EmptyState
          variant="error"
          icon={AlertTriangle}
          title="无法加载审计日志"
          description="请确认后端服务可达。"
          action={{ label: "重试", onClick: () => window.location.reload() }}
        />
      ) : isPending ? (
        <div aria-busy="true" className="space-y-4 pl-24">
          {Array.from({ length: 3 }).map((_, i) => (
            <div key={i} className="rounded-2xl border border-[#ebe7df] bg-card p-4 space-y-2">
              <Skeleton className="h-4 w-1/4" />
              <SkeletonLines count={2} />
            </div>
          ))}
        </div>
      ) : data && data.length > 0 ? (
        <>
          <ol className="relative" aria-label="审计时间轴">
            {data.map((item, i) => (
              <TimelineNode
                key={item.audit_id}
                createdAt={item.created_at}
                action={item.action}
                target={item.target}
                operator={item.operator}
                detail={item.detail}
                isLast={i === data.length - 1}
                index={i}
              />
            ))}
          </ol>
          <div
            className="flex flex-wrap items-center justify-between gap-2 pl-24 text-xs text-foreground-muted"
            aria-live="polite"
          >
            <span>
              已显示 <span className="font-mono tabular-nums">{data.length}</span> 条
              {isFull ? " · 可能还有更多" : " · 已到底"}
            </span>
            <div className="flex items-center gap-3">
              {isFetching ? <span className="text-foreground-soft">加载中…</span> : null}
              {isFull ? (
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setLimit((n) => n + 50)}
                  aria-label="加载更多审计记录"
                >
                  加载更多(+50)
                </Button>
              ) : null}
            </div>
          </div>
        </>
      ) : (
        <EmptyState
          icon={ScrollText}
          title="暂无审计记录"
          description="该筛选条件下还没有任何操作记录。试试切换动作类型或扩大时间范围。"
        />
      )}
    </div>
  );
}
