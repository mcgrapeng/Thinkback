/** P6 系统配置:Editorial Premium 重做。
 *
 * 分组 key-value + 敏感值脱敏 + 搜索;含 secret 标记(URL/TOKEN/KEY/PASSWORD/SECRET 关键词)。 */

import { useMemo, useState } from "react";
import { useSearch } from "@tanstack/react-router";
import { AlertTriangle, Check, Lock, Search, SearchX } from "lucide-react";
import { toast } from "sonner";
import { useConfig } from "@/api/queries";
import { PageHeader } from "@/components/page-header";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { SkeletonLines } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/empty-state";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { Info } from "lucide-react";
import { cn } from "@/lib/utils";

const GROUP_LABELS: Record<string, string> = {
  app: "应用标识",
  postgres: "PostgreSQL",
  database: "数据库",
  grpc: "gRPC",
  memory: "记忆引擎",
  l2: "L2 摘要",
  l3: "L3 后台写",
  decay: "遗忘衰减",
  task: "任务治理",
  admin: "治理台",
  milvus: "Milvus",
  api: "API 工作池",
  log: "日志",
  otel: "可观测",
};

const GROUP_COLORS: Record<string, string> = {
  app: "bg-violet",
  postgres: "bg-info",
  database: "bg-info",
  grpc: "bg-success",
  memory: "bg-warning",
  l2: "bg-violet",
  l3: "bg-info",
  decay: "bg-warning",
  task: "bg-success",
  admin: "bg-error",
  milvus: "bg-info",
  api: "bg-success",
  log: "bg-neutral",
  otel: "bg-violet",
  general: "bg-neutral",
};

const GROUP_ALIASES: Record<string, string> = {
  postgres: "数据库 database pg",
  database: "数据库 database",
  milvus: "向量库 向量 milvus",
  memory: "记忆 memory",
  task: "任务 task",
  admin: "治理台 管理 admin",
  grpc: "通信 grpc",
  otel: "监控 链路 otel",
  log: "日志 log",
  api: "接口 api",
};

/** 敏感字段关键词(大写不敏感) */
const SECRET_PATTERNS = [
  /password/i,
  /secret/i,
  /token/i,
  /api[_-]?key/i,
  /credential/i,
  /private[_-]?key/i,
];

function isSecret(name: string, value: unknown): boolean {
  if (SECRET_PATTERNS.some((re) => re.test(name))) return true;
  if (typeof value === "string") {
    if (value.includes("***") || value.includes("://") && /:\*\*\*@/.test(value)) return true;
  }
  return false;
}

function groupOf(name: string): string {
  const head = name.split("_", 1)[0].toLowerCase();
  return GROUP_LABELS[head] ? head : "general";
}

function renderValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "boolean") return value ? "true" : "false";
  return String(value);
}

function CopyButton({ text, label }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className="inline-flex max-w-full items-center gap-1.5 rounded-md px-1.5 py-0.5 font-mono text-xs text-foreground-emphasis hover:bg-background-muted focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[var(--ring)] transition-colors"
      title={`${text}（点击复制）`}
      onClick={() => {
        void navigator.clipboard?.writeText(text).then(() => {
          setCopied(true);
          toast.success(label ? `已复制 ${label}` : "已复制");
          setTimeout(() => setCopied(false), 1500);
        });
      }}
    >
      <span className="min-w-0 max-w-[32ch] truncate">{text}</span>
      {copied ? (
        <Check aria-hidden="true" className="size-3 shrink-0 text-success" />
      ) : (
        <span aria-hidden="true" className="shrink-0 text-foreground-soft text-[10px]">copy</span>
      )}
    </button>
  );
}

export function ConfigPage() {
  const search = useSearch({ from: "/config" }) as { q?: string };
  const { data, isPending, isError } = useConfig();
  const [searchInput, setSearchInput] = useState(search.q ?? "");

  const groups = useMemo(() => {
    if (!data) return [];
    const needle = searchInput.trim().toLowerCase();
    const bucket = new Map<string, Array<{ name: string; value: unknown; secret: boolean }>>();
    for (const field of data.fields) {
      const group = groupOf(field.name);
      const secret = isSecret(field.name, field.value);
      if (
        needle &&
        !field.name.toLowerCase().includes(needle) &&
        !renderValue(field.value).toLowerCase().includes(needle) &&
        !(GROUP_LABELS[group] ?? "").toLowerCase().includes(needle) &&
        !(GROUP_ALIASES[group] ?? "").toLowerCase().includes(needle)
      ) {
        continue;
      }
      bucket.set(group, [
        ...(bucket.get(group) ?? []),
        { name: field.name, value: field.value, secret },
      ]);
    }
    return Array.from(bucket.entries()).sort(([a], [b]) => a.localeCompare(b));
  }, [data, searchInput]);

  return (
    <div className="space-y-6">
      <PageHeader
        title="CONFIG · 系统配置"
        description="运行时只读视图 · 敏感值已脱敏（本端点不回明文）"
        actions={
          data ? (
            <div className="flex items-center gap-2 text-xs">
              <span className="text-foreground-muted">环境</span>
              <Badge variant="secondary" className="font-mono">
                {data.environment}
              </Badge>
            </div>
          ) : undefined
        }
      />

      {/* 搜索 + 别名提示 */}
      <section className="animate-editorial-fade-up space-y-3">
        <div className="relative max-w-md">
          <Search
            aria-hidden="true"
            className="pointer-events-none absolute left-4 top-1/2 size-4 -translate-y-1/2 text-foreground-muted"
          />
          <Input
            id="config-search"
            className="pl-11 h-11 rounded-xl"
            placeholder="搜索配置项名称 / 别名(如「数据库」「向量库」)…"
            value={searchInput}
            onChange={(event) => setSearchInput(event.target.value)}
            aria-describedby="config-search-hint"
          />
        </div>
        <p id="config-search-hint" className="flex items-center gap-1.5 text-xs text-foreground-muted">
          <Info aria-hidden="true" className="size-3" />
          提示:支持中英文搜索 + 分组别名。例如搜「数据库」命中 PostgreSQL,搜「向量库」命中 Milvus。
        </p>
      </section>

      <div className="editorial-rule" />

      {isError ? (
        <EmptyState
          variant="error"
          icon={AlertTriangle}
          title="无法加载配置"
          description="请确认后端服务可达。"
          action={{ label: "重试", onClick: () => window.location.reload() }}
        />
      ) : isPending ? (
        <div aria-busy="true" className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          {Array.from({ length: 4 }).map((_, i) => (
            <Card key={i} className="border-border">
              <CardHeader className="border-b border-border-muted pb-3">
                <SkeletonLines count={1} />
              </CardHeader>
              <CardContent className="space-y-2">
                <SkeletonLines count={4} gap={1} />
              </CardContent>
            </Card>
          ))}
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          {groups.map(([group, fields]) => (
            <Card key={group} variant="elevated" className="animate-editorial-fade-up">
              <CardHeader>
                <div className="flex items-center gap-2">
                  <span
                    className={cn("size-2 rounded-full", GROUP_COLORS[group] ?? "bg-neutral")}
                    aria-hidden="true"
                  />
                  <CardTitle className="text-base">{GROUP_LABELS[group] ?? "其他"}</CardTitle>
                </div>
                <CardDescription>{fields.length} 项</CardDescription>
              </CardHeader>
              <CardContent className="space-y-0">
                <dl>
                  {fields.map((field) => (
                    <div
                      key={field.name}
                      className="flex items-center justify-between gap-3 overflow-hidden border-b border-border-muted py-2.5 last:border-0"
                    >
                      <dt className="flex min-w-0 shrink items-center gap-1.5 font-mono text-xs text-foreground-muted">
                        <span className="truncate">{field.name}</span>
                        {field.secret ? (
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <span
                                aria-label="敏感字段"
                                className="inline-flex items-center gap-0.5 rounded-md bg-warning-soft px-1 py-0.5 text-[10px] font-medium text-warning"
                              >
                                <Lock aria-hidden="true" className="size-2.5" />
                                SECRET
                              </span>
                            </TooltipTrigger>
                            <TooltipContent className="max-w-xs">
                              敏感字段:值已脱敏,不回明文。
                            </TooltipContent>
                          </Tooltip>
                        ) : null}
                      </dt>
                      <dd className="flex min-w-0 shrink items-center gap-2 overflow-hidden">
                        <CopyButton
                          text={renderValue(field.value)}
                          label="配置项"
                        />
                      </dd>
                    </div>
                  ))}
                </dl>
              </CardContent>
            </Card>
          ))}
          {groups.length === 0 ? (
            <div className="lg:col-span-2">
              <EmptyState
                icon={SearchX}
                title="没有匹配的配置项"
                description="试试别的搜索词,或用别名(如「数据库」命中 PostgreSQL、「向量库」命中 Milvus)。"
              />
            </div>
          ) : null}
        </div>
      )}
    </div>
  );
}
