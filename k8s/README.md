# Thinkback Kubernetes Assets

These manifests provide a baseline API and worker deployment for Thinkback.
They assume PostgreSQL, Redis, OpenAI, and an independent shared Qdrant service
are available through managed services addressed by the ConfigMap and Secret.
Mem0 runs as a Library inside Thinkback and owns L3 vector writes and semantic
search through Qdrant.

## Build Image

```bash
docker build -t thinkback:0.1.0 .
```

## Apply Runtime Settings

```bash
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/secret.example.yaml
```

Replace values in `secret.example.yaml` before using it outside a local cluster.

The default ConfigMap values are aligned with the current P0 pressure-test
baseline: `MEMORY_L3_EXECUTOR_WORKERS=16`,
`MEMORY_L3_MAX_PENDING_TASKS=256`, `MEMORY_L3_QUEUE_WAIT_SECONDS=5`, and
`READINESS_TIMEOUT_SECONDS=30`. These values are not an online capacity
promise; recalibrate them after real traffic, pod resources, and external model
rate limits are known.

## Deploy

```bash
kubectl apply -f k8s/service.yaml
kubectl apply -f k8s/deployment-api.yaml
kubectl apply -f k8s/deployment-worker.yaml
```

## Verify

```bash
kubectl rollout status deployment/thinkback-api
kubectl rollout status deployment/thinkback-worker
kubectl port-forward service/thinkback 8000:8000
curl http://127.0.0.1:8000/health/live
curl http://127.0.0.1:8000/health/ready
```
