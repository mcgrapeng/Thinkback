# Memory OSS Landscape — mem0 / Zep / Letta / cognee

Date: 2026-11-01. Benchmark target: **Thinkback** (Python 3.12 FastAPI + gRPC memory service wrapping mem0).

All claims below cite primary sources: upstream GitHub source at a pinned ref, official docs, or the installed
`mem0ai==2.2.1` package source (read from `.venv/lib/python3.12/site-packages/mem0/`, which matches
`mem0ai/mem0@main` for `mem0/configs/base.py` — verified byte-identical against
`https://raw.githubusercontent.com/mem0ai/mem0/main/mem0/configs/base.py`).

> **Headline finding.** mem0's v3 algorithm is **ADD-only**: `add()` never returns UPDATE or DELETE, memories
> accumulate, and contradiction resolution moved to ranking (Platform "Dream") — which is **not in OSS**.
> Source: `mem0/memory/main.py` (installed 2.2.1) L942-L1000 `_add_to_vector_store` phase 2 ("Single-pass
> ADD-only"); https://docs.mem0.ai/migration/oss-v2-to-v3 ("`add()` events | Returns `ADD`, `UPDATE`, `DELETE` |
> Returns `ADD` only"). Open issue https://github.com/mem0ai/mem0/issues/5867 ("ADD-only memory extraction can
> create conflicting memories") and https://github.com/mem0ai/mem0/issues/4956 ("ADD-only extraction in v3 may
> surface stale/contradictory facts for time-sensitive attributes") confirm this is known, not fixed.

---

## 1. mem0 (mem0ai/mem0) — the engine Thinkback wraps

### 1.1 Integration surface

Three surfaces exist. The one Thinkback uses is the in-process Python library.

| Surface | Entry point | Auth | Notes |
|---|---|---|---|
| Library (in-process) | `from mem0 import Memory` / `AsyncMemory` | none (you own the process) | What Thinkback's `Mem0LibraryMemoryBackend` calls (`src/thinkback/memory/backends/mem0_library.py`) |
| Self-hosted REST server | `server/main.py` FastAPI, `POST /memories`, `POST /search`, `GET /memories/{id}/history` | JWT / `X-API-Key` / `ADMIN_API_KEY` | https://docs.mem0.ai/open-source/features/rest-api |
| Managed Platform | `from mem0 import MemoryClient` (REST `/v1/`) | API key | https://docs.mem0.ai/llms.txt |
| MCP | `https://mcp.mem0.ai/mcp` | API key | 11 tools; Platform only — https://docs.mem0.ai/platform/features/mem0-mcp |

**Minimal library integration** (from `mem0/memory/main.py` L760-L773, L1269-L1284, L1393-L1405):

```python
from mem0 import Memory

m = Memory()  # or Memory.from_config({...})

# (a) write a memory
m.add(
    [{"role": "user", "content": "I prefer dark mode"}],
    user_id="alice",
    metadata={"category": "preferences"},
    expiration_date="2030-01-31",  # YYYY-MM-DD only
    infer=True,
)  # False => store raw, no LLM

# (b) retrieve context for a prompt
res = m.search(
    "what does alice like?",
    filters={"user_id": "alice"},  # entity IDs MUST be inside filters
    top_k=20,
    threshold=0.1,
    rerank=False,
    explain=False,
)
memories_str = "\n".join(f"- {r['memory']}" for r in res["results"])
```

**REST equivalent** (`server/main.py`): `POST /memories` with `{messages, user_id, metadata, expiration_date,
infer, memory_type, prompt}`; `POST /search` with `{query, filters, top_k, threshold, explain, show_expired}`.

### 1.2 Memory model

A memory is a **flat vector-store record**, not a graph node.

| Field | Source | Notes |
|---|---|---|
| `id` | `uuid4()` string, `main.py` L1981 | |
| `memory` / `data` | payload `data` | the extracted fact text |
| `hash` | `md5(data)` L1984 | dedup key |
| `created_at` / `updated_at` | ISO-8601 UTC L1986-1987 | |
| `text_lemmatized` | `lemmatize_for_bm25(data)` L1988 | internal BM25 index field, stripped on read |
| `user_id` / `agent_id` / `run_id` | identity scope | set from **entity params only** |
| `actor_id` / `role` / `attributed_to` | speaker attribution | promoted to top level on read |
| `expiration_date` | `YYYY-MM-DD` | see §1.5 |
| arbitrary `metadata` | caller dict | freeform; identity keys are stripped |

`MemoryItem` (the public read model) is: `id, memory, hash, metadata, score, created_at, updated_at`
— `mem0/configs/base.py` L16-26.

**Scoping semantics.** Exactly one of `user_id` / `agent_id` / `run_id` is *required*:
`_build_filters_and_metadata` raises `Mem0ValidationError(VALIDATION_001)` if none is given (L391-397).
Multiple may be given (AND-ed at the vector store). Crucially, identity is set **from the entity params only** —
`_strip_identity_keys` (L143-162) drops `user_id`/`agent_id`/`run_id` from caller `metadata`, so freeform
metadata cannot plant a memory into a scope the caller did not request. On `update()`, identity keys are
immutable (L2074, docstring L1843-1845).

**Graph memory is NOT in OSS.** v3 removed `enable_graph` and `graph_store` entirely ("~4000 lines" of external
graph drivers deleted) — https://docs.mem0.ai/migration/oss-v2-to-v3. OSS instead extracts entities into a
**parallel collection** `{collection}_entities` and uses them only to *boost ranking* at search time
(`main.py` L559-580 `entity_store`, L1747+ `_compute_entity_boosts`). There is no queryable graph and the
`relations` field is no longer populated. Full graph memory is Platform-only:
https://docs.mem0.ai/platform/features/graph-memory.

### 1.3 Lifecycle

| Operation | Behaviour | Source |
|---|---|---|
| **Create** | `add()` → LLM extracts facts in ONE call → dedup by hash → batch embed → insert. `infer=False` stores raw message content verbatim. | `main.py` L881-L1210 |
| **Update** | `update(memory_id, text, metadata, expiration_date)` — full overwrite of one record; writes an `UPDATE` row to the SQLite history table. **No automatic contradiction handling** (v3 is ADD-only). | L1829-1881, L2052-2112 |
| **Contradiction** | Not resolved in OSS. Old + new facts coexist; ranking is expected to surface the current one. Platform "Dream" (supersede/merge/synthesize) does this — Platform only. | https://docs.mem0.ai/platform/platform-vs-oss |
| **Dedup** | MD5 hash of the text + entity-store exact/semantic match (threshold ≥ 0.95). | L1984, L605-650 |
| **Decay / expiry** | `expiration_date` (date granularity, inclusive of the date, UTC). Hides from `search()`/`get_all()`; `get(id)` still returns it; `show_expired=True` includes it. **Nothing is deleted.** | L427-L459, https://docs.mem0.ai/platform/features/memory-expiration |
| **Delete (one)** | `delete(memory_id)` — **hard delete** from the vector store; writes a `DELETE` row with `is_deleted=1` and the previous text into the SQLite history table. | L1883-1902, L2114-2142 |
| **Delete (bulk)** | `delete_all(user_id=..., agent_id=..., run_id=...)` — batches of 100 with a repeated-batch guard. **Filters are limited to the three identity keys**; there is no arbitrary-metadata bulk delete. | L1904-1958 |
| **History / version** | `history(memory_id)` → rows from a local SQLite `history` table: `id, memory_id, old_memory, new_memory, event, created_at, updated_at, is_deleted, actor_id, role`. Per-memory only. **No project-wide event log in OSS** (Platform has `GET /v1/events/`). | `mem0/memory/storage.py` L227-L252; https://docs.mem0.ai/platform/platform-vs-oss |

**Known OSS erasure gap.** `delete_all()` clears memories and entity records but leaves the scope's raw
messages in the history DB `messages` table; the next `add()` re-sends them to the LLM where they can be
re-extracted into new memories. Open issue https://github.com/mem0ai/mem0/issues/7452 (reproduced with a
stubbed LLM). This is a live erasure-compliance bug in the engine Thinkback wraps.

Also open: https://github.com/mem0ai/mem0/issues/7440 (`Memory.close()` races in-flight writes → silent
audit-history loss), https://github.com/mem0ai/mem0/issues/7502 (`history()` returns wrong audit order when
`created_at` carries different UTC offsets), https://github.com/mem0ai/mem0/issues/6512 (`disable_history`
config absent from the Python SDK — parity gap with TS), https://github.com/mem0ai/mem0/issues/7782 (proposal:
typed, filterable source-kind provenance on memory records — i.e. **no source provenance today**), and
https://github.com/mem0ai/mem0/issues/7047 (link extracted memories to the source messages they came from —
also open, so no write-path lineage).

### 1.4 Retrieval

`search(query, *, top_k=20, filters, threshold=0.1, rerank=False, explain=False, reference_date=None,
show_expired=False)` — `main.py` L1393-1536.

Three signals fused in `_search_vector_store` (L1642-1745) and `mem0/utils/scoring.py`:

1. **Semantic** — vector similarity. This is the only *recall expander*; BM25/entity only re-rank.
2. **BM25 keyword** — `vector_store.keyword_search()`, normalized via `normalize_bm25`. Requires the store to
   implement `keyword_search`; Qdrant needs `fastembed` for sparse vectors.
3. **Entity boost** — query entities matched against `{collection}_entities`, boosting linked memories.

`rerank=True` applies a configured reranker (LLM/Cohere/ST/HF/ZeroEntropy) — L1514-1519.
`explain=True` returns `score_details` per result: `{semantic_score, bm25_score, entity_boost, raw_score,
max_possible_score, final_score, threshold}` — `mem0/utils/scoring.py` L126-135. **This is signal-level
explainability, not "why this memory is relevant to this user"** — it does not explain attribution, recency
relative to other candidates, or policy decisions.

**Platform-only retrieval** (all raise in OSS): `decay` ("The decay parameter is not supported by the OSS
Memory SDK."), `timestamp`, `reference_date` — `mem0/memory/notices.py` L130-134.

**Filter operator support varies by store** (documented matrix) — https://docs.mem0.ai/open-source/features/metadata-filtering:
Qdrant full; pgvector full but `AND/OR/NOT` only translated at the `search()` layer, **not** `get_all()`
(pgvector silently matches nothing for logical wrappers on `get_all`); Chroma `contains` silently falls back
to equality; Pinecone has no `contains` and no logical ops; **Weaviate silently drops every metadata key and
filters only on `user_id`/`agent_id`/`run_id`**. Wildcard `*` is a no-op on Qdrant/Chroma, a real
"field exists" check on pgvector, and the literal string `"*"` on Pinecone.

### 1.5 Extraction

The **engine's LLM decides** what becomes a memory. The caller supplies messages, not facts.
One LLM call extracts facts against the top-10 existing memories for the scope (`main.py` L925-L955).
Caller control is limited to:

- `custom_instructions` (config-level) and `prompt=` (per-`add()` call) — free-text extraction guidance.
  `main.py` L948: `custom_instr = prompt or self.custom_instructions`.
- `infer=False` — bypass the LLM, store raw message content as one memory per message.
- `metadata` — freeform labels attached to the *result*, but they do not drive extraction.

**There is no per-memory-type schema.** `memory_type` accepts exactly one value, `"procedural_memory"`, and
anything else raises `Mem0ValidationError(VALIDATION_002)` — `main.py` L831-837. The procedural path is
marked for removal ("TODO: Remove procedural-memory support in a future breaking release", L853-854).
Custom *categories* (a 15-label taxonomy: `personal_details`, `family`, `professional_details`, …) are
**Platform-only** — https://docs.mem0.ai/platform/features/custom-categories; OSS `Memory.add()` has no
`custom_categories` parameter and OSS project updates are rejected.

### 1.6 Config surface

`MemoryConfig` (OSS) is exactly six fields — `mem0/configs/base.py` L29-57:

```python
class MemoryConfig(BaseModel):
    vector_store: VectorStoreConfig
    llm: LlmConfig
    embedder: EmbedderConfig
    history_db_path: str = os.path.join(mem0_dir, "history.db")
    reranker: Optional[RerankerConfig] = None
    version: str = "v1.1"
    custom_instructions: Optional[str] = None
```

**Code-first only** (a Python dict via `Memory.from_config({...})`). There is no file/remote/declarative
config in the library. The REST server adds one indirection: `POST /configure` persists a JSON override blob
to a Postgres `settings` table and re-creates the `Memory` instance
(`server/server_state.py` `_merge_config` / `update_config`). Bundled providers are restricted to
`openai`/`anthropic`/`gemini` for LLM and `openai`/`gemini` for embedder; anything else → HTTP 400
(`server/main.py` `_validate_bundled_providers`).

### 1.7 Operational surface

| Dimension | OSS library | OSS self-hosted server | Platform |
|---|---|---|---|
| Hosting | your process | Docker Compose (pgvector + Postgres 17) | managed / BYOC |
| Async | `AsyncMemory` | sync FastAPI handlers | `AsyncMemoryClient` |
| Idempotency | **none** | **none** | `Idempotency-Key` on `generate_profile` / `sample_profiles` only (`client/main.py` L860) |
| Rate limiting | n/a | `slowapi` `Limiter(key_func=get_remote_address)` — **per-IP, and only `/auth/register` (5/min) and `/auth/login` (10/min) are limited** (`server/rate_limit.py`, `server/routers/auth.py`). No limit on `/memories` or `/search`. | account-level RPM |
| Auth | none | JWT (30 min access / 30 day refresh, `jti` single-use enforced by conditional UPDATE) + `m0sk_` API keys (bcrypt-hashed, prefix-indexed) + `ADMIN_API_KEY`; `AUTH_DISABLED=true` escape hatch. `JWT_SECRET` required or the process refuses to start. | API key |
| Multi-tenant isolation | **filter-level only.** Any caller can pass any `user_id`. No tenant boundary is enforced by the engine. | **same filter-level isolation.** `verify_auth` returns a `User` but the memory endpoints use `_auth=Depends(verify_auth)` and never constrain `user_id` to the caller's identity. Only `DELETE /memories`, `POST /reset`, and `DELETE /entities/...` require `require_admin`. A non-admin key can read/write another tenant's `user_id` if it knows the id. | org/project + `app_id` scoping |
| Audit log | SQLite `history` table, **per memory only** | + `request_logs` table (method, path, status, latency, auth_type) — **no user_id, no memory content, no actor identity** (`server/models.py` `RequestLog`; `server/main.py` `_persist_request_log`). `GET /requests` filters `auth_type IN ('api_key','admin_api_key')` only | `GET /v1/events/` per org/project; dashboard audit logs |
| Metrics / tracing | **none** (no prometheus/OTel in the package) | request latency in `request_logs` only | dashboard |
| Soft delete / tombstones | **none.** `delete()` removes the vector record; the tombstone lives only in the history table | same | delete is permanent |
| Content safety gates | **none** | **none** | content policies exist only in Zep (§2) |

### 1.8 Stated limitations

- OSS excludes: Graph Memory, Memory Decay, Temporal Reasoning, Dream, custom categories, webhooks, Memory
  Export, batch operations, feedback, summaries, org/project concept, project-wide event log.
  Source: https://docs.mem0.ai/platform/platform-vs-oss ("What's actually different" tables).
- Filter semantics silently degrade per vector store (table above).
- Multimodal: `POST /v3/memories/add/` accepts only a string `content`; `image_url`/`pdf_url` are rejected with
  400 — https://docs.mem0.ai/platform/features/multimodal-support.
- Hybrid search needs optional deps; without spaCy → semantic-only, without fastembed → no BM25
  (https://docs.mem0.ai/migration/oss-v2-to-v3 "Graceful Degradation").
- `get_all()` on pgvector silently drops `AND`/`OR`/`NOT`; Weaviate silently drops all metadata filters.
- Open issues already listed in §1.3 (erasure, close-race, audit ordering, provenance, contradictions).

---

## 2. Zep / GraphRAG (getzep/zep → getzep/graphiti)

**The OSS story changed.** `getzep/zep` is now an *examples/integrations* repo; Zep Community Edition is
**deprecated and moved to `legacy/`** (README: "Zep Community Edition is no longer supported"). The
open-source temporal-KG engine is **Graphiti** (`getzep/graphiti`), and Zep Cloud is the managed product.
Source: https://raw.githubusercontent.com/getzep/zep/main/README.md; https://raw.githubusercontent.com/getzep/graphiti/main/README.md.

### 2.1 Integration surface

Graphiti is a **library + graph DB** (Neo4j / FalkorDB / Neptune / Kuzu[deprecated]). Plus reference MCP and
REST servers that are explicitly **not production deployments**.

```python
from graphiti_core import Graphiti

graphiti = Graphiti("bolt://localhost:7687", "neo4j", "password")

# (a) write a memory
await graphiti.add_episode(
    name="msg_001",
    episode_body="Jane likes Adidas",
    source=EpisodeType.message,
    reference_time=datetime.now(timezone.utc),  # bi-temporal event time
    group_id="tenant_alice",
)

# (b) retrieve context
results = await graphiti.search(
    "what does jane like?",
    group_ids=["tenant_alice"],
    config=SearchConfig(
        edge_config=EdgeSearchConfig(
            search_methods=[EdgeSearchMethod.cosine_similarity, EdgeSearchMethod.bm25],
            reranker=EdgeReranker.rrf,
        )
    ),
)
```

Zep Cloud SDK (managed, `pip install zep-cloud`):

```python
client = Zep(api_key=API_KEY)
zep_user = client.user.create(email=..., first_name=...)  # -> uuid
zep_thread = client.thread.create(user_uuid=zep_user.uuid_)
client.thread.add_messages(
    zep_thread.uuid_,
    messages=[AddMessage(role="user", content=..., name="Jane Smith", reference_time=...)],
)
client.graph.episode.add(zep_user.uuid_, type="json", data=json.dumps(event_data))
ctx = client.thread.get_context(zep_thread.uuid_)  # -> .context (prompt-ready Context Block)
```
Source: https://help.getzep.com/quickstart.

### 2.2 Memory model

Graphiti's unit is a **temporal context graph**:

| Element | Fields | Source |
|---|---|---|
| **Episode** (provenance) | `uuid, name, content, source (EpisodeType), group_id, valid_at, created_at` | `graphiti_core/nodes.py` |
| **Entity node** | `uuid, name, group_id, summary (evolving), created_at, valid_at` | `graphiti_core/nodes.py` |
| **Entity edge** (fact) | `uuid, name, fact, fact_embedding, episodes[], group_id, created_at, expired_at, valid_at, invalid_at, reference_time, attributes{}` | `graphiti_core/edges.py` `EntityEdge` |
| **Community node/edge** | GRL-derived clusters + summaries | `graphiti_core/nodes.py` |
| **Saga node** | narrative thread summary with two watermarks (`last_summarized_at` = ingestion-time filter, `last_summarized_episode_valid_at` = event-time) | `graphiti_core/graphiti.py` `summarize_saga` |

**Bi-temporal model** — the differentiator. Each edge carries:
- `valid_at` — when the fact became true (event time)
- `invalid_at` — when it stopped being true
- `expired_at` — when it was invalidated
- `created_at` — ingestion time (transaction time)
- `reference_time` — the episode's reference timestamp

`invalid_at` and `expired_at` differ: `invalid_at` = "fact stopped being true"; `expired_at` = "node was
invalidated". Soft deadlines are explicitly **not** `invalid_at` (field docstring, `edges.py` `EntityEdge`).

**Contradiction handling = invalidation, not deletion.** "Temporal Fact Management: Facts have validity
windows. When information changes, old facts are invalidated — not deleted." — Graphiti README. The pipeline
returns `(resolved_edges, invalidated_edges, new_edges)` — `graphiti_core/graphiti.py` `_extract_and_resolve_edges`.

**Scoping.** `group_id` is the partition key on every node and edge ("partition of the graph", `edges.py`
`Edge.group_id`). On FalkorDB, each `group_id` is a **separate graph** (MCP README). Graphiti's reference
servers explicitly have **no authentication, no authorization, no prompt-injection protection** — and PRs
adding them are out of scope (`SECURITY.md` scope). Tenancy is the integrator's job.

### 2.3 Lifecycle

| Operation | Behaviour |
|---|---|
| Create | `add_episode()` → extract nodes+edges (LLM, structured output) → resolve/dedupe against existing graph → summarize entity attributes → invalidate contradicted edges → build episodic edges → optionally build communities. Also `add_bulk_episode`, `add_triplet` (bypass extraction). |
| Update | No in-place edit of a fact. New episode → LLM dedupe → old edge invalidated with `invalid_at`, new edge created. Both remain queryable. |
| Dedup | LLM-based node dedupe + edge resolution (`resolve_extracted_nodes`, `resolve_extracted_edges`); Jev/OpenAI Decisions clients can route dedupe to a dedicated decision model. |
| Decay / expiry | **No TTL.** Temporal validity windows are not expiry. Facts persist indefinitely unless invalidated. |
| Delete | `delete_entity_edge`, `delete_episode` (cascades to entities/facts it *solely* created), `clear_graph`. Physical delete — no tombstone, no undelete. |
| History | Full temporal history is native: the graph *is* the history (valid_at/invalid_at/expired_at + episodes[] provenance). |

### 2.4 Retrieval

Hybrid by construction: `EdgeSearchMethod` / `NodeSearchMethod` ∈ {`cosine_similarity`, `bm25`,
`breadth_first_search`}; rerankers ∈ {`rrf`, `node_distance`, `episode_mentions`, `mmr`, `cross_encoder`}
(`graphiti_core/search/search_config.py`). Preset recipes: `EDGE_HYBRID_SEARCH_RRF`,
`EDGE_HYBRID_SEARCH_CROSS_ENCODER`, `EDGE_HYBRID_SEARCH_NODE_DISTANCE`. Per-scope filtering by node/edge
type and by `valid_at`/`invalid_at` date ranges.

### 2.5 Extraction

The engine's LLM extracts, **but the caller defines the ontology**. `entity_types: dict[str, type[BaseModel]]`
and `edge_types` are Pydantic models the caller supplies — prescribed ontology. `custom_extraction_instructions`
is a free-text parameter on `add_episode`. Prompt *builders* are overridable per client via
`create_prompt_library({'extract_nodes': {'extract_message': fn}})` (response schemas are fixed and not
overridable — Graphiti README "Configurable Prompts").

### 2.6 Config surface

Code-first Python (`Graphiti(...)` kwargs, `prompt_library`, `llm_runtime`) plus **file-based YAML** for the
reference MCP server (`config.yaml` with env-var expansion `${NEO4J_URI}`, plus CLI flags). The MCP server's
`graphiti.entity_types` / `edge_types` / `edge_type_map` keys let an operator define an ontology **without
code**. Model routing per prompt is possible via `LLMRuntime` + `PromptRoutes` (e.g. cheap model for
`extract_attributes`, expensive for `resolve_edge`).

### 2.7 Operational surface

- Self-host only (Graphiti). Zep Cloud is managed or BYOC.
- Concurrency: `SEMAPHORE_LIMIT` (default 10) / `max_coroutines`; explicit guidance to keep it low to avoid
  LLM 429s.
- Observability: **OpenTelemetry tracer** (`tracer=`, `trace_span_prefix`) — the only project here with
  first-class tracing.
- **No auth, no authz, no rate limiting, no audit log** in Graphiti itself. The reference servers explicitly
  disclaim all of these.
- Retention/erasure: physical delete only. Zep Cloud adds retention *policies* (see §2.8).

### 2.8 Zep Cloud feature gap (managed)

Zep Cloud is where the enterprise governance surface lives — and it is **Enterprise-plan only**:
RBAC, ABAC policy sets bound to API keys, source traceability, Audit Logs, API Logs, content policies,
retention policies (`retain_for: 30d`, modes `off`/`report_only`/`enforce`), per-object retention by episode
metadata, `X-RateLimit-*` headers, `Idempotency-Key` on writes.
Sources: https://help.getzep.com/v4/context-retention, https://help.getzep.com/v4/audit-logging,
https://help.getzep.com/v4/rate-limits, https://help.getzep.com/v4/governance.

Deletion is permanent with **no undelete**; backups retain deleted data up to 365 days; graph write log keeps
changes up to 90 days; snapshot versions keep data until 150 newer versions replace them
(https://help.getzep.com/v4/context-retention "Deletion behavior"). Audit Logs cover **dashboard actions
only** — SDK/API calls are in API Logs instead, and the two have different retention (30 days vs 7/30 days +
1–7 year archive).

---

## 3. Letta (letta-ai/letta → letta-ai/letta-code)

**Current source moved.** The V1 API server is retired into the `archive` branch; active development is
`letta-ai/letta-code` (the "Letta Harness"), plus the Letta Agent SDK.
Source: https://raw.githubusercontent.com/letta-ai/letta/main/README.md.

### 3.1 Integration surface

TypeScript Agent SDK over the Letta App Server (WebSocket) or Letta Cloud:

```ts
import { LettaAgentClient } from "@letta-ai/letta-agent-sdk";
const client = new LettaAgentClient({ backend: "cloud", apiKey: process.env.LETTA_API_KEY });
const agentId = await client.createAgent({
  model: "anthropic/claude-opus-4-8",
  persona: "You are Quinn, a digital research analyst.",
  human:   "The user prefers concise memos with evidence.",
  memory:  [{ label: "team-context", value: "The team ships weekly on Thursdays." }],
});
await using session = client.createSession(agentId);
await session.sendMessage("What did we decide about pricing?");
```

Python SDK equivalent; also `npm install -g @letta-ai/letta-code` + `letta server` for self-hosting.
Source: https://docs.letta.com/agent-sdk/agents/index.md.

### 3.2 Memory model — the big shift

Letta v1 had **core memory / archival memory / recall memory** with self-editing tools. The current Letta Code
harness replaced that with **MemFS**: a **git-backed memory filesystem**.

| Layer | Where it lives | In-context? |
|---|---|---|
| Root memory files | `$MEMORY_DIR/*.md` with YAML frontmatter (`name`, `description`) | **Yes** — loaded into the system prompt every turn |
| `system/` directory (legacy layout) | `$MEMORY_DIR/system/*.md` | Yes |
| Subdirectories with their own `MEMORY.md` index | `$MEMORY_DIR/reference/…` | **No** — the agent sees the file tree and reads on demand |
| `skills/` | `$MEMORY_DIR/skills/<name>/SKILL.md` | On demand |

A memory label *is* its path: label `persona` → `persona.md`. Choosing a label chooses where the memory lives
and whether it stays in context. Source: https://docs.letta.com/concepts/memfs/index.md.

The v1 "block" concept survives only as the API shape: `CreateBlock` (`{label, value, description, limit,
read_only, …}`) with `POST /v1/blocks/`, and `memory_blocks[]` on agent creation
(https://docs.letta.com/api/resources/blocks; `letta-code/src/agent/create-agent-request.ts`).

### 3.3 Lifecycle

- **Create/edit**: the agent writes Markdown files with ordinary file tools and commits them to its MemFS git
  repo. **Edits become memory only once committed and pushed** — uncommitted edits are not memory yet.
- **Self-editing core memory**: yes, but at the *file* level, not a block-level tool. The agent rewrites its
  own persona/human files.
- **Versioning**: every memory edit is a git commit → version history, conflict resolution, and a clean
  boundary between saved memory and uncommitted changes. Memory subagents (dreaming, memory doctor) use git
  worktrees so they can update memory concurrently without blocking the main agent.
- **Consolidation / decay**: **"Dreaming"** — background subagents review recent conversations, consolidate
  lessons, and update memory without interrupting active work. Trigger: `step-count` (after N agent steps) or
  `compaction-event`; behavior: `reminder` vs `auto-launch`. Optional second-pass review
  ("Agent reviews before applying"). Source: https://docs.letta.com/configuration/memory/index.md.
- **Recall memory / search**: **MemFS has no semantic or vector index by default.** Agents find memory with
  normal file-search and read tools. Keyword search requires the optional
  [`@letta-ai/memfs-search`](https://github.com/letta-ai/mods/tree/main/packages/memfs-search) mod; semantic
  and hybrid modes additionally require QMD installed and indexed over `$MEMORY_DIR`. Conversation-history
  search is separate from MemFS.
- **Deletion**: git history — the file can be reverted. No memory-level tombstone or erasure semantics.

### 3.4 Extraction

**The agent decides.** There is no extraction pipeline in the library: the agent reads a message, decides what
is durable, and writes it. `/remember` and `/init` are prompts that drive that. "The agent decides where the
lesson belongs and commits the update to MemFS." Dreaming does the same in the background. There is no caller-
supplied extraction schema.

### 3.5 Config surface

Files + git + code. `MEMORY_DIR` env var, `systemPrompt`/`persona`/`human` at creation (creation-only —
`createAgent()` accepts custom system prompt strings, not preset names), `skillSources` (`bundled` >
`global` > `agent` > `project` by specificity), `dreaming` config (creation-only for `behavior`), and a
per-session `dreaming` override for `trigger`/`stepCount`. Memory size allowance is plan-bound (Free 5 MiB /
Pro 50 MiB per repository).

### 3.6 Operational surface

Local runtime (all state on-device, no account) or self-hosted App Server; Letta Cloud for sync across
machines. The Agent SDK is the programmatic surface; a hosted MCP server exists for MCP-capable agents.
Permissions: allowlist + interactive tool approval (`/configuration/permissions`); secrets stored separately
(`/configuration/secrets`); subagents with tags. Memory is scoped **per agent**, and shared memory
repositories are a **cloud-only** feature (self-hosted must point agents at their own git remote).

### 3.7 Stated limitations

- MemFS ships **without a semantic/vector index**; semantic/hybrid search is opt-in via a mod + QMD.
- Local backends support **full-text only** for conversation transcripts.
- Shared memory repositories are cloud-only.
- MemFS cannot be disabled once enabled on a capable backend (`applyMemfsFlags` docstring).

---

## 4. cognee (topoteretes/cognee)

### 4.1 Integration surface

Python SDK, TS SDK, Rust SDK, REST API, MCP server, plus agent plugins for Claude Code / Codex / Cursor /
OpenClaw.

```python
import cognee

# (a) write a memory (permanent = full pipeline; session_id = fast path)
await cognee.remember("Einstein was born in Ulm.", dataset_name="docs")

# (b) retrieve context for a prompt
results = await cognee.recall("Where was Einstein born?", datasets=["docs"])
# or inspect the exact prompt the LLM would have received:
ctx = await cognee.recall("Where was Einstein born?", datasets=["docs"], only_context=True)
```

Source: https://docs.cognee.ai/core-concepts/main-operations/remember,
https://docs.cognee.ai/core-concepts/main-operations/recall.

`remember()` = Add → Cognify → Improve. `recall()` = Search with auto-routing. `forget()` = unified deletion.
`improve()` = enrichment + session→graph bridge.

### 4.2 Memory model

| Element | Description |
|---|---|
| **Dataset** | the scoping unit; owned by a user; default `main_dataset` |
| **Data item** | an ingested unit (text/file/URL/`DataItem`); carries `external_metadata`, `data_id` |
| **Chunk** | the retrieval unit; `external_metadata` stamped on every chunk (v1.6.1) |
| **Graph node** | entities (typed via `graph_model`/`DataPoint` subclass) |
| **Graph edge** | relationships with per-source-document ownership records |
| **Session** | short-term cache keyed by user + session; `CACHE_BACKEND=sqlite` (default) or `redis` |
| **QA entry** | cached question/context/answer written on every completion-backed recall |

**Scoping**: datasets are owned by users; ownership determines who can read/write/share/improve/forget.
Dataset-level permissions (`delete` permission checked at resolution) — https://docs.cognee.ai/setup-configuration/permissions.
Physical layout per user: `.cognee_system/databases/<user_uuid>/<dataset_uuid>.pkl` (graph) + `<dataset_uuid>.lance.db/` (vector).

### 4.3 Lifecycle

- **Create**: `remember()` → ingest → chunk → extract entities/relationships (LLM, or GLiNER local model when
  no LLM key) → embed → optional Improve pass. `run_in_background=True` returns a promise-like `RememberResult`.
- **Update**: `update()` runs item-level deletion with `memory_only=True` then re-cognifies.
- **Dedup**: dlt `write_disposition="replace"|"merge"|"append"`; `incremental_loading` reuses unchanged data.
- **Decay/expiry**: **none.** No TTL, no validity window, no decay.
- **Delete**: `forget()` — item / dataset / everything / `memory_only=True`. Removes relational + graph +
  vector, purges contaminated session turns (with contamination propagation through feedback → lesson →
  later turns), and can purge search history. `everything=True` = "everything the *current user* can delete".
- **History**: search/recall history rows are kept per dataset, with their own retention and a
  `clean_history` flag; `GET /api/v1/search` and `/api/v1/recall` list them.

### 4.4 Retrieval

**Graph-first, not vector-first.** `recall()` auto-routes by a **rule-based** router (five ordered regex rules:
`quoted_phrase` → CHUNKS_LEXICAL, `coding_rules_intent` → CODING_RULES, `code_question` → CODE,
`count_question` → BROAD, `time_scoped_question` → TEMPORAL; default `HYBRID_COMPLETION`). Only
`FEELING_LUCKY` uses an LLM to pick the strategy. `HYBRID_COMPLETION` renders a dict of `chunks`, `entities`,
`facts`. Explicit `query_type` reaches ~14 more strategies (GRAPH_COMPLETION, RAG_COMPLETION, CYPHER,
SUMMARIES, CHUNKS, GRAPH_COMPLETION_COT, AGENTIC_COMPLETION, …). **CYPHER is disabled by default**
(`ALLOW_CYPHER_QUERY=true` to enable) precisely because it can mutate the graph — a nice precedent for
Thinkback's "no raw query passthrough" stance.

Results carry a `source` tag (`graph`/`session`/`trace`/`session_context`), `search_type`, `kind`, and
`metadata` with `data_id`/`chunk_id`/`chunk_index`/`document_name`; `include_references=True` adds
`metadata.evidence` listing exactly which chunks/nodes/edges entered the LLM context.

### 4.5 Extraction

Caller-configurable in three ways:
- `custom_prompt` — overrides the graph-extraction prompt for a call.
- `graph_model` — a `DataPoint` subclass constrains extraction to your own fields/relationships (only the
  `llm` extractor honours a custom schema).
- `extractor` — `"llm"` or `"gliner_demo"` (local GLiNER, no LLM key needed).

Per memory *type* there is no dedicated policy, but `MemorySource`/typed `MemoryEntry` payloads (`qa`,
`trace`, `feedback`, `skill_run`) are typed ingestion sources.

### 4.6 Config surface

**Environment-variable + code** (the richest env surface of the four): `ENABLE_BACKEND_ACCESS_CONTROL`,
`REQUIRE_AUTHENTICATION`, `CACHE_BACKEND`, `CACHING`, `GRAPH_DATABASE_PROVIDER`, `VECTOR_DB_PROVIDER`,
`DATASET_QUEUE_MAX_CONCURRENT`, `DATASET_ROUTING_TOP_K` / `_DISTANCE_CUTOFF` / `_TIE_MARGIN`,
`DATASET_QUEUE_ENABLED`, `SUBPROCESS_IDLE_TTL_SECONDS`, `ALLOW_CYPHER_QUERY`, `DEFAULT_USER_PASSWORD`.
Datasources are declarative (dlt `query`/`primary_key`/`temporal_columns`). HTTP/UI/MCP via Docker Compose.

### 4.7 Operational surface

- Self-host (Docker) or Cognee Cloud. Multi-tenant mode is the **default** (every `/api/v1` call needs an
  authenticated user) unless `ENABLE_BACKEND_ACCESS_CONTROL=false`.
- Async throughout; `run_in_background=True` for background ingestion; `GET /api/v1/datasets/status` and
  `GET /api/v1/activity/pipeline-runs` for progress.
- **Hard constraint the docs state plainly**: "Cognee runs as a single process. The embedded file-based
  defaults (Ladybug graph, SQLite, LanceDB) must not be shared across processes." Multi-process requires
  external Neo4j + Postgres + a single Cognee service. This is a real operational tax.
- No idempotency key, no Prometheus metrics, no distributed tracing documented.
- Search history is user-scoped with its own retention.

### 4.8 Stated limitations

- Postgres-as-graph-store is a **demo feature**; production is a licensed product (README warning).
- Embedded stores are single-process only.
- `forget()` does **not** scrub bytes: LanceDB keeps deleted rows in append-only fragments until compaction;
  the embedded graph file has no compaction. "If you need a byte-level erasure guarantee, run the storage
  engine's own compaction or vacuum" — https://docs.cognee.ai/core-concepts/main-operations/forget.
- Session-cache cleanup during `forget()` is **best-effort by contract** — a `status: success` does not
  guarantee every session entry was removed.
- GLiNER is "a demo of Cognee's small-model pipeline"; production accuracy needs the licensed product.
- BEAM 10M-token result is self-described as "exploratory", with a documented reproduction gap.

---

## 5. memary (kingjulio8238/Memary)

Worth one paragraph. It is a research prototype, not a competitor: LlamaIndex + FalkorDB/Neo4j + ReAct
routing agent, with a "Memory Stream" (all entities + timestamps) and an "Entity Knowledge Store" (entity
frequency + recency), inspired by K-LaMP. Recursive retrieval with subgraph depth ≤ 2 and multi-hop joining.
Setup is a Streamlit app with JSON files on disk (`memory_stream.json`, `entity_knowledge_store.json`); the
ReAct demo agent is slated for removal "so that memary can support any type of agents".
Source: https://raw.githubusercontent.com/kingjulio8238/Memary/main/README.md.

Notable: it has **no tenancy model at all** (multi-agent = separate `*_json` files per `user_id`), no
retention, no audit. Useful only as a counter-example for "memory as a growing append-only stream".

---

## 6. Capability matrix

Legend: **N** = native (documented + implemented), **P** = partial (works, with a documented caveat or
narrow scope), **A** = absent. Parenthetical = the caveat.

### 6.1 Core memory mechanics

| Capability | mem0 (OSS) | zep / graphiti | letta | cognee |
|---|---|---|---|---|
| Flat vector record as memory unit | **N** | P (facts are edges, not flat records) | A (files, not records) | P (chunks are the retrieval unit) |
| Graph node/edge as memory unit | **A** (removed in v3; entity *boost* only) | **N** | A | **N** |
| Episode/provenance as first-class | P (only via `history()` per memory) | **N** | P (git commit per edit) | P (data item → node/edge ownership) |
| Memory blocks / typed slots | **A** | **A** | **N** (legacy blocks; now git files) | **A** |
| Caller-supplied extraction schema | P (free-text `custom_instructions` only) | **N** (Pydantic `entity_types`/`edge_types`) | A | **N** (`graph_model` = `DataPoint` subclass) |
| Caller can bypass the LLM | **N** (`infer=False`) | **N** (`add_triplet`) | **N** (agent writes file directly) | **N** (`extractor="gliner"`, `custom_prompt`) |
| `add()` decides what's a memory | **N** (engine LLM) | **N** (engine LLM) | **N** (the agent itself) | **N** (engine LLM) |

### 6.2 Lifecycle & lifecycle governance

| Capability | mem0 (OSS) | zep / graphiti | letta | cognee |
|---|---|---|---|---|
| Automatic contradiction handling | **A** (v3 is ADD-only; known issue #5867/#4956) | **N** (invalidates old edge, keeps both) | P (dreaming consolidation) | P (dedupe + re-cognify; no temporal validity) |
| Deduplication | **N** (md5 hash + entity match) | **N** (LLM dedupe, routable to a decision model) | A (git dedupe by hand) | **N** (dlt write_disposition) |
| Per-memory TTL / expiry | **P** (`expiration_date`, date-only, hides not deletes) | A | A | A |
| Decay / recency re-ranking | A (Platform-only `decay`) | A | A | A |
| Soft delete + tombstone | P (history row with `is_deleted=1`; vector record hard-deleted) | A (physical delete) | P (git revert) | A (physical delete) |
| Bulk delete by arbitrary filter | P (`delete_all` limited to the 3 identity keys) | **N** (`delete_episode` cascade, `clear_graph`) | A | **N** (dataset + `memory_only` + per-item) |
| Version / history of a memory | P (per-memory SQLite `history`; no project-wide log) | **N** (bi-temporal by construction) | **N** (git history) | P (search/recall history, per dataset) |
| Background consolidation | A (Platform "Dream" only) | P (community summaries) | **N** (dreaming) | **N** (Improve pass) |

### 6.3 Retrieval

| Capability | mem0 (OSS) | zep / graphiti | letta | cognee |
|---|---|---|---|---|
| Vector-only retrieval | **N** | P | A | P |
| Hybrid (semantic + keyword) out of the box | **P** (BM25 needs store support + `fastembed`/spaCy) | **N** (cosine + bm25 + BFS, configurable) | A (needs the search mod + QMD) | **N** (`HYBRID_COMPLETION` = chunks + entities + facts) |
| Graph traversal in retrieval | A | **N** (`bfs`) | A | **N** (neighbourhood expansion) |
| Rerank | **N** (optional reranker; off by default) | **N** (6 rerankers incl. cross-encoder) | A | P (internal to retriever) |
| Per-result score breakdown | **P** (`explain=True` → signal scores only) | P (per-scope reranker scores) | A | P (`metadata.evidence` = what entered the context) |
| "Why was this returned" narrative | **A** | A | A | A |
| Time-aware retrieval | A (Platform-only `timestamp`/`reference_date`) | **N** (bi-temporal, point-in-time queries) | A | **N** (`TEMPORAL` strategy) |
| Untrusted-data framing guidance | A | **N** (provider-specific untrusted-data channel tables) | P (context vs system message guidance) | A |

### 6.4 Tenancy, access control, and audit

| Capability | mem0 (OSS) | zep / graphiti | letta | cognee |
|---|---|---|---|---|
| Multi-tenant scoping key | **P** (`user_id`/`agent_id`/`run_id`, filter-level only) | **N** (`group_id`, partition on every node/edge) | P (per-agent; shared repos = cloud only) | **N** (user-owned datasets) |
| Enforced tenant isolation (a tenant cannot read another's) | **A** (engine: none; server: filter-level, key not bound to `user_id`) | A in Graphiti (no auth at all); **N** in Zep Cloud (project scoping) | P (per-agent, no cross-tenant API) | **N** (ownership + dataset permissions) |
| Auth on the API | A in library; **N** in self-hosted server (JWT + API key + admin key) | A in Graphiti; **N** in Zep Cloud | **N** (Letta Cloud / self-host app server) | **N** (`REQUIRE_AUTHENTICATION`) |
| Rate limiting | A on memory endpoints (only `/auth/*` is limited, per-IP) | A in Graphiti; **N** in Zep Cloud (`X-RateLimit-*`) | P | A |
| Request/audit log | P (`request_logs`: method/path/status/latency/auth_type — no actor, no content, no `user_id`) | A in Graphiti; **N** in Zep Cloud (Audit Logs = dashboard only; API Logs separate) | A | P (search/recall history, user-scoped) |
| Audit of *what memory content was written* | P (per-memory `history` with `old_memory`/`new_memory`, but no cross-memory event log and no actor beyond `actor_id`) | **N** (episode provenance → facts → entities) | **N** (git commit trail) | P (pipeline run records) |
| Source provenance (memory ← source message) | A (open issue #7047) | **N** (`episodes[]` on edges) | **N** (git commit) | **N** (data-item ownership of nodes/edges) |
| Webhooks / change notifications | A (Platform-only) | **N** (Zep Cloud) | A | P (pipeline status endpoints) |
| Content safety / poisoning gates | A | P (Zep Cloud content policies + memory-security guidance) | A | A |
| Byte-level erasure guarantee | A (history DB keeps deleted text; issue #7452) | A (backups keep data 365d) | P (git history retains deleted content) | A (LanceDB fragments; compaction required) |

### 6.5 Config & operations

| Capability | mem0 (OSS) | zep / graphiti | letta | cognee |
|---|---|---|---|---|
| Code-first config | **N** (Python dict) | **N** | **N** | **N** |
| File/remote/declarative config | P (server `POST /configure` → DB settings row) | **N** (YAML + env for the MCP server) | **N** (files + env) | **N** (env vars + declarative datasources) |
| New memory type added purely by config | **A** | P (new ontology type via YAML/Pydantic, not a new *policy*) | A | P (new `DataPoint` schema, not a new *policy*) |
| Per-type policy (TTL, retention, extraction, access) | **A** | A | A | A |
| OpenTelemetry tracing | A | **N** | A | A |
| Prometheus metrics | A | A | A | A |
| Idempotency keys | A (Platform: profile jobs only) | **N** (Zep Cloud) | A | A |
| Self-hostable | **N** | **N** | **N** | **N** |
| Managed offering | **N** (Platform) | **N** (Zep Cloud) | **N** (Letta Cloud) | **N** (Cognee Cloud) |
| Async API | **N** (`AsyncMemory`) | **N** | P (async streaming sessions) | **N** |
| Streaming / partial writes | A | P (background ingestion, status polling) | **N** (streaming turns) | **N** (`run_in_background`) |

**Footnote — where each answer came from.** All mem0 cells: read from the installed `mem0ai==2.2.1` source
(`mem0/memory/main.py`, `mem0/configs/base.py`, `mem0/memory/storage.py`, `mem0/utils/scoring.py`,
`mem0/memory/notices.py`), cross-checked against `mem0ai/mem0@main` (`mem0/configs/base.py` byte-identical)
and the docs at https://docs.mem0.ai/platform/platform-vs-oss, https://docs.mem0.ai/migration/oss-v2-to-v3,
https://docs.mem0.ai/open-source/features/metadata-filtering, https://docs.mem0.ai/platform/features/memory-expiration,
https://docs.mem0.ai/open-source/features/rest-api, and the upstream `server/` tree (`server/main.py`,
`server/auth.py`, `server/models.py`, `server/rate_limit.py`, `server/routers/*.py`, `server/server_state.py`).
Open-issue claims cite the GitHub issue URL directly. Zep/Graphiti cells: `getzep/graphiti@main`
(`graphiti_core/graphiti.py`, `graphiti_core/edges.py`, `graphiti_core/search/search_config.py`),
`getzep/graphiti@main/mcp_server/README.md`, `getzep/zep@main/README.md`, and https://help.getzep.com/v4/*
(context-retention, audit-logging, rate-limits, governance, quickstart). Letta cells:
`letta-ai/letta@main/README.md`, `letta-ai/letta-code@main/src/agent/memory-filesystem.ts` and
`src/agent/create-agent-request.ts`, and https://docs.letta.com/{concepts/memfs, configuration/memory,
agent-sdk/memory, agent-sdk/agents, api/resources/blocks}. cognee cells:
`topoteretes/cognee@main/README.md` and https://docs.cognee.ai/{core-concepts/main-operations/{remember,
recall,forget}, setup-configuration/permissions}. Memary cells: `kingjulio8238/Memary@main/README.md`.

---

## 7. Where mem0 is strong (don't rebuild these)

1. **Extraction.** One LLM call, dedup, batch embed, batch insert, entity extraction — already done and
   benchmarked (LoCoMo 92.5 / LongMemEval 94.4 per the project, platform numbers).
   `mem0/memory/main.py` L918-L1000.
2. **Multi-signal retrieval.** Semantic + BM25 + entity boost with score fusion and graceful degradation to
   semantic-only. `mem0/utils/scoring.py`; migration guide "Retrieval: Multi-Signal Hybrid Search".
3. **Vector-store abstraction.** 25 stores, provider factories for LLM/embedder/reranker.
   `mem0/utils/factory.py`, https://docs.mem0.ai/components/vectordbs/overview.
4. **Metadata filter grammar.** `eq/ne/gt/gte/lt/lte/in/nin/contains/icontains/*` + `AND/OR/NOT`.
   `main.py` L1538-L1613.
5. **Per-memory history.** ADD/UPDATE/DELETE rows with `old_memory`/`new_memory`/`actor_id`/`role`.
   `mem0/memory/storage.py` L150-L252.
6. **Identity-key hygiene.** `_strip_identity_keys` prevents metadata from reassigning a memory's tenant.
   `main.py` L143-L162 (fixes issue #6277/#6342).
7. **`infer=False` raw writes.** Lets a caller bypass the LLM entirely.
8. **Expiry.** `expiration_date` with UTC, inclusive-date, fail-open parsing semantics — already matches the
   retention-window pattern most enterprise policies want (soft expiry, then scheduled hard delete).

## 8. Where mem0 stops — Thinkback's legitimate workspace

| Gap | mem0 evidence | Why a wrapper is the right place |
|---|---|---|
| **Tenancy isolation not enforced** | `server/main.py` memory endpoints use `verify_auth` and never bind `user_id` to the caller's identity; `delete_all`/`reset`/`entities DELETE` are admin-only but reads/writes are not. Engine-side there is no tenant concept at all. | Isolation is a *service* property (who may name which scope), not an engine property. Thinkback can enforce scope-by-caller and fail closed. |
| **Deletion/erasure auditability** | `delete_all()` leaves the scope's raw messages in the history DB (issue #7452) and the history table retains deleted memory text; `request_logs` records no actor, no `user_id`, no content. | A wrapper owns the erasure workflow: purge history rows, emit an erasure receipt, verify residual stores. |
| **Retention/expiry enforcement** | `expiration_date` only *hides*; nothing purges. No sweeper, no per-type TTL policy, no retention audit. No `decay` in OSS. | Retention windows (e.g. "90 days searchable, then purge") are a service policy with a scheduler and an evidence trail. |
| **Per-type policy** | `memory_type` accepts only `"procedural_memory"` and is slated for removal; no per-type TTL, per-type extraction, or per-type access rules. | A registry mapping memory type → extraction prompt + retention + access rule is exactly a wrapper concern (and Thinkback already has `MemoryTypeRegistry`). |
| **Explainability of *why* a memory was returned** | `explain=True` yields only `{semantic_score, bm25_score, entity_boost, …}` — signal magnitudes, not attribution/recency/policy reasoning. | "Why did the assistant remember this about you?" needs a structured rationale: source turn, age, reinforcement count, policy basis. |
| **Audit of what was written** | Per-memory `history` only; no project-wide event log in OSS; no source-message lineage (issue #7047). `GET /v1/events/` is Platform-only. | A wrapper can emit a project-scoped, tenant-scoped write audit with actor, scope, source, and outcome. |
| **Content safety gates** | Nothing. No PII filter, no injection detection, no per-type redaction. | Pre-write screening and redaction are service concerns. |
| **Access control beyond scope** | No RBAC/ABAC. One API key = one scope chosen by the caller. | Policy evaluation per request is a wrapper concern. |
| **Rate limiting / idempotency on writes** | `slowapi` limits only `/auth/*`; no limit on `/memories` or `/search`; no idempotency key on writes. | A wrapper owns quotas, backpressure, and idempotent write semantics. |
| **Observability** | No Prometheus, no OTel in the library or server (only `request_logs` latency). | Metrics/traces are a wrapper concern. |
| **Contradiction / staleness** | ADD-only in OSS (issues #5867, #4956); Platform "Dream" is the answer but is managed-only. | A wrapper that owns a supersede/merge policy — or a version/history model with "current fact" resolution — has real work to do. |

**Reinventing?** Two things to watch:
- **Per-memory expiry** — mem0 already has `expiration_date` with sensible semantics. Don't rebuild date
  arithmetic; call mem0's and add the *policy layer* (per-type windows, purge job, evidence).
- **Hybrid retrieval + rerank** — mem0 already fuses semantic/BM25/entity and has a reranker factory. Don't
  reimplement scoring; do add the *explainability layer* on top of `explain=True`.
- Conversely, **rebuilding mem0's extraction** would be pure waste — do not.

## 9. Open questions for Thinkback (not answerable from public sources)

- Whether the deployed mem0 is OSS library or self-hosted server determines which auth/limit surface
  Thinkback inherits. The OSS server's non-admin key can read any `user_id` — if Thinkback fronts the server
  rather than the library, it must add that binding itself.
- Thinkback's `_mem0_session_scope` (`src/thinkback/memory/backends/mem0_library.py` L855-871) **correctly**
  replicates mem0's `_build_session_scope` (`mem0/memory/main.py` L412-419): same `%`/`&`/`=` escaping, same
  key order (`agent_id`, `run_id`, `user_id` — mem0's `sorted([...])` resolves to the same order). Worth
  keeping in sync if mem0 changes its escaping, since a mismatch silently splits the message buffer.
- Thinkback already implements the erasure step mem0 omits: `purge_history` deletes the scope's rows from
  mem0's SQLite `messages` table (L822-842), which is exactly the gap filed as mem0 issue #7452. That work
  is real and should stay — but it is a *mem0-internals* workaround, and it should be re-verified against
  mem0 upgrades, since the upstream table/column names are not a stable API.
- mem0's OSS `history` table and `messages` table are **private implementation details** of
  `mem0/memory/storage.py`, not a public API. Thinkback reads them directly. That is a maintenance risk
  worth an explicit upstream-issue or a pinned-version note.
