# Deployment Guide

Thinkback is Kubernetes-native. The guide covers local, Docker, and K8s deployment.

## Quick start (local)

```bash
git clone https://github.com/mcgrapeng/thinkback.git
cd thinkback
uv sync --frozen --group dev && cd web && npm install && cd ..
cp .env.example .env   # fill in LLM/embedding + Milvus
make dev
```

API at `localhost:8000`, web UI at `localhost:7001`.

## Docker

```bash
docker build -t thinkback:latest .
docker run -p 8000:8000 -p 7001:7001 \
  -e MEMORY_LLM_KEY=$MEMORY_LLM_KEY \
  -e MEMORY_LLM_BASE_URL=$MEMORY_LLM_BASE_URL \
  -e MILVUS_URL=$MILVUS_URL \
  -e MILVUS_DATABASE=Thinkback \
  thinkback:latest
```

## Docker Compose

```bash
docker compose up -d
```

## Kubernetes

Manifests are in `k8s/`:

| File | Purpose |
| --- | --- |
| `deployment-api.yaml` | API deployment (replicas, probes, resources) |
| `service.yaml` | Service (ClusterIP) |
| `configmap.yaml` | Runtime config (non-sensitive) |
| `secret.example.yaml` | Template for secrets |
| `job-migrate.yaml` | Alembic migration job |

### Deploy

```bash
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/secret.yaml       # create from secret.example.yaml
kubectl apply -f k8s/deployment-api.yaml
kubectl apply -f k8s/service.yaml
kubectl apply -f k8s/job-migrate.yaml  # run Alembic migrations
```

### Health endpoints

| Endpoint | Purpose |
| --- | --- |
| `GET /health/live` | Liveness probe |
| `GET /health/ready` | Readiness probe |
| `GET /metrics` | Prometheus metrics |

### Prometheus metrics

| Metric | Type | Description |
| --- | --- | --- |
| `thinkback_request_duration_seconds` | Histogram | HTTP request latency |
| `thinkback_task_state_total` | Counter | Tasks by state |
| `thinkback_l3_queue_depth` | Gauge | Background task queue depth |
| `thinkback_l3_worker_active` | Gauge | Active L3 workers |
| `thinkback_memory_recall_degraded_total` | Counter | Degraded recall responses |

## Configuration

See [README](../README.md#configuration). Key environment variables:

| Variable | Default | Notes |
| --- | --- | --- |
| `MEMORY_LLM_KEY` | — | Required |
| `MEMORY_LLM_BASE_URL` | — | OpenAI-compatible |
| `MEMORY_EMBEDDING_KEY` | — | Required |
| `MILVUS_URL` | `http://localhost:19530` | |
| `MEMORY_L3_WRITE_MODE` | `async` | `sync` for debugging |
| `MEMORY_L3_EXECUTOR_WORKERS` | `16` | Scale for throughput |
| `GRPC_ENABLED` | `true` | Set `false` to disable gRPC |

## Scaling

- **Horizontal**: scale `deployment-api.yaml` replicas (stateless)
- **L3 queue**: adjust `MEMORY_L3_MAX_PENDING_TASKS` and `MEMORY_L3_EXECUTOR_WORKERS`
- **Database**: PostgreSQL can be external RDS / Cloud SQL
- **Milvus**: use managed Milvus (Zilliz Cloud) for production

## Monitoring

- Query `/metrics` directly or import the Grafana dashboard (coming soon)
- Structured JSON logs with `trace_id` for request correlation
- Alerts: queue depth, degraded response rate, task failure rate

See [Operations runbook](RUNBOOK.md) for debugging and recovery.
