# thinkback Kubernetes 资源

本目录提供 `thinkback` 的 Kubernetes 基线资源，包括 API Deployment、
迁移 Job、Service、ConfigMap 和 Secret 示例。

生产环境需要由平台或运维先准备 PostgreSQL、OpenAI-compatible LLM endpoint、
独立 OpenAI-compatible embedding endpoint，以及独立共享 Milvus 服务。
Mem0 以 Library 方式运行在 `thinkback` 进程内，并通过 Milvus 承载 L3 向量写入和语义检索。
短期记忆（L1/L2）只落 PostgreSQL，服务不依赖 Redis 等外部缓存，也没有独立 worker 进程。

thinkback 是 Thinkback 内部服务，进程内不做调用方鉴权；公网入口、流量准入由网关、mTLS、服务身份或内网 ACL 在上游解决。
本服务调用 LLM、Embedding、Milvus、PostgreSQL 等外部依赖必须的凭据由 Secret 注入。
认证边界以 [Thinkback记忆三层架构](../docs/memory/Thinkback记忆三层架构.md) 第 0.1 节为准，本文不重复定义。

## 构建镜像

```bash
docker build -t thinkback:0.1.0 .
```

## 应用运行配置

```bash
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/secret.example.yaml
```

`secret.example.yaml` 只说明字段结构，离开本地集群前必须替换为真实 Secret 管理方案。
生产 Pod 不读取本地 `.env` 文件，运行配置必须来自 `ConfigMap` 和 `Secret`，镜像内不能包含本地调试密钥。

默认 ConfigMap 值对齐当前 P0 压测基线：

| 变量 | 中文解释 |
| --- | --- |
| `APP_NAME` | 应用名称，日志、健康检查和部署识别使用。 |
| `APP_VERSION` | 应用版本，应与镜像 tag、digest 或发布版本保持一致。 |
| `ENVIRONMENT` | 运行环境；Kubernetes 生产环境必须为 `production`。 |
| `DEBUG` | 是否开启调试模式；生产不应开启。 |
| `LOG_LEVEL` | 日志级别；生产默认 `INFO`。 |
| `POSTGRES_HOST` | PostgreSQL 主机地址。 |
| `POSTGRES_PORT` | PostgreSQL 端口。 |
| `POSTGRES_USER` | PostgreSQL 用户名。 |
| `POSTGRES_PASSWORD` | PostgreSQL 密码；生产必须通过 Secret 注入。 |
| `POSTGRES_DATABASE` | PostgreSQL 数据库名，当前为 `thinkback`。 |
| `MILVUS_URL` | Milvus 服务地址；生产不要使用 `localhost`。 |
| `MILVUS_DATABASE` | Milvus database 名称。 |
| `MILVUS_USER` | Milvus 用户名；无鉴权时可为空。 |
| `MILVUS_PASSWORD` | Milvus 密码；无鉴权时可为空。 |
| `MEMORY_LLM_KEY` | LLM API key；生产必须通过 Secret 注入。 |
| `MEMORY_LLM_BASE_URL` | OpenAI-compatible LLM endpoint 地址。 |
| `MEMORY_EMBEDDING_BASE_URL` | OpenAI-compatible Embedding endpoint 地址。 |
| `MEMORY_EMBEDDING_API_KEY` | Embedding API key；当前 endpoint 不需要鉴权时留空。 |
| `MEMORY_MILVUS_COLLECTION` | Mem0 写入 Milvus 的 collection 名称，当前为 `thinkback`。 |
| `MEMORY_EMBEDDING_DIMS` | Embedding 向量维度，必须与 embedding 服务输出一致。 |
| `MEMORY_LLM_MODEL` | LLM 模型名称，当前为 `qwen-plus-latest`。 |
| `MEMORY_EMBEDDING_MODEL` | Embedding 模型名称，当前为 `default-embedding`。 |
| `MEM0_HISTORY_DB_PATH` | Mem0 Library 本地历史数据库路径，需要挂载可写目录。 |
| `MEMORY_L3_WRITE_MODE` | L3 写入模式；`async` 表示后台抽取长期记忆。 |
| `MEMORY_API_WORKER_LIMIT` | `/memory/*` 读池和写池各自的同步工作线程并发上限，当前基线为 `8`。 |
| `MEMORY_API_WORKER_WAIT_SECONDS` | `/memory/*` 路由进入同步 worker 并等待返回的总时间窗口，当前基线为 `5`。 |
| `MEMORY_L3_EXECUTOR_WORKERS` | L3 后台抽取线程数，当前基线为 `16`。 |
| `MEMORY_L3_MAX_PENDING_TASKS` | L3 后台写入队列容量，当前基线为 `256`。 |
| `MEMORY_L3_QUEUE_WAIT_SECONDS` | L3 队列满前的最长等待秒数，当前基线为 `5`。 |
| `MEMORY_BACKEND_MAX_CONCURRENT_CALLS` | 单进程内 Mem0 Library 后端调用并发上限，当前基线为 `4`。 |
| `READINESS_TIMEOUT_SECONDS` | 单个依赖 readiness 检查超时时间，当前基线为 `3` 秒。 |

这些基线值不是线上容量承诺。真实流量、Pod 资源和外部模型限流明确后，需要重新校准。

## 部署

```bash
kubectl apply -f k8s/job-migrate.yaml
kubectl wait --for=condition=complete job/thinkback-migrate --timeout=120s
kubectl apply -f k8s/service.yaml
kubectl apply -f k8s/deployment-api.yaml
```

## 验证

```bash
kubectl rollout status deployment/thinkback-api
kubectl port-forward service/thinkback 8000:8000
curl http://127.0.0.1:8000/health/live
curl http://127.0.0.1:8000/health/ready
```
