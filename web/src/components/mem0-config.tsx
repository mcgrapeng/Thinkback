/** mem0 抽取提示词配置 — 治理台可编辑的 mem0 custom_instructions。
 *
 * 存储在 ins_mem0_config 表,每次 append 时实时读取(无需重启)。
 * 当前支持: custom_instructions(mem0 v2 的 user prompt 追加段)。
 */

import { useEffect, useState } from "react";
import { AlertTriangle, Copy, RotateCcw, Save, Sparkles } from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { EmptyState } from "@/components/empty-state";
import { SkeletonLines } from "@/components/ui/skeleton";
import { formatTime } from "@/lib/utils";

interface Mem0ConfigItem {
  key: string;
  value: string;
  description: string;
  is_active: boolean;
  updated_at: string | null;
}

async function fetchConfigs(): Promise<Mem0ConfigItem[]> {
  const res = await fetch("/admin/api/mem0/configs");
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

async function saveConfig(key: string, value: string): Promise<void> {
  const res = await fetch(`/admin/api/mem0/configs/${key}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ value }),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
}

async function resetConfig(key: string): Promise<void> {
  const res = await fetch(`/admin/api/mem0/configs/${key}/reset`, {
    method: "POST",
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
}

export function Mem0ConfigSection() {
  const [configs, setConfigs] = useState<Mem0ConfigItem[]>([]);
  const [isPending, setIsPending] = useState(true);
  const [isError, setIsError] = useState(false);
  const [editKey, setEditKey] = useState<string | null>(null);
  const [editValue, setEditValue] = useState("");
  const [saving, setSaving] = useState(false);

  const load = async () => {
    setIsPending(true);
    setIsError(false);
    try {
      const data = await fetchConfigs();
      setConfigs(data);
    } catch {
      setIsError(true);
    } finally {
      setIsPending(false);
    }
  };

  useEffect(() => {
    void load();
  }, []);

  const handleSave = async (key: string) => {
    if (!editValue.trim()) return;
    setSaving(true);
    try {
      await saveConfig(key, editValue);
      toast.success("配置已保存，下次写入记忆时生效");
      setEditKey(null);
      await load();
    } catch {
      toast.error("保存失败");
    } finally {
      setSaving(false);
    }
  };

  const handleReset = async (key: string) => {
    if (!confirm("确认恢复默认值？当前自定义内容将被覆盖。")) return;
    setSaving(true);
    try {
      await resetConfig(key);
      toast.success("已恢复默认值");
      await load();
    } catch {
      toast.error("恢复失败");
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="flex items-center gap-2 text-lg font-semibold tracking-tight">
          <Sparkles className="size-4" /> mem0 抽取提示词
          <Badge variant="secondary" className="ml-1 text-[10px]">实时生效</Badge>
        </h2>
        <p className="text-xs text-foreground-muted">
          修改后下次写入记忆时生效，无需重启服务
        </p>
      </div>

      {isError ? (
        <EmptyState
          variant="error"
          icon={AlertTriangle}
          title="无法加载 mem0 配置"
          description="请确认后端服务可达。"
          action={{ label: "重试", onClick: () => load() }}
        />
      ) : isPending ? (
        <Card className="border-border-muted">
          <CardContent className="p-4 space-y-2">
            <SkeletonLines count={4} />
          </CardContent>
        </Card>
      ) : (
        configs.map((config) => {
          const isEditing = editKey === config.key;
          return (
            <Card key={config.key} className="border-border-muted">
              <CardContent className="p-4 space-y-3">
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <p className="text-sm font-medium text-foreground-intense">
                      {config.key}
                    </p>
                    <p className="text-xs text-foreground-muted mt-0.5">
                      {config.description}
                    </p>
                    {config.updated_at && (
                      <p className="text-[10px] text-foreground-soft mt-1">
                        最后更新 {formatTime(config.updated_at)}
                      </p>
                    )}
                  </div>
                  <div className="flex items-center gap-1.5">
                    {!isEditing && (
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => {
                          setEditKey(config.key);
                          setEditValue(config.value);
                        }}
                      >
                        编辑
                      </Button>
                    )}
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => handleReset(config.key)}
                      disabled={saving}
                      title="恢复默认值"
                    >
                      <RotateCcw className="size-3.5" />
                      重置
                    </Button>
                  </div>
                </div>

                {isEditing ? (
                  <div className="space-y-2">
                    <textarea
                      value={editValue}
                      onChange={(e) => setEditValue(e.target.value)}
                      className="w-full rounded-xl border border-border bg-background-muted p-4 font-mono text-xs text-foreground-emphasis leading-relaxed min-h-[200px] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--ring)]"
                      placeholder="输入 mem0 抽取约束..."
                      rows={12}
                    />
                    <div className="flex items-center justify-end gap-2">
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => setEditKey(null)}
                      >
                        取消
                      </Button>
                      <Button
                        size="sm"
                        onClick={() => handleSave(config.key)}
                        disabled={saving || !editValue.trim()}
                      >
                        {saving ? "保存中..." : "保存"}
                        {!saving && <Save className="size-3.5" />}
                      </Button>
                    </div>
                  </div>
                ) : (
                  <div className="relative rounded-xl bg-foreground-intense p-4">
                    <pre className="whitespace-pre-wrap font-mono text-[11px] leading-relaxed text-background overflow-x-auto max-h-[200px] overflow-y-auto">
                      {config.value}
                    </pre>
                    <button
                      type="button"
                      className="absolute top-2 right-2 rounded-md bg-background/10 px-2 py-1 text-[10px] text-background/70 hover:bg-background/20 transition-colors"
                      onClick={() => {
                        void navigator.clipboard.writeText(config.value).then(() =>
                          toast.success("已复制"),
                        );
                      }}
                    >
                      <Copy className="size-3 inline mr-1" />
                      复制
                    </button>
                  </div>
                )}
              </CardContent>
            </Card>
          );
        })
      )}

      <div className="rounded-xl border border-border-muted bg-background-subtle p-4 text-xs text-foreground-muted space-y-1.5">
        <p className="font-medium text-foreground-emphasis">如何使用这些提示词？</p>
        <p>
          这些约束会以「## Custom Instructions」段拼入 mem0 的 user prompt，指导 LLM 从对话中抽取记忆事实。
        </p>
        <p>
          <strong>custom_instructions</strong> 控制：记忆文本的语言、输出格式（JSON schema）、
          实体保留规则、修正记忆的处理方式、是否拆分多条事实等。
        </p>
        <p>
          常见调优：加入业务领域术语（如「产品名 XX 保持英文」）或调整记忆粒度（如「一次对话最多抽 3 条」）。
        </p>
      </div>
    </section>
  );
}
