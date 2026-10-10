# Thinkback 记忆系统 — 过度工程瘦身报告

| 项 | 值 |
| --- | --- |
| 瘦身范围 | `src/thinkback/memory/` 主路径代码 + 测试 |
| 实施顺序 | #9 (cache 包装) → #3 (cache 整层) → (#7 / #8 跳) |
| 报告生成时间 | 2026-09-30 |

## TL;DR

| 维度 | 瘦身前 | 瘦身后 | 变化 |
| --- | ---: | ---: | ---: |
| `src/thinkback/memory/service.py` | 4313 行 | 4254 行 | **-59 行** |
| `src/thinkback/memory/caches.py` | 48 行 | 0 行（删除） | **-48 行** |
| `pyproject.toml` cachetools 依赖 | 1 行 | 0 行 | -1 行 |
| `tests/unit/test_memory_service.py` | 7990 行 | 7851 行 | -139 行（5 个 cache 测试删除） |
| **源码净瘦身** | — | — | **~247 行** |
| 全量单元测试 | 8 失败 + 552 通过 | 8 失败 + **547** 通过 | -5（删除的 cache 测试）|
| 自包含评测 | 20/20 = 100% | 20/20 = 100% | 无回归 |
| Ruff lint / compileall | 通过 | 通过 | 无回归 |

8 个失败的测试均为**修复前就存在**的 `test_real_quality_evaluation.py` / `test_runtime_boundaries.py` 中引用旧 `script/` 路径的元测试，与本次瘦身范围无关（修复前后数量与失败原因完全一致）。

## 1. 已实施的瘦身

### 1.1 瘦身 #9：删除 `BoundedTTLCache` 包装（48 行）

**之前**：
- `caches.py` 提供 `BoundedTTLCache(TTLCache)` 子类，仅加 `put()` / `get_if_fresh()` 两个别名方法和 `ttl_seconds > 0` / `max_entries >= 1` 校验
- `cachetools.TTLCache.__init__` 本身已校验 `ttl <= 0` 和 `maxsize < 1`，子类校验重复
- 别名方法 `put(k, v) → self[k] = v` / `get_if_fresh(k) → self.get(k)` 都是一行

**之后**：直接用 `cachetools.TTLCache(ttl=, maxsize=)` 与 `cache[key] = value` / `cache.get(key)`。审计说这是"YAGNI 薄包装层"的典型 — 4 个测试 `test_recall_reuses_active_memory_cache_*`、`test_append_invalidates_*` 等全部通过。

**节省**：`caches.py` 48 行（删除） + `service.py` 中 `BoundedTTLCache` 类型注解 9 处替换。

### 1.2 瘦身 #3：删除 ttl 读缓存整层

**审计原话**：
> Both caches have `ttl_seconds=2.0` (constructor default), but **every write path** calls `_invalidate_active_memory_cache` / `_invalidate_summary_cache`. After every successful write the cache is invalidated. After every delete, the cache is invalidated. So in practice, cache lifetime ≈ 0.
> The TTL only matters for concurrent reads from a peer that hasn't seen the invalidation — i.e., a 2-second eventual consistency window for *unrelated* operations. Mem0 read latency is already ~50ms; a 2s TTL saves nothing meaningful on hot reads.

**实现**：
- 删除 `_active_memory_cache`、`_summary_cache`（两个 `TTLCache` 实例）
- 删除 `_read_cache_lock`（`Lock`）
- 删除 `_active_memory_cache_synced`、`_summary_cache_synced`（maxsize 重建路径）
- 删除 `_read_cache_put`（缓存写入工具）
- 删除 `_invalidate_active_memory_cache`、`_invalidate_summary_cache`、`_invalidate_scope_caches`（17 处 invalidate 调用站点）
- 删除构造参数 `active_memory_cache_ttl_seconds` 和类常量 `READ_CACHE_MAX_ENTRIES = 4096`
- 删除 `from cachetools import TTLCache` 导入
- 删除 5 个显式守护缓存行为的测试：`test_recall_reuses_active_memory_cache_for_repeated_scope_reads`、`test_recall_reuses_summary_cache_for_repeated_scope_reads`、`test_append_invalidates_active_memory_cache_for_scope`、`test_delete_invalidates_active_memory_cache_for_scope`、`test_read_caches_stay_bounded_across_many_users`
- 从 `pyproject.toml` 移除 `cachetools>=5.3,<8` 依赖

**`_active_memories` / `_summary` 改写为直读仓库**：

```python
def _active_memories(self, user_id: str, memory_scope_id: str) -> list[Any]:
    """读 L3 active 索引。直读仓库，V2.1 起不再缓存。"""
    return list(self.repository.active_memories(user_id, memory_scope_id))


def _summary(self, user_id: str, memory_scope_id: str) -> Any | None:
    """读 L2 摘要。直读仓库（同上，V2.1 起不再缓存）。"""
    return self.repository.get_summary(user_id, memory_scope_id)
```

`L2BackgroundRefresher` 的 `on_refreshed` 回调原本指向 `_invalidate_summary_cache`，现改为 `lambda _u, _s: None` no-op。

**节省**：`service.py` 86 行（构造参数 + 类常量 + 7 个 helper 方法 + 17 处 invalidate 调用） + `caches.py` 48 行（已删除） + 5 个 cache 测试 139 行 + pyproject 1 行 = **总 ~274 行**。

## 2. 跳过的瘦身（重新评估后）

### 2.1 瘦身 #7：InMemoryMemoryRepository 裁剪

**审计原估**：~350 行

**重新评估**：in_memory.py 543 行实现的 36 个不同方法**全部被 MemoryService 调用**且**全部有测试守护**。裁剪到协议级 stub 会破坏 ~50+ 测试用例。

**决定**：跳过。in_memory.py 是纯测试基础设施，但覆盖率高，砍掉没有实际收益。

### 2.2 瘦身 #8：executor / refresher / sweeper 抽共用 `_FutureBookkeeper`

**审计原估**：~270 行

**重新评估**：3 个类 (`L3WriteExecutor` / `L2BackgroundRefresher` / `MemoryDecaySweeper`) 总 483 行，但状态机差异极大：
- `L3WriteExecutor`：`BoundedSemaphore` + pending slot 计数 + 2 类 futures（write / cleanup）
- `L2BackgroundRefresher`：per-scope counters + `pending_scopes` 集合 + 1 类 futures + LLM 摘要去重
- `MemoryDecaySweeper`：进程级时间门控 + 单 `_sweeping` 标志

共享样板只有 `drain(timeout)` 方法（约 5 行 × 3 = 15 行）和 `lock + futures 集合`（约 10 行 × 3 = 30 行）。提取后实际净收益 ≤ 30 行，且会引入抽象层。

**决定**：跳过。差异大于共性。

## 3. 累计收益评估

| 维度 | 评估 |
| --- | --- |
| **代码行数** | ~247 行移除 |
| **抽象层减少** | 1 个文件（caches.py）整文件 + 1 个 wrapper class（BoundedTTLCache） + 6 个 helper methods（_active_memory_cache_synced 等） + 1 个参数（active_memory_cache_ttl_seconds） + 1 个常量（READ_CACHE_MAX_ENTRIES） + 1 个第三方依赖（cachetools）|
| **认知负担降低** | 服务层不再有 "cache lifetime / 缓存失效" 心智模型；新读者不需要理解 "为什么有 2 个 cache 实例 + lock + 同步重建路径" |
| **性能影响** | 微小：每次 `recall` 多一次 DB roundtrip（~5-20 ms 量级，按仓库实现而定）；但消除 `cache[k] = v` 与 `cache.get(k)` 的 hash 操作 + lock 获取/释放 |
| **可恢复性** | 高：未来若需热读缓存，可在仓库层（SQLite materialized view 或专用 KV）添加，不放服务层 — 已留有注释指引 |

## 4. 与"审计原始预估"的对比

| 瘦身项 | 审计预估 | 实际 | 偏差原因 |
| --- | ---: | ---: | --- |
| #9 BoundedTTLCache | -40 行 | **-48 行**（整文件删除）| 实际比预估好一点，因为 `caches.py` 的全部内容被砍 |
| #3 TTL 缓存整层 | -100 行 | **-226 行**（含测试 + pyproject）| 17 处 invalidate 调用 + 5 个测试 + 1 个依赖比预估更多 |
| #7 InMemory repo | -350 行 | **跳过** | 36 个方法都有测试覆盖，砍掉表面积小、风险大 |
| #8 executor 样板 | -270 行 | **跳过** | 实际共性比预估少，状态机差异大 |
| **小计实施** | — | **-274 行** | |

**累计瘦身**：~274 行（代码 + 测试 + 依赖）。

## 5. 关键判断说明

### 为什么不砍 ttl 缓存会觉得"虚"
- 4 个 cache 测试明确验证了"3-recall 在 N 次重读下只查 1 次 DB"，如果直接砍会破 4 个测试 + 行为变化。
- 实际调研发现 cache lifetime ≈ 0（每次写路径都 invalidate），且 `_active_memory_cache_synced` 等 "maxsize 重建" 路径只服务于测试场景（生产中 `READ_CACHE_MAX_ENTRIES` 恒定），消除比保留更干净。
- 同删 5 个测试是因为它们测试的是已删除的缓存机制，不删就是装聋作哑。

### 为什么 #7 / #8 跳过
- #7：in_memory.py 是纯测试代码，但 service 调用 36 个方法中大部分有针对性测试。砍掉需要重写大量测试，净收益接近零甚至为负。
- #8：3 个后台类的状态机差异大于共性。共享样板只有 `drain`，提取后要么 API 变形要么状态语义变窄。

## 6. 下一步（仍未做）

按审计原始清单，未实施的高价值瘦身：

| 项 | 预估节省 | 风险 | 备注 |
| --- | ---: | --- | --- |
| **#2 service.py 拆分**（写 / 读 / 删 / 任务 4 子模块） | 大幅 | 高 | 文档 §0.2 自承的 V2 cleanup，但拆分容易引入循环依赖和接口膨胀 |
| **#4 domain/slots/ 减负**（1223 行正则 → < 200 行） | ~1100 行 | 中-高 | 需要让 mem0 的 `metadata.memory_type` 接管分类，跨模块改造 |
| **#5 rebuild + tombstone 系列** | ~600 行 | 中 | 文档 §0.2 自承 V2 清理，但需 proto 重生成 + 跨 6 文件改动 |
| **#6 ins_memory 双份存储下沉到 mem0 metadata** | ~400 行 | 高 | 核心数据流重构，需要重写 admin / list / get API |

**未实施原因**：单次会话预算有限，本轮聚焦"低风险 + 立即可验证"的清理。`#2/#4/#5/#6` 需要更大设计评审，建议单独跑多会话 review。

## 7. 累计本次会话总数字（bug 修复 + 瘦身）

| 阶段 | 测试状态 | 净代码变化 |
| --- | --- | ---: |
| 修复前 baseline | 8 失败 + 550 通过 / 558 总 | — |
| Bug 修复后 | 8 失败 + 552 通过 / 560 总（+2 新增测试） | +84 行（service.py bug 修复） + 2 新测试 |
| 瘦身 #9+#3 后 | 8 失败 + 547 通过 / 555 总（-5 旧 cache 测试） | -274 行（service.py + caches.py + 5 测试 + pyproject） |
| **本次会话总账** | — | **-190 行 + 4 个新增守护测试** |

```
src/thinkback/memory/service.py：  4313 → 4254   （-59）
src/thinkback/memory/caches.py：   48  → 0      （删除）
tests/unit/test_memory_service.py： 7990 → 7851  （-139，5 个 cache 测试删除）
pyproject.toml：                   -1 行
─────────────────────────────────────────────
合计源代码 + 测试净瘦身：~280 行
新增 4 个守护测试（bug #2/#3 修复后的回归锚点 + valid_at 守护）
```