# Innies记忆服务部署与运维交付文档

本文档合并原部署指南与运维交付文档，同时保留本地部署、生产配置、主链路验证、运维交付、上线验证、监控告警和回滚内容。

## 1. 部署指南

本文档说明 Innies 记忆服务的本地部署、配置、主链路验证和评测入口。

### 1.1 运行组件

首版运行至少需要：

| 组件 | 用途 |
| --- | --- |
| FastAPI app | 对外提供 `/memory/*` 和健康检查 |
| PostgreSQL | 保存 L2、可靠轮次、L3 业务索引和任务状态 |
| Redis | Celery broker/result backend 和运行时基础设施 |
| Mem0 Library | 嵌入 innies-memory 进程内的 L3 长期记忆引擎 |
| Milvus | 独立向量数据库，Mem0 Library 用它承载 L3 向量，innies-memory 也会做就绪探测 |
| LLM 服务 | OpenAI-compatible endpoint，Mem0 Library 用它做记忆抽取和整理 |
| Embedding 服务 | OpenAI-compatible endpoint，当前 embedding endpoint 独立于 LLM endpoint，且不需要 API key |

副本部署边界：

- V1 阶段并发安全主要靠 PostgreSQL active-only 唯一索引 `uq_ins_memory_active_backend_scope` 兜底，不依赖应用层分布式锁；
- 进程内 `_index_mutation_lock` 仅在**单进程多线程**内减少 `IntegrityError` 噪声，多副本时该 lock 不跨进程生效；
- V1 推荐**单副本**或上游网关**会话粘性（sticky session）**部署，保证同一 `user_id` 路由到同一 Pod，减少跨 Pod 并发写同一记忆的概率；
- 多副本部署仍然业务正确（唯一索引保护），但可能出现 `IntegrityError` 日志噪声升高、删除/重建 L3 抽取竞态触发更多 `SUPERSEDED` 替换；
- 真要做多副本且高并发，V2 / V3 计划改用 PG advisory lock 或 Redis 分布式锁，当前不在 V1 范围内。

### 1.2 环境变量

innies-memory 是 Innies 域内的内部服务，进程内不做调用方鉴权；服务调用 LLM、Embedding、Milvus、PostgreSQL、Redis 等外部依赖时仍必须持有显式凭据。
认证边界以 [Innies记忆三层架构](Innies记忆三层架构.md) 第 0.1 节为准，本节只列具体环境变量。

最小配置见 `.env.example`。生产环境必须显式设置：

```bash
APP_NAME=innies-memory
APP_VERSION=0.1.0
ENVIRONMENT=production
DEBUG=false
LOG_LEVEL=INFO

POSTGRES_HOST=
POSTGRES_PORT=
POSTGRES_USER=
POSTGRES_PASSWORD=
POSTGRES_DATABASE=

REDIS_HOST=
REDIS_PORT=
REDIS_DB=
REDIS_PASSWORD=
CELERY_BROKER_URL=
CELERY_RESULT_BACKEND=
CELERY_TASK_TIME_LIMIT=3600
CELERY_TASK_SOFT_TIME_LIMIT=3000

MILVUS_URL=http://milvus.example.internal:19530
MILVUS_DATABASE=default
MILVUS_USER=
MILVUS_PASSWORD=

OPENAI_API_KEY=<secret>
MEMORY_LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
MEMORY_EMBEDDING_BASE_URL=http://<embedding-host>:7345/v1
MEMORY_EMBEDDING_API_KEY=
MEMORY_MILVUS_COLLECTION=innies_memory
MEMORY_EMBEDDING_DIMS=1024
MEMORY_LLM_MODEL=qwen-plus-latest
MEMORY_EMBEDDING_MODEL=zhiman-embedding
MEM0_HISTORY_DB_PATH=/tmp/innies-memory/mem0/history.db
MEMORY_L3_WRITE_MODE=async
MEMORY_API_WORKER_LIMIT=8
MEMORY_API_WORKER_WAIT_SECONDS=5
MEMORY_L3_EXECUTOR_WORKERS=16
MEMORY_L3_MAX_PENDING_TASKS=256
MEMORY_L3_QUEUE_WAIT_SECONDS=5
MEMORY_BACKEND_MAX_CONCURRENT_CALLS=4
READINESS_TIMEOUT_SECONDS=3
```

环境变量说明：

| 变量 | 中文解释 |
| --- | --- |
| `APP_NAME` | 应用名称，日志、健康检查和部署识别使用 |
| `APP_VERSION` | 应用版本，应与镜像版本或发布版本保持一致 |
| `ENVIRONMENT` | 运行环境；本地为 `development`，生产为 `production` |
| `DEBUG` | 是否开启调试模式；生产必须关闭 |
| `LOG_LEVEL` | 日志级别；生产默认 `INFO` |
| `POSTGRES_HOST` | PostgreSQL 主机地址 |
| `POSTGRES_PORT` | PostgreSQL 端口 |
| `POSTGRES_USER` | PostgreSQL 用户名 |
| `POSTGRES_PASSWORD` | PostgreSQL 密码；生产必须通过 Secret 注入 |
| `POSTGRES_DATABASE` | PostgreSQL 数据库名，当前为 `innies-memory` |
| `REDIS_HOST` | Redis 主机地址 |
| `REDIS_PORT` | Redis 端口 |
| `REDIS_DB` | Redis DB 编号 |
| `REDIS_PASSWORD` | Redis 密码；无密码时留空 |
| `CELERY_BROKER_URL` | Celery broker URL；Redis 有密码时必须带认证 |
| `CELERY_RESULT_BACKEND` | Celery result backend URL；Redis 有密码时必须带认证 |
| `CELERY_TASK_TIME_LIMIT` | Celery 单任务硬超时，当前只约束 worker 诊断和后续异步扩展任务 |
| `CELERY_TASK_SOFT_TIME_LIMIT` | Celery 单任务软超时，必须小于硬超时 |
| `MILVUS_URL` | Milvus 服务地址；生产不要使用 `localhost` |
| `MILVUS_DATABASE` | Milvus database 名称，代码默认值为 `default`，本地示例和生产配置应按实际 Milvus 规划覆盖 |
| `MILVUS_USER` | Milvus 用户名；无鉴权时留空 |
| `MILVUS_PASSWORD` | Milvus 密码；无鉴权时留空 |
| `OPENAI_API_KEY` | LLM API key；生产必须通过 Secret 注入 |
| `MEMORY_LLM_BASE_URL` | OpenAI-compatible LLM endpoint 地址 |
| `MEMORY_EMBEDDING_BASE_URL` | OpenAI-compatible Embedding endpoint 地址 |
| `MEMORY_EMBEDDING_API_KEY` | Embedding API key；当前 endpoint 不需要鉴权时留空 |
| `MEMORY_MILVUS_COLLECTION` | Mem0 写入 Milvus 的 collection 名称，代码默认值为 `innies_memory` |
| `MEMORY_EMBEDDING_DIMS` | Embedding 向量维度，必须与服务端输出一致 |
| `MEMORY_LLM_MODEL` | LLM 模型名称 |
| `MEMORY_EMBEDDING_MODEL` | Embedding 模型名称 |
| `MEM0_HISTORY_DB_PATH` | Mem0 Library 本地历史数据库路径，需要进程可写 |
| `MEMORY_L3_WRITE_MODE` | L3 写入模式；`async` 表示后台抽取长期记忆 |
| `MEMORY_API_WORKER_LIMIT` | API 读池和写池各自的同步工作线程并发上限 |
| `MEMORY_API_WORKER_WAIT_SECONDS` | API 同步工作进入 worker 并等待返回的总时间窗口；超过后返回 503 背压错误 |
| `MEMORY_L3_EXECUTOR_WORKERS` | L3 后台抽取线程数 |
| `MEMORY_L3_MAX_PENDING_TASKS` | L3 后台写入队列容量 |
| `MEMORY_L3_QUEUE_WAIT_SECONDS` | L3 队列满前的最长等待秒数 |
| `MEMORY_BACKEND_MAX_CONCURRENT_CALLS` | 单进程内 Mem0 Library 后端调用并发上限，用于限制 LLM、Embedding 和 Milvus 压力 |
| `READINESS_TIMEOUT_SECONDS` | 单个依赖 readiness 检查超时时间，单位秒；K8s readinessProbe `timeoutSeconds` 必须大于等于该值 |

Mem0 不再作为独立 REST Server 部署。
innies-memory 通过 `mem0ai` Library 直接调用 Mem0 能力，Mem0 Library 再访问 LLM endpoint、Embedding endpoint 和 Milvus。
生产不要把 `MILVUS_URL` 写死为 `localhost`。
应使用内网域名或服务发现地址，例如 `http://milvus.example.internal:19530`。

`MEMORY_LLM_BASE_URL` 用于 OpenAI-compatible LLM endpoint。
`MEMORY_EMBEDDING_BASE_URL` 用于 OpenAI-compatible embedding endpoint。
如果 embedding 服务不需要鉴权，`MEMORY_EMBEDDING_API_KEY` 可以留空。
应确保 `MEMORY_LLM_MODEL`、`MEMORY_EMBEDDING_MODEL` 和 `MEMORY_EMBEDDING_DIMS` 与服务端实际模型一致。

如果 Redis 启用密码，`REDIS_PASSWORD`、`CELERY_BROKER_URL / CELERY_RESULT_BACKEND` 必须同步配置。
Celery URL 应包含认证信息，例如 `redis://:<password>@redis:6379/0` 和 `redis://:<password>@redis:6379/1`。

本地调试使用 `.env` 或控制台环境变量，并以 `.env.example` 作为模板。
`.env` 已被 `.gitignore` 和 `.dockerignore` 忽略，只保存本机调试密钥和本机端口，不进入 Git，也不进入镜像。
本地优先使用共享 `liaohe-postgresql`，保持本机各工程 PostgreSQL 口径一致。
compose 自带 PostgreSQL 仅用于隔离验证或 CI 类临时环境；选择这条路线时，可以用 `docker compose up -d postgres redis` 启动依赖，并必须确认 `.env` 指向 compose 暴露端口。
API、worker 和脚本可以用控制台命令启动。

生产进程在 `ENVIRONMENT=production` 时不读取 `.env`。
生产配置通过 `k8s/configmap.yaml` 和 Kubernetes Secret 注入，API、worker 和迁移 Job 都使用 `envFrom` 读取同一组 ConfigMap/Secret。
生产镜像只包含应用代码、Alembic 迁移和运行时依赖，不复制本地 `.env`。

Milvus 按独立基础设施配置，未来可以与 innies-memory 部署在不同主机上。
Mem0 Library 负责 L3 向量写入和语义检索。
业务路径必须遵守 [技术栈选型](Innies记忆服务技术栈选型.md) 第 2 节的 Mem0 边界。
innies-memory 的 readiness 会通过 Milvus SDK 执行 `list_collections()` 做只读连通性诊断，
也会构造 Mem0 Library 后端验证配置完整性和适配器可用性，不执行 add/delete 写入探针。
该检查用于暴露 LLM、Embedding、Mem0 或 Milvus 链路不可用，不是业务写入旁路。

生产如果启用 Milvus 鉴权，应配置 `MILVUS_USER` 和 `MILVUS_PASSWORD`。
本地无鉴权验证可以使用 `MILVUS_URL=http://localhost:19530` 且不填账号密码。

`MEM0_HISTORY_DB_PATH` 是 Mem0 Library 的本地历史数据库路径。
本地开发默认 `MEM0_HISTORY_DB_PATH=.mem0/history.db`，便于把 Mem0 历史库留在仓库工作目录下的未跟踪路径。
容器和生产建议 `MEM0_HISTORY_DB_PATH=/tmp/innies-memory/mem0/history.db`，并挂载可写目录，避免应用进程没有写权限。
当前 `k8s/deployment-api.yaml` 和 `k8s/deployment-worker.yaml` 把 `/tmp/innies-memory` 声明为 `emptyDir` 卷，Pod 重建后该路径会被清空，Mem0 Library 的历史数据库会重新初始化。
这只影响 Mem0 Library 内部的去重和增量历史，不会丢失业务记忆：L3 业务索引保存在 PostgreSQL `ins_memory` 表，长期向量保存在 Milvus，召回链路不依赖 `history.db`。
如需把 Mem0 历史库做持久化，应在 deployment 中把 `/tmp/innies-memory` 改为持久化卷，并由运维评估容量和备份策略，不在首版交付范围内。

`MEMORY_L3_WRITE_MODE=async` 是首版推荐配置。
append 请求同步完成可靠轮次存储、L1/L2 更新和P0 槽位索引。
Mem0 L3 抽取在后台任务中沉淀，避免外部 LLM/Embedding 抖动把写入接口拖到超时。

注：下面几个并发参数不是线上容量承诺，只是当前 P0 压测基线。

| 变量 | 直白解释 |
| --- | --- |
| `MEMORY_API_WORKER_LIMIT` | `/memory/*` 读池和写池各自进入线程池的并发上限；调大后外部依赖压力也会增加 |
| `MEMORY_API_WORKER_WAIT_SECONDS` | `/memory/*` 路由进入同步 worker 并等待返回的总时间窗口；超过后返回可重试的 503，worker 线程可能仍在继续执行 |
| `MEMORY_L3_EXECUTOR_WORKERS` | L3 后台抽取线程数，影响 Mem0 写入并发，不影响 append 已经完成可靠轮次存储这件事 |
| `MEMORY_L3_MAX_PENDING_TASKS` | L3 后台写入队列容量，队列满时 append 会触发背压，避免无限堆积 |
| `MEMORY_L3_QUEUE_WAIT_SECONDS` | 队列接近满时最多等待多久，超过后仍无容量，会返回明确错误而不是静默丢任务 |
| `MEMORY_BACKEND_MAX_CONCURRENT_CALLS` | 单个 API 进程同时进入 Mem0 Library 的调用数；调大前必须确认 LLM、Embedding、Milvus 和主机线程资源可承载 |
| `READINESS_TIMEOUT_SECONDS` | `/health/ready` 检查数据库、Redis、Milvus 和 Mem0 Library 配置时的单项依赖超时时间；Mem0 Library 检查不执行 add/delete 写入探针；K8s readinessProbe `timeoutSeconds` 必须同步设置为不小于该值，否则 kubelet 会先终止 HTTP probe |

调高 `MEMORY_API_WORKER_LIMIT` 会让更多读写同步工作同时执行；当前实现读池和写池各使用这一上限。
如需更保守的背压，可以调小 `MEMORY_API_WORKER_WAIT_SECONDS`，但它同时约束等待 worker 和同步调用返回时间，不应把它设为无限等待。
需要同步观察数据库、Mem0、LLM 和 Embedding 服务压力。
生产前压测不要依赖本机 `.env`，应由发布环境显式注入上述脚本变量和运行时变量。

### 1.3 本地启动

安装依赖：

```bash
poetry install
```

启动依赖：

```bash
docker compose up -d postgres redis
```

这条 compose 路线只适合隔离验证。
日常本地联调仍优先使用共享 `liaohe-postgresql`，只需要确认共享 PostgreSQL、Redis、Milvus 和模型 endpoint 可达，再启动 API。

执行数据库迁移：

```bash
.venv/bin/alembic upgrade head
```

启动 API：

```bash
.venv/bin/uvicorn innies_memory.api.app:app --app-dir src --host 0.0.0.0 --port 8000
```

可选启动 Celery worker：

```bash
.venv/bin/celery -A innies_memory.infra.tasks.celery_app.celery_app worker -l info
```

当前 L3 `MEMORY_L3_WRITE_MODE=async` 使用 API 进程内后台执行器抽取，不依赖 Celery worker 消费。
Celery worker 目前只承载诊断任务和后续异步任务扩展入口；只有部署环境明确启用 Celery 任务时才是必需组件。

### 1.4 图形化 API 文档

启动 API 后，可以通过 FastAPI 自动生成的 OpenAPI 文档查看和调试接口。

| 入口 | 地址 | 用途 |
| --- | --- | --- |
| Swagger UI | `http://localhost:8000/docs` | 图形化查看接口、请求体、响应体，并可直接发起调试请求 |
| ReDoc | `http://localhost:8000/redoc` | 以文档阅读方式查看 API 分组、模型和字段 |
| OpenAPI JSON | `http://localhost:8000/openapi.json` | 给自动化工具、SDK 生成器或接口校验工具使用 |

图形化 API 文档只描述 HTTP 接口契约。
真实质量、性能与稳定性结论应以对应脚本输出和发布归档为准。

当前 HTTP 主链路接口如下：

| 接口 | 用途 |
| --- | --- |
| `POST /memory/append` | 写入一个完整 user -> assistant 轮次，生成可靠轮次记录，并触发 L1/L2 更新和 L3 长期记忆沉淀 |
| `POST /memory/recall` | 按 user、session 和 query 召回 L1/L2/L3 记忆，并返回降级状态和原因 |
| `POST /memory/delete` | 按单条 memory、session 或用户全局范围删除记忆，并记录删除任务和摘要脏标记 |
| `GET /memory/items` | 列出当前用户可管理的长期记忆；默认只返回 `ACTIVE` 业务记忆 |
| `GET /memory/items/{memory_id}` | 查询单条可管理长期记忆，不暴露内部作用域、backend id 或 source refs |
| `POST /memory/update` | 编辑单条 `ACTIVE` 长期记忆，并记录编辑任务和摘要脏标记 |
| `GET /memory/tasks/{task_id}` | 查询写入、编辑、删除任务的执行状态、错误和结果 |
| `GET /memory/l3/background-status` | 查看 L3 后台写入模式、线程池、排队任务数和剩余容量 |

V1 不对外暴露 `POST /memory/rebuild` HTTP 端点；`MemoryService.rebuild()` 作为内部能力保留，仅供运维脚本和真实质量回归通过 Python 级调用触发。
具体取舍见 [Innies记忆三层架构](Innies记忆三层架构.md) 第 5 节。

### 1.5 健康检查

```bash
curl http://localhost:8000/health
curl http://localhost:8000/health/live
curl http://localhost:8000/health/ready
```

就绪检查会检查数据库、Redis、共享 Milvus 和 Mem0 Library。
其中 Milvus 使用只读连通性探测，Mem0 Library 只做配置和适配器构造检查，不在 readiness 中调用 LLM、Embedding 或向量库写入链路。
如果出现：

```text
password authentication failed for user "postgres"
```

说明应用连到了一个可达但密码不匹配的 PostgreSQL。
常见原因是本机已有服务占用 `5432`，而不是 compose 内的 `innies-memory-postgres`。

如果看到 `Connection refused` 或 `Connect call failed`，含义不同：应用没有连到可用 PostgreSQL。
这时优先检查 `POSTGRES_HOST / POSTGRES_PORT`、`docker compose ps postgres`，以及 compose 暴露端口是否和 `.env` 一致。

### 1.6 真实主链路验证

当前仓库保留真实主链路脚本作为验证入口。
`make real-test` 面向本地或受控验证环境，直接构造真实 `SqlAlchemyMemoryRepository`
和真实 Mem0 Library 后端运行 5 轮主链路，不经过 HTTP API。
HTTP 部署后的 smoke test 应另行通过 `/memory/*` 接口执行。
质量回归使用 `script/real_mem0_quality_regression.py` / `script/run_real_mem0_quality_evaluation.py`；
性能稳定性使用 `script/real_mem0_stability_preprod.py` 和 P0 压测脚本；
当前仓库不再维护独立质量评测方案、报告字段、报告模板或性能稳定性方案文档。
质量、性能与稳定性口径应以脚本输出、脚本内报告字段和发布归档为准。
具体评测报告如果由 CI、发布流程或人工归档生成，应随发布记录保存。
真实结论必须分别看召回质量、删除重建、隔离，以及并发、资源、稳定性和失败重试。

脚本：

```bash
make real-test
```

HTTP 部署后的质量回归和 P0 压测脚本通过 `INNIES_MEMORY_API_URL` 访问 innies-memory API。

| 变量 | 用途 |
| --- | --- |
| `INNIES_MEMORY_API_URL` | 真实质量回归和 P0 压测脚本访问 innies-memory API 的地址 |

它会执行：

1. 检查 `OPENAI_API_KEY`、`MILVUS_URL` 是否存在。
2. 检查 PostgreSQL、Redis 和 Milvus 是否可达。
3. 使用真实 `SqlAlchemyMemoryRepository` 和真实 Mem0 Library 后端。
4. 追加 5 轮对话。
5. 在 append 和 L3 生成检查中实际触发 LLM / Embedding / Mem0 / Milvus 链路。
6. 召回长期偏好。
7. 删除一条目标记忆。
8. 重建 L2/L3。
9. 再次召回确认链路可用。

脚本不支持 fake 模式。缺少密钥时会失败退出，例如：

```text
real validation preflight failed: missing_config=OPENAI_API_KEY database=ready redis=ready milvus=ready
```

这不是测试通过，也不是代码失败，而是环境未满足真实验证条件。

### 1.7 生产注意事项

1. API 默认使用 PostgreSQL 业务仓库，不允许生产路径退回内存仓库。
2. Mem0 以 Library 方式嵌入 innies-memory；Milvus 独立部署，不能假设和 innies-memory 同主机。
3. innies-memory 的业务读写和语义检索必须遵守第 1.2 节的 Mem0 和 readiness 边界。
4. 生产 Kubernetes 使用 ConfigMap/Secret 注入配置，`ENVIRONMENT=production` 时进程不读取 `.env`。
5. 删除边界见 [技术栈选型](Innies记忆服务技术栈选型.md) 第 5 节。
6. 删除、重建和写入失败时，`ins_memory_task` 必须记录 `failed` 和 `last_error`。
7. 同步边界和线程池口径见 [工程实现与运行边界](Innies记忆三层工程实现与运行边界.md) 第 9 节。
8. `SqlAlchemyMemoryRepository` 在固定后台 event loop 上运行 async SQLAlchemy 调用。
   SQLAlchemy engine 使用默认连接池，避免每次仓库调用新建 event loop 和 asyncpg 连接。
   生产如要进一步降低同步边界成本，再把仓库和服务整体改成 async。
9. 强治理阶段的语义抑制、写入栅栏和衰减任务尚未启用。
   不能在隐私强合规场景中把首版当成最终治理方案。
10. 当前中心化日志允许记录 `user_id`、`session_id`、`round_id`、`operation_id` 和 `task_id` 等业务标识用于链路追踪，但不记录消息正文、查询正文、token 或密钥；若后续需要对这些标识做脱敏或哈希化，必须先补充安全/法务决策记录。

## 2. 运维交付文档

### 2.1 交付目标

本文档用于将 `innies-memory` 从开发侧交付给运维或平台团队。
覆盖生产发布前需要准备的资源、配置、部署步骤、上线验证、监控告警和回滚要求。

`innies-memory` 生产部署以 Kubernetes 为准。Docker Compose 只用于本地开发和本地压测，不作为生产部署方式。

### 2.2 服务边界

| 项目 | 内容 |
| --- | --- |
| 服务名 | `innies-memory` |
| 运行形态 | FastAPI API、Alembic 迁移 Job；Celery worker Deployment 保留为诊断和后续异步扩展组件，不承载当前 L3 抽取 |
| 容器端口 | `8000` |
| K8s Deployment | `innies-memory-api`、`innies-memory-worker` |
| K8s Service | `innies-memory` |
| 迁移 Job | `innies-memory-migrate` |
| 健康检查 | `GET /health/live` |
| 就绪检查 | `GET /health/ready` |
| 基础健康信息 | `GET /health` |
| OpenAPI | `GET /docs`、`GET /redoc`、`GET /openapi.json` |
| 指标地址 | 当前未暴露应用内 `/metrics`，先使用网关、Kubernetes 和日志平台指标 |
| 依赖服务 | PostgreSQL、Redis、Milvus、OpenAI-compatible LLM endpoint、OpenAI-compatible Embedding endpoint |
| 不依赖 | 独立 Mem0 REST Server、Qdrant、对象存储、搜索引擎 |

`/health/live` 只表示 API 进程存活。
`/health/ready` 会检查 PostgreSQL、Redis、Milvus 和 Mem0 Library 等依赖。
生产发布和流量接入应以 `/health/ready` 通过为准。

Mem0 以 Library 方式嵌入 `innies-memory` 进程。
业务边界沿用第 1.2 节：业务路径走 Mem0，Milvus 只由 Mem0 Library 承载 L3 向量写入和检索。
readiness 只做诊断性依赖探测，用于暴露 LLM、Embedding、Mem0 或 Milvus 链路不可用。

当前 L3 异步写入执行位置沿用第 1.3 节口径，不依赖 Celery worker 消费。
Celery worker 目前只承载诊断任务和后续异步任务扩展入口。
排查 L3 后台积压时，应优先查看 API 日志、`ins_memory_task` 和 `/memory/l3/background-status`。

#### 2.2.1 业务可见性边界

innies-memory 当前主链路采用 `MEMORY_L3_WRITE_MODE=async`，业务可见性边界如下：

| 调用场景 | 可见性 | 解释 |
| --- | --- | --- |
| 同会话紧接 recall（刚 append 完立即查） | 立即可见 | 该轮已写入 `ins_summary_round_journal` 并刷新 L1，recall 通过 L1 即可拿到 |
| 同会话内 L2 摘要相关召回 | 立即可见 | L2 在 append 同步路径里更新 |
| 跨会话恢复（新 session 召回上一会话沉淀的事实） | **存在秒级到分钟级延迟** | L3 Mem0 抽取在后台执行器中沉淀，延迟取决于 L3 队列长度、Mem0 抽取耗时和外部 LLM / Embedding 响应时间；调用方不应假设 append 完成后立即可跨会话恢复 |
| 调用方需要确认 L3 已沉淀 | 通过任务态查询 | 轮询 `GET /memory/tasks/{task_id}` 或 `GET /memory/l3/background-status`；`l3_replay_status=completed` 才代表 L3 沉淀完成 |

这条边界是当前首版的明确取舍：append 接口的延迟优先保证对话流不被外部 LLM/Embedding 抖动拖死，代价是 L3 跨会话可见性不是同步契约。
真实压测样本和 SLO 基线由 `script/real_mem0_p0_*` 脚本采集，调用方按这条 SLO 设计上游对话编排逻辑，不要假设记忆服务做了同步等待。

### 2.3 开发侧交付物

开发侧应向运维提供：

| 交付物 | 说明 |
| --- | --- |
| 镜像名 | 生产镜像仓库地址，例如 `registry.example.com/innies/innies-memory` |
| 镜像 tag 或 digest | 禁止使用 `latest`，建议使用 git SHA、语义版本或不可变 digest |
| K8s 基线 | `k8s/` 目录 |
| API Deployment | `k8s/deployment-api.yaml` |
| worker Deployment | `k8s/deployment-worker.yaml` |
| 迁移 Job | `k8s/job-migrate.yaml` |
| Service | `k8s/service.yaml` |
| ConfigMap 模板 | `k8s/configmap.yaml` |
| Secret 字段示例 | `k8s/secret.example.yaml`，只说明字段结构，不能直接用于生产 |
| 健康检查地址 | `/health/live`、`/health/ready` |
| OpenAPI | `/docs`、`/redoc`、`/openapi.json` |
| 开发验证手册 | `docs/memory/Innies记忆服务开发手册.md` |
| 本地依赖启动 | 仅本地开发使用；详见开发手册中的本地配置和共享 PostgreSQL 口径 |
| 数据库迁移入口 | `poetry run alembic upgrade head`，K8s 使用 `innies-memory-migrate` Job |
| 真实主链路验证 | `make real-test` |
| 单元和静态检查 | `poetry run pytest -q`、`poetry run ruff check src test script alembic`、`poetry run mypy src` |
| 部署参考 | `docs/memory/Innies记忆服务部署与运维交付文档.md` |
| 架构参考 | `docs/memory/Innies记忆三层架构.md`、`docs/memory/Innies记忆三层工程实现与运行边界.md` |
| 性能稳定性验证入口 | `script/real_mem0_stability_preprod.py`、`script/real_mem0_p0_*` |
| 质量回归验证入口 | `script/real_mem0_quality_regression.py`、`script/run_real_mem0_quality_evaluation.py` |
| 评测报告归档 | 由 CI、发布流程或人工归档提供，路径写入发布准入记录 |

已退役的评测和压测方案文档不再作为交付物。
质量、性能与稳定性结论以脚本输出、脚本内报告字段和发布归档为准。

### 2.4 运维侧资源准备清单

#### 2.4.1 Kubernetes 环境

| 检查项 | 要求 |
| --- | --- |
| Kubernetes 版本 | 建议 `1.25+` |
| namespace | 由运维创建并纳入权限管理 |
| 镜像仓库访问 | 节点或 Pod 可拉取生产镜像 |
| imagePullSecrets | 私有仓库必须配置 |
| Ingress / Gateway | 按平台标准配置域名、TLS 和路由 |
| DNS | 服务域名和外部依赖域名可解析 |
| NetworkPolicy | 按真实入口、数据库、Redis、Milvus、LLM 和 Embedding 地址收紧 |
| 日志采集 | API、worker、migrate Job 的 stdout/stderr 接入日志平台 |
| 指标采集 | 当前使用 Kubernetes、网关和日志平台指标；应用内 `/metrics` 需要开发补充后再 scrape |
| 资源配额 | API、worker、migrate Job 的 requests/limits 需按压测结果校准 |
| 可写临时目录 | Pod 需要可写 `/tmp/innies-memory`，用于 Mem0 本地历史数据库和临时文件 |

#### 2.4.2 PostgreSQL

生产 PostgreSQL 由运维或数据库平台维护，不由 API Deployment 承载。

| 检查项 | 要求 |
| --- | --- |
| 数据库名 | `POSTGRES_DATABASE=innies-memory` |
| 表名规范 | 表名以 `ins_` 为前缀，使用单数命名 |
| 当前业务表 | `ins_current_summary`、`ins_summary_round_journal`、`ins_memory`、`ins_memory_task` |
| 账号权限 | 迁移 Job 需要建表、建索引和执行 Alembic 迁移权限 |
| 连接地址 | 写入 `POSTGRES_HOST`、`POSTGRES_PORT` |
| 账号密码 | 用户名写入 ConfigMap，密码写入 Secret |
| 持久化 | 必须配置可靠存储 |
| 备份 | 必须配置定期备份 |
| 恢复演练 | 上线前至少完成一次恢复路径验证 |
| 监控 | CPU、内存、磁盘、连接数、事务、慢查询、锁等待 |

#### 2.4.3 Redis

Redis 用于 Celery broker/result backend 和运行时基础设施。

| 检查项 | 要求 |
| --- | --- |
| 地址 | 写入 `REDIS_HOST`、`REDIS_PORT`、`REDIS_DB` |
| 密码 | 如启用密码，写入 `REDIS_PASSWORD` Secret |
| Celery URL | 如启用密码，`CELERY_BROKER_URL` 和 `CELERY_RESULT_BACKEND` 必须包含认证信息 |
| 持久化 | 按平台标准配置，至少避免单点故障导致任务状态不可恢复 |
| 监控 | 连接数、内存、命中率、阻塞命令、延迟、eviction |

Celery URL 示例：

```text
redis://:<password>@redis:6379/0
redis://:<password>@redis:6379/1
```

#### 2.4.4 Milvus

Milvus 是独立向量数据库，不由 `innies-memory` 的 K8s Deployment 承载。

| 检查项 | 要求 |
| --- | --- |
| 地址 | 写入 `MILVUS_URL`，生产不要使用 `localhost` |
| database | 写入 `MILVUS_DATABASE` |
| collection | `MEMORY_MILVUS_COLLECTION=innies_memory` |
| embedding 维度 | `MEMORY_EMBEDDING_DIMS=1024`，必须与 embedding 服务输出一致 |
| 认证 | 如启用鉴权，提供 `MILVUS_USER` 和 `MILVUS_PASSWORD` Secret |
| 网络 | Pod 到 Milvus 地址和端口必须可达 |
| 监控 | collection、索引、查询延迟、写入延迟、内存、磁盘、compaction、错误率 |

#### 2.4.5 LLM 和 Embedding endpoint

| 检查项 | 要求 |
| --- | --- |
| LLM 地址 | `MEMORY_LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1` |
| LLM 模型 | `MEMORY_LLM_MODEL=qwen-plus-latest` |
| LLM API key | 写入 `OPENAI_API_KEY` Secret，不写入 ConfigMap、镜像或 Git |
| Embedding 地址 | `MEMORY_EMBEDDING_BASE_URL=http://<embedding-host>:7345/v1` |
| Embedding 模型 | `MEMORY_EMBEDDING_MODEL=zhiman-embedding` |
| Embedding API key | 当前可为空；如未来启用鉴权，写入 `MEMORY_EMBEDDING_API_KEY` Secret |
| 网络出口 | Pod 必须能访问上述 endpoint；公网出口、代理、DNS 和防火墙策略由运维确认 |
| 限流 | 需要确认外部服务 QPS、并发、超时和失败重试策略 |

#### 2.4.6 Secret

生产环境不要使用 `k8s/secret.example.yaml`。它只说明字段结构。

运维应使用以下任一方式注入真实 Secret：

- 平台 Secret Manager；
- External Secrets Operator；
- Sealed Secrets；
- CI/CD Secret；
- 其他平台批准的 Secret 管理机制。

Secret 对象应包含以下 key。
其中 `POSTGRES_PASSWORD` 和 `OPENAI_API_KEY` 是当前生产必填；
`REDIS_PASSWORD`、`MILVUS_USER`、`MILVUS_PASSWORD`、`MEMORY_EMBEDDING_API_KEY`
按目标环境鉴权要求填写，无鉴权时可为空但 key 仍应保留，避免 Deployment/Job 的环境注入缺项。

```text
POSTGRES_PASSWORD
REDIS_PASSWORD
MILVUS_USER
MILVUS_PASSWORD
OPENAI_API_KEY
MEMORY_EMBEDDING_API_KEY
```

Secret 对象名称需要与 Deployment 和 Job 匹配：

```text
innies-memory-secret
```

### 2.5 生产配置清单

#### 2.5.1 环境交付参数表

运维接手前，应把下表填完整，并作为发布记录的一部分保存。

| 参数 | 生产值 | 负责人 | 备注 |
| --- | --- | --- | --- |
| Kubernetes 集群 | `<cluster>` | `<owner>` |  |
| namespace | `<namespace>` | `<owner>` |  |
| 镜像仓库 | `<registry/repository>` | `<owner>` |  |
| 镜像 tag 或 digest | `<tag-or-digest>` | `<owner>` | 禁止使用 `latest` |
| imagePullSecrets | `<secret-name>` | `<owner>` | 私有仓库必填 |
| API 域名 | `<domain>` | `<owner>` | 仅内网时填写内网域名 |
| Ingress / Gateway | `<name>` | `<owner>` |  |
| TLS 证书来源 | `<issuer-or-secret>` | `<owner>` |  |
| PostgreSQL 地址 | `<host:port>` | `<owner>` | 写入 `POSTGRES_HOST / POSTGRES_PORT` |
| PostgreSQL database | `innies-memory` | `<owner>` | 写入 `POSTGRES_DATABASE` |
| PostgreSQL Secret 来源 | `<secret-manager-path>` | `<owner>` | 不写真实密码 |
| Redis 地址 | `<host:port/db>` | `<owner>` | 写入 `REDIS_HOST / REDIS_PORT / REDIS_DB` |
| Redis Secret 来源 | `<secret-manager-path>` | `<owner>` | 不写真实密码 |
| Celery broker URL | `<redis-url>` | `<owner>` | Redis 有密码时必须带认证 |
| Celery result backend | `<redis-url>` | `<owner>` | Redis 有密码时必须带认证 |
| Milvus URL | `<milvus-url>` | `<owner>` | 写入 `MILVUS_URL` |
| Milvus database | `<database>` | `<owner>` | 写入 `MILVUS_DATABASE` |
| Milvus Secret 来源 | `<secret-manager-path / N/A>` | `<owner>` | 不写真实密码 |
| LLM endpoint | `<base-url>` | `<owner>` | 默认 DashScope compatible endpoint |
| LLM Secret 来源 | `<secret-manager-path>` | `<owner>` | `OPENAI_API_KEY` |
| Embedding endpoint | `<base-url>` | `<owner>` | 当前 endpoint 不需要 API key |
| 日志索引或项目 | `<log-index>` | `<owner>` |  |
| 告警接收组 | `<team/channel>` | `<owner>` |  |
| 真实主链路测试报告 | `<report-path>` | `<owner>` | 发布前保存 |
| 回滚镜像版本 | `<tag-or-digest>` | `<owner>` |  |

#### 2.5.2 ConfigMap 环境变量

| 变量 | 生产建议 |
| --- | --- |
| `APP_NAME` | 应用名称，生产建议 `innies-memory` |
| `APP_VERSION` | 与镜像版本一致 |
| `ENVIRONMENT` | 运行环境，生产必须为 `production` |
| `DEBUG` | 是否开启调试模式，生产必须为 `false` |
| `LOG_LEVEL` | 日志级别，生产建议 `INFO` |
| `POSTGRES_HOST` | 生产 PostgreSQL 地址 |
| `POSTGRES_PORT` | 生产 PostgreSQL 端口 |
| `POSTGRES_USER` | 生产 PostgreSQL 用户名 |
| `POSTGRES_DATABASE` | PostgreSQL 数据库名，当前固定为 `innies-memory` |
| `REDIS_HOST` | 生产 Redis 地址 |
| `REDIS_PORT` | 生产 Redis 端口 |
| `REDIS_DB` | Redis DB 编号 |
| `CELERY_BROKER_URL` | 生产 Redis broker URL |
| `CELERY_RESULT_BACKEND` | 生产 Redis result backend URL |
| `CELERY_TASK_TIME_LIMIT` | Celery 单任务硬超时，当前基线 `3600` |
| `CELERY_TASK_SOFT_TIME_LIMIT` | Celery 单任务软超时，当前基线 `3000` |
| `MILVUS_URL` | 生产 Milvus 地址 |
| `MILVUS_DATABASE` | 生产 Milvus database，仓库代码默认值为 `default`；生产按真实 Milvus 规划覆盖 |
| `MEMORY_LLM_BASE_URL` | OpenAI-compatible LLM endpoint 地址 |
| `MEMORY_EMBEDDING_BASE_URL` | OpenAI-compatible Embedding endpoint 地址 |
| `MEMORY_MILVUS_COLLECTION` | Mem0 写入 Milvus 的 collection 名称，当前为 `innies_memory` |
| `MEMORY_EMBEDDING_DIMS` | Embedding 向量维度，当前为 `1024` |
| `MEMORY_LLM_MODEL` | LLM 模型名称，当前为 `qwen-plus-latest` |
| `MEMORY_EMBEDDING_MODEL` | Embedding 模型名称，当前为 `zhiman-embedding` |
| `MEM0_HISTORY_DB_PATH` | Mem0 Library 本地历史数据库路径，生产建议 `/tmp/innies-memory/mem0/history.db` |
| `MEMORY_L3_WRITE_MODE` | L3 写入模式，生产建议 `async` |
| `MEMORY_API_WORKER_LIMIT` | 按压测结果调整，当前基线 `8` |
| `MEMORY_API_WORKER_WAIT_SECONDS` | 按压测结果调整，当前基线 `5` |
| `MEMORY_L3_EXECUTOR_WORKERS` | 按压测结果调整，当前基线 `16` |
| `MEMORY_L3_MAX_PENDING_TASKS` | 按压测结果调整，当前基线 `256` |
| `MEMORY_L3_QUEUE_WAIT_SECONDS` | 按压测结果调整，当前基线 `5` |
| `MEMORY_BACKEND_MAX_CONCURRENT_CALLS` | 按后端依赖承载能力调整，当前基线 `4` |
| `READINESS_TIMEOUT_SECONDS` | 当前基线 `3`；K8s readinessProbe `timeoutSeconds` 当前为 `5`，必须不小于该值且小于 `periodSeconds` |

`MEMORY_API_WORKER_LIMIT` 是 API 读池和写池各自的同步工作线程上限，不是单进程全局唯一线程数上限。
`MEMORY_API_WORKER_WAIT_SECONDS` 同时约束 `/memory/*` 路由等待同步 worker 和等待同步调用返回的总时间窗口；超过后会返回可重试 503。
503 只表示 HTTP 层等待超时或 worker 队列满，不保证后端业务操作已经取消。
调用方重试 append 必须复用原 `round_id`，重试 update/delete/rebuild 必须复用原 `operation_id`，并可用返回或已知的 `task_id` 查询最终状态。
update 的完成幂等只在目标记忆仍是该次编辑结果时返回 `already_done`；如果同一记忆已被后续编辑或删除，旧 `operation_id` 重试会返回冲突，调用方应以当前管理列表或详情为准重新决策。
`MEMORY_BACKEND_MAX_CONCURRENT_CALLS` 是 Mem0 Library 后端调用并发上限，和 L3 后台队列容量不是同一层控制。
`CELERY_TASK_TIME_LIMIT` 和 `CELERY_TASK_SOFT_TIME_LIMIT` 当前只约束 worker 诊断和后续异步扩展任务；L3 后台抽取执行位置见第 1.3 节。
调大前必须确认 LLM、Embedding、Milvus、Pod CPU 和主机线程资源都能承载。
`k8s/deployment-api.yaml` 的 readinessProbe `timeoutSeconds` 必须大于等于 `READINESS_TIMEOUT_SECONDS`，并且小于 `periodSeconds`，避免探针重叠。
如果 probe 超时更短，Kubernetes 会先终止 HTTP 探测，应用内部单项依赖超时不会真正生效。

#### 2.5.3 Secret 环境变量

| 变量 | 生产建议 |
| --- | --- |
| `POSTGRES_PASSWORD` | 生产 PostgreSQL 密码 |
| `REDIS_PASSWORD` | 生产 Redis 密码；无密码时可为空 |
| `MILVUS_USER` | 生产 Milvus 用户名；无鉴权时可为空 |
| `MILVUS_PASSWORD` | 生产 Milvus 密码；无鉴权时可为空 |
| `OPENAI_API_KEY` | 生产 LLM API key |
| `MEMORY_EMBEDDING_API_KEY` | 当前可为空；启用鉴权后填写 |

生产环境不得依赖本地 `.env` 文件。
应用在 `ENVIRONMENT=production` 时会禁用 `.env` 读取，只使用进程环境变量。
运维必须通过 Kubernetes ConfigMap/Secret 或平台等价机制注入生产配置。

### 2.6 发布执行步骤

#### 2.6.1 发布前检查

开发侧提交前检查清单沿用 [Innies记忆服务开发手册](Innies记忆服务开发手册.md)：
按第 6 章选择并完成对应质量门禁，再进入本节发布前检查。

在部署前确认：

- 镜像已经构建并推送到生产镜像仓库；
- 镜像 tag 或 digest 不可变；
- 目标 namespace 已创建；
- Secret 已创建；
- ConfigMap 中 PostgreSQL、Redis、Milvus、LLM 和 Embedding 地址已替换为生产值；
- Redis 启用密码时，Celery URL 已包含认证；
- imagePullSecrets 已配置；
- Ingress 或 Gateway 已准备；
- PostgreSQL、Redis、Milvus 和外部模型 endpoint 可从集群内访问；
- `kubectl apply --dry-run=server` 或平台等价校验通过；
- 开发侧 `poetry run pytest -q`、Ruff、mypy 已通过；
- 必要时已完成真实主链路测试、质量评测和性能稳定性测试。

#### 2.6.2 部署命令

先确认 Secret 已存在：

```bash
kubectl get secret innies-memory-secret
```

应用配置：

```bash
kubectl apply -f k8s/configmap.yaml
kubectl apply -f <production-secret.yaml>
```

执行迁移 Job：

```bash
kubectl apply -f k8s/job-migrate.yaml
kubectl wait --for=condition=complete job/innies-memory-migrate --timeout=120s
```

应用 API 和 worker：

```bash
kubectl apply -f k8s/service.yaml
kubectl apply -f k8s/deployment-api.yaml
kubectl apply -f k8s/deployment-worker.yaml
```

等待滚动发布完成：

```bash
kubectl rollout status deployment/innies-memory-api
kubectl rollout status deployment/innies-memory-worker
```

查看 Pod 状态：

```bash
kubectl get pods -l app=innies-memory
```

#### 2.6.3 上线后验证

通过端口转发验证：

```bash
kubectl port-forward service/innies-memory 8000:8000
curl http://127.0.0.1:8000/health/live
curl http://127.0.0.1:8000/health/ready
curl http://127.0.0.1:8000/openapi.json
```

接入真实入口后验证：

```bash
curl https://<生产域名>/health/live
curl https://<生产域名>/health/ready
```

发布准入至少应满足：

- 迁移 Job 完成；
- API Deployment rollout 成功；
- worker Deployment rollout 成功；
- Pod 无持续重启；
- `/health/live` 返回成功；
- `/health/ready` 返回成功；
- API 访问日志进入日志平台；
- PostgreSQL、Redis、Milvus 无连接失败或认证失败；
- 外部 LLM / Embedding 无持续超时、限流或认证失败；
- 关键业务 API smoke test 通过；
- 真实主链路测试报告已归档。

### 2.7 监控和告警

| 告警项 | 来源 | 建议级别 | 说明 |
| --- | --- | --- | --- |
| Pod 持续重启 | Kubernetes | P1 | 可能是配置、镜像、依赖或资源问题 |
| API Deployment 副本不可用 | Kubernetes | P1 | 可用副本低于期望值 |
| worker Deployment 副本不可用 | Kubernetes | P2 | Celery 诊断或后续扩展任务不可用；当前 L3 后台抽取不依赖它 |
| 迁移 Job 失败 | Kubernetes Job | P1 | 发布不应继续 |
| `/health/ready` 失败 | HTTP probe | P1 | PostgreSQL、Redis、Milvus 或 Mem0 Library 未就绪 |
| HTTP 5xx 增加 | 网关 / 日志 | P1 | 服务端错误 |
| p95 / p99 延迟升高 | 网关 / 日志 | P2 | 数据库、Mem0、LLM、Embedding 或 Milvus 性能下降 |
| L3 队列积压 | API 日志 / 后台状态接口 | P2 | `MEMORY_L3_MAX_PENDING_TASKS` 接近容量 |
| trace 关联检索 | 日志平台 | P2 | 通过 `trace_id` 关联同一请求链路，优先检索入口、后台写入和异常点 |
| CPU 持续高水位 | Kubernetes metrics | P2 | 需要扩容或排查热点请求 |
| 内存接近 limit | Kubernetes metrics | P1 | 有 OOM 风险 |
| PostgreSQL 连接失败 | API 日志 / 数据库监控 | P1 | 服务不可用风险 |
| PostgreSQL 慢查询或锁等待 | 数据库监控 | P2 | 召回或写入可能变慢 |
| Redis 连接失败 | API / worker 日志 | P1 | Celery broker/result backend 不可用，worker 诊断和后续异步扩展受影响 |
| Milvus 连接失败 | readiness / API 日志 | P1 | L3 向量能力不可用 |
| LLM 认证失败或限流 | API 日志 / 外部服务监控 | P1 | L3 抽取不可用 |
| Embedding endpoint 失败 | API 日志 / 外部服务监控 | P1 | L3 写入和检索不可用 |

当前未暴露应用内 `/metrics`。如果生产要求 Prometheus scrape，需要开发补充指标端点后再接入。

### 2.8 网络和安全要求

| 项目 | 要求 |
| --- | --- |
| 入口流量 | 只允许来自网关、Ingress、Service Mesh 或批准 namespace |
| PostgreSQL 出口 | 只允许访问真实 PostgreSQL 地址和端口 |
| Redis 出口 | 只允许访问真实 Redis 地址和端口 |
| Milvus 出口 | 只允许访问真实 Milvus 地址和端口 |
| LLM 出口 | 只允许访问批准的 OpenAI-compatible LLM endpoint |
| Embedding 出口 | 只允许访问批准的 OpenAI-compatible Embedding endpoint |
| DNS 出口 | 允许集群 DNS |
| `/docs`、`/redoc`、`/openapi.json` | 内网环境可保留；公网或敏感环境建议由网关限制 |
| 写入 API | innies-memory 是内部服务，不在进程内做调用方鉴权；公网入口、跨域准入、流量限速由网关、mTLS、服务身份或内网 ACL 在上游解决，详见 [Innies记忆三层架构](Innies记忆三层架构.md) 第 0.1 节 |
| Secret | 不写入 Git，不写入镜像，不输出到日志 |

当前基线未提供 NetworkPolicy。生产环境需要运维按真实 service、namespace 或 CIDR 补充。

### 2.9 回滚方案

#### 2.9.1 镜像版本回滚

查看发布历史：

```bash
kubectl rollout history deployment/innies-memory-api
kubectl rollout history deployment/innies-memory-worker
```

回滚到上一版本：

```bash
kubectl rollout undo deployment/innies-memory-api
kubectl rollout undo deployment/innies-memory-worker
kubectl rollout status deployment/innies-memory-api
kubectl rollout status deployment/innies-memory-worker
```

回滚后验证：

```bash
curl https://<生产域名>/health/live
curl https://<生产域名>/health/ready
```

#### 2.9.2 配置回滚

如果是 ConfigMap、Secret、Ingress 或 NetworkPolicy 配置问题，
应按平台变更系统回退到上一版配置，然后重新触发 rollout 或重启 Pod。

#### 2.9.3 数据回滚

Alembic 迁移只管理 PostgreSQL 业务表和索引，不会自动删除 Mem0 或 Milvus 中的长期记忆。

生产发布前必须明确：

- PostgreSQL 备份保留周期；
- PostgreSQL 恢复目标时间；
- 是否允许直接恢复数据库快照；
- 恢复后是否需要通过 `innies-memory` 业务索引同步清理 Mem0 / Milvus；
- 脏数据修复责任人；
- 恢复后如何重新执行 smoke test 和真实主链路测试。

### 2.10 常见故障排查

#### 2.10.1 Pod 无法启动

排查：

```bash
kubectl describe pod <pod-name>
kubectl logs <pod-name>
```

重点检查：

- 镜像是否可拉取；
- imagePullSecrets 是否正确；
- Secret 是否存在；
- ConfigMap 是否存在；
- 只读文件系统下 `/tmp/innies-memory` 是否已挂载为可写目录；
- CPU/内存限制是否过低。

#### 2.10.2 `/health/live` 成功但 `/health/ready` 失败

说明 API 进程存活，但至少一个依赖不可用。重点检查：

- `POSTGRES_HOST / POSTGRES_PORT / POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DATABASE`；
- `REDIS_HOST / REDIS_PORT / REDIS_DB / REDIS_PASSWORD`；
- `CELERY_BROKER_URL / CELERY_RESULT_BACKEND`；
- `MILVUS_URL / MILVUS_DATABASE / MILVUS_USER / MILVUS_PASSWORD`；
- `OPENAI_API_KEY / MEMORY_LLM_BASE_URL / MEMORY_LLM_MODEL`；
- `MEMORY_EMBEDDING_BASE_URL / MEMORY_EMBEDDING_MODEL / MEMORY_EMBEDDING_DIMS`；
- NetworkPolicy、DNS、防火墙和外部服务限流。

`/health/ready` 的 Mem0 Library 检查不执行 add/delete 写入探针。
如果 detail 中出现 `mem0 library readiness check failed`、配置缺失或 Milvus 访问异常，应按完整 L3 依赖链路排查，而不是只检查 API 进程。
如果需要串联同一请求的入口、后台写入和异常日志，优先按 `trace_id` 检索日志平台中的同一链路。

#### 2.10.3 迁移 Job 失败

排查：

```bash
kubectl logs job/innies-memory-migrate
kubectl describe job innies-memory-migrate
```

常见原因：

- PostgreSQL 账号权限不足；
- 数据库名不是 `innies-memory`；
- Secret 缺失或密码错误；
- 迁移 Job 使用的镜像版本和 API 版本不一致；
- 已存在不兼容 schema，需要人工迁移确认。

#### 2.10.4 rollout 卡住

排查：

```bash
kubectl rollout status deployment/innies-memory-api
kubectl rollout status deployment/innies-memory-worker
kubectl get pods -l app=innies-memory
kubectl describe deployment innies-memory-api
```

常见原因：

- 新 Pod readiness 失败；
- PostgreSQL、Redis、Milvus 或模型 endpoint 不可达；
- Secret 缺失；
- 镜像拉取失败；
- 资源不足无法调度。

#### 2.10.5 召回质量或延迟异常

排查：

- API p95 / p99；
- PostgreSQL 慢查询；
- Redis 延迟；
- Milvus 查询延迟；
- LLM / Embedding 限流或超时；
- L3 后台队列积压；
- 近期写入量和数据规模变化；
- 是否有未预期的大批量删除、重建或压测。

#### 2.10.6 卡死的 RUNNING task

现象：`ins_memory_task` 表中存在 `status='RUNNING'` 且 `updated_at` 长时间不再变化的任务（典型阈值 >= 5 分钟，可按生产实际 SLA 校准）。

成因：V1 阶段 L3 后台抽取在 API 进程内线程池执行，无独立 worker。进程被 SIGKILL / OOM / kubelet evict 时，正在跑的 `_complete_async_l3_write` 后台线程不会优雅收尾，对应 task 永远停留在 `RUNNING`。

业务影响：

- `ins_summary_round_journal`（可靠轮次账本）和 L1/L2 缓存**不受影响**，该轮次的对话上下文仍可被同会话延续召回；
- 对应的 L3 长期记忆**抽取丢失**，跨会话恢复时该事实可能召回不到；
- 调用方按原 `round_id` 重试 `/memory/append` 会命中 `already_done`（task 仍是 RUNNING），不会重新触发抽取。

V1 运维处置（手工）：

```sql
-- 1. 找出超时未更新的 RUNNING task（threshold 按 SLA 调）
SELECT task_id, op_type, scope, updated_at
FROM ins_memory_task
WHERE status = 'RUNNING'
  AND updated_at < now() - interval '5 minutes';

-- 2. 标 FAILED 释放重试入口；上游按原 round_id / operation_id 重试即可
UPDATE ins_memory_task
SET status = 'FAILED', last_error = 'orphan task reaped by ops'
WHERE task_id IN (...);
```

恢复路径：

- append 类（`memory-extract:{round_id}`）：标 FAILED 后上游用同 `round_id` 重发 `/memory/append`，V1 会走 FAILED 重试分支重新提交 L3 抽取；
- update / delete / rebuild 类：上游用同 `operation_id` 重发；如果同一记忆已被后续编辑或删除，旧 update 操作会按冲突处理，避免返回错误的当前正文。

未来版本：V2/V3 计划补 startup-time reaper（进程启动时自动扫超时 RUNNING task 标 FAILED），消除手工处置环节；当前 V1 不引入额外配置项或定时任务，明确以人工 SOP 兜底。

### 2.11 发布准入记录模板

每次生产发布建议记录：

```text
服务：innies-memory
环境：
namespace：
镜像：
发布时间：
发布人：
变更摘要：
PostgreSQL 地址：
PostgreSQL database：
Redis 地址：
Milvus URL：
LLM endpoint：
Embedding endpoint：
Secret 来源：
Ingress / Gateway：
迁移 Job 结果：
API rollout 结果：
worker rollout 结果：
/health/live：
/health/ready：
OpenAPI：
smoke test：
真实主链路测试：
质量评测报告：
性能稳定性测试报告：
性能稳定性报告 JSON：
P0 stress 轮次/持续时间：
服务线程数观测：
L3 队列观测：
监控告警状态：
回滚版本：
遗留风险：
```

### 2.12 相关文档

- `docs/memory/Innies记忆服务部署与运维交付文档.md`
- `docs/memory/Innies记忆服务开发手册.md`
- `docs/memory/Innies记忆三层架构.md`
- `docs/memory/Innies记忆三层工程实现与运行边界.md`
- `docs/memory/Innies记忆服务数据库迁移指南.md`
- `docs/memory/Innies记忆服务技术栈选型.md`
