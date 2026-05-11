# Thinkback

Thinkback is the memory service for the AI virtual social stack. It owns the
three-layer memory business workflow and uses Mem0 Library for L3 long-term
memory.

## Scope

Included:

- FastAPI service shell
- Health, liveness, and readiness probes
- PostgreSQL configuration and Alembic scaffold
- Redis configuration
- P0 memory workflows aligned with the three-layer memory architecture
- Mem0 Library long-term memory adapter
- Docker Compose local debug stack
- Kubernetes production manifests

Not included in P0:

- P2 write fences, semantic suppression, tombstones, dead-letter governance, and decay jobs
- Relationship scoring
- RAG, document parsing, object storage, safety, and evaluation features

## Requirements

- Python `>=3.11,<3.14`
- Poetry
- Docker and Docker Compose for local infrastructure

## Quick Start

```bash
poetry install
cp .env.example .env
make test
make run
```

启动本地依赖。这里的 Docker Compose 只用于本地控制台调试或本地容器调试，不作为生产入口：

```bash
make docker-up
```

## Local Debug

本地真实链路调试使用当前工程约定的数据库和本机中间件，先从本地模板生成 `.env.local`：

```bash
cp .env.local.example .env.local
```

在 `.env.local` 中填入 `OPENAI_API_KEY`，然后启动本地 API：

```bash
make debug-api
make debug-ready
```

`make debug-api` 监听 `127.0.0.1:18082`，使用
`POSTGRES_DATABASE=liaoriver_memory`、`localhost:6379` Redis、
`http://localhost:6333` Qdrant，并启用同步 L3 写入，方便 append 后立刻
recall 排查问题。

运行真实质量主链路评测：

```bash
make quality-real
```

提交前运行本地工程门禁：

```bash
make verify-local
```

Thinkback embeds Mem0 Library. Mem0 uses OpenAI for extraction/embeddings and
an independently deployed Qdrant for L3 vector storage:

```bash
OPENAI_API_KEY=<secret>
QDRANT_URL=https://qdrant.example.internal
QDRANT_API_KEY=<secret>
MEMORY_QDRANT_COLLECTION=memories_qwen_1024
MEMORY_LLM_MODEL=qwen3.5-flash
MEMORY_EMBEDDING_MODEL=text-embedding-v4
MEM0_HISTORY_DB_PATH=.mem0/history.db
MEMORY_L3_WRITE_MODE=async
MEMORY_API_WORKER_LIMIT=8
MEMORY_L3_EXECUTOR_WORKERS=16
MEMORY_L3_MAX_PENDING_TASKS=256
MEMORY_L3_QUEUE_WAIT_SECONDS=5
READINESS_TIMEOUT_SECONDS=30
```

Thinkback and Qdrant can run on different hosts. The service must not bypass
Mem0 Library and write Qdrant directly.

Run deterministic tests:

```bash
PYTHONPATH=src .venv/bin/pytest
```

Run the real Mem0 5-round pressure scenario locally:

```bash
make docker-up
PYTHONPATH=src .venv/bin/python script/real_mem0_pressure.py
```

The real pressure script requires `OPENAI_API_KEY` and `QDRANT_URL`. Mem0
Library owns LLM extraction, embedding, L3 vector writes, semantic search,
update, and delete.

## Production Scaffold

生产部署入口是 `k8s/` 下的 Kubernetes 配置。Docker Compose 只保留给本地依赖和本地容器调试，不作为生产部署路径。

```bash
docker build -t thinkback:0.1.0 .
kubectl apply -k k8s
kubectl wait --for=condition=complete job/thinkback-migrate --timeout=120s
kubectl rollout status deployment/thinkback-api
```

生产数据库名统一为 `liaoriver_memory`。上线前需要替换 K8s Secret、外部
PostgreSQL/Redis/Qdrant 地址、镜像 tag 和资源限制。

当前仓库补齐了以下生产工程基线：

- GitHub Actions CI in `.github/workflows/ci.yml` for Ruff, Mypy, Pytest, and
  Docker image build.
- Docker multi-stage runtime image with a non-root user.
- Kubernetes API deployment with probes, resource requests/limits,
  `ServiceAccount`, `PodDisruptionBudget`, `NetworkPolicy`, writable Mem0
  history `emptyDir` mounts, and a migration `job-migrate.yaml`.
- `script/README.md` documenting quality, pre-production, and stability script
  boundaries.

K8s manifest 是可落地的基线模板，但不是最终容量承诺。真实上线前仍要按集群、流量、外部模型限流和网络策略重新校准。稳定性压测不包含在本次质量门禁结论内。

## Health

```bash
curl http://localhost:8000/health
curl http://localhost:8000/health/live
curl http://localhost:8000/health/ready
```

## Project Layout

```text
src/api      HTTP application and routes
src/infra    runtime integrations
src/memory   memory-service boundaries
test         scaffold tests
k8s          Kubernetes deployment assets
```
