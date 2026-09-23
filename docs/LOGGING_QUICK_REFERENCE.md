"""
英文日志快速参考 - thinkback

====== 日志方法速查表 ======

started(action, **metrics)
  → [OP] action started | metrics
  例：LOGGER_MEMORY.started("observation_save", entity_count=5, content_length=200)

completed(action, status, **metrics)
  → [OP] action completed (status) | metrics
  例：LOGGER_STORE.completed("embedding_store", "ok", vector_dim=1536, stored_count=10, duration_ms=120)

failed(action, error_type, detail="", **metrics)
  → [OP] action failed (ERROR_TYPE: detail) | metrics
  例：LOGGER_EMBED.failed("embedding_gen", "API_ERROR", detail="OpenAI API rate limit exceeded")

degraded(action, reason, **metrics)
  → [OP] action degraded (reason) | metrics
  例：LOGGER_MEMORY.degraded("consolidation", "partial_success", consolidated=8, total=10)

with timed_block(action, warn_threshold_ms=1000, **metrics):
  → 自动计时，超过阈值标记为 slow
  例：with LOGGER_QUERY.timed_block("semantic_search", warn_threshold_ms=500, k=10):


====== thinkback 预定义日志器 ======

LOGGER_MEMORY      # 内存操作（保存、搜索、更新）
LOGGER_EMBED       # Embedding 生成
LOGGER_STORE       # 后端存储（向量DB、文本DB）
LOGGER_QUERY       # 查询执行


====== 快速搜索 ======

# 所有内存操作失败
grep '\[MEMORY\].*failed' logs/

# embedding 生成超时
grep '\[EMBED\].*slow' logs/

# 特定实体的操作
grep 'entity_name=EntityA' logs/

# 追踪完整链路
grep 'trace_id=abc123' logs/
"""
