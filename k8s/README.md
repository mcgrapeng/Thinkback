# Thinkback Kubernetes Assets

These manifests provide a baseline API and worker deployment for Thinkback.
They assume PostgreSQL, Redis, a remote Mem0 REST API, and an independent shared
Qdrant service are available through managed services addressed by the ConfigMap
and Secret. Thinkback only probes Qdrant readiness; Mem0 owns L3 vector writes
and semantic search.

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
