/** P2 记忆浏览器：多用户检索 + 状态筛选 + 详情抽屉（概要/来源反链/操作）。
 *
 * 来源反链是本页灵魂：L3 抽取记忆 → journal 原文对照，人工核验「有源」。
 */

import { useState } from "react";
import { Link } from "@tanstack/react-router";
import { AlertTriangle, ChevronLeft, ChevronRight, Search } from "lucide-react";
import { toast } from "sonner";
import { useMemories, useMemorySource, type MemoryFilters } from "@/api/queries";
import type { AdminMemoryItem } from "@/api/client";
import { PageHeader } from "@/components/page-header";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { StatusBadge } from "@/components/status-badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { formatTime } from "@/lib/utils";

const STATUS_OPTIONS = [
  { value: "ALL", label: "全部状态" },
  { value: "ACTIVE", label: "有效" },
  { value: "SUPERSEDED", label: "已取代" },
  { value: "DELETED", label: "已删除" },
  { value: "SUPPRESSED", label: "已抑制" },
] as const;

const PAGE_SIZE = 50;

/** 最长公共连续段（中文无分词，靠公共子串定位命中片段）。 */
function longestCommonSpan(a: string, b: string): string {
  let best = "";
  for (let i = 0; i < a.length; i++) {
    for (let j = 0; j < b.length; j++) {
      let k = 0;
      while (i + k < a.length && j + k < b.length && a[i + k] === b[j + k]) k++;
      if (k > best.length && /[\p{L}\p{N}]/u.test(a.slice(i, i + k))) {
        best = a.slice(i, i + k);
      }
    }
  }
  return best;
}

/** 在原文中高亮命中记忆的关键词：拉丁词元 + 中英公共子串（≥2 字符）。 */
function highlightSource(content: string, memoryText: string) {
  const tokens = new Set(
    memoryText
      .split(/[\s,.;:!?，。；：！？、]+/)
      .filter((token) => token.length >= 2),
  );
  const span = longestCommonSpan(memoryText, content);
  if (span.length >= 2) tokens.add(span);
  const list = Array.from(tokens).slice(0, 8);
  if (list.length === 0) return content;
  const pattern = new RegExp(
    list.map((token) => token.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|"),
    "g",
  );
  const parts = content.split(pattern);
  const matches = content.match(pattern) ?? [];
  return parts.flatMap((part, index) => [
    <span key={`p-${index}`}>{part}</span>,
    matches[index] ? (
      <mark key={`m-${index}`} className="rounded-sm bg-warning-soft px-0.5 font-medium">
        {matches[index]}
      </mark>
    ) : null,
  ]);
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-0.5">
      <dt className="text-xs text-foreground-muted">{label}</dt>
      <dd className="text-sm text-foreground-emphasis">{children}</dd>
    </div>
  );
}

function MemoryDetail({
  memory,
  onClose,
}: {
  memory: AdminMemoryItem;
  onClose: () => void;
}) {
  const source = useMemorySource(memory.memory_id);
  return (
    <div className="flex h-full flex-col overflow-hidden">
      <SheetHeader className="border-b border-border">
        <SheetTitle className="truncate font-mono text-sm" title={memory.memory_id}>
          {memory.memory_id}
        </SheetTitle>
        <SheetDescription>
          {memory.user_id} · {memory.memory_scope_id}
        </SheetDescription>
      </SheetHeader>
      <Tabs defaultValue="summary" className="flex min-h-0 flex-1 flex-col">
        <TabsList className="mx-4 mt-2 w-fit">
          <TabsTrigger value="summary">概要</TabsTrigger>
          <TabsTrigger value="source">来源反链</TabsTrigger>
          <TabsTrigger value="ops">操作</TabsTrigger>
        </TabsList>
        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          <TabsContent value="summary" className="mt-0 space-y-4">
            <dl className="grid grid-cols-2 gap-3">
              <Field label="状态">
                <StatusBadge status={memory.memory_status} />
              </Field>
              <Field label="冲突槽位">{memory.conflict_slot ?? "—"}</Field>
              <Field label="记忆分类">{memory.memory_type ?? "—"}</Field>
              <Field label="敏感等级">{memory.data_classification}</Field>
              <Field label="生效时刻（valid_at）">{formatTime(memory.valid_at)}</Field>
              <Field label="失效时刻（invalid_at）">{formatTime(memory.invalid_at)}</Field>
              <Field label="最近召回">{formatTime(memory.last_recalled_at)}</Field>
              <Field label="召回次数">{memory.recall_count}</Field>
              <Field label="后端记忆 ID">
                <span className="font-mono text-xs break-all">{memory.backend_memory_id}</span>
              </Field>
              <Field label="来源回合数">{memory.source_refs.length}</Field>
            </dl>
            <div>
              <h3 className="mb-1 text-xs text-foreground-muted">记忆文本</h3>
              <p className="rounded-lg bg-background-muted p-3 text-sm text-foreground-emphasis">
                {memory.memory_text}
              </p>
            </div>
          </TabsContent>

          <TabsContent value="source" className="mt-0 space-y-4">
            {source.isPending ? (
              <div aria-busy="true" className="space-y-2">
                <Skeleton className="h-16" />
                <Skeleton className="h-16" />
              </div>
            ) : source.isError ? (
              <Alert variant="destructive">
                <AlertTriangle aria-hidden="true" />
                <AlertTitle>无法加载来源回合</AlertTitle>
              </Alert>
            ) : source.data!.rounds.length === 0 ? (
              <p className="py-6 text-center text-sm text-foreground-muted">
                无 journal 原文反链（可能来自本地槽位回填或历史数据）
              </p>
            ) : (
              source.data!.rounds.map((round) => (
                <Card key={round.round_id} className="border-border-muted">
                  <CardContent className="space-y-2 p-3">
                    <p className="flex flex-wrap items-center gap-2 text-xs text-foreground-muted">
                      <span className="font-mono">{round.round_id}</span>
                      {round.session_id ? <span>会话 {round.session_id}</span> : null}
                      <span>{formatTime(round.source_timestamp)}</span>
                    </p>
                    {round.messages.map((message, index) => (
                      <div
                        key={message.message_id ?? index}
                        className={
                          message.role === "assistant"
                            ? "ml-6 rounded-lg bg-background-muted p-2 text-sm"
                            : "mr-6 rounded-lg border border-border-muted p-2 text-sm"
                        }
                      >
                        <p
                          className={
                            message.role === "assistant"
                              ? "mb-0.5 text-xs font-medium text-foreground-muted"
                              : "mb-0.5 text-xs font-medium text-info"
                          }
                        >
                          {message.role === "assistant" ? "助手" : "用户"}
                        </p>
                        <p>{highlightSource(message.content ?? "", memory.memory_text)}</p>
                      </div>
                    ))}
                  </CardContent>
                </Card>
              ))
            )}
          </TabsContent>

          <TabsContent value="ops" className="mt-0 space-y-3">
            <p className="text-sm text-foreground-muted">
              治理操作（删除 / 更新）在治理操作台执行，操作将记录审计。
            </p>
            <Button asChild variant="outline" size="sm">
              <Link to="/govern" search={{ memory_id: memory.memory_id, user_id: memory.user_id }}>
                前往治理操作台处理此记忆
              </Link>
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                void navigator.clipboard?.writeText(memory.memory_id).then(() => {
                  toast.success("已复制记忆 ID");
                  onClose();
                });
              }}
            >
              复制记忆 ID
            </Button>
          </TabsContent>
        </div>
      </Tabs>
    </div>
  );
}

export function MemoriesPage() {
  const [userIdInput, setUserIdInput] = useState("");
  const [scopeInput, setScopeInput] = useState("");
  const [statusInput, setStatusInput] = useState<string>("ALL");
  const [applied, setApplied] = useState<MemoryFilters>({ page: 1 });
  const [selected, setSelected] = useState<AdminMemoryItem | null>(null);
  const { data, isPending, isError } = useMemories({
    ...applied,
    statuses: statusInput === "ALL" ? undefined : statusInput,
  });

  const applyFilters = () => {
    setApplied({
      user_id: userIdInput.trim() || undefined,
      memory_scope_id: scopeInput.trim() || undefined,
      page: 1,
    });
  };

  const totalPages = data ? Math.max(1, Math.ceil(data.total / PAGE_SIZE)) : 1;

  return (
    <div className="space-y-6">
      <PageHeader title="记忆浏览器" description="本地索引多用户视图 · 点击行查看详情与来源反链" />

      <form
        className="flex flex-col gap-3 sm:flex-row sm:items-center"
        onSubmit={(event) => {
          event.preventDefault();
          applyFilters();
        }}
      >
        <label className="sr-only" htmlFor="filter-user">
          用户 ID
        </label>
        <Input
          id="filter-user"
          className="flex-1"
          placeholder="用户 ID（留空 = 全量）"
          value={userIdInput}
          onChange={(event) => setUserIdInput(event.target.value)}
        />
        <label className="sr-only" htmlFor="filter-scope">
          范围
        </label>
        <Input
          id="filter-scope"
          className="flex-1"
          placeholder="范围（默认全部）"
          value={scopeInput}
          onChange={(event) => setScopeInput(event.target.value)}
        />
        <label className="sr-only" htmlFor="filter-status">
          状态
        </label>
        <Select value={statusInput} onValueChange={setStatusInput}>
          <SelectTrigger id="filter-status" aria-label="状态筛选" className="sm:w-44">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {STATUS_OPTIONS.map((option) => (
              <SelectItem key={option.value} value={option.value}>
                {option.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Button type="submit" className="sm:w-28">
          <Search aria-hidden="true" /> 检索
        </Button>
      </form>

      {isError ? (
        <Alert variant="destructive">
          <AlertTriangle aria-hidden="true" />
          <AlertTitle>无法加载记忆列表</AlertTitle>
          <AlertDescription>请确认后端服务可达、管理 token 正确。</AlertDescription>
        </Alert>
      ) : null}

      {isPending && !data ? (
        <div aria-busy="true" className="space-y-2">
          {Array.from({ length: 5 }).map((_, index) => (
            <Skeleton key={index} className="h-12 rounded-xl" />
          ))}
        </div>
      ) : null}

      {data ? (
        <>
          {/* 桌面表格：记忆 ID 为真实按钮（键盘可达），整行仍可点击 */}
          <div className="hidden md:block">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>记忆 ID</TableHead>
                  <TableHead>记忆文本</TableHead>
                  <TableHead>槽位</TableHead>
                  <TableHead>状态</TableHead>
                  <TableHead>生效时刻</TableHead>
                  <TableHead>失效时刻</TableHead>
                  <TableHead className="text-right">召回</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.items.map((item) => (
                  <TableRow
                    key={item.memory_id}
                    className="cursor-pointer"
                    onClick={() => setSelected(item)}
                  >
                    <TableCell className="max-w-40">
                      <button
                        type="button"
                        className="block w-full truncate text-left font-mono text-xs text-foreground-muted hover:text-foreground-intense hover:underline"
                        title={item.memory_id}
                        onClick={() => setSelected(item)}
                      >
                        {item.memory_id}
                      </button>
                    </TableCell>
                    <TableCell className="max-w-72">
                      <span
                        className="block truncate text-sm text-foreground-emphasis"
                        title={item.memory_text}
                      >
                        {item.memory_text}
                      </span>
                    </TableCell>
                    <TableCell className="text-xs text-foreground-muted">
                      {item.conflict_slot ?? "—"}
                    </TableCell>
                    <TableCell>
                      <StatusBadge status={item.memory_status} />
                    </TableCell>
                    <TableCell className="text-xs text-foreground-muted">
                      {formatTime(item.valid_at)}
                    </TableCell>
                    <TableCell className="text-xs text-foreground-muted">
                      {formatTime(item.invalid_at)}
                    </TableCell>
                    <TableCell className="text-right font-mono text-xs tabular-nums">
                      {item.recall_count}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>

          {/* 移动卡片（触达 ≥44px） */}
          <div className="space-y-2 md:hidden">
            {data.items.map((item) => (
              <Card key={item.memory_id} className="border-border-muted">
                <CardContent className="p-4">
                  <button
                    type="button"
                    className="flex min-h-11 w-full flex-col gap-1 text-left"
                    onClick={() => setSelected(item)}
                  >
                    <span className="flex items-center justify-between gap-2">
                      <span className="text-sm text-foreground-emphasis">{item.memory_text}</span>
                      <StatusBadge status={item.memory_status} />
                    </span>
                    <span className="flex gap-2 text-xs text-foreground-muted">
                      <span>{item.conflict_slot ?? "无槽位"}</span>
                      <span>·</span>
                      <span>{formatTime(item.valid_at)}</span>
                    </span>
                  </button>
                </CardContent>
              </Card>
            ))}
          </div>

          <nav className="flex items-center justify-between" aria-label="分页">
            {totalPages > 1 ? (
              <>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={applied.page <= 1}
                  onClick={() => setApplied({ ...applied, page: applied.page - 1 })}
                >
                  <ChevronLeft aria-hidden="true" /> 上一页
                </Button>
                <p className="text-sm tabular-nums text-foreground-muted" aria-live="polite">
                  第 {applied.page} / {totalPages} 页 · 共 {data.total} 条
                </p>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={applied.page >= totalPages}
                  onClick={() => setApplied({ ...applied, page: applied.page + 1 })}
                >
                  下一页 <ChevronRight aria-hidden="true" />
                </Button>
              </>
            ) : (
              <p className="w-full text-center text-sm tabular-nums text-foreground-muted" aria-live="polite">
                共 {data.total} 条
              </p>
            )}
          </nav>
        </>
      ) : null}

      <Sheet open={selected !== null} onOpenChange={(open) => (open ? null : setSelected(null))}>
        <SheetContent className="p-0 sm:max-w-xl">
          {selected ? <MemoryDetail memory={selected} onClose={() => setSelected(null)} /> : null}
        </SheetContent>
      </Sheet>
    </div>
  );
}
