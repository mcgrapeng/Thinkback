# 真实栈实测：Mem0 2.2.1 + Milvus 能力核查与擦除残留矩阵

日期：2026-10-28
基线：`refactor/memory-architecture-rewrite@4935c2b`，mem0ai 2.2.1（已装）
环境：本机 `thinkback-postgres`（5432）、`thinkback-milvus`（宿主机 19540 → 容器 19530）、库 `Thinkback`、collection `agent_semantic_memory_v1`
端点：embedding `http://101.237.37.116:7345/v1`（200，模型 `zhiman-embedding` / Qwen3-Embedding-0.6B）；LLM `http://101.237.37.116:7383/v1`（`/models` 需鉴权 401，实际调用带 key 成功）
说明：所有查询限定在探针自己的 `user_id=probe_20261028` / `agent_id=probe_ns`，只输出计数与分数，不打印任何原文。探针数据已在结束时清除（Milvus 该 scope 归零，history/messages 探针文本归零）。

## 1. 结论

| 能力 | 实测结果 | 证据 |
| --- | --- | --- |
| Milvus collection schema | 支持 BM25：`text` 字段 `enable_analyzer=true`、`sparse` 字段 + `sparse_index` 索引、`vector_index` | `describe_collection`；`_has_bm25_schema=True` |
| BM25 英文/型号词 | 生效：`MA6000` bm25_score=0.289，`28nm` bm25_score=0.406 | `search(explain=True)` 的 `score_details` |
| BM25 中文 | 不生效：`点检`、`光刻机` 两个查询所有候选 bm25_score=0 | 同上；`text` 字段 `analyzer_params=None`，默认分析器不切中文 |
| entity matching | 完全关闭：`entity_boost` 恒为 0，无 `_entities` collection | 运行期日志 "spaCy is not installed"；`mem0ai[nlp]` 未安装（spacy / en_core_web_sm / rank_bm25 / scikit-learn 全缺） |
| score_details 可观测性 | mem0 支持，但只在 `explain=True` 时返回 | `mem0/memory/main.py:1740`；Thinkback 的 backend 从不传该参数 |
| 事实抽取质量 | 一条消息抽出 4 条中文事实，一条一个事实，实体逐字保留（MA6000 / 28nm / 气浮平台 / 上海） | `add()` 返回 4 条 ADD 事件 |
| 向量删除 | `mem0 delete` 后该 scope 的 Milvus 行归零，collection 回到 114 行 | `query(metadata["user_id"]==...)` |
| history 残留 | `mem0 delete` 让 history 表**新增** 4 行（329→333），被删事实文本仍留在表里（探针文本命中 1→2） | 计数对比 |
| messages 残留 | 原始用户消息留在 `messages` 表（每个 session scope 保留最近 10 条），mem0 公开 CRUD 不清理 | 计数对比 |
| Thinkback 的 purge_history | 清掉 history 8 行 + messages 1 行，探针文本归零；未误删同 scope 其他会话的消息 | `purge_history` 返回 `{'history': 8, 'messages': 1, 'supported': True}` |
| 多副本擦除 | `history.db` 是 pod 本地文件，一次擦除只清当前 pod | 适配器 docstring 已记录，真实栈复核一致 |

## 2. 三个需要决策的引擎级事实

### 2.1 中文 BM25 需要重建 collection

当前 collection 的 `text` 字段没有 `analyzer_params`，Milvus 默认分析器不切中文，中文查询的 BM25 通道等于关闭。语义召回不受影响（embedding 能处理中文），但领域专名、型号、缩写这类靠精确词命中的场景只能靠语义。

可选做法：新建 collection 并显式指定中文分词器（Milvus 2.6 支持 jieba tokenizer），配合回放、双读、回滚验证；或者接受「中文只有语义召回」，把领域召回补在服务层。两条路都要先有评测再决定。

### 2.2 排序分母随查询变化

mem0 2.2.1 的 `score_and_rank` 里，`max_possible_score` 取决于本次查询是否**有任何**候选命中 BM25：命中则全部除以 2.0，未命中则全部除以 1.0。同一批记忆在两个查询下的 final_score 不可比；同一个查询里，命中 BM25 的条目因为分母变大反而可能被排到未命中条目的后面。

`MA6000` 查询下的实测：命中 BM25 的条目 final_score 0.447，未命中的 0.232 / 0.188 / 0.184，本次排序仍正确，但这是运气不是保证。Thinkback 的 `recall_policy.dedupe_and_clip` 不能把引擎 final_score 当成唯一排序依据。

### 2.3 服务侧看不到 BM25 与 entity 的分解

`explain=True` 是 mem0 唯一的观测入口，Thinkback 的 `Mem0LibraryMemoryBackend.search` 没有透传参数。要么在后端加一个可选的 explain 开关，要么在服务层另建检索指标（分查询类型统计 BM25 命中数）。

## 3. 擦除残留矩阵（实测）

| 资源 | mem0 公开 CRUD 之后 | Thinkback purge_history 之后 | 剩余风险 |
| --- | --- | --- | --- |
| Milvus 主 collection | 已删 | 已删 | compaction 之前的物理字节回收依赖 Milvus 自身 |
| Mem0 SQLite `history` | 保留旧事实文本，且新增 DELETE 行 | 探针文本归零 | 只清当前 pod 的文件；备份与日志归档未覆盖 |
| Mem0 SQLite `messages` | 保留原始用户消息（每 scope 最近 10 条） | 按正文精确删除，探针文本归零 | 同上；跨副本只清到本 pod |
| PG L1 轮次 / L2 摘要 / 治理索引 | 不涉及 | 由 Thinkback 的 erase 路径处理（此前审计已验证） | 备份过期策略未定义 |
| 审计正文与日志 | 不涉及 | 未覆盖 | 需要按保留期单独定义 |

对外口径建议分四档声明：不可召回、逻辑删除、物理擦除、备份过期。当前实现能把前三档做到「单副本内可验证」，第四档没有机制。

## 4. 复现方式

```bash
# 环境
export NO_PROXY="*"          # mac 本地代理会劫持内网 httpx
export MILVUS_URL=http://localhost:19540

# 能力核查
uv run python - <<'PY'
from thinkback.infra.config import settings
from mem0 import Memory
from thinkback.memory.backends.mem0_library import build_mem0_library_config, register_mem0_embedder_provider
cfg = build_mem0_library_config(...)      # 参数见 src/thinkback/api/dependencies.py:123
register_mem0_embedder_provider()
m = Memory.from_config(cfg)
r = m.search("MA6000", filters={"user_id": "<probe>", "agent_id": "<probe>"}, limit=3, explain=True)
print(r["results"][0]["score_details"])   # semantic_score / bm25_score / entity_boost
PY
```

## 5. 未覆盖

- 真实 PostgreSQL 下的多实例并发 claim/CAS、SIGKILL 恢复、erase 各阶段崩溃点。
- 多 Pod 部署下的擦除与 history.db 行为。
- bge reranker 的同语料 A/B（端点可达，未跑对照）。
- 中文 jieba analyzer 重建 collection 后的召回对比。
