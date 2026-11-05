# Operations Runbook

This runbook helps operators debug and recover Thinkback in production.

## Quick reference

| Symptom | First check | Action |
| --- | --- | --- |
| API returns 500 | `/health/ready`, logs | Check DB / Milvus connectivity |
| API returns 503 | Queue depth | L3 backlog full; adjust workers |
| API returns 429 | `X-RateLimit-*` headers | Adjust plan / rate limits |
| Recall degraded | `degradation_reasons` in response | LLM/embedding/vector store down |
| Task stuck in `running` | Task state machine | Reclaim orphan tasks |
| Memory missing | `memory_status` in DB | Check `DELETED` / `SUPPRESSED` |

## L3 queue overflow

**Symptom**: `POST /v2/memory/events` returns 503.

**Fix**:
1. Increase `MEMORY_L3_EXECUTOR_WORKERS` (default 16)
2. Increase `MEMORY_L3_MAX_PENDING_TASKS` (default 256)
3. Or switch to `MEMORY_L3_WRITE_MODE=sync` for debugging

## Orphan tasks

**Symptom**: Tasks stuck in `running` state forever.

**Cause**: Process crashed mid-task.

**Fix**:
```bash
curl -X POST http://localhost:8000/admin/api/maintenance/reclaim-orphan-tasks
```

Or use admin UI: Overview → 回收孤儿 button.

## LLM / embedding down

**Symptom**: Recall responses marked `degraded=True`.

**Check**:
```bash
curl $MEMORY_LLM_BASE_URL/v1/chat/completions \
  -H "Authorization: Bearer $MEMORY_LLM_KEY"
```

**Fix**:
1. Verify `MEMORY_LLM_BASE_URL` and `MEMORY_LLM_KEY`
2. If using Ollama / vLLM, verify the server is running
3. Switch to `MEMORY_L3_WRITE_MODE=sync` as fallback

## Milvus unreachable

**Symptom**: Append fails, logs show Milvus errors.

**Check**: `curl $MILVUS_URL/healthz`

## Milvus collection rebuild (中文 BM25)

**Symptom**: 日志出现 `milvus collection schema drifted from thinkback`，
或者中文查询的 BM25 分数恒为 0。

**Why**: Milvus 不允许原地改 `text` 字段的分析器（`analyzer_params does not
allow update`），而 mem0 自建的集合没有 analyzer_params，中文整句被当成一个词。
换分析器只能新建集合再把行搬过去（ADR-0007）。

**Steps**（新集合名自己定，旧集合保留至少 30 天）：

```bash
# 1. 体检：看两个集合的行数与分析器
uv run python script/migrate_milvus_collection.py status \
  --source agent_semantic_memory_v1 --target agent_semantic_memory_v2

# 2. 建好新集合（带中文分析器）并复制式回填，可重复执行
uv run python script/migrate_milvus_collection.py backfill \
  --source agent_semantic_memory_v1 --target agent_semantic_memory_v2

# 3. 切换判据：行数一致 + 抽样召回有命中
uv run python script/migrate_milvus_collection.py verify \
  --source agent_semantic_memory_v1 --target agent_semantic_memory_v2
```

**Switch**（判据通过后改配置并重启）：

```
MEMORY_MILVUS_COLLECTION=agent_semantic_memory_v2
MEMORY_MILVUS_MIRROR_COLLECTION=agent_semantic_memory_v1
```

镜像集合开着时写入、删除、修改都会同步到旧集合，召回先读新集合、命中不足
时回落旧集合。

**Rollback**（48 小时双读窗口内）：把两个值换回去并重启，不用补数据。

**Cleanup**（窗口结束后）：清空 `MEMORY_MILVUS_MIRROR_COLLECTION`，确认旧集合
不再被写入后再删除；旧集合至少保留 30 天。

## 真实栈门禁（gate-real）

真实栈门禁验的是「真实引擎 + 真实向量库」上的抽取质量、召回水位、擦除残留与
混合检索，自包含评测（`make eval`）全绿只证明 fake 编排契约。

**前置**：真实 Milvus、真实 LLM/embedding endpoint、可写的 PostgreSQL。

```bash
docker compose -f deploy/milvus-local/docker-compose.yml up -d
MILVUS_URL=http://localhost:19540 make gate-real
```

宿主访问 Milvus 走 compose 发布的端口（`MILVUS_HOST_PORT`，默认 19540 →
容器 19530）；`.env` 里的 `MILVUS_URL` 要写宿主端口，写成容器端口会连不上，
服务会静默降级（向量库不可达，记忆写不进去）。

**门禁项与地板**：抽取非空率 1.00；期望事实覆盖率 / 召回率 ≥ 0.66；禁存词泄漏 0；
擦除残留 0 条（Milvus / mem0 history / mem0 messages / PG 索引）；幂等重放完全一致；
集合 schema 归 Thinkback（分析器 `chinese`）；英文词与中文词各抽样一次必须命中；
引擎里的每条事实都要有对应的本地 ACTIVE 索引行。

**报告**：`docs/memory/report/real/gate-<commit>-<时间戳>.json`，带 commit 号。
退出码 0 = 全绿。

**自清理**：门禁用自己的作用域（`gate-<hash>`），跑完擦除，两个集合的行数回到
跑前的值 —— 跑完看行数对不上，说明有东西没擦干净。

**评测集合**：真实栈的评测/探针脚本写独立集合 `agent_semantic_memory_eval`，
不落生产集合。走 HTTP 驱动运行中服务的脚本（如 `real_mem0_quality_regression.py`）
由服务进程的配置决定集合，被驱动的服务要用
`MEMORY_MILVUS_COLLECTION=agent_semantic_memory_eval` 启动。

## 垂类评测（真实模式）

`gate-real` 验的是「记忆系统本身对不对」；垂类评测验的是「换成垂类语料后还对不对」
—— 工艺咨询 / 设备选型 / 良率分析 / 客户支持四类场景的专业词能不能被记住、被召回，
以及垂类机制（namespace 隔离、类型准入、保留期、assistant 不成为事实）。

两种模式：

- **自包含**（`make eval` 的一步）：内存仓储 + 假后端，不联网、确定性、进 CI。
  **只证明编排契约** —— 假后端不跑 LLM 抽取，整轮原文直接当记忆存，
  所以这里的「抽出来了」是假的，抽取与召回质量都不作数。
- **真实**（`--real`）：真实 PG + Milvus + Mem0 + LLM，看真实抽取与语义召回的水位。
  **不进 PR 门禁**（要真实栈 + 分钟级），但**进夜间 CI**（`real-stack-gate` job）。

```bash
uv run --group dev python tests/script/eval/run_semiconductor_evaluation.py --real
uv run --group dev python tests/script/eval/check_baseline.py \
  --report-dir docs/memory/report/semiconductor \
  --baseline tests/eval/baseline_semiconductor_real.json
```

**模型名要写对**：端点上没有的模型名会让抽取**全空**，门禁以「召回 0 条」的
形式红，而根因是配置。`MEMORY_LLM_MODEL` / `MEMORY_EMBEDDING_MODEL` 要与端点
实际提供的模型一致（端点上有哪些模型：`curl -H "Authorization: Bearer $KEY" $BASE/models`）。
产品默认值面向公有云端点，自建端点必须覆盖。

**报告**：`docs/memory/report/semiconductor/semi-eval-<时间戳>-<run_id>.json`。
真实模式的失败按原因分桶：抽取空 / 抽出来但缺事实 / 事实已在索引却没召回 /
禁词泄漏 / 机制违规。**泄漏与机制违规卡 0** —— 那是正确性问题，不是质量问题。

## Retention cleanup (保留期物理清理)

**What it does**: 到期的事实先不可达（引擎隐藏 + 本地过滤），过了 7 天宽限期
才由这个作业物理删除：向量、引擎侧事实历史、治理行一起删。原始消息不清 ——
消息属于轮次，同轮次可能还挂着没过期的事实。

**Run**:

```bash
# 先看清楚要删哪些，不动数据
uv run python script/run_retention_cleanup.py --dry-run

# 真删（作业默认关闭，要 MEMORY_RETENTION_ENABLED=true 或 --force）
uv run python script/run_retention_cleanup.py --namespace thinkback --user-id u-123
```

**Scope**: 共享库里跑作业时务必带 `--namespace` / `--user-id`，否则一次运行会
清掉别人的到期数据。

**Audit**: 动作名 `memory.retention.purge`，只留计数。抑制（decay 置
SUPERSEDED）与删除在审计上是两件事，看动作名就能分开。

## Database migration issues

**Symptom**: Alembic migration fails on startup.

**Fix**:
```bash
uv run alembic upgrade head
uv run alembic current
uv run alembic downgrade <revision>
```

## Memory stuck as DELETED

**Symptom**: Memory shows as deleted but shouldn't be.

**Fix**:
1. Check audit log: `GET /admin/api/audit?action=delete`
2. Use governance action to rebuild from journal

## Recovery: rebuild from journal

```bash
curl -X POST http://localhost:8000/admin/api/rebuild \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "alice",
    "session_id": "support-001",
    "rebuild_l2": true,
    "rebuild_l3": true
  }'
```

## Performance tuning

| Knob | Default | When to adjust |
| --- | --- | --- |
| `MEMORY_L3_EXECUTOR_WORKERS` | 16 | High write throughput |
| `MEMORY_L3_MAX_PENDING_TASKS` | 256 | Burst traffic |
| `MEMORY_L2_LLM_ENABLED` | `true` | Disable to save LLM cost |
| `MEMORY_DECAY_ENABLED` | `false` | Enable for long-tail suppression |

## Contact

For production issues, open an [issue](../.github/ISSUE_TEMPLATE/bug_report.md).
