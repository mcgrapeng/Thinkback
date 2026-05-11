# Thinkback Kubernetes Assets

这些 manifest 是 Thinkback 的生产部署入口。它们假设 PostgreSQL、Redis、
OpenAI 兼容模型服务和独立 Qdrant 已由平台提供，并通过 ConfigMap 和
Secret 注入。Mem0 以 Library 方式运行在 API Pod 内，负责 L3 向量写入和语义搜索。

## Build Image

```bash
docker build -t thinkback:0.1.0 .
```

## Runtime Settings

生产数据库名统一为 `liaoriver_memory`。应用到真实集群前，必须替换服务地址、镜像 tag 和 Secret 值。

`secret.example.yaml` 只是模板。真实密钥应放在集群 Secret、SealedSecret 或外部 Secret 管理流程中。

ConfigMap 中的并发值是基线限制：
`MEMORY_API_WORKER_LIMIT=8`, `MEMORY_L3_EXECUTOR_WORKERS=16`,
`MEMORY_L3_MAX_PENDING_TASKS=256`, `MEMORY_L3_QUEUE_WAIT_SECONDS=5`, and
`READINESS_TIMEOUT_SECONDS=30`。这些值不是线上容量承诺，需要结合真实流量、Pod 资源和外部模型限流重新校准。

## Deploy

```bash
kubectl apply -k k8s
kubectl wait --for=condition=complete job/thinkback-migrate --timeout=120s
```

## Verify

```bash
kubectl rollout status deployment/thinkback-api
kubectl port-forward service/thinkback 8000:8000
curl http://127.0.0.1:8000/health/live
curl http://127.0.0.1:8000/health/ready
```
