/** P6 系统配置（只读）：分组 key-value + 敏感值脱敏 + 搜索。 */

import { useMemo, useState } from "react";
import { AlertTriangle, SearchX } from "lucide-react";
import { toast } from "sonner";
import { useConfig } from "@/api/queries";
import { PageHeader } from "@/components/page-header";
import { Alert, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";

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

/** 搜索别名：中文关键词 → 分组（搜「数据库」命中 PostgreSQL，搜「向量库」命中 Milvus）。 */
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

function groupOf(name: string): string {
  const head = name.split("_", 1)[0].toLowerCase();
  return GROUP_LABELS[head] ? head : "general";
}

function renderValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "boolean") return value ? "true" : "false";
  return String(value);
}

export function ConfigPage() {
  const { data, isPending, isError } = useConfig();
  const [search, setSearch] = useState("");

  const groups = useMemo(() => {
    if (!data) return [];
    const needle = search.trim().toLowerCase();
    const bucket = new Map<string, Array<{ name: string; value: unknown }>>();
    for (const field of data.fields) {
      const group = groupOf(field.name);
      // 搜索命中：字段名 ∪ 分组中文名 ∪ 分组别名
      if (
        needle &&
        !field.name.toLowerCase().includes(needle) &&
        !(GROUP_LABELS[group] ?? "").toLowerCase().includes(needle) &&
        !(GROUP_ALIASES[group] ?? "").toLowerCase().includes(needle)
      ) {
        continue;
      }
      bucket.set(group, [...(bucket.get(group) ?? []), field]);
    }
    return Array.from(bucket.entries()).sort(([a], [b]) => a.localeCompare(b));
  }, [data, search]);

  return (
    <div className="space-y-6">
      <PageHeader
        title="系统配置"
        description="运行时只读视图 · 敏感值已脱敏（本端点不回明文）"
        actions={
          data ? (
            <Badge variant="secondary" className="font-mono">
              环境：{data.environment}
            </Badge>
          ) : undefined
        }
      />

      <div className="max-w-sm">
        <label className="sr-only" htmlFor="config-search">
          搜索配置项
        </label>
        <Input
          id="config-search"
          placeholder="搜索配置项名称…"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
      </div>

      {isError ? (
        <Alert variant="destructive">
          <AlertTriangle aria-hidden="true" />
          <AlertTitle>无法加载配置</AlertTitle>
        </Alert>
      ) : isPending ? (
        <div aria-busy="true" className="space-y-3">
          <Skeleton className="h-40" />
          <Skeleton className="h-40" />
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            {groups.map(([group, fields]) => (
              <Card key={group}>
                <CardHeader className="border-b border-border-muted pb-3">
                  <CardTitle className="text-base">{GROUP_LABELS[group] ?? "其他"}</CardTitle>
                  <CardDescription>{fields.length} 项</CardDescription>
                </CardHeader>
              <CardContent>
                <dl className="space-y-0">
                  {fields.map((field) => (
                    <div
                      key={field.name}
                      className="flex items-baseline justify-between gap-3 border-b border-border-muted py-2 last:border-0"
                    >
                      <dt className="font-mono text-xs text-foreground-muted">{field.name}</dt>
                      <dd className="max-w-[60%] truncate text-right font-mono text-xs text-foreground-emphasis">
                        <button
                          type="button"
                          className="hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--ring)]"
                          title={`${field.name} = ${renderValue(field.value)}（点击复制）`}
                          onClick={() => {
                            void navigator.clipboard
                              ?.writeText(`${field.name} = ${renderValue(field.value)}`)
                              .then(() => toast.success("已复制配置项"));
                          }}
                        >
                          {renderValue(field.value)}
                        </button>
                      </dd>
                    </div>
                  ))}
                </dl>
              </CardContent>
            </Card>
          ))}
          {groups.length === 0 ? (
            <div className="flex flex-col items-center gap-2 py-12 text-center lg:col-span-2">
              <SearchX aria-hidden="true" className="size-8 text-foreground-soft" />
              <p className="text-sm text-foreground-muted">没有匹配的配置项</p>
            </div>
          ) : null}
        </div>
      )}
    </div>
  );
}
