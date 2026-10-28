/** 集成页面：当前接入契约参考。
 *
 * 内网部署（决策 D10）：业务端点不做调用方鉴权，信任边界是网络边界
 * （k8s NetworkPolicy 默认拒绝 + 服务不对公网暴露）；管理端点用共享
 * ADMIN_TOKEN Bearer。v1 公开 API（API Key / Scope / X-Tenant-Id）已下线，
 * 本页是只读参考，没有任何写操作。
 *
 * 端点清单来源：src/thinkback/api/memory.py（prefix=/memory）、
 * src/thinkback/api/admin.py（prefix=/admin/api）、
 * src/thinkback/api/errors_standard.py（错误码）。
 */

import { useState } from "react";
import {
  AlertTriangle,
  BookOpen,
  Check,
  Code2,
  Copy,
  Hash,
  ListChecks,
  Lock,
  ShieldCheck,
  Terminal,
  Zap,
} from "lucide-react";
import { toast } from "sonner";
import { PageHeader } from "@/components/page-header";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { cn } from "@/lib/utils";

type EndpointRow = {
  method: "GET" | "POST";
  path: string;
  desc: string;
};

/** 业务面：/v2/memory/*，内网直连，无调用方鉴权。 */
const BUSINESS_ENDPOINTS: EndpointRow[] = [
  { method: "POST", path: "/v2/memory/events", desc: "写入一条原始事件（event_id 即幂等锚点），触发 L1/L2 更新与 L3 沉淀" },
  { method: "POST", path: "/v2/memory/recall", desc: "按 user / session / query 召回 L1/L2/L3 记忆，返回降级状态与原因" },
  { method: "POST", path: "/v2/memory/update", desc: "编辑单条 ACTIVE 长期记忆，并把相关 L1/L2 置脏" },
  { method: "POST", path: "/v2/memory/delete", desc: "按单条 / 会话 / 用户全局范围删除记忆" },
  { method: "POST", path: "/v2/memory/suppress", desc: "逻辑下架：不再进入召回，原文与墓碑保留以供审计" },
  { method: "POST", path: "/v2/memory/erase", desc: "物理擦除 L1 轮次原文、L2 摘要正文、L3 索引与后端向量" },
  { method: "GET", path: "/v2/memory/items", desc: "列出可编辑 / 可删除的长期记忆（query: user_id 必填）" },
  { method: "GET", path: "/v2/memory/items/{memory_id}", desc: "查询单条长期记忆详情（query: user_id 必填）" },
  { method: "GET", path: "/v2/memory/tasks/{task_id}", desc: "查询后台任务终态（异步写入的轮询入口）" },
  { method: "GET", path: "/v2/memory/l3/background-status", desc: "L3 后台写队列状态：写模式 / 线程数 / 剩余容量" },
];

/** 管理面：/admin/api/*，全部需要 Authorization: Bearer <ADMIN_TOKEN>。 */
const ADMIN_ENDPOINTS: EndpointRow[] = [
  { method: "GET", path: "/admin/api/overview", desc: "总览聚合：状态计数、分布、5min 吞吐、Top-5 失败任务、最近审计" },
  { method: "GET", path: "/admin/api/tasks", desc: "任务列表：按状态 / 年龄过滤，分页" },
  { method: "GET", path: "/admin/api/memories", desc: "记忆检索：本地索引的多用户只读视图" },
  { method: "GET", path: "/admin/api/memories/{memory_id}/source", desc: "来源反链：记忆条目 + 指向的回合原文" },
  { method: "POST", path: "/admin/api/delete", desc: "受控删除（幂等语义同业务路由）+ 审计留痕" },
  { method: "POST", path: "/admin/api/update", desc: "受控更新单条记忆 + 审计留痕（只记长度与指纹，不记正文）" },
  { method: "POST", path: "/admin/api/rebuild", desc: "触发 L2 / L3 重建 + 审计留痕" },
  { method: "GET", path: "/admin/api/audit", desc: "审计查询：created_at 倒序分页，只读不可篡改" },
  { method: "POST", path: "/admin/api/maintenance/reclaim-orphan-tasks", desc: "维护：回收僵死的 running 任务为 failed" },
  { method: "GET", path: "/admin/api/health/detail", desc: "运行时身份：uptime、alembic head、连接池、关键开关" },
  { method: "GET", path: "/admin/api/config", desc: "运行配置只读视图（敏感值脱敏，不回明文）" },
];

/** 错误码：src/thinkback/api/errors_standard.py 的 code_map + COMMON_ERROR_RESPONSES。 */
const ERROR_CODES = [
  { code: "TB-1001", http: 400, desc: "请求业务规则校验失败" },
  { code: "TB-1002", http: 401, desc: "管理面 Bearer token 校验失败" },
  { code: "TB-1003", http: 403, desc: "fail-closed 拒绝（敏感内容策略拦截）" },
  { code: "TB-1004", http: 404, desc: "资源不存在（记忆 / 记忆项 / 任务）" },
  { code: "TB-1005", http: 409, desc: "冲突：幂等键复用不同请求体、擦除后重写同一轮次" },
  { code: "TB-1006", http: 422, desc: "请求体语义校验失败（字段缺失或类型错误）" },
  { code: "TB-1007", http: 429, desc: "限流：按客户端 IP 分桶，超限带 Retry-After" },
  { code: "TB-2001", http: 500, desc: "服务内部错误" },
  { code: "TB-2002", http: 502, desc: "上游依赖故障（Milvus / Mem0）" },
  { code: "TB-2003", http: 503, desc: "服务过载、依赖未就绪，或管理面未配置 ADMIN_TOKEN" },
];

const METHOD_TONE: Record<string, string> = {
  GET: "bg-info/10 text-info",
  POST: "bg-success/10 text-success",
};

const FACTS = [
  { icon: ShieldCheck, title: "业务面免鉴权", desc: "信任边界 = 网络边界" },
  { icon: Lock, title: "管理面 Bearer", desc: "共享 ADMIN_TOKEN" },
  { icon: Hash, title: "Idempotency-Key", desc: "仅写操作 · 24h 缓存" },
  { icon: AlertTriangle, title: "错误码 TB-1xxx", desc: "标准化错误响应" },
];

const APPEND_CURL = `# 写入一轮对话（写操作，带幂等键）
curl -X POST https://thinkback.internal/v2/memory/append \\
  -H "Content-Type: application/json" \\
  -H "Idempotency-Key: idem_001" \\
  -d '{
    "request_id": "req_001",
    "user_id": "u_8421",
    "session_id": "s_001",
    "event": {
      "event_id": "e_001",
      "source": "chat_round",
      "role": "user",
      "payload": "我平时用 Neovim 写 Python。",
      "occurred_at": "2026-10-27T08:00:00Z"
    },
    "namespace": "thinkback"
  }'

# 异步模式下返回 status="accepted"，轮询任务取终态
curl -X GET https://thinkback.internal/v2/memory/tasks/task_123`;

const RECALL_CURL = `# 召回记忆（读操作：不带幂等键，不进幂等缓存）
curl -X POST https://thinkback.internal/v2/memory/recall \\
  -H "Content-Type: application/json" \\
  -d '{
    "user_id": "u_8421",
    "session_id": "s_001",
    "query": "用户偏好什么编辑器？",
    "l3_limit": 5,
    "namespace": "thinkback"
  }'`;

const ADMIN_CURL = `# 管理面：共享 Bearer token；未配置 ADMIN_TOKEN 时返回 503
curl -X GET https://thinkback.internal/admin/api/overview \\
  -H "Authorization: Bearer $ADMIN_TOKEN"`;

function CopyButton({ text, label = "已复制" }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      onClick={() => {
        void navigator.clipboard?.writeText(text)?.then(() => {
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

function EndpointTable({
  rows,
  authNote,
}: {
  rows: EndpointRow[];
  authNote: string;
}) {
  return (
    <Card className="border-border-muted overflow-hidden p-0">
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-background-subtle">
            <tr className="border-b border-border-muted">
              <th className="px-4 py-2.5 text-left text-xs font-medium uppercase tracking-wider text-foreground-muted">方法</th>
              <th className="px-4 py-2.5 text-left text-xs font-medium uppercase tracking-wider text-foreground-muted">路径</th>
              <th className="px-4 py-2.5 text-left text-xs font-medium uppercase tracking-wider text-foreground-muted">说明</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((ep) => (
              <tr key={ep.method + ep.path} className="border-b border-border-muted last:border-0">
                <td className="px-4 py-2.5">
                  <span className={cn("rounded-md px-2 py-0.5 font-mono text-[10px] font-medium", METHOD_TONE[ep.method])}>
                    {ep.method}
                  </span>
                </td>
                <td className="px-4 py-2.5">
                  <code className="font-mono text-xs text-foreground-intense">{ep.path}</code>
                </td>
                <td className="px-4 py-2.5 text-foreground-muted">{ep.desc}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="border-t border-border-muted bg-background-subtle px-4 py-2 text-xs text-foreground-muted">
        {authNote}
      </p>
    </Card>
  );
}

function CodeBlock({ title, code }: { title: string; code: string }) {
  return (
    <Card className="border-border-muted">
      <CardContent className="space-y-3 p-4">
        <div className="flex items-center justify-between gap-3">
          <p className="flex items-center gap-2 text-sm font-medium text-foreground-emphasis">
            <Terminal className="size-4" /> {title}
          </p>
          <CopyButton text={code} />
        </div>
        <div className="overflow-x-auto rounded-xl bg-foreground-intense p-4 font-mono text-xs text-background">
          <pre className="whitespace-pre">{code}</pre>
        </div>
      </CardContent>
    </Card>
  );
}

export function IntegrationPage() {
  return (
    <div className="space-y-6">
      <PageHeader
        title="INTEGRATION · 接入协议"
        description="业务面 /v2/memory/* · 管理面 /admin/api/* · 内网部署，业务端点免鉴权"
        actions={
          <div className="flex items-center gap-2">
            <Badge variant="info">业务面免鉴权</Badge>
            <Badge variant="warning">管理面需 ADMIN_TOKEN</Badge>
          </div>
        }
      />

      {/* 信任边界 —— 先说清楚「谁能调」 */}
      <section className="space-y-3">
        <h2 className="flex items-center gap-2 text-lg font-semibold tracking-tight">
          <ShieldCheck className="size-4" /> 信任边界
        </h2>
        <Card className="border-border-muted">
          <CardContent className="space-y-3 p-4 text-sm leading-relaxed text-foreground-muted">
            <p>
              业务端点 <code className="font-mono text-foreground-emphasis">/v2/memory/*</code>{" "}
              <span className="font-medium text-foreground-emphasis">不做调用方鉴权</span>
              ：没有 API Key、没有 Scope、没有{" "}
              <code className="font-mono">X-Tenant-Id</code>。服务假定调用方位于受控内网，
              <span className="font-medium text-foreground-emphasis">信任边界就是网络边界</span>
              （k8s NetworkPolicy 默认拒绝 + 服务不对公网暴露）。
            </p>
            <p>
              管理端点 <code className="font-mono text-foreground-emphasis">/admin/api/*</code>{" "}
              <span className="font-medium text-foreground-emphasis">需要共享 Bearer token</span>
              ：<code className="font-mono">Authorization: Bearer &lt;ADMIN_TOKEN&gt;</code>。
              未配置 <code className="font-mono">ADMIN_TOKEN</code> 时管理面 fail-closed，直接返回 503。
            </p>
            <p>
              v1 公开 API（<code className="font-mono">/v1/memory/*</code>、API Key、Scope、租户头）
              已下线，本页只描述当前契约，不提供任何写操作。
            </p>
          </CardContent>
        </Card>
      </section>

      {/* 协议概览（meta strip，降低视觉权重） */}
      <section className="animate-editorial-fade-up">
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 text-xs">
          {FACTS.map((item) => (
            <div
              key={item.title}
              className="flex items-center gap-2 rounded-lg border border-border-muted bg-background-subtle/40 px-3 py-2"
            >
              <item.icon className="size-3.5 shrink-0 text-foreground-emphasis" aria-hidden="true" />
              <div className="min-w-0">
                <p className="truncate text-xs font-medium text-foreground-intense">{item.title}</p>
                <p className="truncate text-[10px] text-foreground-muted">{item.desc}</p>
              </div>
            </div>
          ))}
        </div>
      </section>

      <div className="editorial-rule" />

      {/* 业务端点 */}
      <section className="space-y-3">
        <h2 className="flex items-center gap-2 text-lg font-semibold tracking-tight">
          <BookOpen className="size-4" /> 业务端点
          <span className="text-sm font-normal text-foreground-muted">
            ({BUSINESS_ENDPOINTS.length})
          </span>
        </h2>
        <EndpointTable
          rows={BUSINESS_ENDPOINTS}
          authNote="鉴权：无。业务面不做调用方鉴权，靠内网可达性约束调用方。"
        />
      </section>

      <div className="editorial-rule" />

      {/* 管理端点 */}
      <section className="space-y-3">
        <h2 className="flex items-center gap-2 text-lg font-semibold tracking-tight">
          <ListChecks className="size-4" /> 管理端点
          <span className="text-sm font-normal text-foreground-muted">
            ({ADMIN_ENDPOINTS.length})
          </span>
        </h2>
        <EndpointTable
          rows={ADMIN_ENDPOINTS}
          authNote="鉴权：Authorization: Bearer <ADMIN_TOKEN>（共享 token，未配置时 503 fail-closed）。"
        />
      </section>

      <div className="editorial-rule" />

      {/* 写入语义 */}
      <section className="space-y-3">
        <h2 className="flex items-center gap-2 text-lg font-semibold tracking-tight">
          <Zap className="size-4" /> 写入语义与幂等
        </h2>
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
          <Card className="border-border-muted">
            <CardContent className="p-4">
              <p className="section-label mb-2">异步写入（默认）</p>
              <p className="text-sm leading-relaxed text-foreground-muted">
                <code className="font-mono text-foreground-emphasis">POST /v2/memory/events</code>{" "}
                返回 <code className="font-mono text-foreground-emphasis">status="accepted"</code>
                ，L3 抽取在后台执行。轮询{" "}
                <code className="font-mono text-foreground-emphasis">
                  {"GET /v2/memory/tasks/{task_id}"}
                </code>{" "}
                取终态：completed / failed / dead_letter。
              </p>
            </CardContent>
          </Card>
          <Card className="border-border-muted">
            <CardContent className="p-4">
              <p className="section-label mb-2">同步写入（memory_l3_write_mode=sync）</p>
              <p className="text-sm leading-relaxed text-foreground-muted">
                返回 <code className="font-mono text-foreground-emphasis">status="completed"</code>
                ，L3 已落盘，无需再轮询任务。其余状态（already_done / running / failed）按响应体
                <code className="font-mono">status</code> 字段判断。
              </p>
            </CardContent>
          </Card>
          <Card className="border-border-muted">
            <CardContent className="p-4">
              <p className="section-label mb-2">Idempotency-Key（仅写操作）</p>
              <p className="text-sm leading-relaxed text-foreground-muted">
                只对 <code className="font-mono text-foreground-emphasis">/v2/memory/*</code> 的写操作生效，
                缓存 24h：同 key 同 body 回放上次响应（带{" "}
                <code className="font-mono">X-Idempotent-Replay</code>），同 key 不同 body → 409。
                <code className="font-mono text-foreground-emphasis">/v2/memory/recall</code>{" "}
                是读路径，不进缓存，也不接受幂等键。
              </p>
            </CardContent>
          </Card>
          <Card className="border-border-muted">
            <CardContent className="p-4">
              <p className="section-label mb-2">擦除后不可重写</p>
              <p className="text-sm leading-relaxed text-foreground-muted">
                某轮被 <code className="font-mono text-foreground-emphasis">erase</code>{" "}
                物理擦除后，重放同一 <code className="font-mono">round_id</code> 会返回 409
                （TB-1005，round_id conflict）——调用方必须换一个新的{" "}
                <code className="font-mono">round_id</code>。
              </p>
            </CardContent>
          </Card>
          <Card className="border-border-muted">
            <CardContent className="p-4">
              <p className="section-label mb-2">限流（429）</p>
              <p className="text-sm leading-relaxed text-foreground-muted">
                业务面按客户端 IP 分桶：600 请求/分钟、突发 50。超限返回 429（TB-1007）并带{" "}
                <code className="font-mono">Retry-After</code> 头，调用方应退避重试。
              </p>
            </CardContent>
          </Card>
          <Card className="border-border-muted">
            <CardContent className="p-4">
              <p className="section-label mb-2">业务级幂等键 operation_id</p>
              <p className="text-sm leading-relaxed text-foreground-muted">
                删除 / 下架 / 擦除 / 编辑 / 重建在请求体里带{" "}
                <code className="font-mono text-foreground-emphasis">operation_id</code>
                ，同一 operation_id 的重复提交会合并到同一个后台任务，与 Idempotency-Key 是两套机制。
              </p>
            </CardContent>
          </Card>
        </div>
      </section>

      <div className="editorial-rule" />

      {/* Quick Start */}
      <section className="space-y-3">
        <h2 className="flex items-center gap-2 text-lg font-semibold tracking-tight">
          <Code2 className="size-4" /> Quick Start
        </h2>
        <div className="space-y-3">
          <CodeBlock title="写入一条事件" code={APPEND_CURL} />
          <CodeBlock title="召回相关记忆" code={RECALL_CURL} />
          <CodeBlock title="调用管理端点" code={ADMIN_CURL} />
        </div>
        <p className="text-xs text-foreground-muted">
          必填字段：request_id、user_id、session_id、event[event_id, source, role, payload,
          occurred_at]；namespace、memory_type 与 metadata 可选。event_id 即幂等锚点，必须全局唯一。完整 schema 见{" "}
          <code className="font-mono">src/thinkback/memory/schemas.py</code>，接口文档见{" "}
          <code className="font-mono">/docs</code>。
        </p>
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
    </div>
  );
}
