/** P2 记忆浏览器:多用户检索 + 状态筛选 + 详情抽屉(概要/来源反链/操作)。
 *
 * 来源反链是本页灵魂:L3 抽取记忆 → journal 原文对照,人工核验「有源」。
 * Editorial Premium 重做:hero KPI + editorial card 列表 + 分页器。
 */

import { useEffect, useMemo, useRef, useState } from "react";
import type * as React from "react";
import { Link } from "@tanstack/react-router";
import { AlertTriangle, ChevronLeft, ChevronRight, Info, Loader2, Search } from "lucide-react";
import { toast } from "sonner";
import { useMemories, useMemorySource, type MemoryFilters } from "@/api/queries";
import type { AdminMemoryItem } from "@/api/client";
import { PageHeader } from "@/components/page-header";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { StatusBadge } from "@/components/status-badge";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { findHighlightSegments } from "@/lib/highlight";
import { cn, formatTime } from "@/lib/utils";

const STATUS_CHIPS: Array<{ value: string; label: string }> = [
  { value: "ALL", label: "全部" },
  { value: "ACTIVE", label: "有效" },
  { value: "SUPERSEDED", label: "已取代" },
  { value: "DELETED", label: "已删除" },
  { value: "SUPPRESSED", label: "已抑制" },
];

const PAGE_SIZE = 50;

function HighlightedText({
  content,
  memoryText,
}: {
  content: string;
  memoryText: string;
}) {
  const segments = useMemo(
    () => findHighlightSegments(content, memoryText),
    [content, memoryText],
  );
  return (
    <>
      {segments.map((segment, index) =>
        segment.kind === "match" ? (
          <mark
            key={index}
            className="rounded-sm bg-warning-soft px-0.5 font-medium text-foreground-emphasis"
          >
            {segment.value}
          </mark>
        ) : (
          <span key={index}>{segment.value}</span>
        ),
      )}
    </>
  );
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-0.5">
      <dt className="flex items-center gap-1 text-xs text-foreground-muted">
        <span>{label}</span>
        {hint ? (
          <Tooltip>
            <TooltipTrigger asChild>
              <button
                type="button"
                aria-label={`${label} 字段说明`}
                className="inline-flex size-3.5 items-center justify-center rounded-full text-foreground-soft hover:text-foreground-emphasis hover:bg-background-muted transition-colors focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[var(--ring)]"
              >
                <Info aria-hidden="true" className="size-3" />
              </button>
            </TooltipTrigger>
            <TooltipContent>{hint}</TooltipContent>
          </Tooltip>
        ) : null}
      </dt>
      <dd className="text-sm text-foreground-emphasis">{children}</dd>
    </div>
  );
}

function MemoryDetail({ memory }: { memory: AdminMemoryItem }) {
  const source = useMemorySource(memory.memory_id);
  return (
    <div className="flex h-full flex-col overflow-hidden">
      <SheetHeader className="space-y-3 border-b border-[#f5f2ec] p-8 pb-6">
        <p className="section-label">MEMORY · {memory.memory_id.slice(-3)}</p>
        <SheetTitle className="text-3xl font-semibold tracking-tight leading-tight">
          {memory.memory_text}
        </SheetTitle>
        <SheetDescription className="font-mono text-sm">
          {memory.user_id} · {memory.memory_scope_id}
        </SheetDescription>
      </SheetHeader>
      <Tabs defaultValue="summary" className="flex min-h-0 flex-1 flex-col">
        <TabsList className="mx-8 mt-4 w-fit">
          <TabsTrigger value="summary">概要</TabsTrigger>
          <TabsTrigger value="source">来源反链</TabsTrigger>
          <TabsTrigger value="ops">操作</TabsTrigger>
        </TabsList>
        <div className="min-h-0 flex-1 overflow-y-auto p-8">
          <TabsContent value="summary" className="mt-0 space-y-4">
            <dl className="grid grid-cols-2 gap-3">
              <Field
                label="状态"
                hint="记忆当前是否在用。有效=正常使用;已取代=被新版本替换但仍可查;已删除=软删除(可恢复);已抑制=被人工标记不可检索。"
              >
                <StatusBadge status={memory.memory_status} />
              </Field>
              <Field
                label="冲突槽位"
                hint="同一类偏好在系统里的归类位置(如 dietary=饮食偏好)。冲突时新记忆会替换槽位里的旧记忆,旧记忆自动标为已取代。"
              >
                {memory.conflict_slot ?? "—"}
              </Field>
              <Field
                label="记忆分类"
                hint="记忆的类型标签,如 preference=偏好、fact=事实、person=人物、event=事件。"
              >
                {memory.memory_type ?? "—"}
              </Field>
              <Field
                label="敏感等级"
                hint="数据合规等级:普通=一般信息;个人=可识别到个人;敏感=健康/财务等;受限=最高保护,默认不可召回。"
              >
                {memory.data_classification}
              </Field>
              <Field
                label="生效时刻"
                hint="从这一刻起这条记忆被系统视为有效;在此之前的相关事实以新记忆为准。"
              >
                {formatTime(memory.valid_at)}
              </Field>
              <Field
                label="失效时刻"
                hint="从这一刻起这条记忆不再被召回(但仍可查);为空表示永久有效。"
              >
                {formatTime(memory.invalid_at)}
              </Field>
              <Field
                label="最近召回"
                hint="最近一次被模型在生成回复时引用到此记忆的本地时间。"
              >
                {formatTime(memory.last_recalled_at)}
              </Field>
              <Field
                label="召回次数"
                hint="累计被模型在生成回复时引用的次数;数字越大说明这条记忆越关键。"
              >
                {memory.recall_count}
              </Field>
              <Field
                label="后端记忆 ID"
                hint="存储层(Milvus / Postgres)里的真实 ID;治理台 ID 仅是前端展示用,后端 ID 才是持久化键。"
              >
                <span className="font-mono text-xs break-all">{memory.backend_memory_id}</span>
              </Field>
              <Field
                label="来源回合数"
                hint="生成这条记忆时引用的对话轮数;≥1 表明有 journal 原文可追溯(来源反链 tab 可看)。"
              >
                {memory.source_refs.length}
              </Field>
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
            ) : !source.data || source.data.rounds.length === 0 ? (
              <p className="py-6 text-center text-sm text-foreground-muted">
                无 journal 原文反链(可能来自本地槽位回填或历史数据)
              </p>
            ) : (
              source.data.rounds.map((round) => (
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
                        <p>
                          <HighlightedText
                            content={message.content ?? ""}
                            memoryText={memory.memory_text}
                          />
                        </p>
                      </div>
                    ))}
                  </CardContent>
                </Card>
              ))
            )}
          </TabsContent>

          <TabsContent value="ops" className="mt-0 space-y-3">
            <p className="text-sm text-foreground-muted">
              治理操作(删除 / 更新)在治理操作台执行,操作将记录审计。
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
                void navigator.clipboard?.writeText(memory.memory_id).then(
                  () => {
                    toast.success("已复制记忆 ID");
                  },
                  () => {
                    toast.error("复制失败:浏览器拒绝访问剪贴板");
                  },
                );
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
  const resultsRef = useRef<HTMLDivElement>(null);
  const lastFilterKeyRef = useRef<string>("");

  const trimmedUserId = userIdInput.trim() || undefined;
  const trimmedScope = scopeInput.trim() || undefined;

  // 文本输入 300ms debounce → 自动应用
  useEffect(() => {
    const timer = setTimeout(() => {
      setApplied((prev) => {
        if (prev.user_id === trimmedUserId && prev.memory_scope_id === trimmedScope) {
          return prev;
        }
        return { user_id: trimmedUserId, memory_scope_id: trimmedScope, page: 1 };
      });
    }, 300);
    return () => clearTimeout(timer);
  }, [trimmedUserId, trimmedScope]);

  // status 切换立即应用 → 但必须重置 page 到 1
  useEffect(() => {
    setApplied((prev) => (prev.page === 1 ? prev : { ...prev, page: 1 }));
  }, [statusInput]);

  const { data, isPending, isError, isFetching } = useMemories({
    ...applied,
    statuses: statusInput === "ALL" ? undefined : statusInput,
  });

  // 筛选 / 分页 / 状态变化 → 滚到结果顶部
  useEffect(() => {
    if (!data) return;
    const filterKey = `${applied.user_id ?? ""}|${applied.memory_scope_id ?? ""}|${statusInput}|${applied.page}`;
    if (lastFilterKeyRef.current && lastFilterKeyRef.current !== filterKey) {
      resultsRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    }
    lastFilterKeyRef.current = filterKey;
  }, [data, applied.user_id, applied.memory_scope_id, applied.page, statusInput]);

  const applyFilters = () => {
    setApplied({
      user_id: trimmedUserId,
      memory_scope_id: trimmedScope,
      page: 1,
    });
  };

  const clearFilters = () => {
    setUserIdInput("");
    setScopeInput("");
    setStatusInput("ALL");
  };

  const hasActiveFilter =
    userIdInput.trim() !== "" || scopeInput.trim() !== "" || statusInput !== "ALL";

  const totalPages = data ? Math.max(1, Math.ceil(data.total / PAGE_SIZE)) : 1;

  const openDetail = (item: AdminMemoryItem) => setSelected(item);
  const onRowKeyDown = (event: React.KeyboardEvent, item: AdminMemoryItem) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      openDetail(item);
    }
  };

  return (
    <div className="space-y-6">
      <PageHeader
        title="MEMORY BROWSER · 记忆浏览器"
        description={
          data
            ? `${data.total.toLocaleString()} memories · indexed for retrieval`
            : "本地索引多用户视图 · 点击行查看详情与来源反链"
        }
      />

      {/* HERO: 总数大字 + 搜索 + 状态 chip */}
      <section className="animate-editorial-fade-up">
        <p className="section-label mb-3">INDEXED MEMORIES · 索引记忆总数</p>
        <p className="text-display-md text-foreground-intense tabular-nums">
          {(data?.total ?? 0).toLocaleString()}
        </p>
      </section>

      <div className="editorial-rule" />

      {/* 搜索 + 状态 chip 区 */}
      <section className="space-y-4 animate-editorial-fade-up" style={{ animationDelay: "80ms" }}>
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
          <div className="relative flex-1">
            <Search
              aria-hidden="true"
              className="pointer-events-none absolute left-4 top-1/2 size-4 -translate-y-1/2 text-foreground-muted"
            />
            <Input
              id="filter-user"
              className="pl-11 h-11 rounded-xl"
              placeholder="用户 ID / 记忆 ID / scope..."
              value={userIdInput}
              onChange={(event) => setUserIdInput(event.target.value)}
            />
          </div>
          <label className="sr-only" htmlFor="filter-scope">
            范围
          </label>
          <div className="relative flex-1">
            <Search
              aria-hidden="true"
              className="pointer-events-none absolute left-4 top-1/2 size-4 -translate-y-1/2 text-foreground-muted"
            />
            <Input
              id="filter-scope"
              className="pl-11 h-11 rounded-xl"
              placeholder="范围(默认全部)"
              value={scopeInput}
              onChange={(event) => setScopeInput(event.target.value)}
            />
          </div>
          <Button type="submit" className="sm:w-28">
            <Search aria-hidden="true" /> 检索
          </Button>
          <button
            type="button"
            onClick={() => window.dispatchEvent(new CustomEvent("open-command-palette"))}
            className="hidden md:flex items-center gap-1.5 rounded-xl border border-border bg-background px-3 h-11 text-xs text-foreground-muted hover:bg-background-muted transition-colors"
          >
            <span>Quick switch</span>
            <kbd className="rounded-md bg-background-muted px-1.5 py-0.5 font-mono text-[10px]">⌘K</kbd>
          </button>
        </form>

        <div className="flex flex-wrap items-center gap-2">
          <p className="section-label mr-2">STATUS</p>
          {STATUS_CHIPS.map((chip) => {
            const active = statusInput === chip.value;
            return (
              <button
                key={chip.value}
                onClick={() => setStatusInput(chip.value)}
                className={cn(
                  "relative rounded-xl border px-4 py-1.5 text-sm font-medium transition-colors",
                  active
                    ? "border-foreground-intense text-foreground-intense bg-background"
                    : "border-border text-foreground-muted hover:text-foreground-emphasis hover:bg-background-muted",
                  active && "shadow-[inset_0_-1px_0_0_var(--foreground-intense)]",
                )}
              >
                {chip.label}
              </button>
            );
          })}
          {hasActiveFilter ? (
            <button
              type="button"
              onClick={clearFilters}
              className="text-xs text-foreground-muted underline-offset-2 hover:underline"
            >
              清空
            </button>
          ) : null}
        </div>
      </section>

      <div className="editorial-rule" />

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
            <Skeleton key={index} className="h-24 rounded-2xl" />
          ))}
        </div>
      ) : null}

      <div ref={resultsRef} aria-busy={isFetching} aria-live="polite">
        {data ? (
          <>
            {/* 桌面:editorial card 列表 */}
            <ul className="hidden md:block divide-y divide-[#f5f2ec] border-t border-b border-[#f5f2ec]">
              {data.items.map((memory, i) => (
                <li
                  key={memory.memory_id}
                  className="group cursor-pointer row-hover-warm border-l-2 border-transparent pl-4 -ml-4 pr-2 py-5 animate-editorial-fade-up"
                  style={{ animationDelay: `${i * 40}ms` }}
                  onClick={() => openDetail(memory)}
                  onKeyDown={(e) => onRowKeyDown(e, memory)}
                  tabIndex={0}
                  role="button"
                  aria-label={`打开记忆 ${memory.memory_id}`}
                >
                  <div className="flex items-baseline gap-3 mb-2">
                    <span className="section-label text-[10px]">
                      MEMORY · {memory.memory_id.slice(-3)}
                    </span>
                    <span className="font-mono text-xs text-foreground-soft">
                      {memory.last_recalled_at ? formatTime(memory.last_recalled_at) : "never"}
                    </span>
                    <span className="ml-auto text-sm text-foreground-muted tabular-nums">
                      recalled <strong className="font-semibold text-foreground-intense">{memory.recall_count}×</strong>
                    </span>
                  </div>
                  <p className="text-lg leading-snug text-foreground-intense mb-2 line-clamp-2">
                    {memory.memory_text}
                  </p>
                  <div className="flex flex-wrap items-center gap-3 text-xs text-foreground-muted">
                    <span className="font-mono">{memory.user_id}</span>
                    <span aria-hidden="true">·</span>
                    <span>{memory.memory_scope_id}</span>
                    {memory.conflict_slot ? (
                      <>
                        <span aria-hidden="true">·</span>
                        <span>{memory.conflict_slot}</span>
                      </>
                    ) : null}
                    <StatusBadge status={memory.memory_status} />
                    <Badge variant="neutral">{memory.data_classification}</Badge>
                  </div>
                </li>
              ))}
            </ul>

            {/* 移动卡片(触达 ≥44px) */}
            <div className="space-y-2 md:hidden">
              {data.items.map((item) => (
                <Card key={item.memory_id} className="border-border-muted">
                  <CardContent className="p-4">
                    <button
                      type="button"
                      className="flex min-h-11 w-full flex-col gap-1 text-left"
                      onClick={() => openDetail(item)}
                    >
                      <span className="flex items-center justify-between gap-2">
                        <span className="text-sm text-foreground-emphasis line-clamp-2">{item.memory_text}</span>
                        <StatusBadge status={item.memory_status} />
                      </span>
                      <span className="flex gap-2 text-xs text-foreground-muted">
                        <span>{item.conflict_slot ?? "无槽位"}</span>
                        <span>·</span>
                        <span>{formatTime(item.valid_at)}</span>
                        <span>·</span>
                        <span className="font-mono tabular-nums">{item.recall_count}×</span>
                      </span>
                    </button>
                  </CardContent>
                </Card>
              ))}
            </div>

            {/* 分页器 */}
            <nav className="flex items-center justify-between pt-4" aria-label="分页">
              <p className="flex items-center gap-2 text-sm tabular-nums text-foreground-muted">
                {isFetching ? <Loader2 aria-hidden="true" className="size-3.5 animate-spin" /> : null}
                {totalPages > 1
                  ? `第 ${applied.page} / ${totalPages} 页 · 共 ${data.total.toLocaleString()} 条`
                  : `共 ${data.total.toLocaleString()} 条`}
              </p>
              {totalPages > 1 ? (
                <div className="flex gap-2">
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={applied.page <= 1}
                    onClick={() => setApplied({ ...applied, page: applied.page - 1 })}
                  >
                    <ChevronLeft aria-hidden="true" /> 上一页
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={applied.page >= totalPages}
                    onClick={() => setApplied({ ...applied, page: applied.page + 1 })}
                  >
                    下一页 <ChevronRight aria-hidden="true" />
                  </Button>
                </div>
              ) : null}
            </nav>
          </>
        ) : null}
      </div>

      <Sheet open={selected !== null} onOpenChange={(open) => (open ? null : setSelected(null))}>
        <SheetContent className="p-0">
          {selected ? <MemoryDetail memory={selected} /> : null}
        </SheetContent>
      </Sheet>
    </div>
  );
}
