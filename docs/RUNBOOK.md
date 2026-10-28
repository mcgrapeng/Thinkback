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
