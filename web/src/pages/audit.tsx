/** P5 审计日志：只读，治理动作全留痕。 */

import { useState } from "react";
import { AlertTriangle, ScrollText } from "lucide-react";
import { toast } from "sonner";
import { useAudit } from "@/api/queries";
import { PageHeader } from "@/components/page-header";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/empty-state";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { formatTime } from "@/lib/utils";

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

/** 动作徽章语义：删除 → error，其余中性（不借用任务状态色）。 */
const ACTION_TONES: Record<string, "error" | "neutral"> = {
  delete: "error",
  update: "neutral",
  rebuild: "neutral",
};

function ActionBadge({ action }: { action: string }) {
  if (action === "delete") {
    return (
      <Badge variant="error" className="bg-destructive text-destructive-foreground">
        {ACTION_LABELS[action] ?? action}
      </Badge>
    );
  }
  const tone = ACTION_TONES[action] ?? "neutral";
  return <Badge variant={tone}>{ACTION_LABELS[action] ?? action}</Badge>;
}

export function AuditPage() {
  const [action, setAction] = useState("all");
  const [expanded, setExpanded] = useState<string | null>(null);
  const [limit, setLimit] = useState(50);
  const { data, isPending, isError } = useAudit({
    action: action === "all" ? undefined : action,
    limit,
  });
  const isFull = (data?.length ?? 0) >= limit;

  return (
    <div className="space-y-6">
      <PageHeader
        title="审计日志"
        description="治理台操作留痕 · 只读不可篡改"
        actions={
          <div className="flex items-center gap-2">
            <div className="w-40">
              <label className="sr-only" htmlFor="audit-action">
                动作筛选
              </label>
              <Select value={action} onValueChange={setAction}>
                <SelectTrigger id="audit-action" aria-label="按动作筛选">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {ACTION_OPTIONS.map((option) => (
                    <SelectItem key={option.value} value={option.value}>
                      {option.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <Select
              value={String(limit)}
              onValueChange={(v) => setLimit(Number(v))}
            >
              <SelectTrigger className="w-28" aria-label="每页条数">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="50">50 / 页</SelectItem>
                <SelectItem value="100">100 / 页</SelectItem>
                <SelectItem value="200">200 / 页</SelectItem>
              </SelectContent>
            </Select>
          </div>
        }
      />

      {isError ? (
        <EmptyState
          variant="error"
          icon={AlertTriangle}
          title="无法加载审计日志"
          description="请确认后端服务可达。"
          action={{ label: "重试", onClick: () => window.location.reload() }}
        />
      ) : isPending ? (
        <div aria-busy="true" className="space-y-2">
          {Array.from({ length: 5 }).map((_, index) => (
            <Skeleton key={index} className="h-10" />
          ))}
        </div>
      ) : data && data.length > 0 ? (
        <>
          {/* 桌面表格 */}
          <div className="hidden md:block">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>时间</TableHead>
                  <TableHead>操作者</TableHead>
                  <TableHead>动作</TableHead>
                  <TableHead>目标</TableHead>
                  <TableHead>详情</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.map((item) => (
                  <TableRow key={item.audit_id}>
                    <TableCell className="whitespace-nowrap font-mono text-xs">
                      {formatTime(item.created_at)}
                    </TableCell>
                    <TableCell className="text-xs">{item.operator}</TableCell>
                    <TableCell>
                      <ActionBadge action={item.action} />
                    </TableCell>
                    <TableCell className="max-w-56">
                      <button
                        type="button"
                        className="block w-full truncate text-left font-mono text-xs text-foreground-muted hover:text-foreground-intense hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--ring)]"
                        title={`${item.target}（点击复制）`}
                        onClick={() => {
                          void navigator.clipboard?.writeText(item.target).then(() =>
                            toast.success("已复制目标 ID"),
                          );
                        }}
                      >
                        {item.target}
                      </button>
                    </TableCell>
                    <TableCell>
                      {Object.keys(item.detail).length > 0 ? (
                        <Button
                          variant="ghost"
                          size="sm"
                          aria-expanded={expanded === item.audit_id}
                          onClick={() =>
                            setExpanded(expanded === item.audit_id ? null : item.audit_id)
                          }
                        >
                          {expanded === item.audit_id ? "收起" : "展开"}
                        </Button>
                      ) : (
                        <span className="text-xs text-foreground-muted">—</span>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            {data
              .filter((item) => item.audit_id === expanded)
              .map((item) => (
                <pre
                  key={item.audit_id}
                  className="mt-3 max-h-60 overflow-auto rounded-xl bg-background-muted p-4 font-mono text-xs"
                >
                  {JSON.stringify(item.detail, null, 2)}
                </pre>
              ))}
          </div>

          {/* 移动卡片 */}
          <div className="space-y-2 md:hidden">
            {data.map((item) => (
              <Card key={item.audit_id} className="border-border-muted">
                <CardContent className="p-4">
                  <div className="flex flex-col gap-1.5">
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-xs text-foreground-muted">
                        {formatTime(item.created_at)}
                      </span>
                      <ActionBadge action={item.action} />
                    </div>
                    <p className="font-mono text-xs break-all text-foreground-emphasis">
                      {item.target}
                    </p>
                    <p className="text-xs text-foreground-muted">
                      操作者 {item.operator} · {ACTION_LABELS[item.action] ?? item.action}
                    </p>
                  </div>
                </CardContent>
              </Card>
            ))}
          </div>
          <div
            className="flex flex-wrap items-center justify-between gap-2 text-xs text-foreground-muted"
            aria-live="polite"
          >
            <span>
              已显示 <span className="font-mono tabular-nums">{data.length}</span> 条
              {isFull ? " · 可能还有更多" : " · 已到底"}
            </span>
            {isFull ? (
              <Button
                variant="outline"
                size="sm"
                onClick={() => setLimit((n) => n + 50)}
                aria-label="加载更多审计记录"
              >
                加载更多（+50）
              </Button>
            ) : null}
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
