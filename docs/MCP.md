# MCP 接入

Thinkback 的记忆能力同时以三种面貌暴露：**HTTP**（`/v2/memory/*`）、**gRPC**（`proto/memory.proto`）
和 **MCP**（本文件）。MCP 不是第三套语义，是同一套服务的投影：MCP 工具直接调用
`MemoryService`，不重写业务逻辑，所以校验、幂等、预算裁剪、擦除口径与 HTTP 完全一致。

MCP 的意义是「让任何能挂 MCP 的助手接上记忆」：Claude Desktop、Cursor、OpenAI Agents SDK
之类客户端按标准协议发现工具即可，不需要为 Thinkback 单独写客户端。

## 启动

```bash
# stdio：助手本地拉起进程（进程边界就是信任边界，不需要 token）
thinkback-mcp --namespace semiconductor

# streamable-http：容器/集群里跑，客户端按 URL 连
thinkback-mcp --transport http --host 0.0.0.0 --port 8765 \
  --namespace semiconductor --token "$THINKBACK_MCP_TOKEN"
```

也可用 `python -m thinkback.mcp`。参数与环境变量一一对应：
`--namespace` / `THINKBACK_MCP_NAMESPACE`、`--transport` / `THINKBACK_MCP_TRANSPORT`、
`--host` / `THINKBACK_MCP_HOST`、`--port` / `THINKBACK_MCP_PORT`、`--token` / `THINKBACK_MCP_TOKEN`。

http 传输下配了 token 就必须带 `Authorization: Bearer <token>`；没配就是**不鉴权**，
只适合受信内网。

## 工具面

工具面刻意克制：读 2、写 1、删 1、状态 1。客户端靠工具清单做发现，工具一多就没人看得完
（Cognee 只 pin 3 个是最好参照）。每个工具都带 MCP annotations 与结构化返回。

| 工具 | 作用 | annotations |
| --- | --- | --- |
| `append_memory` | 写入一条原始事件（触发 L1/L2/L3 沉淀） | `readOnlyHint=false` `destructiveHint=false` `idempotentHint=true` |
| `recall_memories` | 按 user / session / query 召回 L1/L2/L3 记忆 | `readOnlyHint=true` |
| `list_memories` | 列出可管理的长期记忆（默认只返回 ACTIVE） | `readOnlyHint=true` |
| `delete_memory` | 逻辑删除一条长期记忆（留墓碑） | `readOnlyHint=false` `destructiveHint=true` |
| `get_operation` | 查后台任务终态（写工具返回的是任务句柄） | `readOnlyHint=true` |

要点：

- **写成功 ≠ 全链路成功**。`append_memory` / `delete_memory` 返回的是任务句柄，
  最终状态要问 `get_operation`。
- **幂等锚点是 `event_id`**。同一事件重试要传同一个 `event_id`，否则会写成两条。
- **删 ≠ 擦除**。`delete_memory` 是逻辑删除（不可召回、保留原文与墓碑）；
  物理擦除（各层原文消失）走管理面 `POST /v2/memory/erase`。
- **namespace 由服务端决定**，不作为工具参数暴露。namespace 是租户/业务分组的边界，
  让调用方自己传等于任何助手都能换租户。

## 接 Lumen 怎么选

Lumen（`/Users/zhangpeng/workspace/zhiman/lumen`）是 TypeScript 服务，两条路都通：

| 方式 | 适用 | 说明 |
| --- | --- | --- |
| HTTP 客户端 | Lumen 后端调用 | `POST /v2/memory/events` / `/v2/memory/recall`；Lumen 的 `tenantId` 映射成 namespace，`userId` 映射成 `user_id` |
| MCP 客户端 | Lumen 已有 MCP client 能力（`service/src/mcp-server/client.ts`） | 把 Thinkback 当成远程 MCP server，工具直接暴露给 Lumen 的 agent |

两条路共用同一份 namespace 配置：**建议一企业一部署**。namespace 是分组标签，不是安全边界；
跨企业共用一套部署前必须先补租户鉴权（见下节）。

## 已知边界

- **namespace 不是安全边界**。业务面目前免鉴权，信任边界是内网网络。跨企业共用一套部署前
  要补租户鉴权，或坚持一企业一部署。
- **只支持单副本**。幂等缓存与限流桶在进程内，`infra/replica_guard.py` 会在「宣称多副本但
  前置没齐」时启动期硬失败。要对外承诺 HA，先把幂等与限流迁到 PG。
- **mem0 私有表依赖**。物理擦除要动 mem0 的 SQLite `history` / `messages` 表，因此
  `mem0ai` 锁死 `==2.2.1`；表结构漂移会**显式失败**（不会静默跳过后谎报擦除完成）。
- **MCP 与 HTTP 同进程同实例**。MCP 服务器直接持有 `MemoryService`，与 HTTP 面共享同一份
  L1 缓存与任务账本；跨进程的 mem0 history 擦除需要逐副本执行。

## 契约验收

接入方可以直接跑本仓库的接入契约测试（见 `tests/unit/test_integration_contract.py`）：
自定义类型全链路、事件模型、metadata、namespace 矩阵、HTTP↔gRPC 同形、幂等、擦除回执。
MCP 自身的契约在 `tests/unit/test_mcp_server.py`。
