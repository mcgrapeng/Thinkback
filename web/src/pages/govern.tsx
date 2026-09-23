/** P4 治理操作台：删除 / 更新 / rebuild 受控执行。
 *
 * 安全设计：delete-all 要求复述 user_id；全部操作经 AlertDialog 确认；
 * 移动端只允许查看（重操作不下移动端）；结果 toast + 跳转任务跟踪。
 * 布局：每个操作一张卡（标题/说明 → 表单字段 → 底部操作行），
 * 危险操作（删除）用 destructive 按钮，其余用主按钮。
 */

import { useState } from "react";
import { Link, useSearch } from "@tanstack/react-router";
import { ShieldAlert } from "lucide-react";
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
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useIsMobile } from "@/lib/use-is-mobile";

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

/** 字段：标签在上、控件在下、可选提示（appica 表单规范）。 */
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
      <label htmlFor={htmlFor} className="text-sm font-medium text-foreground-emphasis">
        {label}
      </label>
      {children}
      {hint ? <p className="text-xs text-foreground-muted">{hint}</p> : null}
    </div>
  );
}

/** 卡片底部操作行：左侧说明、右侧执行按钮。 */
function CardFooterAction({
  note,
  children,
}: {
  note: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border p-5">
      <p className="text-xs text-foreground-muted">{note}</p>
      {children}
    </div>
  );
}

export function GovernPage() {
  const search = useSearch({ strict: false }) as {
    memory_id?: string;
    user_id?: string;
  };
  const isMobile = useIsMobile();

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

  const request_id = () => `admin-${Date.now()}`;
  const operationId = (form: { operation_id: string }, prefix: string) =>
    form.operation_id.trim() || `${prefix}-${Date.now()}`;

  const deleteIsAll = deleteForm.scope === "all";
  const deleteReady =
    deleteForm.user_id.trim().length > 0 &&
    deleteForm.operation_id.trim().length >= 0 &&
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
    try {
      const result = (await op.run()) as { task_id?: string };
      toast.success(`${op.title} 已提交`, {
        description: result?.task_id ? `任务 ${result.task_id}，可在任务监控页跟踪` : undefined,
      });
    } catch (error) {
      toast.error(`${op.title} 失败`, { description: errorDetail(error) });
    } finally {
      setPending(null);
      setConfirmText("");
    }
  };

  return (
    <div className="space-y-6">
      <PageHeader
        title="治理操作台"
        description="危险区 · 所有操作记录审计 · 不可逆操作要求复述确认"
      />

      {isMobile ? (
        <div className="flex items-start gap-2.5 rounded-xl border border-warning-strong/30 bg-warning-soft p-4 text-sm text-foreground">
          <ShieldAlert aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-warning" />
          <p>移动端仅支持查看与准备参数；执行请在桌面端进行。</p>
        </div>
      ) : null}

      <Tabs defaultValue="delete">
        <TabsList aria-label="选择治理操作">
          <TabsTrigger value="delete">删除</TabsTrigger>
          <TabsTrigger value="update">更新</TabsTrigger>
          <TabsTrigger value="rebuild">重建</TabsTrigger>
        </TabsList>

        {/* ── 删除（危险操作） ─────────────────────────────── */}
        <TabsContent value="delete" className="mt-4">
          <Card className="max-w-2xl">
            <CardHeader>
              <CardTitle>删除记忆</CardTitle>
              <CardDescription>
                按范围删除指定用户的记忆并留下删除屏障；用户全局删除不可逆。
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="grid gap-4 sm:grid-cols-2">
                <FormField label="用户 ID" htmlFor="del-user">
                  <Input
                    id="del-user"
                    value={deleteForm.user_id}
                    onChange={(event) =>
                      setDeleteForm({ ...deleteForm, user_id: event.target.value })
                    }
                  />
                </FormField>
                <FormField label="删除范围" htmlFor="del-scope">
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
                <FormField label="记忆 ID" htmlFor="del-memory">
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
                <FormField
                  label="复述该用户 ID 以确认（不可逆操作）"
                  htmlFor="del-confirm"
                  hint={`输入 ${deleteForm.user_id || "用户 ID"} 后按钮才会启用。`}
                >
                  <Input
                    id="del-confirm"
                    value={confirmText}
                    onChange={(event) => setConfirmText(event.target.value)}
                  />
                </FormField>
              ) : null}
              <FormField label="操作 ID" htmlFor="del-op" hint="幂等键，可留空自动生成。">
                <Input
                  id="del-op"
                  value={deleteForm.operation_id}
                  onChange={(event) =>
                    setDeleteForm({ ...deleteForm, operation_id: event.target.value })
                  }
                />
              </FormField>
            </CardContent>
            <CardFooterAction note="执行后进入后台任务队列，结果可在任务监控页跟踪。">
              <Button
                variant="destructive"
                disabled={!deleteReady || isMobile}
                onClick={() =>
                  setPending({
                    kind: "delete",
                    title: "删除",
                    description: deleteIsAll
                      ? `将删除 ${deleteForm.user_id} 的全部记忆并留下删除屏障，操作不可逆。`
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
                执行删除
              </Button>
            </CardFooterAction>
          </Card>
        </TabsContent>

        {/* ── 更新 ─────────────────────────────────────────── */}
        <TabsContent value="update" className="mt-4">
          <Card className="max-w-2xl">
            <CardHeader>
              <CardTitle>更新记忆正文</CardTitle>
              <CardDescription>覆写指定记忆的文本内容，原内容进入审计留痕。</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="grid gap-4 sm:grid-cols-2">
                <FormField label="用户 ID" htmlFor="upd-user">
                  <Input
                    id="upd-user"
                    value={updateForm.user_id}
                    onChange={(event) =>
                      setUpdateForm({ ...updateForm, user_id: event.target.value })
                    }
                  />
                </FormField>
                <FormField label="记忆 ID" htmlFor="upd-memory">
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
              <FormField label="新的记忆正文" htmlFor="upd-content">
                <Textarea
                  id="upd-content"
                  value={updateForm.content}
                  onChange={(event) =>
                    setUpdateForm({ ...updateForm, content: event.target.value })
                  }
                />
              </FormField>
              <FormField label="操作 ID" htmlFor="upd-op" hint="幂等键，可留空自动生成。">
                <Input
                  id="upd-op"
                  value={updateForm.operation_id}
                  onChange={(event) =>
                    setUpdateForm({ ...updateForm, operation_id: event.target.value })
                  }
                />
              </FormField>
            </CardContent>
            <CardFooterAction note="执行后进入后台任务队列，结果可在任务监控页跟踪。">
              <Button
                disabled={!updateReady || isMobile}
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
                执行更新
              </Button>
            </CardFooterAction>
          </Card>
        </TabsContent>

        {/* ── 重建 ─────────────────────────────────────────── */}
        <TabsContent value="rebuild" className="mt-4">
          <Card className="max-w-2xl">
            <CardHeader>
              <CardTitle>重建记忆索引</CardTitle>
              <CardDescription>从 journal 原文重放，重建 L2 摘要与 L3 长期索引。</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="grid gap-4 sm:grid-cols-2">
                <FormField label="用户 ID" htmlFor="rb-user">
                  <Input
                    id="rb-user"
                    value={rebuildForm.user_id}
                    onChange={(event) =>
                      setRebuildForm({ ...rebuildForm, user_id: event.target.value })
                    }
                  />
                </FormField>
                <FormField label="会话 ID（可选）" htmlFor="rb-session">
                  <Input
                    id="rb-session"
                    value={rebuildForm.session_id}
                    onChange={(event) =>
                      setRebuildForm({ ...rebuildForm, session_id: event.target.value })
                    }
                  />
                </FormField>
                <FormField label="重建 L2 摘要" htmlFor="rb-l2">
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
                <FormField label="重建 L3 索引" htmlFor="rb-l3">
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
              <FormField label="操作 ID" htmlFor="rb-op" hint="幂等键，可留空自动生成。">
                <Input
                  id="rb-op"
                  value={rebuildForm.operation_id}
                  onChange={(event) =>
                    setRebuildForm({ ...rebuildForm, operation_id: event.target.value })
                  }
                />
              </FormField>
            </CardContent>
            <CardFooterAction note="重建耗时与用户记忆量成正比，结果可在任务监控页跟踪。">
              <Button
                disabled={!rebuildReady || isMobile}
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
                执行重建
              </Button>
            </CardFooterAction>
          </Card>
        </TabsContent>
      </Tabs>

      <AlertDialog
        open={pending !== null}
        onOpenChange={(open) => (open ? null : (setPending(null), setConfirmText("")))}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>确认执行：{pending?.title}</AlertDialogTitle>
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
