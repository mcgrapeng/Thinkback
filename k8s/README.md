# thinkback Kubernetes 资源

本目录提供 `thinkback` 的 Kubernetes 基线资源，包括 API Deployment、
迁移 Job、Service、NetworkPolicy、ConfigMap 和 Secret 示例。

生产环境需要由平台或运维先准备 PostgreSQL、OpenAI-compatible LLM endpoint、
独立 OpenAI-compatible embedding endpoint，以及独立共享 Milvus 服务。
Mem0 以 Library 方式运行在 `thinkback` 进程内，并通过 Milvus 承载 L3 向量写入和语义检索。
短期记忆（L1/L2）只落 PostgreSQL，服务不依赖 Redis 等外部缓存，也没有独立 worker 进程。

thinkback 是 Thinkback 内部服务，进程内不做调用方鉴权；公网入口、流量准入由网关、mTLS、服务身份或内网 ACL 在上游解决。
本服务调用 LLM、Embedding、Milvus、PostgreSQL 等外部依赖必须的凭据由 Secret 注入。
认证边界以 [Thinkback记忆三层架构](../docs/memory/Thinkback记忆三层架构.md) 第 0.1 节为准，本文不重复定义。

## 网络边界（必读）

业务面没有鉴权，意味着「集群内谁能连到 `Service/thinkback`」就是唯一边界。
`k8s/networkpolicy.yaml` 用两条策略把这层边界显式写出来：

1. `thinkback-api-default-deny-ingress`：对 API Pod（`app=thinkback, component=api`）
   默认拒绝一切入站流量；
2. `thinkback-api-allow-caller-namespaces`：只放行带
   `thinkback.io/allowed-caller-namespace=true` 标签的 namespace 到 8000（HTTP）
   与 50051（gRPC）。

部署方在上线前必须做的事（**不用改 manifest**，给网关 namespace 打标签即可）：

```bash
kubectl label namespace <你的网关 namespace> thinkback.io/allowed-caller-namespace=true
```

如果集群不允许给 namespace 打标签，就改 `k8s/networkpolicy.yaml` 里 allow 策略的
`namespaceSelector.matchLabels`——具体是 `k8s/networkpolicy.yaml:67`，把

```yaml
              thinkback.io/allowed-caller-namespace: "true"
```

换成

```yaml
              kubernetes.io/metadata.name: <your-gateway-ns>
```

（该文件里只有这一处需要改，位置在 `# >>> 部署方改这里 <<<` 注释下方。）

注意：

- 集群 CNI 必须实现 NetworkPolicy（Calico / Cilium / Antrea 等）。不支持时
  本文件不生效，服务仍是「集群内任意 Pod 可调」。
- 本文件只收口入口，不收口出口。API 出站还要连 PostgreSQL / Milvus / LLM /
  Embedding / DNS，这些地址与部署相关，需要平台方按真实地址另写 egress 策略。
- kubelet 探针不受影响（k8s 规定到 Pod 所在节点的流量始终放行）。
- 监控抓 `/health/metrics` 也要从被放行的 namespace 发起，否则抓不到。

## 单副本约束

`deployment-api.yaml` 的 `replicas` 固定为 1，**在下面这些状态搬到共享存储
（Redis / PostgreSQL）之前不要改**：

| 进程内状态 | 位置 | 多副本后果 |
| --- | --- | --- |
| 幂等缓存 | `src/thinkback/api/idempotency.py` | 同一个 `Idempotency-Key` 在两个 Pod 上都被执行 |
| 限流桶 | `src/thinkback/api/ratelimit.py` | 按客户端 IP 分桶，配额实际翻倍 |
| L1 短期记忆缓存 | `src/thinkback/memory/repositories/_l1_cache.py` | 旧 Pod 的缓存可能把已删除的记忆召回回来（复活） |
| Mem0 history.db | `src/thinkback/infra/config.py`（`mem0_history_db_path`） | 每个 Pod 各一份本地 SQLite，多副本擦除要逐副本做 |
| 滑动窗口 metrics | `src/thinkback/api/metrics.py` | 进程内计数，重启清零，多副本需外部聚合 |

发布策略用 `strategy: Recreate`：旧 Pod 先退出 Service endpoints、SIGTERM 后不再接新
请求，再起新 Pod，任何时刻不会有两个 Pod 同时对外服务，也就不会出现两个 Pod 共享
一套被劈开的进程内状态。代价是每次发布有短暂不可用——单副本本来就无法零停机滚动，
这个取舍是刻意的。`RollingUpdate` + `maxSurge: 1` 会让新 Pod 先 Ready、旧 Pod 还在
服务，流量被劈成两半，是不能接受的。

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

## 接入新企业（加一个 namespace，不用重建镜像）

记忆类型 profile 决定「这个 namespace 记什么、怎么冲突、留多久」。它是**运行时**加载的
（`load_registry` 每次读盘，无缓存），所以新企业接入是加一份配置，不是改代码、也不是重建镜像。

镜像里自带 `profiles/semiconductor.json` 与 `profiles/prompts/semiconductor/*.md`。
新企业把自己的 profile 以 ConfigMap 挂进去即可（`subPath` 挂单个文件 + 目录挂指令文件）：

```yaml
# k8s/overlays/acme/patch-deployment.yaml（overlay 由平台维护，不进主干）
      volumeMounts:
        - name: tmp
          mountPath: /tmp/thinkback
        # profile 本体：<profile_dir>/<namespace>.json
        - name: acme-profile
          mountPath: /app/profiles/acme.json
          subPath: acme.json
        # per-type 抽取指令：<profile_dir>/prompts/<namespace>/*.vN.md
        - name: acme-prompts
          mountPath: /app/profiles/prompts/acme
      volumes:
        - name: tmp
          emptyDir: {}
        - name: acme-profile
          configMap:
            name: thinkback-profiles-acme
        - name: acme-prompts
          configMap:
            name: thinkback-profiles-acme
```

```yaml
# ConfigMap thinkback-profiles-acme
apiVersion: v1
kind: ConfigMap
metadata:
  name: thinkback-profiles-acme
data:
  acme.json: |
    { "namespace": "acme", "types": { ... } }
  profile.v1.md: |
    <该类型的抽取指令正文>
```

ConfigMap 的 key 只能是扁平名字，所以指令文件靠**目录挂载**落到
`/app/profiles/prompts/acme/`；profile 本体靠 `subPath` 落到 `/app/profiles/acme.json`。
`subPath` 挂载在 ConfigMap 更新后不会自动刷新 —— 换 profile 要重建 Pod。

要点：

- 生产环境下 namespace 缺 profile 会**启动期/调用期显式报错**（不静默回落内置默认类型），
  这正是「配置看起来生效了、实际跑的是另一套规则」的防线。
- `MEMORY_TYPE_PROFILE_DIR` 可以把整个 profile 目录指到别处（例如企业自己的挂载点），
  指走后镜像内置的 `profiles/` 不再生效。
- 换 namespace 只需改调用方的 `namespace` 字段；MCP 场景下 namespace 由服务端固定，
  见 [docs/MCP.md](../docs/MCP.md)。

## 部署

```bash
kubectl apply -f k8s/job-migrate.yaml
kubectl wait --for=condition=complete job/thinkback-migrate --timeout=120s
kubectl apply -f k8s/service.yaml
kubectl apply -f k8s/networkpolicy.yaml
kubectl apply -f k8s/deployment-api.yaml
```

`networkpolicy.yaml` 建议和 Service、Deployment 一起 apply：先落默认拒绝策略，
再放行批准 namespace，避免中间存在一个「谁都能调」的窗口。

## 验证

```bash
kubectl rollout status deployment/thinkback-api
kubectl port-forward service/thinkback 8000:8000
curl http://127.0.0.1:8000/health/live
curl http://127.0.0.1:8000/health/ready
```

验证网络边界。`thinkback` Service 的 FQDN 是
`thinkback.<thinkback 所在 namespace>.svc.cluster.local`，从两个不同的 namespace 各发一次：

```bash
# 边界内（打了标签的网关 namespace）—— 应当成功返回 {"status":"alive"}
kubectl run netpol-ok --rm -it --restart=Never --image=curlimages/curl \
  --namespace=<打了标签的网关 namespace> -- \
  curl -sS -m 5 http://thinkback.<thinkback 所在 namespace>.svc.cluster.local:8000/health/live

# 边界外（未打标签的 namespace）—— 应当超时或连接被拒
kubectl run netpol-deny --rm -it --restart=Never --image=curlimages/curl \
  --namespace=<未打标签的 namespace> -- \
  curl -sS -m 5 http://thinkback.<thinkback 所在 namespace>.svc.cluster.local:8000/health/live
```

`curlimages/curl` 未安装时用同镜像的等价物（`busybox wget` 等）也可以，重点是
**两个 namespace 的结果必须不同**；如果都能通，说明 CNI 没实现 NetworkPolicy 或
标签没打上。

