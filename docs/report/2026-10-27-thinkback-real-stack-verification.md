# Thinkback 真实栈验证记录（Milvus + Mem0 + LLM）

日期：2026-10-27

## 范围

在真实依赖上验证三件事，每条都有可复跑的脚本：

| 验证项 | 脚本 | 结果 |
| --- | --- | --- |
| erase 的物理擦除（各层残留） | `tests/script/real_mem0_erase_verification.py` | 16/16 断言通过 |
| 半导体垂类语料的真实抽取与召回水位 | `tests/script/eval/run_semiconductor_evaluation.py --real` | 68.8% ~ 75%（11~12/16） |
| 自包含评测 + 垂类评测（CI 门禁） | `make eval` | 20/20 与 16/16，基线达标 |

真实依赖：Milvus 2.6.15（standalone，内嵌 etcd + 本地存储）、Mem0 2.2.1（进程内）、
PostgreSQL 18.4、LLM `zhiman38_27b`、embedding `zhiman-embedding`（1024 维）。
本机为 arm64 macOS；Milvus 以 `deploy/milvus-local/docker-compose.yml` 起。

## 过程中定位并修掉的真问题

### 1. L3 抽取在「只有 user 消息」时静默为空（P0）

Thinkback 的 L3 抽取只把 user 消息喂给 mem0（assistant 的话不是用户事实来源）。
实测该模型在「New Messages 只有一条 user」时直接返回 `{"memory": []}` —— 整轮抽取
为空、不报错、L3 永远写不进东西。补了 custom_instructions 规则后恢复。

### 2. 抽取会丢掉具体事实（垂类语料实测）

补规则前，垂类语料上的抽取空返回率高达 81%。补了两条规则：

- 只有 user 消息是正常输入，仍要抽取；
- 工作/业务上下文（职责、工艺设备、目标、要求、约束、时限、客户信息、投诉、
  数字指标）都属于可记事实，消息里有就必须抽一条。

之后垂类用例从 56% 升到 68.8%~75%。**剩余失败全部是模型侧保真度**（丢掉
「套刻」「AEC-Q100」这类具体项，或对某些表述整段不抽），不是系统错误。

### 3. Milvus 集合默认 Bounded 一致性，写后立刻查不到

集合默认一致性级别是 Bounded —— 写入后立刻用同一连接查，新向量可能还看不见。
对记忆服务是硬伤：用户刚说过的话，紧接着问，召回却查不到。现在首次写入后
把集合级别提到 Strong（集合由 mem0 创建，只能事后改属性）。

### 4. 内网网关请求被环境代理劫持

LLM / embedding 客户端此前继承环境里的 HTTP/SOCKS 代理：本机个人代理会让请求
走 SOCKS，且缺 `socksio` 直接构造失败。改成 `httpx.Client(trust_env=False)`。

### 5. `MEMORY_LLM_KEY` 没配时被当成「免鉴权」

`.env` 里只配了 `OPENAI_API_KEY`，`MEMORY_LLM_KEY` 为空 → 服务按「端点免鉴权」
发请求 → 401。`.env` 已补上该变量（生产用 Secret 注入）。

## erase 的真实残留面

Mem0 2.2.1 没有公开的 history 清理接口，但它把两类原文落在本地 SQLite
（`history_db_path`）：

- `history` 表：`old_memory` / `new_memory` 存事实原文，按 `memory_id` 索引；
- `messages` 表：按 `user_id + agent_id` 存最近 10 条**原始消息**。

erase 现在按 memory_id / session scope 定向 DELETE 这两张表。真实栈验证覆盖：
PG 三层（轮次原文、摘要正文、索引行）、Milvus 向量、Mem0 事实抽取历史、Mem0
原始消息 —— 擦除后全部查不到密文。

仍然存在的残留面（审计里如实记录，不宣称彻底擦除）：

- `history.db` 是**进程本地文件**，多副本部署每个副本各有一份，一次擦除只清到
  当前副本；
- 向量库删除是**逻辑删除**，物理回收要靠 compaction。

## 复跑方式

```bash
docker compose -f deploy/milvus-local/docker-compose.yml up -d

MILVUS_URL=http://localhost:19540 \
MEM0_HISTORY_DB_PATH=.mem0/verify-history.db \
  uv run --group dev python tests/script/real_mem0_erase_verification.py

MILVUS_URL=http://localhost:19540 \
MEM0_HISTORY_DB_PATH=.mem0/verify-history.db \
  uv run --group dev python tests/script/eval/run_semiconductor_evaluation.py --real
```

本机装了个人 SOCKS 代理时，两个脚本都会先清掉代理环境变量（生产 pod 没有这些
变量）。真实栈评测的数字会随模型采样波动，**CI 门禁只跑自包含模式**
（`make eval`：内存仓储 + fake 后端，确定性）。

## 还没做的

- 真实 Milvus 上「相关度阈值 0.5」的精度标定：当前阈值是经验值，垂类语料里
  能看到「抽出来了但没召回」的情况，需要用真实语料标定阈值曲线。
- 抽取保真度的进一步提升需要换更强模型或加 few-shot 示例；继续在
  custom_instructions 里堆规则会开始对语料过拟合。
