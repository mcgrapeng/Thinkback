/** P4 治理操作台:Editorial Premium 重做。
 *
 * 3 个操作(删除/更新/重建)各一张 elevated 卡,字段 label 在上、控件在下。
 * 删除要求复述 user_id(sc=all);所有操作经 AlertDialog 二次确认。
 * 操作中按钮 disabled + Loader2 spinner,toast 含\"查看任务\"跳转。
 */

import { useEffect, useState } from "react";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { AlertTriangle, Info, Loader2, ShieldAlert, Trash2, Wrench, RefreshCw } from "lucide-react";
import { toast } from "sonner";
import { api, ApiError } from "@/api/client";
import { PageHeader } from "@/components/page-header";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useIsMobile } from "@/lib/use-is-mobile";
import { cn } from "@/lib/utils";

type PendingOp =
  | { kind: "delete"; title: string; description: string; run: () => Promise<unknown> }
  | { kind: "update"; title: string; description: string; run: () => Promise<unknown> }
  | { kind: "rebuild"; title: string; description: string; run: () => Promise<unknown> };

function errorDetail(error: unknown): string {
  if (error instanceof ApiError) {
    try {
      const parsed = JSON.parse(error.message) as { detail?: unknown };
      if (typeof parsed.detail === "string") return parsed.detail;
      if (Array.isArray(parsed.detail)) {
        const first = parsed.detail[0] as { loc?: unknown[]; msg?: string } | undefined;
        if (first?.msg) return `${(first.loc ?? []).join(".")}: ${first.msg}`;
      }
    } catch {
      return error.message;
    }
  }
  return error instanceof Error ? error.message : String(error);
}

/** 字段:label 在上、ⓘ hint 浮窗、控件在下、底注灰字。 */
function FormField({
  label,
  htmlFor,
  hint,
  children,
}: {
  label: string;
  htmlFor: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <label
        htmlFor={htmlFor}
        className="flex items-center gap-1.5 text-sm font-medium text-foreground-emphasis"
      >
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
            <TooltipContent className="max-w-xs">{hint}</TooltipContent>
          </Tooltip>
        ) : null}
      </label>
      {children}
    </div>
  );
}

const OPERATION_KIND_META: Record<
  "delete" | "update" | "rebuild",
  { icon: typeof Trash2; label: string; tone: "error" | "info" | "violet" }
> = {
  delete: { icon: Trash2, label: "DELETE", tone: "error" },
  update: { icon: Wrench, label: "UPDATE", tone: "info" },
  rebuild: { icon: RefreshCw, label: "REBUILD", tone: "violet" },
};

export function GovernPage() {
  const navigate = useNavigate({ from: "/govern" });
  const search = useSearch({ from: "/govern" }) as {
    memory_id?: string;
    user_id?: string;
    tab?: string;
  };
  const isMobile = useIsMobile();

  const initialTab: "delete" | "update" | "rebuild" =
    search.tab === "update" || search.tab === "rebuild" ? search.tab : "delete";
  const [activeTab, setActiveTab] = useState<"delete" | "update" | "rebuild">(initialTab);

  const [deleteForm, setDeleteForm] = useState({
    user_id: search.user_id ?? "",
    scope: "memory",
    memory_id: search.memory_id ?? "",
    session_id: "",
    operation_id: "",
  });
  const [updateForm, setUpdateForm] = useState({
    user_id: search.user_id ?? "",
    memory_id: search.memory_id ?? "",
    content: "",
    operation_id: "",
  });
  const [rebuildForm, setRebuildForm] = useState({
    user_id: "",
    session_id: "",
    operation_id: "",
    rebuild_l2: true,
    rebuild_l3: true,
  });
  const [confirmText, setConfirmText] = useState("");
  const [pending, setPending] = useState<PendingOp | null>(null);
  const [running, setRunning] = useState(false);

  // tab 同步 URL
  useEffect(() => {
    navigate({
      search: {
        ...(search.memory_id ? { memory_id: search.memory_id } : {}),
        ...(search.user_id ? { user_id: search.user_id } : {}),
        ...(activeTab !== "delete" ? { tab: activeTab } : {}),
      },
      replace: true,
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab]);

  const request_id = () => `admin-${Date.now()}`;
  const operationId = (form: { operation_id: string }, prefix: string) =>
    form.operation_id.trim() || `${prefix}-${Date.now()}`;

  const deleteIsAll = deleteForm.scope === "all";
  const deleteReady =
    deleteForm.user_id.trim().length > 0 &&
    ((deleteForm.scope === "memory" && deleteForm.memory_id.trim().length > 0) ||
      (deleteForm.scope === "session" && deleteForm.session_id.trim().length > 0) ||
      deleteIsAll) &&
    (!deleteIsAll || confirmText === deleteForm.user_id);

  const updateReady =
    updateForm.user_id.trim().length > 0 &&
    updateForm.memory_id.trim().length > 0 &&
    updateForm.content.trim().length > 0;

  const rebuildReady = rebuildForm.user_id.trim().length > 0;

  const runOp = async (op: PendingOp) => {
    setRunning(true);
    try {
      const result = (await op.run()) as { task_id?: string };
      const taskId = result?.task_id;
      toast.success(`${op.title} 已提交`, {
        description: taskId ? `任务 ${taskId} · 后台执行中` : "已进入后台任务队列",
        action: taskId
          ? { label: "查看任务", onClick: () => navigate({ to: "/tasks", search: { tab: "all" } }) }
          : undefined,
        duration: 6000,
      });
    } catch (error) {
      toast.error(`${op.title} 失败`, {
        description: errorDetail(error),
        duration: 8000,
      });
    } finally {
      setRunning(false);
      setPending(null);
      setConfirmText("");
    }
  };

  return (
    <div className="space-y-6">
      <PageHeader
        title="GOVERN · 治理操作台"
        description="危险区 · 所有操作记录审计 · 不可逆操作要求复述确认"
      />

      {isMobile ? (
        <div className="flex items-start gap-3 rounded-2xl border border-warning-strong/30 bg-warning-soft p-4 animate-editorial-fade-up">
          <ShieldAlert aria-hidden="true" className="mt-0.5 size-5 shrink-0 text-warning" />
          <div>
            <p className="text-sm font-medium text-foreground-intense">移动端仅可查看</p>
            <p className="mt-0.5 text-xs text-foreground-muted">治理操作请在桌面端执行</p>
          </div>
        </div>
      ) : null}

      {/* Tab 长条 */}
      <div className="flex flex-wrap items-center gap-2 animate-editorial-fade-up" style={{ animationDelay: "60ms" }}>
        <p className="section-label mr-2">OPERATION</p>
        {(["delete", "update", "rebuild"] as const).map((kind) => {
          const meta = OPERATION_KIND_META[kind];
          const active = activeTab === kind;
          return (
            <button
              key={kind}
              onClick={() => setActiveTab(kind)}
              className={cn(
                "flex items-center gap-2 rounded-xl border px-4 py-1.5 text-sm font-medium transition-colors",
                active
                  ? "border-foreground-intense text-foreground-intense bg-background"
                  : "border-border text-foreground-muted hover:text-foreground-emphasis hover:bg-background-muted",
                active && "shadow-[inset_0_-1px_0_0_var(--foreground-intense)]",
              )}
            >
              <meta.icon aria-hidden="true" className="size-3.5" />
              {meta.label}
            </button>
          );
        })}
      </div>

      <div className="editorial-rule" />

      {/* ── 删除（危险操作） ─────────────────────────────── */}
      {activeTab === "delete" ? (
        <Card variant="elevated" className="max-w-2xl animate-editorial-fade-up border-l-[3px] border-l-error">
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <Trash2 aria-hidden="true" className="size-4 text-error" />
              删除记忆
            </CardTitle>
            <CardDescription>
              按范围删除指定用户的记忆并留下删除屏障;用户全局删除不可逆。
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="grid gap-4 sm:grid-cols-2">
              <FormField
                label="用户 ID"
                htmlFor="del-user"
                hint="要操作的目标用户唯一标识。"
              >
                <Input
                  id="del-user"
                  value={deleteForm.user_id}
                  onChange={(event) =>
                    setDeleteForm({ ...deleteForm, user_id: event.target.value })
                  }
                />
              </FormField>
              <FormField
                label="删除范围"
                htmlFor="del-scope"
                hint="单条=精准删除一条记忆;会话=删一个 session 的所有记忆;用户全局=删该用户所有记忆且不可逆。"
              >
                <Select
                  value={deleteForm.scope}
                  onValueChange={(value) =>
                    setDeleteForm({ ...deleteForm, scope: value })
                  }
                >
                  <SelectTrigger id="del-scope">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="memory">单条记忆</SelectItem>
                    <SelectItem value="session">整个会话</SelectItem>
                    <SelectItem value="all">用户全局（不可逆）</SelectItem>
                  </SelectContent>
                </Select>
              </FormField>
            </div>

            {deleteForm.scope === "memory" ? (
              <FormField
                label="记忆 ID"
                htmlFor="del-memory"
                hint="从记忆浏览器可复制;模糊匹配将精确到单条。"
              >
                <Input
                  id="del-memory"
                  className="font-mono"
                  value={deleteForm.memory_id}
                  onChange={(event) =>
                    setDeleteForm({ ...deleteForm, memory_id: event.target.value })
                  }
                />
              </FormField>
            ) : null}
            {deleteForm.scope === "session" ? (
              <FormField label="会话 ID" htmlFor="del-session">
                <Input
                  id="del-session"
                  value={deleteForm.session_id}
                  onChange={(event) =>
                    setDeleteForm({ ...deleteForm, session_id: event.target.value })
                  }
                />
              </FormField>
            ) : null}
            {deleteIsAll ? (
              <div className="rounded-2xl border border-error-strong/40 bg-error-soft/30 p-4 space-y-2">
                <FormField
                  label="复述该用户 ID 以确认（不可逆操作）"
                  htmlFor="del-confirm"
                  hint={`输入 ${deleteForm.user_id || "用户 ID"} 后按钮才会启用。`}
                >
                  <Input
                    id="del-confirm"
                    className="font-mono"
                    value={confirmText}
                    onChange={(event) => setConfirmText(event.target.value)}
                  />
                </FormField>
                <p className="flex items-center gap-1.5 text-xs text-error">
                  <AlertTriangle aria-hidden="true" className="size-3.5" />
                  用户全局删除将抹除该用户全部记忆,且审计记录中标记为不可恢复。
                </p>
              </div>
            ) : null}
            <FormField
              label="操作 ID"
              htmlFor="del-op"
              hint="幂等键。相同 ID 重提会复用任务不会重复执行;留空自动生成。"
            >
              <Input
                id="del-op"
                value={deleteForm.operation_id}
                onChange={(event) =>
                  setDeleteForm({ ...deleteForm, operation_id: event.target.value })
                }
              />
            </FormField>
          </CardContent>
          <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border p-5">
            <p className="text-xs text-foreground-muted">
              执行后进入后台任务队列,结果可在任务监控页跟踪。
            </p>
            <Button
              variant="destructive"
              disabled={!deleteReady || isMobile || running}
              onClick={() =>
                setPending({
                  kind: "delete",
                  title: "删除",
                  description: deleteIsAll
                    ? `将删除 ${deleteForm.user_id} 的全部记忆并留下删除屏障,操作不可逆。`
                    : `将对 ${deleteForm.user_id} 执行 ${deleteForm.scope === "memory" ? "单条" : "会话"}删除。`,
                  run: () =>
                    api.deleteMemory({
                      request_id: request_id(),
                      user_id: deleteForm.user_id.trim(),
                      scope: deleteForm.scope,
                      operation_id: operationId(deleteForm, "admin-del"),
                      memory_id:
                        deleteForm.scope === "memory" ? deleteForm.memory_id.trim() : undefined,
                      session_id:
                        deleteForm.scope === "session"
                          ? deleteForm.session_id.trim()
                          : undefined,
                    }),
                })
              }
            >
              {running && pending?.kind === "delete" ? (
                <Loader2 aria-hidden="true" className="size-4 animate-spin" />
              ) : null}
              执行删除
            </Button>
          </div>
        </Card>
      ) : null}

      {/* ── 更新 ─────────────────────────────────────────── */}
      {activeTab === "update" ? (
        <Card variant="elevated" className="max-w-2xl animate-editorial-fade-up border-l-[3px] border-l-info">
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <Wrench aria-hidden="true" className="size-4 text-info" />
              更新记忆正文
            </CardTitle>
            <CardDescription>覆写指定记忆的文本内容,原内容进入审计留痕。</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="grid gap-4 sm:grid-cols-2">
              <FormField label="用户 ID" htmlFor="upd-user" hint="目标用户 ID。">
                <Input
                  id="upd-user"
                  value={updateForm.user_id}
                  onChange={(event) =>
                    setUpdateForm({ ...updateForm, user_id: event.target.value })
                  }
                />
              </FormField>
              <FormField
                label="记忆 ID"
                htmlFor="upd-memory"
                hint="要覆写的记忆 ID。"
              >
                <Input
                  id="upd-memory"
                  className="font-mono"
                  value={updateForm.memory_id}
                  onChange={(event) =>
                    setUpdateForm({ ...updateForm, memory_id: event.target.value })
                  }
                />
              </FormField>
            </div>
            <FormField
              label="新的记忆正文"
              htmlFor="upd-content"
              hint="将完整替换原内容;原内容会进审计可恢复。"
            >
              <Textarea
                id="upd-content"
                value={updateForm.content}
                onChange={(event) =>
                  setUpdateForm({ ...updateForm, content: event.target.value })
                }
              />
            </FormField>
            <FormField
              label="操作 ID"
              htmlFor="upd-op"
              hint="幂等键,可留空自动生成。"
            >
              <Input
                id="upd-op"
                value={updateForm.operation_id}
                onChange={(event) =>
                  setUpdateForm({ ...updateForm, operation_id: event.target.value })
                }
              />
            </FormField>
          </CardContent>
          <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border p-5">
            <p className="text-xs text-foreground-muted">
              执行后进入后台任务队列,结果可在任务监控页跟踪。
            </p>
            <Button
              disabled={!updateReady || isMobile || running}
              onClick={() =>
                setPending({
                  kind: "update",
                  title: "更新",
                  description: `将更新记忆 ${updateForm.memory_id} 的正文。`,
                  run: () =>
                    api.updateMemory({
                      request_id: request_id(),
                      user_id: updateForm.user_id.trim(),
                      memory_id: updateForm.memory_id.trim(),
                      content: updateForm.content.trim(),
                      operation_id: operationId(updateForm, "admin-upd"),
                    }),
                })
              }
            >
              {running && pending?.kind === "update" ? (
                <Loader2 aria-hidden="true" className="size-4 animate-spin" />
              ) : null}
              执行更新
            </Button>
          </div>
        </Card>
      ) : null}

      {/* ── 重建 ─────────────────────────────────────────── */}
      {activeTab === "rebuild" ? (
        <Card variant="elevated" className="max-w-2xl animate-editorial-fade-up border-l-[3px] border-l-violet">
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <RefreshCw aria-hidden="true" className="size-4 text-violet" />
              重建记忆索引
            </CardTitle>
            <CardDescription>从 journal 原文重放,重建 L2 摘要与 L3 长期索引。</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="grid gap-4 sm:grid-cols-2">
              <FormField label="用户 ID" htmlFor="rb-user" hint="目标用户 ID。">
                <Input
                  id="rb-user"
                  value={rebuildForm.user_id}
                  onChange={(event) =>
                    setRebuildForm({ ...rebuildForm, user_id: event.target.value })
                  }
                />
              </FormField>
              <FormField
                label="会话 ID（可选）"
                htmlFor="rb-session"
                hint="留空重建该用户所有会话;填写则仅重建该会话。"
              >
                <Input
                  id="rb-session"
                  value={rebuildForm.session_id}
                  onChange={(event) =>
                    setRebuildForm({ ...rebuildForm, session_id: event.target.value })
                  }
                />
              </FormField>
              <FormField
                label="重建 L2 摘要"
                htmlFor="rb-l2"
                hint="L2 是记忆的浓缩摘要;开启会重新生成。"
              >
                <Select
                  value={rebuildForm.rebuild_l2 ? "yes" : "no"}
                  onValueChange={(value) =>
                    setRebuildForm({ ...rebuildForm, rebuild_l2: value === "yes" })
                  }
                >
                  <SelectTrigger id="rb-l2">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="yes">是</SelectItem>
                    <SelectItem value="no">否</SelectItem>
                  </SelectContent>
                </Select>
              </FormField>
              <FormField
                label="重建 L3 索引"
                htmlFor="rb-l3"
                hint="L3 是长期向量索引;开启会重新嵌入并入库。"
              >
                <Select
                  value={rebuildForm.rebuild_l3 ? "yes" : "no"}
                  onValueChange={(value) =>
                    setRebuildForm({ ...rebuildForm, rebuild_l3: value === "yes" })
                  }
                >
                  <SelectTrigger id="rb-l3">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="yes">是</SelectItem>
                    <SelectItem value="no">否</SelectItem>
                  </SelectContent>
                </Select>
              </FormField>
            </div>
            <FormField label="操作 ID" htmlFor="rb-op" hint="幂等键,可留空自动生成。">
              <Input
                id="rb-op"
                value={rebuildForm.operation_id}
                onChange={(event) =>
                  setRebuildForm({ ...rebuildForm, operation_id: event.target.value })
                }
              />
            </FormField>
          </CardContent>
          <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border p-5">
            <p className="text-xs text-foreground-muted">
              重建耗时与用户记忆量成正比,结果可在任务监控页跟踪。
            </p>
            <Button
              disabled={!rebuildReady || isMobile || running}
              onClick={() =>
                setPending({
                  kind: "rebuild",
                  title: "重建",
                  description: `将对 ${rebuildForm.user_id}${
                    rebuildForm.session_id ? ` 会话 ${rebuildForm.session_id}` : ""
                  } 执行重建（L2: ${rebuildForm.rebuild_l2 ? "是" : "否"}，L3: ${
                    rebuildForm.rebuild_l3 ? "是" : "否"
                  }）。`,
                  run: () =>
                    api.rebuildMemory({
                      request_id: request_id(),
                      user_id: rebuildForm.user_id.trim(),
                      session_id: rebuildForm.session_id.trim() || undefined,
                      rebuild_l2: rebuildForm.rebuild_l2,
                      rebuild_l3: rebuildForm.rebuild_l3,
                      operation_id: operationId(rebuildForm, "admin-rb"),
                    }),
                })
              }
            >
              {running && pending?.kind === "rebuild" ? (
                <Loader2 aria-hidden="true" className="size-4 animate-spin" />
              ) : null}
              执行重建
            </Button>
          </div>
        </Card>
      ) : null}

      <AlertDialog
        open={pending !== null}
        onOpenChange={(open) => (open ? null : (setPending(null), setConfirmText("")))}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>确认执行:{pending?.title}</AlertDialogTitle>
            <AlertDialogDescription>{pending?.description}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction
              variant={pending?.kind === "delete" ? "destructive" : "default"}
              onClick={() => pending && void runOp(pending)}
            >
              确认执行
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <p className="text-sm text-foreground-muted">
        提交后可在{" "}
        <Link
          to="/tasks"
          className="font-medium text-foreground-intense underline underline-offset-4"
        >
          任务监控
        </Link>{" "}
        跟踪执行状态。
      </p>
    </div>
  );
}
