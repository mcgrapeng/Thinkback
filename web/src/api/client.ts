/** /admin/api 统一客户端：Bearer token（localStorage 覆盖构建期注入）+ 错误规整。 */

const TOKEN_KEY = "thinkback-admin-token";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export function getAdminToken(): string {
  return localStorage.getItem(TOKEN_KEY) ?? import.meta.env.VITE_ADMIN_TOKEN ?? "";
}

export function setAdminToken(token: string): void {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const token = getAdminToken();
  const response = await fetch(path, {
    ...init,
    headers: {
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...init?.headers,
    },
  });
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new ApiError(response.status, detail || response.statusText);
  }
  return (await response.json()) as T;
}

export interface L3BackgroundStatus {
  write_mode: string;
  executor_workers: number;
  max_pending_tasks: number;
  pending_write_tasks: number;
  cleanup_tasks: number;
  available_capacity: number;
}

export interface OverviewResponse {
  memories: Record<string, number>;
  tasks: Record<string, number>;
  by_classification: Record<string, number>;
  by_source_type: Record<string, number>;
  trends: {
    active_memories_24h: number[];
    running_tasks_24h: number[];
    failed_24h: number[];
    l3_queue_24h: number[];
    recall_24h: number[];
  };
  l3: L3BackgroundStatus;
  throughput_5min: {
    append_ok: { count: number; per_minute: number; timeline: number[] };
    append_fail: { count: number; per_minute: number; timeline: number[] };
    recall_ok: { count: number; per_minute: number; timeline: number[] };
    recall_fail: { count: number; per_minute: number; timeline: number[] };
  };
  recent_failed_tasks: Array<{
    task_id: string;
    request_id: string;
    op_type: string;
    status: string;
    scope: Record<string, unknown>;
    last_error: string;
    retry_count: number;
  }>;
  recent_audit_actions: Array<{
    audit_id: string;
    created_at: string | null;
    operator: string;
    action: string;
    target: string;
  }>;
}

export interface ReclaimResponse {
  reclaimed_count: number;
  reclaimed_task_ids: string[];
}

export interface HealthDetailResponse {
  uptime_seconds: number;
  process_started_at: number | null;
  app: { name: string; version: string; environment: string; log_level: string };
  alembic_current: string | null;
  db_pool: {
    size: number;
    checked_out: number;
    overflow: number;
    max_overflow: number | null;
  };
  grpc: {
    enabled: boolean;
    host: string;
    port: number;
    max_workers: number;
  };
  flags: {
    memory_l2_llm_enabled: boolean;
    memory_decay_enabled: boolean;
    memory_infer_facts: boolean;
    memory_p0_slots: string[];
  };
}

export interface TaskItem {
  task_id: string;
  request_id: string;
  op_type: string;
  status: string;
  scope: Record<string, unknown>;
  last_error: string | null;
  result: Record<string, unknown> | null;
  retry_count?: number;
}

export interface AdminMemoryItem {
  memory_id: string;
  backend_memory_id: string;
  user_id: string;
  memory_scope_id: string;
  memory_text: string;
  memory_status: string;
  memory_type: string | null;
  data_classification: string;
  source_refs: Array<{ session_id?: string; round_id?: string }>;
  valid_at: string | null;
  invalid_at: string | null;
  last_recalled_at: string | null;
  recall_count: number;
  conflict_slot: string | null;
}

export interface MemoryListResponse {
  items: AdminMemoryItem[];
  total: number;
  limit: number;
  offset: number;
}

export interface MemorySourceResponse {
  memory: AdminMemoryItem;
  rounds: Array<{
    round_id: string;
    session_id: string | null;
    source_timestamp: string | null;
    messages: Array<{ message_id?: string; role?: string; content?: string; timestamp?: string }>;
  }>;
}

export interface AuditItem {
  audit_id: string;
  created_at: string;
  operator: string;
  action: string;
  target: string;
  detail: Record<string, unknown>;
}

export interface ApiKeyInfo {
  key_id: string;
  tenant_id: string;
  scopes: string[];
  plan: string;
  rate_limit_per_minute: number;
  rate_limit_burst: number;
  is_active: boolean;
  created_at: string;
}

export interface ConfigResponse {
  environment: string;
  fields: Array<{ name: string; value: unknown }>;
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const token = getAdminToken();
  const response = await fetch(path, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new ApiError(response.status, detail || response.statusText);
  }
  return (await response.json()) as T;
}

function queryOf(params: Record<string, string | number | undefined>): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== "") query.set(key, String(value));
  }
  const suffix = query.toString();
  return suffix ? `?${suffix}` : "";
}

export const api = {
  overview: () => request<OverviewResponse>("/admin/api/overview"),
  healthDetail: () => request<HealthDetailResponse>("/admin/api/health/detail"),
  reclaimOrphanTasks: () =>
    post<ReclaimResponse>("/admin/api/maintenance/reclaim-orphan-tasks", {}),
  tasks: (params: { statuses?: string; limit?: number; offset?: number }) => {
    const query = new URLSearchParams();
    if (params.statuses) query.set("statuses", params.statuses);
    if (params.limit) query.set("limit", String(params.limit));
    if (params.offset) query.set("offset", String(params.offset));
    const suffix = query.toString();
    return request<TaskItem[]>(`/admin/api/tasks${suffix ? `?${suffix}` : ""}`);
  },
  memories: (params: {
    user_id?: string;
    memory_scope_id?: string;
    statuses?: string;
    limit?: number;
    offset?: number;
  }) => request<MemoryListResponse>(`/admin/api/memories${queryOf(params)}`),
  memorySource: (memoryId: string) =>
    request<MemorySourceResponse>(`/admin/api/memories/${encodeURIComponent(memoryId)}/source`),
  audit: (params: { action?: string; limit?: number; offset?: number }) =>
    request<AuditItem[]>(`/admin/api/audit${queryOf(params)}`),
  config: () => request<ConfigResponse>("/admin/api/config"),
  // 集成:API Key 管理
  listApiKeys: () => request<ApiKeyInfo[]>("/admin/api/integration/keys"),
  createApiKey: (body: {
    tenant_id: string;
    scopes: string[];
    plan?: string;
    env?: string;
  }) => request<{ key: ApiKeyInfo; plaintext: string }>("/admin/api/integration/keys", {
    method: "POST",
    body: JSON.stringify(body),
  }),
  revokeApiKey: (keyId: string) =>
    request<{ key_id: string; revoked: boolean }>(
      `/admin/api/integration/keys/${encodeURIComponent(keyId)}`,
      { method: "DELETE" },
    ),
  deleteMemory: (body: Record<string, unknown>) =>
    post<Record<string, unknown>>("/admin/api/delete", body),
  updateMemory: (body: Record<string, unknown>) =>
    post<Record<string, unknown>>("/admin/api/update", body),
  rebuildMemory: (body: Record<string, unknown>) =>
    post<Record<string, unknown>>("/admin/api/rebuild", body),
};
