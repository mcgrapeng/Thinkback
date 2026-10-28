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

`make dev` runs `script/dev.py`, which starts:

| Process | Port | Notes |
| --- | --- | --- |
| API (uvicorn) | `http://127.0.0.1:7002` | `BACKEND_PORT` default `7002`; if taken, the launcher walks up to the next free port |
| Admin console (vite) | `http://localhost:7001` | `FRONTEND_PORT` default `7001`; vite itself shifts the port if taken — trust vite's output |

The console proxies to whatever backend port the launcher actually picked, so use the
printed URLs rather than assuming 7002.

`:8000` is the **container / cluster** port (Dockerfile `CMD`, Kubernetes Service),
not the local dev port. If you want the local API on 8000, run
`BACKEND_PORT=8000 make dev`.

## Docker

```bash
docker build -t thinkback:latest .
docker run -p 8000:8000 \
  -e POSTGRES_HOST=... -e POSTGRES_USER=... -e POSTGRES_PASSWORD=... -e POSTGRES_DATABASE=thinkback \
  -e MEMORY_LLM_KEY=$MEMORY_LLM_KEY \
  -e MEMORY_LLM_BASE_URL=$MEMORY_LLM_BASE_URL \
  -e MEMORY_EMBEDDING_BASE_URL=$MEMORY_EMBEDDING_BASE_URL \
  -e MILVUS_URL=$MILVUS_URL \
  -e MILVUS_DATABASE=default \
  thinkback:latest
```

The image contains the API only (`uvicorn` on `:8000`, `EXPOSE 8000`). It does **not**
contain the admin console — that is the separate `web/` vite app, built and served on
its own, so there is no `7001` port to publish from this image.

The image carries no database, vector store, or model endpoint. `docker run` only
works once PostgreSQL, Milvus, and the LLM/embedding endpoints are reachable from the
container.

## Docker Compose

```bash
docker compose up -d
```

Compose starts **PostgreSQL only** (18.3, published on `5432`), as a local-isolation or
CI dependency stack. It does not start the API, the console, Milvus, or any model
endpoint — those come from elsewhere (your `.env` may point at a shared PostgreSQL
instead).

## Kubernetes

Manifests are in `k8s/`:

| File | Purpose |
| --- | --- |
| `deployment-api.yaml` | API deployment (replicas, probes, resources) |
| `service.yaml` | Service (ClusterIP): 8000 HTTP, 50051 gRPC |
| `networkpolicy.yaml` | Default-deny ingress + allow approved caller namespaces |
| `configmap.yaml` | Runtime config (non-sensitive) |
| `secret.example.yaml` | Template for secrets |
| `job-migrate.yaml` | Alembic migration job |

### Prerequisites

- **A CNI that enforces NetworkPolicy** (Calico / Cilium / Antrea / …). Without it
  `networkpolicy.yaml` is inert and the service stays reachable from every pod.
- **A labelled caller namespace.** The business API has no caller authentication by
  design (see [the architecture note](memory/Thinkback记忆三层架构.md) §0.1), so the
  network boundary is the only thing keeping other workloads out of `/v2/memory/*`:

  ```bash
  kubectl label namespace <your-gateway-ns> thinkback.io/allowed-caller-namespace=true
  ```

  If namespace labels are not available in your cluster, edit the one marked
  `# >>> 部署方改这里 <<<` in `k8s/networkpolicy.yaml` and replace the
  `namespaceSelector.matchLabels` with `kubernetes.io/metadata.name: <your-gateway-ns>`.
- `ADMIN_TOKEN` in the Secret. `/admin/api/*` fails closed without it, and with
  `ENVIRONMENT=production` the process refuses to start if it is missing.
- `ENVIRONMENT=production` disables `.env` fallback: runtime config must come from the
  ConfigMap and Secret.

### Deploy

```bash
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/secret.yaml       # create from secret.example.yaml
kubectl apply -f k8s/networkpolicy.yaml
kubectl apply -f k8s/service.yaml
kubectl apply -f k8s/deployment-api.yaml
kubectl apply -f k8s/job-migrate.yaml  # run Alembic migrations
```

### Replicas must stay at 1

`deployment-api.yaml` pins `replicas: 1`, and that is not a placeholder. The service
keeps state **in the process**:

| Process-local state | Where | What breaks with 2+ replicas |
| --- | --- | --- |
| Idempotency cache | `src/thinkback/api/idempotency.py` | the same `Idempotency-Key` is accepted and executed again on the other pod |
| Rate-limit buckets | `src/thinkback/api/ratelimit.py` | buckets are keyed per client IP per process, so the effective quota doubles |
| L1 short-term cache | `src/thinkback/memory/repositories/_l1_cache.py` | the old pod can still recall memories that were deleted (resurrection) |
| Mem0 `history.db` | `src/thinkback/infra/config.py` (`mem0_history_db_path`) | each pod keeps its own local SQLite file; erasure has to be replayed per pod |
| Sliding-window metrics | `src/thinkback/api/metrics.py` | counters are in-process and reset on restart; multi-pod values need external aggregation |

Do not raise `replicas` until all of the above live in shared storage (Redis /
PostgreSQL).

### Rollout strategy

The deployment uses `strategy: Recreate`. A rolling update with `maxSurge: 1` brings
the new pod up and lets it become Ready **while the old pod is still serving**, so
traffic splits across two pods with two divergent copies of the state above.

`Recreate` tears the old pod down first — it is dropped from the Service endpoints and
stops accepting new requests on SIGTERM — before the replacement starts, so two pods
never serve traffic at once. (The terminating old process may still drain in-flight
requests while the new pod comes up.) The trade-off is a short outage on every
release — unavoidable for a single-replica deployment, and preferable to silently
splitting state.

### Health endpoints

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | Basic process info (version, environment) |
| `GET /health/live` | Liveness probe |
| `GET /health/ready` | Readiness probe (PostgreSQL / Milvus / Mem0) |
| `GET /health/metrics` | Prometheus metrics (text exposition) |

### Prometheus metrics

| Metric | Type | Description |
| --- | --- | --- |
| `thinkback_request_total` | Counter | Outcomes since process start, label `kind` ∈ `append_ok`, `append_fail`, `recall_ok`, `recall_fail` |
| `thinkback_request_per_minute` | Gauge | Rate over the rolling 5-minute window |
| `thinkback_request_window_5m` | Gauge | Count inside the rolling 5-minute window |

These are the only metrics the service exposes, and they are per-process: with more
than one replica the Prometheus side has to aggregate across pods.

### Verify the network boundary

Run the probe from a namespace **with** the label (must succeed) and from one
**without** it (must time out or be refused). The Service FQDN is
`thinkback.<thinkback namespace>.svc.cluster.local`:

```bash
# inside the boundary — expect {"status":"alive"}
kubectl run netpol-ok --rm -it --restart=Never --image=curlimages/curl \
  --namespace=<labelled gateway namespace> -- \
  curl -sS -m 5 http://thinkback.<thinkback namespace>.svc.cluster.local:8000/health/live

# outside the boundary — expect a timeout or "connection refused"
kubectl run netpol-deny --rm -it --restart=Never --image=curlimages/curl \
  --namespace=<namespace without the label> -- \
  curl -sS -m 5 http://thinkback.<thinkback namespace>.svc.cluster.local:8000/health/live
```

Both succeeding means the CNI is not enforcing NetworkPolicy (or the label was never
applied). Note that kubelet's own probes are exempt by design, so the deployment stays
healthy even when the policy denies everything else.

## Configuration

See [README](../README.md#configuration). Key environment variables:

| Variable | Default | Notes |
| --- | --- | --- |
| `MEMORY_LLM_KEY` | — | Required |
| `MEMORY_LLM_BASE_URL` | — | OpenAI-compatible |
| `MEMORY_EMBEDDING_API_KEY` | — | Optional; empty when the endpoint needs no auth |
| `MILVUS_URL` | `http://localhost:19530` | |
| `MEMORY_L3_WRITE_MODE` | `async` | `sync` for debugging |
| `MEMORY_L3_EXECUTOR_WORKERS` | `16` | Scale for throughput |
| `GRPC_ENABLED` | `true` | Set `false` to disable gRPC |
| `ADMIN_TOKEN` | — | Required when `ENVIRONMENT=production`; guards `/admin/api/*` |

## Scaling

- **Vertical**: raise the CPU/memory requests and limits in `deployment-api.yaml`.
- **L3 queue**: adjust `MEMORY_L3_MAX_PENDING_TASKS` and `MEMORY_L3_EXECUTOR_WORKERS`.
- **Horizontal**: **not yet** — see [Replicas must stay at 1](#replicas-must-stay-at-1).
  Moving the process-local state to shared storage first is the prerequisite.
- **Database**: PostgreSQL can be external RDS / Cloud SQL.
- **Milvus**: use managed Milvus (Zilliz Cloud) for production.

## Monitoring

- Scrape `/health/metrics` from a namespace allowed by `networkpolicy.yaml`.
- Structured JSON logs with `trace_id` for request correlation.
- Alerts: request error rate (from `/health/metrics`), readiness failures and L3 queue
  depth (from the structured logs and the admin API — they are not exposed as
  Prometheus metrics).

See [Operations runbook](RUNBOOK.md) for debugging and recovery.
