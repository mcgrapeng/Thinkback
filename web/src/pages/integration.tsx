/** 集成页面:Thinkback 接入协议 v1。
 *
 * 内容:
 * 1. 协议概述(认证/限流/幂等/错误码)
 * 2. API Key 管理(列表/生成/撤销)
 * 3. 端点参考 + Quick Start curl 示例
 *
 * 详见 docs/specs/2026-10-08-thinkback-integration-protocol-v1.md
 */

import { useState } from "react";
import {
  AlertTriangle,
  BookOpen,
  Check,
  Code2,
  Copy,
  Key,
  Plus,
  Shield,
  Sparkles,
  Trash2,
  Webhook,
} from "lucide-react";
import { toast } from "sonner";
import {
  useApiKeys,
  useCreateApiKey,
  useRevokeApiKey,
} from "@/api/queries";
import { PageHeader } from "@/components/page-header";
import { StaleIndicator } from "@/components/stale-indicator";
import { Mem0ConfigSection } from "@/components/mem0-config";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
} from "@/components/ui/card";
import { EmptyState } from "@/components/empty-state";
import { Input } from "@/components/ui/input";
import { SkeletonLines } from "@/components/ui/skeleton";
import { cn, formatTime } from "@/lib/utils";

const ALL_SCOPES = [
  { value: "memory:append", label: "memory:append", desc: "写入对话轮次(抽取记忆)" },
  { value: "memory:recall", label: "memory:recall", desc: "召回相关记忆" },
  { value: "memory:read", label: "memory:read", desc: "读取/列出记忆" },
  { value: "memory:update", label: "memory:update", desc: "更新记忆正文" },
  { value: "memory:delete", label: "memory:delete", desc: "删除记忆" },
  { value: "memory:admin", label: "memory:admin", desc: "治理操作(重建等)" },
];

const ENDPOINTS = [
  { method: "POST", path: "/v1/memory/append", scope: "memory:append", desc: "写入一轮对话(自动抽取记忆)" },
  { method: "POST", path: "/v1/memory/recall", scope: "memory:recall", desc: "召回相关记忆" },
  { method: "GET", path: "/v1/memory", scope: "memory:read", desc: "列出记忆(分页)" },
  { method: "GET", path: "/v1/memory/{id}", scope: "memory:read", desc: "获取单条记忆详情" },
  { method: "PUT", path: "/v1/memory/{id}", scope: "memory:update", desc: "更新记忆正文" },
  { method: "DELETE", path: "/v1/memory/{id}", scope: "memory:delete", desc: "删除记忆" },
  { method: "GET", path: "/v1/memory/tasks/{id}", scope: "memory:read", desc: "查询异步任务状态" },
  { method: "GET", path: "/v1/memory/l3/status", scope: "memory:read", desc: "L3 后台写入状态" },
];

const ERROR_CODES = [
  { code: "TB-1001", http: 400, desc: "参数校验失败" },
  { code: "TB-1002", http: 401, desc: "认证失败(API key 缺失或无效)" },
  { code: "TB-1003", http: 403, desc: "权限不足(scope 不足或 tenant 不匹配)" },
  { code: "TB-1004", http: 404, desc: "资源不存在" },
  { code: "TB-1005", http: 409, desc: "冲突(死信/幂等键重复)" },
  { code: "TB-1006", http: 422, desc: "语义校验失败" },
  { code: "TB-1007", http: 429, desc: "限流" },
  { code: "TB-2001", http: 500, desc: "内部错误" },
  { code: "TB-2002", http: 502, desc: "上游依赖故障" },
  { code: "TB-2003", http: 503, desc: "服务过载" },
];

const METHOD_TONE: Record<string, string> = {
  GET: "bg-info/10 text-info",
  POST: "bg-success/10 text-success",
  PUT: "bg-warning/10 text-warning",
  DELETE: "bg-error/10 text-error",
};

const PLAN_TONE: Record<string, string> = {
  free: "bg-neutral-soft text-neutral",
  pro: "bg-info/10 text-info",
  enterprise: "bg-violet/10 text-violet",
};

function CopyButton({ text, label = "已复制" }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      onClick={() => {
        void navigator.clipboard.writeText(text).then(() => {
          setCopied(true);
          toast.success(label);
          setTimeout(() => setCopied(false), 1500);
        });
      }}
      className="inline-flex items-center gap-1.5 rounded-md border border-border bg-background px-2 py-1 font-mono text-[10px] text-foreground-emphasis hover:bg-background-muted transition-colors"
    >
      {copied ? <Check className="size-3 text-success" /> : <Copy className="size-3 text-foreground-soft" />}
      copy
    </button>
  );
}

function GenerateKeyDialog({ onClose }: { onClose: () => void }) {
  const create = useCreateApiKey();
  const [tenantId, setTenantId] = useState("");
  const [plan, setPlan] = useState("free");
  const [selectedScopes, setSelectedScopes] = useState<Set<string>>(
    new Set(["memory:append", "memory:recall", "memory:read"]),
  );
  const [generated, setGenerated] = useState<string | null>(null);

  const submit = async () => {
    if (!tenantId || selectedScopes.size === 0) return;
    const result = await create.mutateAsync({
      tenant_id: tenantId,
      scopes: Array.from(selectedScopes),
      plan,
      env: "live",
    });
    setGenerated(result.plaintext);
    toast.success("API Key 已生成,请妥善保存");
  };

  if (generated) {
    return (
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
        <div className="w-full max-w-2xl rounded-3xl border border-border bg-background p-8">
          <h2 className="mb-2 text-2xl font-semibold tracking-tight">API Key 已生成</h2>
          <p className="mb-4 text-sm text-foreground-muted">
            ⚠️ 明文只显示一次。请立即复制并妥善保存,丢失后需重新生成。
          </p>
          <div className="mb-4 rounded-xl border border-warning/40 bg-warning-soft/30 p-4">
            <code className="block break-all font-mono text-sm text-foreground-intense">
              {generated}
            </code>
          </div>
          <div className="flex items-center justify-end gap-2">
            <CopyButton text={generated} label="明文已复制" />
            <Button onClick={onClose}>我已保存</Button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
      <div className="w-full max-w-xl rounded-3xl border border-border bg-background p-8">
        <h2 className="mb-2 text-2xl font-semibold tracking-tight">生成新 API Key</h2>
        <p className="mb-6 text-sm text-foreground-muted">
          为业务系统创建接入凭证。生成后明文只显示一次。
        </p>

        <div className="space-y-4">
          <div>
            <label className="text-sm font-medium text-foreground-emphasis">租户 ID</label>
            <Input
              className="mt-1.5 font-mono"
              placeholder="tenant_001"
              value={tenantId}
              onChange={(e) => setTenantId(e.target.value)}
            />
          </div>

          <div>
            <label className="text-sm font-medium text-foreground-emphasis">计费计划</label>
            <div className="mt-1.5 flex gap-2">
              {(["free", "pro", "enterprise"] as const).map((p) => (
                <button
                  key={p}
                  type="button"
                  onClick={() => setPlan(p)}
                  className={cn(
                    "rounded-xl border px-4 py-1.5 text-sm font-medium transition-colors",
                    plan === p
                      ? "border-foreground-intense text-foreground-intense bg-background"
                      : "border-border text-foreground-muted hover:bg-background-muted",
                  )}
                >
                  <Badge variant="secondary" className="mr-1.5 font-mono text-[10px]">
                    {p}
                  </Badge>
                </button>
              ))}
            </div>
          </div>

          <div>
            <label className="text-sm font-medium text-foreground-emphasis">授权 scope</label>
            <div className="mt-1.5 space-y-1.5">
              {ALL_SCOPES.map((s) => (
                <label
                  key={s.value}
                  className="flex items-start gap-2 rounded-md p-1.5 hover:bg-background-muted"
                >
                  <input
                    type="checkbox"
                    checked={selectedScopes.has(s.value)}
                    onChange={(e) => {
                      const next = new Set(selectedScopes);
                      if (e.target.checked) next.add(s.value);
                      else next.delete(s.value);
                      setSelectedScopes(next);
                    }}
                    className="mt-1 size-3.5"
                  />
                  <div>
                    <code className="text-xs font-mono text-foreground-emphasis">{s.value}</code>
                    <p className="text-xs text-foreground-muted">{s.desc}</p>
                  </div>
                </label>
              ))}
            </div>
          </div>
        </div>

        <div className="mt-6 flex items-center justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>
            取消
          </Button>
          <Button
            onClick={submit}
            disabled={!tenantId || selectedScopes.size === 0 || create.isPending}
          >
            <Plus className="size-4" />
            {create.isPending ? "生成中..." : "生成"}
          </Button>
        </div>
      </div>
    </div>
  );
}

function KeyCard({
  apiKey,
  onRevoke,
  isRevoking,
}: {
  apiKey: {
    key_id: string;
    tenant_id: string;
    scopes: string[];
    plan: string;
    rate_limit_per_minute: number;
    rate_limit_burst: number;
    is_active: boolean;
    created_at: string;
  };
  onRevoke: () => void;
  isRevoking: boolean;
}) {
  return (
    <div
      className={cn(
        "rounded-2xl border p-4 transition-colors",
        apiKey.is_active ? "border-border bg-card" : "border-border-muted bg-background-muted/30 opacity-60",
      )}
    >
      <div className="flex items-start justify-between gap-3 mb-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <code className="font-mono text-sm font-medium text-foreground-intense">
              {apiKey.key_id}
            </code>
            <Badge variant={apiKey.is_active ? "success" : "neutral"}>
              {apiKey.is_active ? "active" : "revoked"}
            </Badge>
            <Badge variant="secondary" className={cn("text-[10px]", PLAN_TONE[apiKey.plan])}>
              {apiKey.plan}
            </Badge>
          </div>
          <p className="mt-1 text-xs text-foreground-muted">
            租户 <code className="font-mono">{apiKey.tenant_id}</code> · 限流{" "}
            {apiKey.rate_limit_per_minute}/min · 突发 {apiKey.rate_limit_burst} ·{" "}
            {formatTime(apiKey.created_at)}
          </p>
        </div>
        {apiKey.is_active ? (
          <Button
            variant="ghost"
            size="sm"
            onClick={onRevoke}
            disabled={isRevoking}
            className="text-error hover:bg-error-soft"
          >
            <Trash2 className="size-3.5" />
            撤销
          </Button>
        ) : null}
      </div>
      <div className="flex flex-wrap gap-1.5">
        {apiKey.scopes.map((s) => (
          <code
            key={s}
            className="rounded-md bg-background-muted px-1.5 py-0.5 font-mono text-[10px] text-foreground-emphasis"
          >
            {s}
          </code>
        ))}
      </div>
    </div>
  );
}

export function IntegrationPage() {
  const { data: keys, isPending, isError, refetch, dataUpdatedAt } = useApiKeys();
  const revoke = useRevokeApiKey();
  const [showGenerate, setShowGenerate] = useState(false);

  return (
    <div className="space-y-6">
      <PageHeader
        title="INTEGRATION · 接入协议"
        description="v1 公开 API · 业务系统接入 / API Key 管理"
        actions={
          <div className="flex items-center gap-3">
            <StaleIndicator updatedAt={dataUpdatedAt} prefix="last sync" />
            <Button onClick={() => setShowGenerate(true)}>
              <Plus className="size-4" />
              生成 API Key
            </Button>
          </div>
        }
      />

      {/* 协议概述 */}
      <section className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4 animate-editorial-fade-up">
        {[
          { icon: Key, title: "API Key 认证", desc: "Bearer token + Tenant 隔离" },
          { icon: Shield, title: "Scope 授权", desc: "6 个最小权限 scope" },
          { icon: Sparkles, title: "Idempotency-Key", desc: "写操作幂等保证" },
          { icon: AlertTriangle, title: "标准化错误码", desc: "TB-1xxx / TB-2xxx" },
        ].map((item) => (
          <Card key={item.title} className="border-border-muted">
            <CardContent className="flex items-start gap-3 p-4">
              <div className="flex size-9 shrink-0 items-center justify-center rounded-full bg-background-muted">
                <item.icon className="size-4 text-foreground-emphasis" aria-hidden="true" />
              </div>
              <div>
                <p className="text-sm font-medium text-foreground-intense">{item.title}</p>
                <p className="mt-0.5 text-xs text-foreground-muted">{item.desc}</p>
              </div>
            </CardContent>
          </Card>
        ))}
      </section>

      <div className="editorial-rule" />

      {/* API Key 管理 */}
      <section className="space-y-4">
        <div className="flex items-center justify-between">
          <h2 className="flex items-center gap-2 text-lg font-semibold tracking-tight">
            <Key className="size-4" /> API Keys
            <span className="text-sm font-normal text-foreground-muted">
              ({keys?.length ?? 0})
            </span>
          </h2>
        </div>

        {isError ? (
          <EmptyState
            variant="error"
            icon={AlertTriangle}
            title="无法加载 API Key 列表"
            description="请确认后端服务可达,且 admin token 正确。"
            action={{ label: "重试", onClick: () => refetch() }}
          />
        ) : isPending ? (
          <div aria-busy="true" className="space-y-2">
            {Array.from({ length: 2 }).map((_, i) => (
              <Card key={i} className="border-border-muted">
                <CardContent className="p-4 space-y-2">
                  <SkeletonLines count={2} />
                </CardContent>
              </Card>
            ))}
          </div>
        ) : keys && keys.length > 0 ? (
          <div className="space-y-3">
            {keys.map((k) => (
              <KeyCard
                key={k.key_id}
                apiKey={k}
                onRevoke={() => {
                  if (confirm(`确认撤销 API Key ${k.key_id}?此操作不可撤销。`)) {
                    revoke.mutate(k.key_id, {
                      onSuccess: () => toast.success("已撤销"),
                    });
                  }
                }}
                isRevoking={revoke.isPending && revoke.variables === k.key_id}
              />
            ))}
          </div>
        ) : (
          <EmptyState
            icon={Key}
            title="还没有 API Key"
            description="生成第一个 API Key,让业务系统开始接入 Thinkback。"
            action={{ label: "生成 API Key", onClick: () => setShowGenerate(true) }}
          />
        )}
      </section>

      <div className="editorial-rule" />

      {/* mem0 抽取提示词配置 */}
      <Mem0ConfigSection />

      <div className="editorial-rule" />

      {/* Quick Start */}
      <section className="space-y-3">
        <h2 className="flex items-center gap-2 text-lg font-semibold tracking-tight">
          <Code2 className="size-4" /> Quick Start
        </h2>
        <Card className="border-border-muted">
          <CardContent className="p-4 space-y-3">
            <p className="text-sm text-foreground-muted">
              生成 API Key 后,用以下 curl 命令测试接入:
            </p>
            <div className="rounded-xl bg-foreground-intense p-4 font-mono text-xs text-background overflow-x-auto">
              <pre className="whitespace-pre">
{`# 1. 召回记忆
curl -X POST https://your-thinkback/v1/memory/recall \\
  -H "Authorization: Bearer tbk_live_xxxxxxxx" \\
  -H "X-Tenant-Id: tenant_001" \\
  -H "Content-Type: application/json" \\
  -d '{
    "user_id": "u_8421",
    "session_id": "s_001",
    "query": "用户偏好什么编辑器?",
    "intent": "chat",
    "l3_limit": 5
  }'

# 2. 带幂等键写入
curl -X POST https://your-thinkback/v1/memory/append \\
  -H "Authorization: Bearer tbk_live_xxxxxxxx" \\
  -H "X-Tenant-Id: tenant_001" \\
  -H "Idempotency-Key: idem_001" \\
  -H "Content-Type: application/json" \\
  -d '{ "user_id": "u_8421", "messages": [...] }'`}
              </pre>
            </div>
          </CardContent>
        </Card>
      </section>

      <div className="editorial-rule" />

      {/* 端点参考 */}
      <section className="space-y-3">
        <h2 className="flex items-center gap-2 text-lg font-semibold tracking-tight">
          <BookOpen className="size-4" /> 端点参考
        </h2>
        <Card className="border-border-muted overflow-hidden p-0">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-background-subtle">
                <tr className="border-b border-border-muted">
                  <th className="px-4 py-2.5 text-left text-xs font-medium uppercase tracking-wider text-foreground-muted">方法</th>
                  <th className="px-4 py-2.5 text-left text-xs font-medium uppercase tracking-wider text-foreground-muted">路径</th>
                  <th className="px-4 py-2.5 text-left text-xs font-medium uppercase tracking-wider text-foreground-muted">所需 scope</th>
                  <th className="px-4 py-2.5 text-left text-xs font-medium uppercase tracking-wider text-foreground-muted">说明</th>
                </tr>
              </thead>
              <tbody>
                {ENDPOINTS.map((ep) => (
                  <tr key={ep.method + ep.path} className="border-b border-border-muted last:border-0">
                    <td className="px-4 py-2.5">
                      <span className={cn("rounded-md px-2 py-0.5 font-mono text-[10px] font-medium", METHOD_TONE[ep.method])}>
                        {ep.method}
                      </span>
                    </td>
                    <td className="px-4 py-2.5">
                      <code className="font-mono text-xs text-foreground-intense">{ep.path}</code>
                    </td>
                    <td className="px-4 py-2.5">
                      <code className="rounded-md bg-background-muted px-1.5 py-0.5 font-mono text-[10px] text-foreground-emphasis">
                        {ep.scope}
                      </code>
                    </td>
                    <td className="px-4 py-2.5 text-foreground-muted">{ep.desc}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      </section>

      <div className="editorial-rule" />

      {/* 错误码 */}
      <section className="space-y-3">
        <h2 className="flex items-center gap-2 text-lg font-semibold tracking-tight">
          <AlertTriangle className="size-4" /> 错误码
        </h2>
        <Card className="border-border-muted overflow-hidden p-0">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-background-subtle">
                <tr className="border-b border-border-muted">
                  <th className="px-4 py-2.5 text-left text-xs font-medium uppercase tracking-wider text-foreground-muted">错误码</th>
                  <th className="px-4 py-2.5 text-left text-xs font-medium uppercase tracking-wider text-foreground-muted">HTTP</th>
                  <th className="px-4 py-2.5 text-left text-xs font-medium uppercase tracking-wider text-foreground-muted">语义</th>
                </tr>
              </thead>
              <tbody>
                {ERROR_CODES.map((e) => (
                  <tr key={e.code} className="border-b border-border-muted last:border-0">
                    <td className="px-4 py-2.5">
                      <code className="font-mono text-xs font-medium text-foreground-intense">{e.code}</code>
                    </td>
                    <td className="px-4 py-2.5 font-mono text-xs text-foreground-muted">{e.http}</td>
                    <td className="px-4 py-2.5 text-foreground-muted">{e.desc}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      </section>

      <div className="editorial-rule" />

      {/* Webhook(未来) */}
      <section className="space-y-3">
        <h2 className="flex items-center gap-2 text-lg font-semibold tracking-tight text-foreground-muted">
          <Webhook className="size-4" /> Webhook 事件
          <Badge variant="warning" className="ml-1">规划中</Badge>
        </h2>
        <Card className="border-border-muted border-dashed">
          <CardContent className="p-4 text-sm text-foreground-muted">
            <p>
              未来将支持 <code className="font-mono">memory.extracted</code> /{" "}
              <code className="font-mono">memory.superseded</code> /{" "}
              <code className="font-mono">memory.deleted</code> /{" "}
              <code className="font-mono">task.completed</code>{" "}
              等事件投递,带 HMAC 签名校验。
            </p>
          </CardContent>
        </Card>
      </section>

      {showGenerate ? <GenerateKeyDialog onClose={() => { setShowGenerate(false); void refetch(); }} /> : null}
    </div>
  );
}
