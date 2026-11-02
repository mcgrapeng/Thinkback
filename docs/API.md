# API Reference

Thinkback exposes three API surfaces: **Memory core** (for application code), **Health** (probes and metrics), and **Admin** (for the governance console). gRPC surface mirrors HTTP in `proto/memory.proto`; the MCP surface is the same service projected onto MCP tools — see [MCP.md](MCP.md).

## Authentication

Thinkback is deployed on an internal network and no surface is exposed publicly — the network is the trust boundary, not a token. There is no API key, no scope, no tenant header, and no bearer token, on the business surface (`/v2/memory/*`) or on the admin surface (`/admin/api/*`).

Rate limiting still applies: `/v2/memory/*` is throttled per client IP (600 requests/minute, burst 50). The v1 public integration protocol (API key + scope + tenant) was removed; see the [historical spec](specs/2026-10-08-thinkback-integration-protocol-v1.md).

The admin surface can delete every memory of a user and trigger rebuilds, so it must be kept off the public network by the deployment's network policy — the same boundary that protects the business surface.

### Optional trusted-user header (D1 caller-identity binding)

When assistants beyond your own perimeter connect (the plugin deployment shape), the network boundary stops being enough: any caller that can reach the service can read and write any `user_id`. Set `TRUSTED_USER_HEADER=X-User-Id` (a header your authenticating gateway injects) to bind caller identity on the business surface:

- every `/v2/memory/*` request must carry that header; a missing header is rejected with `403` (fail-closed — it means the gateway is misconfigured);
- the `user_id` in the request body (or query string) must exactly match the header value; a mismatch is rejected with `403` rather than silently rewritten, so "assistant thinks it writes for A while the server writes for B" surfaces immediately;
- empty setting (default) keeps the D10 behavior unchanged: `user_id` is caller-supplied and the network is the boundary.

The gRPC surface enforces the same rule on metadata (keys are lowercase in gRPC: `x-user-id`), and the MCP surface binds `user_id` at startup (`--user-id`, see [MCP.md](MCP.md)) — three surfaces, one semantic.

### Optional namespace allowlist (per-deployment tenant scoping)

`TRUSTED_USER_HEADER` binds **which user** a caller may act for. It does not bind **which tenant** the caller is in: `namespace` stays a caller-supplied label, so a caller who can reach the service can still read another namespace's memories.

Set `MEMORY_ALLOWED_NAMESPACES=acme,acme-support` (comma-separated) to pin the deployment to the namespaces it is meant to serve:

- every business-surface request whose namespace is not on the list is rejected — `403` on HTTP, `PERMISSION_DENIED` on gRPC;
- the MCP surface refuses to start on a namespace outside the list (exit code `2`), because its namespace is server-bound rather than per-request;
- a request that omits `namespace` falls back to the default scope, and the default scope is checked against the list too — "not sent" is not "not checked";
- the empty setting (default) serves every namespace, exactly as before.

This is **deployment scoping, not access control**: inside an allowed namespace, `user_id` is still caller-supplied unless `TRUSTED_USER_HEADER` is also set. The two settings are complementary — the allowlist says *which tenant this deployment is*, the trusted header says *which user the caller may act for*. Set both when one deployment serves several assistants of the same tenant.

## Memory core (`/v2/memory/*`)

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/v2/memory/events` | Write one source event; `event_id` is the idempotency anchor |
| `POST` | `/v2/memory/recall` | Retrieve relevant memories (L1/L2/L3) |
| `POST` | `/v2/memory/delete` | Scoped delete (`MEMORY` / `SESSION` / `ALL`), tombstone kept |
| `POST` | `/v2/memory/suppress` | Take memories out of recall, keep the text and the tombstone |
| `POST` | `/v2/memory/erase` | Physically erase the text (L1/L2/L3 + backend vector) |
| `GET` | `/v2/memory/items` | List long-term memories of a user (default `ACTIVE` only) |
| `GET` | `/v2/memory/items/{memory_id}` | Fetch one memory |
| `GET` | `/v2/memory/types` | Registered memory types of a namespace and their policies (TTL, classification, conflict, source roles, recall budget) |
| `POST` | `/v2/memory/update` | Edit the text of one memory |
| `GET` | `/v2/memory/tasks/{task_id}` | Async task state (retry, last_error, result) |
| `GET` | `/v2/memory/l3/background-status` | L3 background queue and worker status |

Request bodies use the same Pydantic schemas as the service (`thinkback.memory.schemas`). Unknown fields are rejected — for example `intent` no longer exists on `POST /v2/memory/recall` and sending it returns 422.

### Memory type policies — one thing to know before relying on `conflict: overwrite`

`GET /v2/memory/types` reports each type's policy, including `conflict`
(`overwrite` / `append` / `none`). **`overwrite` only takes effect for types
that ship a per-type extraction prompt** — and the prompt must make the LLM
write each fact as `<attribute name>:<value>` (e.g. `工艺节点：3nm`).

The attribute name *is* the conflict key: it is how the service knows
`工艺节点：3nm` and `工艺节点：5nm` are about the same property and one should
replace the other. A type without such a prompt falls back to nine hardcoded
Chinese consumer slots (nickname / pet name / birthday / location / favourite
drink or food / sleep reminder / work status / communication preference) —
facts outside those get **no conflict key**, so `overwrite` silently does
nothing: both the old and the new value stay ACTIVE and both reach the model.
The service logs a warning when this happens; it does not fail the write.

The eight built-in types ship without prompts, so they cover only those
consumer slots. For enterprise facts (process nodes, machines, yields, work
orders…) define your own namespace with a profile whose types carry
extraction prompts — `profiles/semiconductor.json` is a working example, and
the multi-directory `MEMORY_TYPE_PROFILE_DIR` (see the deployment docs) lets
you add one via ConfigMap without rebuilding the image.

`review_required` in the type list means the type is **unwritable** until an
approval channel exists: writes are rejected at admission rather than queued
for review.

## Health (`/health/*`)

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Process-level health (version, environment) |
| `GET` | `/health/live` | Liveness probe |
| `GET` | `/health/ready` | Readiness probe (PostgreSQL / Milvus / Mem0); 503 when a dependency is down |
| `GET` | `/health/metrics` | Prometheus text exposition |

## Admin (`/admin/api/*`)

No authentication; reachable from the internal network only.

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

`/admin/api/*` is rate limited (same bucket key as the business plane: the
calling peer, since the admin plane has no caller identity of its own).

Every memory read on this surface writes an audit row — listing memories
(`action=list`) and reading one memory's source journal turns
(`action=source`). The admin plane has no caller identity, so whoever asks
for a `user_id` is not that user: a per-user listing is a cross-user read
too. Audit rows carry ids and counts only, never memory content. If the
audit row cannot be written the request fails with 503 — the trail is the
only control left on those reads, so an untracked one is refused.

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

Every operation on the HTTP business plane has a gRPC twin with the same
semantics — including `ListMemoryTypes`, the type-discovery call. The two
surfaces read the same type registry, so a client gets the same list from
either one.

Rate limited with the same limits and the same bucket key as the HTTP
business plane (`namespace` + `user_id`; the peer address when a request
carries no user id). Switching protocol must not buy a bigger quota.
