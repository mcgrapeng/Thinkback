# API Reference

Thinkback exposes two API surfaces: **Memory core** (for application code) and **Admin** (for the governance console). gRPC surface mirrors HTTP in `proto/memory.proto`.

## Authentication

### Internal (`/memory/*`)

No authentication required. Used by admin dashboard / gRPC / internal services.

### Public integration (`/v1/memory/*`)

Requires `Authorization: Bearer tbk_live_xxxxxxxx` + `X-Tenant-Id: tenant_001` headers.
See [Integration Protocol v1](specs/2026-10-08-thinkback-integration-protocol-v1.md).

## Memory core (`/memory/*`)

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/memory/append` | Write a `user → assistant` round; triggers L1/L2/L3 updates |
| `POST` | `/memory/recall` | Retrieve relevant memories (L1/L2/L3) |
| `GET` | `/memory` | List memories with filters |
| `GET` | `/memory/{memory_id}` | Fetch one memory with full metadata |
| `PUT` | `/memory/{memory_id}` | Update memory text; preserved history |
| `DELETE` | `/memory/{memory_id}` | Scoped delete (`MEMORY` / `SESSION` / `ALL`) |
| `GET` | `/memory/tasks/{task_id}` | Async task state (retry, last_error, result) |
| `GET` | `/memory/l3/status` | L3 background queue and worker status |

## Public integration (`/v1/memory/*`)

| Method | Path | Required scope |
| --- | --- | --- |
| `POST` | `/v1/memory/append` | `memory:append` |
| `POST` | `/v1/memory/recall` | `memory:recall` |
| `GET` | `/v1/memory` | `memory:read` |
| `GET` | `/v1/memory/{id}` | `memory:read` |
| `PUT` | `/v1/memory/{id}` | `memory:update` |
| `DELETE` | `/v1/memory/{id}` | `memory:delete` |
| `GET` | `/v1/memory/tasks/{id}` | `memory:read` |
| `GET` | `/v1/memory/l3/status` | `memory:read` |

## Admin (`/admin/api/*`)

Requires `Authorization: Bearer <admin_token>`.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/admin/api/overview` | System pulse |
| `GET` | `/admin/api/tasks` | Task list |
| `GET` | `/admin/api/memories` | Memory list |
| `GET` | `/admin/api/audit` | Audit log |
| `POST` | `/admin/api/{delete,update,rebuild}` | Governance actions |
| `GET` | `/admin/api/config` | Runtime config |
| `GET/PUT` | `/admin/api/mem0/configs[/{key}]` | List/update mem0 prompts |
| `GET` | `/admin/api/integration/keys` | List API keys |
| `POST` | `/admin/api/integration/keys` | Generate API key |
| `DELETE` | `/admin/api/integration/keys/{id}` | Revoke API key |

## Error codes

| Code | HTTP | Meaning |
| --- | --- | --- |
| `TB-1001` | 400 | Parameter validation failed |
| `TB-1002` | 401 | Authentication failed |
| `TB-1003` | 403 | Insufficient scope / tenant mismatch |
| `TB-1004` | 404 | Resource not found |
| `TB-1005` | 409 | Conflict (dead-letter / idempotency key reuse) |
| `TB-1006` | 422 | Semantic validation failed |
| `TB-1007` | 429 | Rate limit exceeded |
| `TB-2001` | 500 | Internal error |
| `TB-2002` | 502 | Upstream dependency failure |
| `TB-2003` | 503 | Service overloaded |

## Response headers

| Header | Meaning |
| --- | --- |
| `X-Request-Id` | Unique request ID for tracing |
| `X-RateLimit-Limit` | Requests per minute allowed |
| `X-RateLimit-Remaining` | Requests remaining in current window |
| `Retry-After` | Seconds to wait (on 429) |
| `X-Idempotent-Replay` | `true` when response is a cached replay |

## gRPC

Full proto3 surface in `proto/memory.proto`. Service name: `MemoryService`.
