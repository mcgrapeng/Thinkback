# Thinkback 接入协议 v1 — 业务系统集成规范

**日期**: 2026-10-08
**状态**: Draft
**目标**: 为业务系统(客服/助手/推荐/搜索等)提供统一、安全、可演进的记忆服务接入协议

---

## 1. 设计原则

| 原则 | 说明 |
|------|------|
| **版本化优先** | 所有端点 `/v1/` 前缀,破坏性变更走 `/v2/`,兼容期 ≥6 个月 |
| **幂等安全** | 所有写操作支持 `Idempotency-Key`,重试不重复执行 |
| **最小权限** | API key 按 scope 授权(append/recall/delete),默认最小集 |
| **可观测** | 每请求返回 `X-Request-Id`,支持按 request_id 追踪全链路 |
| **错误语义化** | 标准化错误码 `TB-XXXX`,不暴露内部实现 |
| **向后兼容** | 新增字段可选,不删字段不改语义;枚举只增不减 |

---

## 2. 认证与授权

### 2.1 API Key 认证

```http
POST /v1/memory/append HTTP/1.1
Authorization: Bearer tbk_live_<key_id>_<secret>
X-Tenant-Id: tenant_001
Content-Type: application/json
```

- **API Key 格式**: `tbk_live_<key_id>_<secret>` 或 `tbk_test_...`
- **Tenant ID**: 每个业务系统一个租户,数据隔离
- **传输**: HTTPS 强制(生产),HTTP 仅限本地开发

### 2.2 Scope 授权

| Scope | 权限 | 默认 |
|-------|------|------|
| `memory:append` | 写入记忆(对话轮次) | ✅ |
| `memory:recall` | 召回记忆 | ✅ |
| `memory:read` | 读取/列出记忆 | ✅ |
| `memory:update` | 更新记忆 | ❌ |
| `memory:delete` | 删除记忆 | ❌ |
| `memory:admin` | 治理操作(重建等) | ❌ |

### 2.3 限流

| 计划 | 请求/分钟 | 突发 | Token 限制 |
|------|----------|------|-----------|
| Free | 60 | 10 | 100K tokens/min |
| Pro | 600 | 50 | 1M tokens/min |
| Enterprise | 自定义 | 自定义 | 自定义 |

限流响应头:
```http
X-RateLimit-Limit: 60
X-RateLimit-Remaining: 45
X-RateLimit-Reset: 1696800000
Retry-After: 5
```

---

## 3. 核心端点

### 3.1 记忆写入 — `POST /v1/memory/append`

```json
// Request
{
  "request_id": "req_abc123",
  "user_id": "u_8421",
  "session_id": "sess_001",
  "round_id": "round_001",
  "messages": [
    {"role": "user", "content": "我平时用 VSCode", "timestamp": "2026-10-08T10:00:00Z"},
    {"role": "assistant", "content": "好的,已记住", "timestamp": "2026-10-08T10:00:05Z"}
  ],
  "source_timestamp": "2026-10-08T10:00:00Z",
  "metadata": {"channel": "web", "lang": "zh"}
}

// Response 202 Accepted
{
  "status": "accepted",
  "task_id": "task_001",
  "round_id": "round_001",
  "l3_events": ["memory.extracted:preferences"]
}
```

### 3.2 记忆召回 — `POST /v1/memory/recall`

```json
// Request
{
  "user_id": "u_8421",
  "session_id": "sess_001",
  "query": "用户偏好什么编辑器?",
  "intent": "chat",
  "l3_limit": 5,
  "l3_score_threshold": 0.7,
  "token_budget": 2000
}

// Response 200
{
  "status": "ok",
  "degraded": false,
  "degradation_reasons": [],
  "items": [
    {
      "layer": "L1",
      "content": "用户偏好使用 VSCode",
      "source": "round_001",
      "memory_id": "mem_001",
      "score": 0.92,
      "metadata": {"slot": "editor_preference"}
    }
  ]
}
```

### 3.3 记忆列表 — `GET /v1/memory`

```http
GET /v1/memory?user_id=u_8421&statuses=ACTIVE&page=1&limit=50
Authorization: Bearer tbk_live_...
```

```json
// Response 200
{
  "items": [...],
  "total": 8247,
  "limit": 50,
  "offset": 0
}
```

### 3.4 记忆详情 — `GET /v1/memory/{memory_id}`

### 3.5 记忆更新 — `PUT /v1/memory/{memory_id}`

### 3.6 记忆删除 — `DELETE /v1/memory/{memory_id}`

### 3.7 任务状态 — `GET /v1/memory/tasks/{task_id}`

### 3.8 L3 状态 — `GET /v1/memory/l3/status`

---

## 4. 错误模型

### 4.1 标准错误响应

```json
{
  "error": {
    "code": "TB-1001",
    "message": "Invalid request: user_id is required",
    "request_id": "req_abc123",
    "details": {"field": "user_id"}
  }
}
```

### 4.2 错误码表

| 错误码 | HTTP | 语义 | 客户端处理 |
|--------|------|------|-----------|
| `TB-1001` | 400 | 参数校验失败 | 修复请求 |
| `TB-1002` | 401 | 认证失败 | 检查 API key |
| `TB-1003` | 403 | 权限不足 | 申请 scope |
| `TB-1004` | 404 | 资源不存在 | — |
| `TB-1005` | 409 | 冲突(死信/幂等键重复) | 可重试 |
| `TB-1006` | 422 | 语义校验失败 | 修复业务逻辑 |
| `TB-1007` | 429 | 限流 | 退避重试 |
| `TB-2001` | 500 | 内部错误 | 重试 + 报告 |
| `TB-2002` | 502 | 上游依赖故障 | 退避重试 |
| `TB-2003` | 503 | 服务过载 | 退避重试 |

---

## 5. 幂等保证

### 5.1 `Idempotency-Key` 头

```http
POST /v1/memory/append HTTP/1.1
Idempotency-Key: idem_xyz789
```

- 同 key + 同请求体 → 返回缓存响应(不重复执行)
- 同 key + 不同请求体 → 409 `TB-1005`
- 缓存 TTL 24h

### 5.2 `request_id` 字段(已有)

业务层幂等键,用于追踪和审计。`Idempotency-Key` 是传输层幂等,两者互补。

---

## 6. 事件 Webhook(未来)

```json
// POST <callback_url>
{
  "event": "memory.extracted",
  "timestamp": "2026-10-08T10:00:05Z",
  "data": {
    "memory_id": "mem_001",
    "user_id": "u_8421",
    "type": "preference"
  },
  "signature": "sha256=<hmac>"
}
```

事件类型:
- `memory.extracted` — 新记忆抽取
- `memory.superseded` — 记忆被取代
- `memory.deleted` — 记忆删除
- `task.completed` — 异步任务完成
- `task.failed` — 异步任务失败

---

## 7. SDK 设计规范

```python
# Python SDK 示例
from thinkback import Client

client = Client(api_key="tbk_live_...", tenant_id="tenant_001")

# 写入
client.memory.append(
    user_id="u_8421",
    session_id="sess_001",
    messages=[{"role": "user", "content": "..."}],
)

# 召回
results = client.memory.recall(user_id="u_8421", query="...")

# 管理
client.memory.list(user_id="u_8421", statuses=["ACTIVE"])
```

```typescript
// TypeScript SDK 示例
import { Thinkback } from "@thinkback/sdk";

const client = new Thinkback({ apiKey: "tbk_live_...", tenantId: "tenant_001" });

await client.memory.append({ userId: "u_8421", messages: [...] });
const results = await client.memory.recall({ userId: "u_8421", query: "..." });
```

---

## 8. 兼容性承诺

- `/v1/` 端点:新增可选字段,不删不改语义
- 枚举:只增不减
- 响应:新增字段可选,客户端应忽略未知字段
- 破坏性变更:走 `/v2/`,提供 6 个月并行期

---

## 9. 实施计划

| 阶段 | 内容 | 状态 |
|------|------|------|
| P0 | API key 认证 + tenant 隔离(`api/auth.py`) | ✅ done |
| P0 | 标准化错误码 + 版本化路由(`api/errors_standard.py`) | ✅ done |
| P0 | 限流中间件 Token Bucket(`api/ratelimit.py`) | ✅ done |
| P0 | 幂等保证 Idempotency-Key(`api/idempotency.py`) | ✅ done |
| P0 | `/v1/memory/*` 路由 + 认证接入(`api/v1_memory.py`) | ✅ done |
| P0 | v1 集成测试(`tests/unit/test_v1_integration.py`) | ✅ done (8 tests) |
| P1 | Python SDK(`thinkback-py` package) | TODO |
| P2 | Webhook 事件通知 | TODO |
| P2 | TypeScript SDK | TODO |

## 10. 路由分层

| 路径 | 鉴权 | 限流 | 幂等 | 用途 |
|------|------|------|------|------|
| `/health` `/docs` `/openapi.json` | ❌ | ❌ | ❌ | 健康/文档 |
| `/memory/*` | ❌ (内部服务) | ❌ | ❌ | admin web / gRPC / 内部服务间调用 |
| `/admin/api/*` | ✅ Admin Token | ❌ | ❌ | 治理台 console |
| `/v1/memory/*` | ✅ API Key + Scope + Tenant | ✅ Token Bucket | ✅ Idempotency-Key | 业务系统接入 |

