/** mem0 深度管理 — 抽取提示词 + 更新策略 + 召回回答 + 架构文档。
 *
 * 治理台可编辑 mem0 的所有核心 prompt,实时生效。
 * 面向 AI 工程师:每个 prompt 附带调优提示、使用示例、架构说明。
 */

import { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle,
  BookOpen,
  ChevronDown,
  ChevronRight,
  Copy,
  Lightbulb,
  RefreshCw,
  RotateCcw,
  Save,
  Search,
  Sparkles,
  Zap,
} from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { EmptyState } from "@/components/empty-state";
import { SkeletonLines } from "@/components/ui/skeleton";
import { cn, formatTime } from "@/lib/utils";

interface Mem0ConfigItem {
  key: string;
  value: string;
  description: string;
  is_active: boolean;
  updated_at: string | null;
}

interface ConfigSection {
  key: string;
  title: string;
  category: string;
  icon: string;
  tips: string[];
  examples: { label: string; code: string }[];
}

const ICON_MAP: Record<string, typeof Sparkles> = {
  Sparkles,
  RefreshCw,
  Search,
  Settings: Zap,
  BookOpen,
};

const CATEGORY_COLOR: Record<string, string> = {
  extraction: "bg-info/10 text-info",
  update: "bg-warning/10 text-warning",
  recall: "bg-success/10 text-success",
};

// ─── mem0 架构文档(给 AI 工程师) ────────────────────────────────────────

const ARCHITECTURE = [
  {
    stage: "1. 事实抽取 (Extraction)",
    description: "从对话中识别并提取值得记住的事实",
    prompts: ["custom_instructions", "extraction_system_prompt"],
    detail: "mem0 的核心 LLM 调用。v2 模式下,`custom_instructions` 以「## Custom Instructions」段拼入 `ADDITIVE_EXTRACTION_PROMPT` 的 user prompt 中。System prompt 由 mem0 自带,负责定义抽取的角色、输入格式、去重规则、时间锚定等。",
    keyPoints: [
      "一次写入 = 一轮 user→assistant 对话",
      "LLM 从对话中抽取 0-N 条记忆事实",
      "每条记忆: id + text(自包含的一句话事实)",
      "自动去重: 与已有记忆对比,跳过已存在的",
      "自动关联: 通过 linked_memory_ids 链接相关记忆",
    ],
  },
  {
    stage: "2. 记忆更新 (Update)",
    description: "新事实与已有记忆冲突时的处理策略",
    prompts: ["update_memory_prompt"],
    detail: "当抽取的新事实与已有记忆相关时,mem0 调用 LLM 决定:ADD(新增) / UPDATE(更新) / DELETE(删除) / NONE(不变)。这个 prompt 控制决策规则。",
    keyPoints: [
      "ADD: 全新信息 → 新增一条记忆",
      "UPDATE: 同类信息但内容不同 → 更新已有记忆",
      "DELETE: 新事实与旧记忆矛盾 → 删除旧记忆",
      "NONE: 已存在或无关 → 不操作",
    ],
  },
  {
    stage: "3. 向量存储 (Vector Store)",
    description: "记忆编码后存入 Milvus 向量库",
    prompts: [],
    detail: "每条记忆文本经 embedding model(OpenAI 兼容)编码为向量,存入 Milvus。支持语义相似度检索、关键词检索(如果 Milvus 支持)。",
    keyPoints: [
      "Embedding 模型: text-embedding-3-small (1536 维)",
      "向量库: Milvus (collection: agent_semantic_memory_v1)",
      "元数据: user_id / memory_scope_id / source_type / data_classification",
    ],
  },
  {
    stage: "4. 记忆召回 (Recall)",
    description: "基于查询语义检索相关记忆",
    prompts: ["memory_answer_prompt"],
    detail: "查询文本经 embedding 编码后,在 Milvus 中做 KNN 检索,返回最相似的 N 条记忆。`memory_answer_prompt` 控制 LLM 如何基于检索结果回答问题。",
    keyPoints: [
      "KNN 语义检索(余弦相似度)",
      "可配置 score_threshold(过滤低相关性)",
      "可配置 token_budget(控制回答长度)",
      "降级策略: LLM 不可用时返回原始检索结果",
    ],
  },
];

// ─── API 调用 ────────────────────────────────────────────────────────────

async function fetchConfigs(): Promise<Mem0ConfigItem[]> {
  const res = await fetch("/admin/api/mem0/configs");
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

async function fetchSections(): Promise<ConfigSection[]> {
  const res = await fetch("/admin/api/mem0/sections");
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
  const res = await fetch(`/admin/api/mem0/configs/${key}/reset`, { method: "POST" });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
}

// ─── 组件 ────────────────────────────────────────────────────────────────

function ConfigCard({
  config,
  section,
  onSave,
  onReset,
  isSaving,
}: {
  config: Mem0ConfigItem;
  section: ConfigSection | undefined;
  onSave: (key: string, value: string) => void;
  onReset: (key: string) => void;
  isSaving: boolean;
}) {
  const [isEditing, setIsEditing] = useState(false);
  const [editValue, setEditValue] = useState(config.value);
  const [showTips, setShowTips] = useState(false);

  useEffect(() => {
    setEditValue(config.value);
  }, [config.value]);

  const icon = section?.icon ? ICON_MAP[section.icon] ?? Zap : Zap;
  const catColor = section?.category ? CATEGORY_COLOR[section.category] ?? "bg-neutral-soft text-neutral" : "bg-neutral-soft text-neutral";

  return (
    <Card className="border-border-muted">
      <CardContent className="p-5 space-y-4">
        {/* 头部 */}
        <div className="flex items-start justify-between gap-3">
          <div className="flex items-start gap-3 min-w-0 flex-1">
            <div className="flex size-9 shrink-0 items-center justify-center rounded-full bg-background-muted">
              {icon && (() => {
                const Icon = icon;
                return <Icon className="size-4 text-foreground-emphasis" aria-hidden="true" />;
              })()}
            </div>
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <code className="text-sm font-mono font-medium text-foreground-intense">{config.key}</code>
                <Badge variant="secondary" className={cn("text-[10px]", catColor)}>
                  {section?.category ?? "config"}
                </Badge>
                {config.updated_at && (
                  <span className="text-[10px] text-foreground-soft">
                    更新于 {formatTime(config.updated_at)}
                  </span>
                )}
              </div>
              <p className="mt-1 text-xs text-foreground-muted leading-relaxed">
                {config.description}
              </p>
            </div>
          </div>
          <div className="flex items-center gap-1.5 shrink-0">
            {!isEditing && (
              <Button variant="ghost" size="sm" onClick={() => setIsEditing(true)}>
                编辑
              </Button>
            )}
            <Button
              variant="ghost"
              size="sm"
              onClick={() => onReset(config.key)}
              disabled={isSaving}
              title="恢复默认值"
            >
              <RotateCcw className="size-3.5" />
              重置
            </Button>
          </div>
        </div>

        {/* Prompt 内容 */}
        {isEditing ? (
          <div className="space-y-3">
            <textarea
              value={editValue}
              onChange={(e) => setEditValue(e.target.value)}
              className="w-full rounded-xl border border-border bg-background-muted p-4 font-mono text-xs text-foreground-emphasis leading-relaxed min-h-[240px] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--ring)]"
              placeholder="输入 prompt 内容..."
              rows={16}
            />
            <div className="flex items-center justify-end gap-2">
              <Button variant="ghost" size="sm" onClick={() => { setIsEditing(false); setEditValue(config.value); }}>
                取消
              </Button>
              <Button
                size="sm"
                onClick={() => onSave(config.key, editValue)}
                disabled={isSaving || !editValue.trim() || editValue === config.value}
              >
                {isSaving ? "保存中..." : "保存"}
                {!isSaving && <Save className="size-3.5" />}
              </Button>
            </div>
          </div>
        ) : (
          <div className="relative rounded-xl bg-foreground-intense p-4">
            <pre
              tabIndex={0}
              role="region"
              aria-label="抽取提示词预览"
              className="whitespace-pre-wrap font-mono text-[11px] leading-relaxed text-background overflow-x-auto max-h-[180px] overflow-y-auto"
            >
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

        {/* 调优提示 */}
        {section && section.tips.length > 0 && (
          <div>
            <button
              type="button"
              className="flex items-center gap-1.5 text-xs font-medium text-foreground-emphasis hover:text-foreground-intense transition-colors"
              onClick={() => setShowTips(!showTips)}
            >
              {showTips ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />}
              <Lightbulb className="size-3.5" />
              调优提示 ({section.tips.length})
            </button>
            {showTips && (
              <ul className="mt-2 space-y-1.5 pl-6">
                {section.tips.map((tip, i) => (
                  <li key={i} className="text-xs text-foreground-muted leading-relaxed flex items-start gap-1.5">
                    <span className="text-foreground-soft mt-0.5">•</span>
                    {tip}
                  </li>
                ))}
                {section.examples.length > 0 && (
                  <li className="mt-2 space-y-1.5">
                    <p className="text-xs font-medium text-foreground-emphasis">示例:</p>
                    {section.examples.map((ex, i) => (
                      <div key={i} className="ml-2">
                        <p className="text-[10px] text-foreground-soft">{ex.label}</p>
                        <code className="block rounded bg-background-muted p-2 font-mono text-[10px] text-foreground-emphasis leading-relaxed">
                          {ex.code}
                        </code>
                      </div>
                    ))}
                  </li>
                )}
              </ul>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function ArchitectureSection() {
  const [expandedStage, setExpandedStage] = useState<number | null>(0);
  return (
    <Card className="border-border-muted">
      <CardContent className="p-5 space-y-4">
        <div className="flex items-center gap-2">
          <BookOpen className="size-4 text-foreground-emphasis" />
          <h3 className="text-base font-semibold text-foreground-intense">mem0 处理流水线</h3>
          <Badge variant="secondary" className="text-[10px]">AI 工程师指南</Badge>
        </div>
        <p className="text-xs text-foreground-muted leading-relaxed">
          理解 mem0 的处理流程,才能精准调整提示词。以下是每一步的说明和可定制的 prompt。
        </p>
        <div className="space-y-2">
          {ARCHITECTURE.map((stage, i) => (
            <div key={i} className="rounded-xl border border-border-muted">
              <button
                type="button"
                className="flex w-full items-center gap-3 p-3 text-left hover:bg-background-muted transition-colors"
                onClick={() => setExpandedStage(expandedStage === i ? null : i)}
              >
                {expandedStage === i ? <ChevronDown className="size-4" /> : <ChevronRight className="size-4" />}
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-medium text-foreground-intense">{stage.stage}</p>
                  <p className="text-xs text-foreground-muted">{stage.description}</p>
                </div>
                <div className="flex gap-1">
                  {stage.prompts.map((p) => (
                    <code key={p} className="rounded bg-background-muted px-1.5 py-0.5 font-mono text-[9px] text-foreground-emphasis">
                      {p}
                    </code>
                  ))}
                </div>
              </button>
              {expandedStage === i && (
                <div className="px-3 pb-3 space-y-2">
                  <p className="text-xs text-foreground-muted leading-relaxed">{stage.detail}</p>
                  <ul className="space-y-1">
                    {stage.keyPoints.map((point, j) => (
                      <li key={j} className="text-xs text-foreground-muted flex items-start gap-1.5">
                        <span className="text-foreground-soft mt-0.5">▸</span>
                        {point}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}

export function Mem0ConfigSection() {
  const [configs, setConfigs] = useState<Mem0ConfigItem[]>([]);
  const [sections, setSections] = useState<ConfigSection[]>([]);
  const [isPending, setIsPending] = useState(true);
  const [isError, setIsError] = useState(false);
  const [savingKey, setSavingKey] = useState<string | null>(null);

  const load = useCallback(async () => {
    setIsPending(true);
    setIsError(false);
    try {
      const [configData, sectionData] = await Promise.all([
        fetchConfigs(),
        fetchSections(),
      ]);
      setConfigs(configData);
      setSections(sectionData);
    } catch {
      setIsError(true);
    } finally {
      setIsPending(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const handleSave = async (key: string, value: string) => {
    setSavingKey(key);
    try {
      await saveConfig(key, value);
      toast.success("配置已保存，下次写入记忆时生效");
      await load();
    } catch {
      toast.error("保存失败");
    } finally {
      setSavingKey(null);
    }
  };

  const handleReset = async (key: string) => {
    if (!confirm("确认恢复默认值？当前自定义内容将被覆盖。")) return;
    setSavingKey(key);
    try {
      await resetConfig(key);
      toast.success("已恢复默认值");
      await load();
    } catch {
      toast.error("恢复失败");
    } finally {
      setSavingKey(null);
    }
  };

  return (
    <section className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="flex items-center gap-2 text-lg font-semibold tracking-tight">
          <Sparkles className="size-4" /> mem0 深度管理
          <Badge variant="secondary" className="ml-1 text-[10px]">实时生效</Badge>
        </h2>
        <p className="text-xs text-foreground-muted">
          修改后下次写入记忆时生效，无需重启服务
        </p>
      </div>

      {/* mem0 架构说明 */}
      <ArchitectureSection />

      <div className="editorial-rule" />

      {/* Prompt 配置 */}
      {isError ? (
        <EmptyState
          variant="error"
          icon={AlertTriangle}
          title="无法加载 mem0 配置"
          description="请确认后端服务可达。"
          action={{ label: "重试", onClick: () => load() }}
        />
      ) : isPending ? (
        <div aria-busy="true" className="space-y-3">
          {Array.from({ length: 2 }).map((_, i) => (
            <Card key={i} className="border-border-muted">
              <CardContent className="p-5 space-y-2">
                <SkeletonLines count={3} />
              </CardContent>
            </Card>
          ))}
        </div>
      ) : (
        configs.map((config) => (
          <ConfigCard
            key={config.key}
            config={config}
            section={sections.find((s) => s.key === config.key)}
            onSave={handleSave}
            onReset={handleReset}
            isSaving={savingKey === config.key}
          />
        ))
      )}

      {/* 使用说明 */}
      <div className="rounded-xl border border-border-muted bg-background-subtle p-4 text-xs text-foreground-muted space-y-1.5">
        <p className="font-medium text-foreground-emphasis">如何最大化 mem0 的效果？</p>
        <p>
          <strong>1. 精准抽取</strong>：custom_instructions 控制 LLM 从对话中抽取什么。约束越精确，记忆质量越高。
          例如「每次对话最多抽 3 条」可避免过度抽取。
        </p>
        <p>
          <strong>2. 冲突解决</strong>：update_memory_prompt 控制新旧记忆冲突时的决策。常见场景：
          用户说「不叫 X，叫 Y」→ 应 UPDATE 而非 ADD。
        </p>
        <p>
          <strong>3. 召回优化</strong>：memory_answer_prompt 控制 LLM 如何利用检索结果。
          业务场景可定制：客服加「主动询问」、写作助手加「引用原文」。
        </p>
        <p>
          <strong>4. 渐进调优</strong>：先用默认值跑通，再根据实际记忆质量逐步调整。
          每次只改一个 prompt，观察效果后再改下一个。
        </p>
      </div>
    </section>
  );
}
