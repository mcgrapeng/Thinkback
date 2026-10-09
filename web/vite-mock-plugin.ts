/** Vite dev-only 假数据中间件：VITE_USE_MOCK=1 时启用
 *
 * 拦截 /admin/api/* 直接返回固定数据，省掉后端 + Postgres + Milvus + LLM 依赖。
 * 仅 dev 环境生效，生产 build 完全不打包（条件 import + .ts 源文件不被引用）。
 */

import type { IncomingMessage, ServerResponse } from "node:http";

const NOW = Date.now();

/** 生成一条伪趋势:基础值 + 周期扰动 + 末端贴近 target,24 个点(过去 24h) */
function trend(base: number, target: number, variance: number, n = 24): number[] {
  const arr: number[] = [];
  for (let i = 0; i < n; i++) {
    const cycle = Math.sin((i / n) * Math.PI * 2) * variance;
    const linear = base + (target - base) * (i / (n - 1));
    const noise = (Math.random() - 0.5) * variance * 0.4;
    arr.push(Math.max(0, Math.round(linear + cycle + noise)));
  }
  return arr;
}

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
  trends: {
    active_memories_24h: trend(8000, 8247, 60, 24),
    running_tasks_24h: trend(10, 14, 6, 24),
    failed_24h: trend(4, 10, 5, 24),
    l3_queue_24h: trend(80, 47, 25, 24),
    recall_24h: trend(1500, 1840, 200, 24),
  },
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
    append_ok:  { count: 312, per_minute: 62, timeline: trend(60, 12, 4, 60) },
    append_fail: { count: 14, per_minute: 2, timeline: trend(60, 0, 1, 60) },
    recall_ok:  { count: 1840, per_minute: 368, timeline: trend(60, 30, 8, 60) },
    recall_fail: { count: 6,  per_minute: 1, timeline: trend(60, 0, 1, 60) },
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

const INTEGRATION_KEYS = [
  {
    key_id: "k8a3f1c2",
    tenant_id: "tenant_chatbot",
    scopes: ["memory:append", "memory:recall", "memory:read"],
    plan: "pro",
    rate_limit_per_minute: 600,
    rate_limit_burst: 50,
    is_active: true,
    created_at: new Date(NOW - 7 * 86_400_000).toISOString(),
  },
  {
    key_id: "e2d9b4f5",
    tenant_id: "tenant_search",
    scopes: ["memory:read"],
    plan: "free",
    rate_limit_per_minute: 60,
    rate_limit_burst: 10,
    is_active: true,
    created_at: new Date(NOW - 2 * 86_400_000).toISOString(),
  },
  {
    key_id: "c4f6a1e8",
    tenant_id: "tenant_legacy",
    scopes: ["memory:read", "memory:recall"],
    plan: "free",
    rate_limit_per_minute: 60,
    rate_limit_burst: 10,
    is_active: false,
    created_at: new Date(NOW - 30 * 86_400_000).toISOString(),
  },
];

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
  if (url.startsWith("/admin/api/integration/keys") && req.method === "GET")
    return json(res, 200, INTEGRATION_KEYS);
  if (url.startsWith("/admin/api/integration/keys") && req.method === "POST")
    return json(res, 200, {
      key: {
        key_id: Math.random().toString(16).slice(2, 10),
        tenant_id: "tenant_new",
        scopes: ["memory:append", "memory:recall", "memory:read"],
        plan: "free",
        rate_limit_per_minute: 60,
        rate_limit_burst: 10,
        is_active: true,
        created_at: new Date().toISOString(),
      },
      plaintext: `tbk_live_${Math.random().toString(16).slice(2, 10)}_${Math.random().toString(16).slice(2, 34)}`,
    });
  if (url.startsWith("/admin/api/mem0/configs"))
    return json(res, 200, [
      {
        key: "custom_instructions",
        value: `Additional requirements:\n1. Language: write each memory text in the SAME language as the conversation (Chinese conversations → Chinese memories). Never translate Chinese names, nicknames, places, or brands into English.\n2. Output shape: return exactly {"memory": [{"id": "<sequential string>", "text": "<one self-contained memory>"}]}. Every "text" MUST be a plain string; never nest arrays or objects inside it.\n3. Preserve Chinese entities verbatim (宠物名/昵称/地点/品牌逐字保留), including tone particles only when they are part of a name.\n4. Corrections write only the new value, never re-state the old one.\n5. One fact per memory. If a turn contains multiple distinct facts, emit one memory per fact.\n6. Output must be pure Chinese characters and Chinese punctuation.`,
        description: "mem0 v2 事实抽取约束(user prompt 追加段)。每次 append 时拼入。控制记忆语言、输出格式、实体保留、修正处理、粒度等。",
        is_active: true,
        updated_at: null,
      },
      {
        key: "update_memory_prompt",
        value: "Compare newly retrieved facts with the existing memory. For each new fact, decide whether to:\n- ADD: Add it to the memory as a new element\n- UPDATE: Update an existing memory element\n- DELETE: Delete an existing memory element\n- NONE: Make no change\n\nGuidelines:\n1. **Add**: If the retrieved facts contain new information not present in the memory.\n2. **Update**: If the retrieved facts contain information that is already present but the information is totally different.\n3. **Delete**: If the retrieved facts contradict existing memories.\n4. **NONE**: If the fact is already present or irrelevant.",
        description: "记忆更新/冲突处理提示词。控制 ADD/UPDATE/DELETE/NONE 四种操作的决策规则。影响记忆合并、取代、去重策略。",
        is_active: true,
        updated_at: "2026-10-08T10:00:00Z",
      },
      {
        key: "memory_answer_prompt",
        value: "You are an expert at answering questions based on the provided memories. Your task is to provide accurate and concise answers to the questions by leveraging the information given in the memories.\n\nGuidelines:\n- Extract relevant information from the memories based on the question.\n- If no relevant information is found, make sure you don't say no information is found. Instead, accept the question and provide a general response.\n- Ensure that the answers are clear, concise, and directly address the question.",
        description: "记忆召回回答提示词。控制如何基于记忆回答问题。影响回答的准确性、完整性、语气。",
        is_active: true,
        updated_at: null,
      },
    ]);
  if (url.startsWith("/admin/api/mem0/sections"))
    return json(res, 200, [
      {
        key: "custom_instructions",
        title: "抽取约束 (custom_instructions)",
        category: "extraction",
        icon: "Sparkles",
        tips: [
          "用「## Custom Instructions」段拼入 mem0 的 user prompt",
          "控制:记忆语言、输出格式(JSON schema)、实体保留规则、修正处理、粒度",
          "常见调优:加业务术语约束、调整记忆粒度(一次最多抽 N 条)、改输出格式",
          "建议:保持简洁,每次写入记忆时都拼入此 prompt,过长会增加 token 消耗",
        ],
        examples: [
          { label: "语言约束", code: "Language: write each memory text in the SAME language as the conversation (Chinese conversations → Chinese memories). Never translate Chinese names, nicknames, places, or brands into English." },
          { label: "输出格式", code: 'Output shape: return exactly {"memory": [{"id": "<sequential string>", "text": "<one self-contained memory>"}]}. Every "text" MUST be a plain string; never nest arrays or objects inside it.' },
          { label: "记忆粒度", code: 'One fact per memory. If a turn contains multiple distinct facts, emit one memory per fact. Never combine "X is Y" + "X is Z" into a single memory.' },
        ],
      },
      {
        key: "update_memory_prompt",
        title: "更新策略 (update_memory_prompt)",
        category: "update",
        icon: "RefreshCw",
        tips: [
          "控制记忆的 ADD/UPDATE/DELETE/NONE 四种操作决策",
          "影响:记忆合并策略、取代逻辑、去重规则",
          "常见调优:修改冲突解决策略(如「新值优先」vs「保留最完整值」)、调整去重阈值",
          "建议:默认策略已较通用,只有特定业务场景才需要修改",
        ],
        examples: [
          { label: "新值优先", code: "If the retrieved fact conveys the same thing as an existing memory, prefer the NEW fact (recent information is more accurate)." },
          { label: "保留最完整", code: "If the retrieved fact conveys the same thing as an existing memory, keep the fact with the MOST information (longest, most detailed)." },
        ],
      },
      {
        key: "memory_answer_prompt",
        title: "召回回答 (memory_answer_prompt)",
        category: "recall",
        icon: "Search",
        tips: [
          "控制记忆召回时的 LLM 回答行为",
          "影响:回答的准确性、完整性、语气",
          "常见调优:改回答风格(如「简洁」vs「详细」)、加引用格式、调整不确定性处理",
          "建议:根据业务场景定制,如客服场景可加「如果记忆不足请主动询问」",
        ],
        examples: [
          { label: "简洁回答", code: "Provide concise answers. Only use information from the provided memories. If the memory doesn't contain the answer, say so briefly." },
          { label: "详细回答", code: "Provide detailed, comprehensive answers. Use all relevant information from the memories. If multiple memories relate to the question, synthesize them into a coherent answer." },
        ],
      },
    ]);
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
