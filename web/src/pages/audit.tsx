/** P5 审计日志：只读，治理动作全留痕。 */

import { useState } from "react";
import { AlertTriangle, ScrollText } from "lucide-react";
import { toast } from "sonner";
import { useAudit } from "@/api/queries";
import { PageHeader } from "@/components/page-header";
import { Alert, AlertTitle } from "@/components/ui/alert";
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
  const tone = ACTION_TONES[action] ?? "neutral";
  return <Badge variant={tone}>{ACTION_LABELS[action] ?? action}</Badge>;
}

export function AuditPage() {
  const [action, setAction] = useState("all");
  const [expanded, setExpanded] = useState<string | null>(null);
  const { data, isPending, isError } = useAudit(action === "all" ? undefined : action);

  return (
    <div className="space-y-6">
      <PageHeader
        title="审计日志"
        description="治理台操作留痕 · 只读不可篡改"
        actions={
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
        }
      />

      {isError ? (
        <Alert variant="destructive">
          <AlertTriangle aria-hidden="true" />
          <AlertTitle>无法加载审计日志</AlertTitle>
        </Alert>
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
                        <span className="text-xs text-muted-foreground">—</span>
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
        </>
      ) : (
        <div className="flex flex-col items-center gap-2 py-16 text-center">
          <ScrollText aria-hidden="true" className="size-8 text-foreground-soft" />
          <p className="text-sm text-foreground-muted">暂无审计记录</p>
        </div>
      )}
    </div>
  );
}
