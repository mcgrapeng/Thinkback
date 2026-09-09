# gRPC API

innies-memory 提供了基于 gRPC 的记忆服务接口，与 HTTP API 并行运行。

## 端口

- **HTTP API**: 8000 (FastAPI + uvicorn)
- **gRPC API**: 50051 (默认，可通过环境变量配置)

## 启用/禁用

通过环境变量控制：

```bash
GRPC_ENABLED=true   # 默认开启
GRPC_PORT=50051     # 默认端口
GRPC_MAX_WORKERS=8  # 线程池大小
```

## Proto 定义

Proto 文件位于 `proto/memory.proto`，定义了 9 个核心 RPC：

- `Append` - 追加对话回合
- `Recall` - 召回记忆
- `Delete` - 删除记忆
- `ListMemories` - 列出可管理记忆
- `GetMemory` - 查询单条记忆
- `UpdateMemory` - 编辑记忆
- `Rebuild` - 重建 L2/L3
- `GetTask` - 查询后台任务
- `GetL3BackgroundStatus` - L3 队列状态

## 生成客户端

### Python

```bash
python -m grpc_tools.protoc \
  -I proto \
  --python_out=. \
  --grpc_python_out=. \
  proto/memory.proto
```

### Go

```bash
protoc -I proto \
  --go_out=. --go_opt=paths=source_relative \
  --go-grpc_out=. --go-grpc_opt=paths=source_relative \
  proto/memory.proto
```

### 其他语言

参考 [gRPC 官方文档](https://grpc.io/docs/languages/)

## 客户端示例

### Python

```python
import grpc
from innies_memory.rpc import memory_pb2, memory_pb2_grpc

channel = grpc.insecure_channel('localhost:50051')
stub = memory_pb2_grpc.MemoryServiceStub(channel)

# Append
response = stub.Append(memory_pb2.AppendRequest(
    request_id='req-1',
    user_id='user-1',
    session_id='session-1',
    round_id='round-1',
    messages=[
        memory_pb2.MemoryMessage(
            message_id='m1',
            role=memory_pb2.MESSAGE_ROLE_USER,
            content='hello',
            timestamp='2026-01-01T00:00:00Z',
        ),
        memory_pb2.MemoryMessage(
            message_id='m2',
            role=memory_pb2.MESSAGE_ROLE_ASSISTANT,
            content='hi',
            timestamp='2026-01-01T00:00:01Z',
        ),
    ],
    source_timestamp='2026-01-01T00:00:01Z',
))
print(response.status, response.task_id)

# Recall
response = stub.Recall(memory_pb2.RecallRequest(
    user_id='user-1',
    session_id='session-1',
    query='hello',
    intent=memory_pb2.RECALL_INTENT_CHAT,
    l3_limit=5,
))
for item in response.items:
    print(f'{item.layer}: {item.content}')
```

### grpcurl (调试工具)

服务启用了 gRPC 反射，可使用 `grpcurl` 探索：

```bash
# 列出服务
grpcurl -plaintext localhost:50051 list

# 列出方法
grpcurl -plaintext localhost:50051 list memory.MemoryService

# 查看方法定义
grpcurl -plaintext localhost:50051 describe memory.MemoryService.Recall

# 调用接口
grpcurl -plaintext -d '{
  "user_id": "u1",
  "session_id": "s1",
  "query": "test",
  "intent": "RECALL_INTENT_CHAT",
  "l3_limit": 5
}' localhost:50051 memory.MemoryService/Recall
```

## 错误码映射

gRPC 错误码与 HTTP 状态码的对应关系：

| 场景 | gRPC Code | HTTP Status |
|------|-----------|-------------|
| fail-closed (隐私拒绝) | PERMISSION_DENIED | 403 |
| operation_id conflict | ABORTED | 409 |
| memory/task not found | NOT_FOUND | 404 |
| 队列满/超时 | UNAVAILABLE | 503 |
| 后端错误 | INTERNAL | 502 |
| 参数错误 | INVALID_ARGUMENT | 400 |

## 部署

### Kubernetes

gRPC 端口已在 `k8s/service.yaml` 和 `k8s/deployment-api.yaml` 中配置：

```yaml
ports:
  - name: http
    port: 8000
  - name: grpc
    port: 50051
```

### Docker Compose

```yaml
services:
  innies-memory:
    ports:
      - "8000:8000"
      - "50051:50051"
```

### 健康检查

gRPC 不提供探针接口。Kubernetes 探针继续使用 HTTP `/health/live` 和 `/health/ready`。

如需 gRPC 健康检查，可使用 [grpc-health-probe](https://github.com/grpc-ecosystem/grpc-health-probe)（需单独实现 `grpc.health.v1.Health` 服务）。

## 开发

### 重新生成 proto stubs

```bash
make proto-gen
```

### 运行测试

```bash
pytest test/unit/test_memory_grpc.py -v
```

## 性能对比

gRPC 相比 HTTP+JSON：

- **序列化** — protobuf 比 JSON 更紧凑（~30-50% 体积）
- **HTTP/2** — 支持多路复用、流式、头部压缩
- **类型安全** — 编译期类型检查
- **适用场景** — 微服务间高频调用、流式处理、强类型客户端

HTTP+JSON 优势：

- **调试友好** — curl/浏览器直接可用
- **生态成熟** — OpenAPI/Swagger 文档、网关支持更好
- **适用场景** — 公开 API、第三方集成、前端直接调用

建议：

- **服务间调用** — 优先 gRPC
- **外部集成/前端** — 优先 HTTP
