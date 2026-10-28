# API Reference

Thinkback exposes three API surfaces: **Memory core** (for application code), **Health** (probes and metrics), and **Admin** (for the governance console). gRPC surface mirrors HTTP in `proto/memory.proto`.

## Authentication

### Business surface (`/v2/memory/*`)

No authentication. Thinkback is deployed on an internal network and the business surface is not exposed publicly — the network is the trust boundary, not a token. There is no API key, no scope, and no tenant header.

Rate limiting still applies: `/v2/memory/*` is throttled per client IP (600 requests/minute, burst 50). The v1 public integration protocol (API key + scope + tenant) was removed; see the [historical spec](specs/2026-10-08-thinkback-integration-protocol-v1.md).

### Admin (`/admin/api/*`)

Requires `Authorization: Bearer <ADMIN_TOKEN>`. Not configured means 503 (fail-closed), never open.

## Memory core (`/v2/memory/*`)

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/v2/memory/events` | Write a `user → assistant` round; triggers L1/L2/L3 updates |
| `POST` | `/v2/memory/recall` | Retrieve relevant memories (L1/L2/L3) |
| `POST` | `/v2/memory/delete` | Scoped delete (`MEMORY` / `SESSION` / `ALL`), tombstone kept |
| `POST` | `/v2/memory/suppress` | Take memories out of recall, keep the text and the tombstone |
| `POST` | `/v2/memory/erase` | Physically erase the text (L1/L2/L3 + backend vector) |
| `GET` | `/v2/memory/items` | List long-term memories of a user (default `ACTIVE` only) |
| `GET` | `/v2/memory/items/{memory_id}` | Fetch one memory |
| `POST` | `/v2/memory/update` | Edit the text of one memory |
| `GET` | `/v2/memory/tasks/{task_id}` | Async task state (retry, last_error, result) |
| `GET` | `/v2/memory/l3/background-status` | L3 background queue and worker status |

Request bodies use the same Pydantic schemas as the service (`thinkback.memory.schemas`). Unknown fields are rejected — for example `intent` no longer exists on `POST /v2/memory/recall` and sending it returns 422.

## Health (`/health/*`)

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Process-level health (version, environment) |
| `GET` | `/health/live` | Liveness probe |
| `GET` | `/health/ready` | Readiness probe (PostgreSQL / Milvus / Mem0); 503 when a dependency is down |
| `GET` | `/health/metrics` | Prometheus text exposition |

## Admin (`/admin/api/*`)

Requires `Authorization: Bearer <ADMIN_TOKEN>`.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/admin/api/overview` | System pulse |
| `GET` | `/admin/api/tasks` | Task list |
| `GET` | `/admin/api/memories` | Memory list |
| `GET` | `/admin/api/memories/{memory_id}/source` | Source back-link of a memory |
| `GET` | `/admin/api/audit` | Audit log |
| `POST` | `/admin/api/{delete,update,rebuild}` | Governance actions |
| `GET` | `/admin/api/health/detail` | Dependency detail |
| `GET` | `/admin/api/config` | Runtime config |
| `POST` | `/admin/api/maintenance/reclaim-orphan-tasks` | Reclaim orphaned background tasks |

API key management and live mem0 prompt editing were removed with the v1 public integration surface; extraction behaviour is configured by code (`THINKBACK_CUSTOM_INSTRUCTIONS`) and by the type profile in `profiles/`.

## Error codes

| Code | HTTP | Meaning |
| --- | --- | --- |
| `TB-1001` | 400 | Parameter validation failed |
| `TB-1002` | 401 | Authentication failed (admin token) |
| `TB-1003` | 403 | Insufficient scope / fail-closed refusal |
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
| `X-Trace-Id` | Trace ID; echoes the `X-Trace-Id` / `X-Request-Id` request header, generated when absent |
| `X-RateLimit-Limit` | Requests per minute allowed for the client IP |
| `X-RateLimit-Remaining` | Requests remaining in current window |
| `X-RateLimit-Reset` | Unix timestamp when the current window resets |
| `Retry-After` | Seconds to wait (on 429) |
| `X-Idempotent-Replay` | `true` when response is a cached replay of the same `Idempotency-Key` |

`Idempotency-Key` is honoured on the write paths of `/v2/memory/*` (`POST` / `PUT` / `DELETE`); `POST /v2/memory/recall` is never replayed from cache.

## gRPC

Full proto3 surface in `proto/memory.proto`. Service name: `MemoryService`.
