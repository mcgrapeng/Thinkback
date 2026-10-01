/** Vite dev-only 假数据中间件：VITE_USE_MOCK=1 时启用
 *
 * 拦截 /admin/api/* 直接返回固定数据，省掉后端 + Postgres + Milvus + LLM 依赖。
 * 仅 dev 环境生效，生产 build 完全不打包（条件 import + .ts 源文件不被引用）。
 */

import type { IncomingMessage, ServerResponse } from "node:http";

const NOW = Date.now();

function json(res: ServerResponse, status: number, body: unknown) {
  res.statusCode = status;
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.end(JSON.stringify(body));
}

function makeTasks() {
  const ops = ["memory.append", "memory.recall", "memory.update", "memory.delete"] as const;
  const statuses = ["running", "completed", "pending", "failed", "dead_letter"] as const;
  return Array.from({ length: 12 }, (_, i) => ({
    task_id: `task_${i.toString().padStart(3, "0")}_${Math.random().toString(36).slice(2, 14)}`,
    request_id: `req_${i}`,
    op_type: ops[i % 4],
    status: statuses[i % 5],
    scope: { user_id: `u_${(8000 + i * 17).toString()}` },
    last_error:
      i % 5 === 3
        ? "DB connection refused at 10.0.1.42:5432"
        : i % 5 === 4
          ? "Schema migration pending: column memory.embedding_v2 missing"
          : null,
    result: i % 5 === 1 ? { memory_id: `mem_xyz_${i}` } : null,
    retry_count: i % 7,
  }));
}

const OVERVIEW = {
  memories: { ACTIVE: 8247, SUPERSEDED: 312, DELETED: 89, SUPPRESSED: 41 },
  tasks: { running: 14, pending: 23, completed: 1280, failed: 7, dead_letter: 3 },
  by_classification: { normal: 6210, personal: 1820, sensitive: 580, restricted: 79 },
  by_source_type: {
    chat_round: 5830, manual_fix: 240, session_rebuild: 112,
    import: 95, system_migration: 62,
  },
  l3: {
    write_mode: "async", executor_workers: 8, max_pending_tasks: 1000,
    pending_write_tasks: 47, cleanup_tasks: 12, available_capacity: 953,
  },
  throughput_5min: {
    append_ok:  { count: 312, per_minute: 62 },
    append_fail: { count: 14, per_minute: 2  },
    recall_ok:  { count: 1840, per_minute: 368 },
    recall_fail: { count: 6,  per_minute: 1  },
  },
  recent_failed_tasks: [
    {
      task_id: "task_a1b2c3d4e5f67890abcdef1234567890",
      request_id: "req_001", op_type: "memory.append", status: "failed",
      scope: {}, last_error: "DB connection timeout after 5s", retry_count: 3,
    },
    {
      task_id: "task_b2c3d4e5f67890abcdef12345678901",
      request_id: "req_002", op_type: "memory.append", status: "dead_letter",
      scope: {}, last_error: "Index conflict on slot preferences", retry_count: 5,
    },
  ],
  recent_audit_actions: [
    {
      audit_id: "audit_001", created_at: new Date(NOW - 60_000).toISOString(),
      operator: "admin@thinkback.io", action: "delete",
      target: "memory_id_abc123def456ghi789jkl012mno345pqr678stu901vwx234yz",
    },
    {
      audit_id: "audit_002", created_at: new Date(NOW - 600_000).toISOString(),
      operator: "admin@thinkback.io", action: "update",
      target: "memory_id_xyz987wvu654tsr321qpo098nml765kji432hgf321dcb000aaa",
    },
    {
      audit_id: "audit_003", created_at: new Date(NOW - 1_500_000).toISOString(),
      operator: "system", action: "rebuild",
      target: "session_session_001_to_010",
    },
  ],
};

const HEALTH = {
  uptime_seconds: 386412, process_started_at: Math.floor(NOW / 1000) - 386412,
  app: { name: "thinkback", version: "0.7.2", environment: "production", log_level: "INFO" },
  alembic_current: "8f3a2b1c4d5e_head",
  db_pool: { size: 20, checked_out: 8, overflow: 0, max_overflow: 10 },
  grpc: { enabled: true, host: "0.0.0.0", port: 50051, max_workers: 16 },
  flags: {
    memory_l2_llm_enabled: true, memory_decay_enabled: true,
    memory_infer_facts: false, memory_p0_slots: ["preferences", "identity"],
  },
};

const MEMORIES = {
  items: [
    {
      memory_id: "mem_001", backend_memory_id: "be_001", user_id: "u_8421",
      memory_scope_id: "scope_a", memory_status: "ACTIVE", memory_type: "preference",
      data_classification: "personal",
      memory_text: "用户偏好使用极简暗色主题的代码编辑器。",
      source_refs: [{ session_id: "sess_001", round_id: "round_001" }],
      valid_at: "2026-09-28T14:32:11", invalid_at: null,
      last_recalled_at: "2026-10-01T08:15:22", recall_count: 47, conflict_slot: null,
    },
    {
      memory_id: "mem_002", backend_memory_id: "be_002", user_id: "u_1980",
      memory_scope_id: "scope_a", memory_status: "ACTIVE", memory_type: "fact",
      data_classification: "sensitive",
      memory_text: "用户对海鲜过敏，特别是贝类。",
      source_refs: [], valid_at: "2026-09-15T10:20:33", invalid_at: null,
      last_recalled_at: "2026-09-29T16:45:11", recall_count: 12, conflict_slot: "dietary",
    },
  ],
  total: 8247, limit: 50, offset: 0,
};

const MEMORY_SOURCE = {
  memory: MEMORIES.items[0],
  rounds: [{
    round_id: "round_001", session_id: "sess_001",
    source_timestamp: "2026-09-28T14:30:11",
    messages: [
      { message_id: "msg_001", role: "user",
        content: "我平时写代码主要用 VSCode，暗色主题用 One Dark Pro。",
        timestamp: "2026-09-28T14:30:11" },
      { message_id: "msg_002", role: "assistant",
        content: "好的，已记住你的偏好。",
        timestamp: "2026-09-28T14:30:45" },
    ],
  }],
};

const AUDIT = [
  {
    audit_id: "audit_001", created_at: new Date(NOW - 60_000).toISOString(),
    operator: "admin@thinkback.io", action: "delete",
    target: "memory_id_abc123def456ghi789jkl012mno345pqr678stu901vwx234yz",
    detail: { reason: "GDPR request from user u_8421" },
  },
  {
    audit_id: "audit_002", created_at: new Date(NOW - 600_000).toISOString(),
    operator: "admin@thinkback.io", action: "update",
    target: "memory_id_xyz987wvu654tsr321qpo098nml765kji432hgf321dcb000aaa",
    detail: { reason: "Data classification changed from normal to personal" },
  },
  {
    audit_id: "audit_003", created_at: new Date(NOW - 1_500_000).toISOString(),
    operator: "system", action: "rebuild",
    target: "session_session_001_to_010",
    detail: { reason: "Scheduled weekly rebuild" },
  },
];

const CONFIG = {
  environment: "production",
  fields: [
    { name: "DATABASE_URL",          value: "postgresql://thinkback:***@10.0.1.42:5432/thinkback" },
    { name: "REDIS_URL",             value: "redis://10.0.1.43:6379/0" },
    { name: "L2_LLM_PROVIDER",       value: "anthropic" },
    { name: "L2_LLM_MODEL",          value: "claude-3-5-sonnet-20241022" },
    { name: "EMBEDDING_MODEL",       value: "text-embedding-3-small" },
    { name: "MEMORY_DECAY_DAYS",     value: 90 },
    { name: "MEMORY_L2_LLM_ENABLED", value: true },
    { name: "MEMORY_INFER_FACTS",    value: false },
    { name: "MEMORY_P0_SLOTS",       value: ["preferences", "identity"] },
  ],
};

function route(req: IncomingMessage, res: ServerResponse, next: () => void) {
  const url = req.url ?? "";
  if (!url.startsWith("/admin/api/")) return next();

  if (url.startsWith("/admin/api/overview"))      return json(res, 200, OVERVIEW);
  if (url.startsWith("/admin/api/health/detail")) return json(res, 200, HEALTH);
  if (url.startsWith("/admin/api/tasks"))         return json(res, 200, makeTasks());
  if (url.includes("/source"))
    return json(res, 200, MEMORY_SOURCE);
  if (url.startsWith("/admin/api/memories"))      return json(res, 200, MEMORIES);
  if (url.startsWith("/admin/api/audit"))         return json(res, 200, AUDIT);
  if (url.startsWith("/admin/api/config"))        return json(res, 200, CONFIG);
  if (url.includes("/admin/api/maintenance"))
    return json(res, 200, { reclaimed_count: 0, reclaimed_task_ids: [] });
  if (url.includes("/admin/api/delete")) return json(res, 200, { ok: true });
  if (url.includes("/admin/api/update")) return json(res, 200, { ok: true });
  if (url.includes("/admin/api/rebuild")) return json(res, 200, { ok: true });

  return json(res, 404, { detail: `mock: no fixture for ${url}` });
}

export function mockApi() {
  return {
    name: "mock-admin-api",
    configureServer(server: { middlewares: { use: (fn: unknown) => void } }) {
      server.middlewares.use(route);
    },
  };
}
