# thinkback

thinkback is the memory service for thinkback, a conversation assistant.
It stores per-session short-term context in PostgreSQL, maintains a
session summary for each conversation, and extracts user-level long-term memory
that can be recalled across newly created sessions. The service has no Redis or
Celery dependency: short-term memory lives in PostgreSQL only.

## Scope

thinkback is an internal service deployed inside the Thinkback VPC. It does
not perform caller authentication, API-key gating or `X-Authenticated-User-Id`
matching in-process. Public ingress, cross-domain auth and rate limiting are
handled upstream by the gateway, mTLS, service identity or network ACL. The
service still needs credentials for the external models, vector store and
database it calls (`OPENAI_API_KEY`, `MEMORY_EMBEDDING_API_KEY`, `MILVUS_USER`
/ `MILVUS_PASSWORD`, `POSTGRES_PASSWORD`). See
[Thinkback记忆三层架构](docs/memory/Thinkback记忆三层架构.md) section 0.1 for the
authoritative boundary.

Included:

- FastAPI service shell
- Health, liveness, and readiness probes
- PostgreSQL configuration and Alembic scaffold
- P0 memory workflows aligned with the short-term summary and long-term memory architecture
- Mem0 Library long-term memory adapter
- Minimal long-term memory management APIs for list, get, update and delete
- Docker Compose local stack
- Kubernetes API manifests

Not included in P0:

- Strong write fences, CAS tombstones, semantic suppression, dead-letter governance, and decay jobs
- Domain-specific memory scoring beyond P0 heuristics
- RAG, document parsing, object storage, safety, and evaluation features

## Requirements

- Python `>=3.12,<3.14`
- Poetry
- Docker and Docker Compose for local infrastructure

## Quick Start

```bash
poetry install
cp .env.example .env
make tests
make run
```

Start local dependencies:

```bash
make docker-up
```

thinkback embeds Mem0 Library. Mem0 uses an OpenAI-compatible LLM for
extraction, a separate OpenAI-compatible embedding service for embeddings, and
an independently deployed Milvus for L3 vector storage:

```bash
OPENAI_API_KEY=<secret>
MEMORY_LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
MEMORY_EMBEDDING_BASE_URL=http://embedding.example.internal:7345/v1
MEMORY_EMBEDDING_API_KEY=
MILVUS_URL=http://localhost:19530
MILVUS_DATABASE=default
MILVUS_USER=
MILVUS_PASSWORD=
MEMORY_MILVUS_COLLECTION=thinkback
MEMORY_LLM_MODEL=qwen-plus-latest
MEMORY_EMBEDDING_MODEL=default-embedding
MEM0_HISTORY_DB_PATH=.mem0/history.db
MEMORY_L3_WRITE_MODE=async
MEMORY_API_WORKER_LIMIT=8
MEMORY_API_WORKER_WAIT_SECONDS=5
MEMORY_L3_EXECUTOR_WORKERS=16
MEMORY_L3_MAX_PENDING_TASKS=256
MEMORY_L3_QUEUE_WAIT_SECONDS=5
MEMORY_BACKEND_MAX_CONCURRENT_CALLS=4
READINESS_TIMEOUT_SECONDS=3
```

Environment variable notes:

| Variable | 中文解释 |
| --- | --- |
| `APP_NAME` | 应用名称，日志、健康检查和部署识别使用。 |
| `APP_VERSION` | 应用版本，应与镜像版本或发布版本保持一致。 |
| `ENVIRONMENT` | 运行环境；本地为 `development`，生产为 `production`。 |
| `DEBUG` | 是否开启调试模式；生产必须关闭。 |
| `LOG_LEVEL` | 日志级别；生产默认 `INFO`。 |
| `POSTGRES_HOST` | PostgreSQL 主机地址。 |
| `POSTGRES_PORT` | PostgreSQL 端口。 |
| `POSTGRES_USER` | PostgreSQL 用户名。 |
| `POSTGRES_PASSWORD` | PostgreSQL 密码；生产必须通过 Secret 注入。 |
| `POSTGRES_DATABASE` | PostgreSQL 数据库名，当前为 `thinkback`。 |
| `MILVUS_URL` | Milvus 服务地址；生产不要使用 `localhost`。 |
| `MILVUS_DATABASE` | Milvus database 名称。 |
| `MILVUS_USER` | Milvus 用户名；无鉴权时留空。 |
| `MILVUS_PASSWORD` | Milvus 密码；无鉴权时留空。 |
| `OPENAI_API_KEY` | LLM API key；生产必须通过 Secret 注入。 |
| `MEMORY_LLM_BASE_URL` | OpenAI-compatible LLM endpoint 地址。 |
| `MEMORY_EMBEDDING_BASE_URL` | OpenAI-compatible Embedding endpoint 地址。 |
| `MEMORY_EMBEDDING_API_KEY` | Embedding API key；当前 endpoint 不需要鉴权时留空。 |
| `MEMORY_MILVUS_COLLECTION` | Mem0 写入 Milvus 的 collection 名称。注意：mem0 v2 的 BM25 混合检索要求 v3 schema（含 `text`/`sparse` 字段）的**新建** collection；在旧 schema collection 上自动退回纯语义检索（启动日志有 warning）。启用混合检索请切换到新 collection 名称并迁移存量数据，见 `docs/DEBUG_REPORT.md` W-3。 |
| `MEMORY_EMBEDDING_DIMS` | Embedding 向量维度，必须与服务端输出一致。 |
| `MEMORY_LLM_MODEL` | LLM 模型名称。 |
| `MEMORY_EMBEDDING_MODEL` | Embedding 模型名称。 |
| `MEM0_HISTORY_DB_PATH` | Mem0 Library 本地历史数据库路径，需要进程可写。 |
| `MEMORY_L3_WRITE_MODE` | L3 写入模式；`async` 表示后台抽取长期记忆。 |
| `MEMORY_L2_LLM_ENABLED` | L2 是否启用 LLM 综合摘要（P0）。关闭则保持拼接式降级实现（V1 行为）。 |
| `MEMORY_L2_REFRESH_INTERVAL_ROUNDS` | L2 去抖间隔：每会话累计 N 个 append 触发一次后台 LLM 刷新（首轮立即）。 |
| `MEMORY_L2_LLM_TIMEOUT_SECONDS` | L2 单次 LLM 调用超时。 |
| `MEMORY_L2_LLM_MAX_TOKENS` | L2 摘要输出 token 上限。 |
| `MEMORY_DECAY_ENABLED` | 遗忘 decay（P2#7）：老且久未召回的长尾记忆置 SUPPRESSED（不可达而非删除，数据保留）。默认关闭。槽位关键事实与墓碑行受保护；被召回即强化。 |
| `MEMORY_DECAY_MIN_AGE_DAYS` | decay 事实年龄下限（默认 90 天）。 |
| `MEMORY_DECAY_UNRECALLED_DAYS` | decay 久未召回阈值（默认 60 天）。 |
| `MEMORY_DECAY_SWEEP_INTERVAL_SECONDS` | decay 全局清扫最小间隔（默认 3600 秒，append 触发时间门控）。 |
| `MEMORY_API_WORKER_LIMIT` | API 读池和写池各自的同步工作线程并发上限。 |
| `MEMORY_API_WORKER_WAIT_SECONDS` | API 同步工作进入 worker 并等待返回的总时间窗口。超过后返回 503，worker 线程可能仍在继续执行；重试必须复用原 `round_id` 或 `operation_id`。 |
| `MEMORY_L3_EXECUTOR_WORKERS` | L3 后台抽取线程数。 |
| `MEMORY_L3_MAX_PENDING_TASKS` | L3 后台写入队列容量。 |
| `MEMORY_L3_QUEUE_WAIT_SECONDS` | L3 队列满前的最长等待秒数。 |
| `MEMORY_BACKEND_MAX_CONCURRENT_CALLS` | 单进程内 Mem0 Library 后端调用并发上限，用于限制 LLM、Embedding 和 Milvus 压力。 |
| `READINESS_TIMEOUT_SECONDS` | 单个依赖 readiness 检查超时时间，单位秒。 |
| `TASK_ORPHAN_RUNNING_SECONDS` | 启动时孤儿 running 任务回收阈值（秒）：超过该时长无更新的 running 任务在服务启动时被回收为 failed（进程被硬杀留下的任务，否则 GetTask 永远返回 running）；必须显著大于最长后台任务时长，最小 60。 |

thinkback and Milvus can run on different hosts. The service must not bypass
Mem0 Library and write Milvus directly.

The L3 background extraction runs in the API process. There is no separate
worker process anymore.

Run deterministic tests:

```bash
make tests
```

Run the real Mem0 5-round pressure scenario:

```bash
make docker-up
make real-tests
```

The real pressure script requires `OPENAI_API_KEY` and `MILVUS_URL`. Mem0
Library owns LLM extraction, embedding, L3 vector writes, semantic search,
update, and delete.

Full-chain real tests (real service process + PG + Milvus + LLM/embedding
endpoints, covering append/idempotency/L3 extraction/items/recall/update/
delete/sensitive fail-closed on both HTTP and gRPC):

```bash
# service must run without proxy env (loopback LLM endpoints get hijacked
# by macOS system proxies otherwise): no_proxy='*'
.venv/bin/python script/realchain/fullchain_http.py
.venv/bin/python script/realchain/fullchain_grpc.py   # needs gRPC server on :50052
```

Chinese recall-quality replay eval (scenario appends → recall assertions,
reports pass rate paired with latency/injection cost; exit 1 on failure, CI-able):

```bash
.venv/bin/python script/eval/zh_replay_eval.py
```

## Health

```bash
curl http://localhost:8000/health
curl http://localhost:8000/health/live
curl http://localhost:8000/health/ready
```

Architecture details (layers, components, data flow, API design, DB schema,
scalability path) live in [docs/ARCHITECTURE.md](docs/report/ARCHITECTURE.md).

## Project Layout

```text
src/thinkback/domain       纯领域层：枚举/实体/端口协议/指纹键
                               ├─ safety         内容安全准入词表
                               ├─ recall_policy  召回去重与预算裁剪
                               ├─ summarization  L2 摘要策略
                               └─ slots/         P0 槽位抽取引擎（正则 NLP）
src/thinkback/memory       应用层：编排与用例
                               ├─ service.py     MemoryService 编排器
                               ├─ schemas.py     API DTO（Pydantic）
                               ├─ caches.py      进程内 TTL 读缓存
                               ├─ repositories/  仓储实现（in_memory / sqlalchemy）
                               └─ backends/      L3 后端适配（fake / mem0_library）
src/thinkback/infra        基础设施：config / database / readiness / logging
src/thinkback/api          HTTP 入口（FastAPI）
src/thinkback/rpc          gRPC 入口
test                           scaffold and behavior tests
k8s                            Kubernetes deployment assets
```

依赖方向只允许向内：`api/rpc -> memory -> infra -> domain`，
`domain` 不依赖任何其他层。
