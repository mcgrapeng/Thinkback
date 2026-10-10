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

**Symptom**: `POST /memory/append` returns 503.

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
