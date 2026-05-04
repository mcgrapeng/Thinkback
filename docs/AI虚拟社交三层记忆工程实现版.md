# AI虚拟社交三层记忆工程实现版

> 本文承接《AI虚拟社交三层记忆架构》的工程实现细节，面向实现、联调和排障。架构主文讲清楚问题、三层分工、四条闭环和数据落点；本文只展开服务入口、任务编排、幂等、降级、治理和可观测性。
>
> 工程实现仍遵守一个边界：L3 不自研记忆引擎。写入侧优先复用 Mem0 的抽取、分类和去重能力；检索侧优先复用 Mem0 的语义检索、过滤、阈值、数量限制和重排能力；显式更新/删除由业务命令调用 Mem0 update/delete/delete_all。冲突收敛按当前 Mem0 版本实测，能交给 Mem0 的交给 Mem0，不能稳定表达的部分才由业务侧显式 update/delete/rebuild 兜底。应用层只做业务隔离、任务编排、审计、L1/L2 和最终召回融合。

---
## 1. 工程参考流程

> 本工程版用于实现和联调时查细节。架构主文先读 §2 和 §3 的主线即可；这里的完整流程不是首版任务清单。

阅读约定：

- `P0` 是生产首版必须闭环的最小实现：可靠轮次存储、L1 派生缓存、L2 摘要、Mem0 L3、基础删除、手动重建、任务查询和降级召回。
- `P1` 是稳定性增强：自动重试、超时补偿、更多查询条件、跨层预算优化和更细的诊断字段。
- `P2` 是强治理能力：write fence、history snapshot、tombstone、suppression、dead_letter、decay 和人工处置。
- 后文流程中标注 `P1/P2` 的步骤不得反向阻塞 P0 上线。实现时先按每节的“P0 主路径”落地，再按真实风险逐项打开增强能力。
- 文中 `memory_task / current_summary / summary_round_journal` 是逻辑组件名，用于解释任务、摘要和轮次账本职责，不强制等同于最终物理表名。物理表命名以数据落点和 Schema 为准；长期记忆索引表为 `tb_memory`，用户-角色关系状态表为 `tb_user_character_state`。

### 1.0 首版实施流程速查

> 这部分只保留首版要跑通的链路。后面的完整流程如果出现 `fence / snapshot / suppression / tombstone / dead_letter`，默认都按增强治理能力理解。

append 首版流程：

```text
+------------------------------+
| append 请求                   |
| 完整 user/assistant 轮次      |
+--------------+---------------+
               |
               v
+------------------------------+
| 契约与幂等校验                |
| round_id / durable round      |
+--------------+---------------+
               |
               v
+------------------------------+
| 可靠轮次存储                  |
| 作为 L2/L3 输入真值           |
+--------------+---------------+
               |
               v
+------------------------------+
| 刷新 L1 派生缓存              |
| 缓存失败不影响真值            |
+--------------+---------------+
               |
               v
+------------------------------+
| 派发 extract task             |
| worker 异步更新 L2 / L3       |
+------------------------------+
```

recall 首版流程：

```text
+------------------------------+
| recall 请求                   |
| query + user×character        |
+--------------+---------------+
               |
               v
+------------------------------+
| 默认读取 L1 / L2              |
| L1 当前会话，L2 阶段摘要      |
+--------------+---------------+
               |
               v
+------------------------------+
| 按需检索 L3                   |
| Mem0 search + filters/rerank  |
+--------------+---------------+
               |
               v
+------------------------------+
| 融合与裁剪                    |
| 安全过滤 / 去重 / 预算裁剪     |
+--------------+---------------+
               |
               v
+------------------------------+
| 返回 items                    |
| 单层失败可降级成功            |
+------------------------------+
```

delete 首版流程：

```text
+------------------------------+
| delete 请求                   |
| memory / session / all        |
+--------------+---------------+
               |
               v
+------------------------------+
| 作用域与任务幂等              |
| scope + version / operation   |
+--------------+---------------+
               |
               v
+------------------------------+
| 删除目标 L3                   |
| Mem0 delete / delete_all      |
+--------------+---------------+
               |
               v
+------------------------------+
| 清理 L1 并标记 L2             |
| dirty 或 stale                |
+--------------+---------------+
               |
               v
+------------------------------+
| 触发或等待 rebuild            |
| 修复可能被污染的摘要          |
+------------------------------+
```

rebuild 首版流程：

```text
+------------------------------+
| rebuild 请求                  |
| session 或 all                |
+--------------+---------------+
               |
               v
+------------------------------+
| 任务身份                      |
| scope + history / operation   |
+--------------+---------------+
               |
               v
+------------------------------+
| 读取历史对话源                |
| 删除/修复后的完整有效轮次     |
+--------------+---------------+
               |
               v
+------------------------------+
| 重算 L2                       |
| user×character 阶段摘要       |
+--------------+---------------+
               |
               v
+------------------------------+
| 重写 L3                       |
| 按 scope 调用 Mem0            |
+--------------+---------------+
               |
               v
+------------------------------+
| 写入任务结果                  |
| task query 可查询             |
+------------------------------+
```

首版是否可上线，主要看这几件事是否成立：

- L1 只是派生缓存，丢失不影响 L2/L3 的输入真值。
- L2/L3 的输入来自 durable round 或历史对话源，不从 L1 反推。
- delete 后 L3 不再直接召回，L2 不把 dirty summary 原样返回；dirty 必须由 rebuild 修复，stale 才允许滚动摘要自然修正。
- rebuild 能从历史源恢复 L2/L3。
- 所有异步任务都能通过 task query 查到最终状态。

### 1.1 append 工程参考流程

```text
[append memory command
 request_id / user_id / character_id /
 session_id / round_id / round_index? /
 messages[] / timestamp]
     |
     v
[输入契约校验]
 必填字段 / 类型 / 枚举值 / 作用域校验
 必填：
 - request_id
 - user_id
 - character_id
 - session_id
 - round_id
 - messages[]
 - timestamp
 推荐：
 - round_index
 校验：
 - round_id 必须全局唯一
 - round_index 若提供，必须是正整数
 - character_id 必须非空
 - messages[] 必须是长度为 2 的数组
 - messages[0].role 必须为 user，messages[1].role 必须为 assistant
 - 每条消息都必须包含 message_id / role / message_content
 - 每条 message_content 必须是字符串
 - timestamp 必须可解析
     |
     +--> invalid -> [返回 rejected
                       code=1001 /
                       message=invalid contract /
                       data(request_id, round_id, session_id)]
     |
     v
[规范化输入]
 - 逐条 trim messages[].message_content -> normalized_messages[].normalized_content
 - 保留 messages[].message_id / role / timestamp
 - 保留 session_id / round_id / round_index? / timestamp 作为来源信息
 - 计算 round_fingerprint = hash(normalized_messages + round_index? + session_id + timestamp)
     |
     +--> normalized_messages 中两条 normalized_content 同时为空
          -> [返回 skipped
              code=1002 /
              message=empty round content /
              data(request_id, round_id, session_id)]
     |
     v
[安全守卫]
 记忆域安全禁入判定
 说明：
 - 拦截平台策略明确禁止进入记忆域的内容
 - 拦截系统提示词、内部控制指令、工具调试回显、密钥/凭证、审计黑名单内容
 - 拦截高风险越权 / prompt 注入内容
 - 这里只判断“能不能进入记忆域”，不判断“值不值得进长期记忆”
     |
     +--> blocked -> [返回 rejected
                      code=1003 /
                      message=blocked by safety /
                      data(request_id, round_id, session_id)
                      并记录 warn 日志]
     |
     v
[推导业务身份]
 business_key = round:{round_id}
 task_id = memory-extract:{round_id}
 说明：
 - request_id 仅用于链路追踪
 - round_id 是完整轮次的业务幂等锚点
     |
     v
[读取 scope 运行态（P2）]
 - P0 可跳过本步骤；只要求按 round_id 查询 durable round 与 task 状态完成幂等分流
 - 读取 current_write_fence_version
 - 读取 append_cursor_round_index
 - 两者的作用域均为 user_id × character_id
 - append_cursor_round_index 表示当前作用域已 durably 接纳的最后一轮编号
     |
     +--> read failed -> [返回 error
     |                    code=2001 /
     |                    message=scope state query failed /
     |                    data(request_id, round_id, session_id)]
     |
     v
[查询 durable round 轮次身份 + PG memory_task(task_id)]
 说明：
 - 即使 task 已 completed，也必须先校验同 round_id 是否与现有 durable round 记录一致
 - 若先按 completed 返回 already_done，会漏掉“同一 round_id 但内容/作用域不同”的冲突提交
     |
     +--> query failed -> [返回 error
     |                    code=2001 /
     |                    message=scope state query failed /
     |                    data(request_id, round_id, session_id)]
     |
     v
[校验轮次身份与顺序]
 - 使用上一步查询到的 summary_round_journal 记录判断 round_id 是否已存在
 - 若 round_id 已存在：
   · P0：round_fingerprint 相同，且 user_id / character_id / session_id 一致
   · P2：额外要求 write_fence_version 一致
     -> 视为并发重放，回到 task 状态分流
   · round_fingerprint 不同，或 scope/generation 不一致
     -> 返回 rejected
        code=1005 / message=round conflict
 - 若 round_id 不存在：
   · P0：允许按 timestamp / round_index 排序进入 durable round；round_index 缺口只记录 warn，不作为默认拒绝条件
   · P2：仅允许 round_index == append_cursor_round_index + 1
   · P2：round_index <= append_cursor_round_index 或 round_index > append_cursor_round_index + 1
     -> 返回 rejected
        code=1006 / message=round index conflict
     |
     v
[幂等分流]
 - 若 round_id 已存在且轮次身份一致：
   · task status=completed -> 返回 already_done(code=1004)
   · task status in {pending, running} -> 返回 accepted(code=0)
   · P0：task status=failed -> 返回 error(code=2006)
   · P2：task status in {failed, dead_letter} -> 返回 error(code=2006)
   · task 不存在 -> 返回 error(code=2006)，并记录 error 日志；该状态表示 durable round/task 不一致，需管理面修复
 - 若 round_id 不存在：
   · task 不存在 -> 继续进入 task 登记 + round 持久化
   · task status=pending 且 last_error_stage=journal_write 且 payload.round_fingerprint 与本次一致
     -> 进入 journal 恢复写入路径，继续尝试持久化 accepted round
   · task 已存在 -> 返回 error(code=2006)，并记录 error 日志；该状态表示 task/journal 不一致，需管理面修复
     |
     v
[构造 extract_task_payload]
 扁平结构，全部顶层字段：
 - request_id
 - user_id
 - character_id
 - session_id
 - round_id
 - round_index
 - messages[]
   每个元素包含 message_id / role / normalized_content / timestamp
 - timestamp
 - round_fingerprint
 - write_fence_version（P2）
     |
     v
[登记或更新 PG memory_task]
 task_id = memory-extract:{round_id}
 task_type = memory
 op_type = extract
 task_id 唯一约束
 task not found -> create(status=pending)
 retry_count / max_retries / next_retry_at
 last_error_stage
 payload = 最新 extract_task_payload
     |
     +--> upsert conflict -> [重新进入“查询 journal + task -> 校验轮次身份 -> 幂等分流”
     |                       避免在未校验 journal 身份前直接按 task 状态返回]
     |
     +--> persist failed -> [返回 error
     |                     code=2003 /
     |                     message=task persist failed /
     |                     data(request_id, round_id, session_id)]
     |
     v
[持久化 accepted round 到 summary_round_journal]
 写入范围：
 - user_id × character_id
 写入字段：
 - P0：round_id / round_index? / session_id / messages[] / timestamp / round_fingerprint / round_state=active
 - P2：额外写入 write_fence_version
 写入规则：
 - summary_round_journal 是完整轮次的 durable truth
 - P0 供 L2 按 timestamp / round_index ASC 稳定排序读取
 - P2 供 L2 按连续 round_index ASC 顺序读取
 - 不依赖 TTL，不随 L1 滑窗淘汰而丢失
 - P2：L2 只读取当前 fence 代的数据
 - P2：推荐与 append_cursor_round_index 在同一个 PostgreSQL 事务中写入，或使用等价 CAS 保证二者原子推进
     |
     +--> persist failed -> [更新 PG memory_task
     |                     可自动重试 -> pending / retry_count + 1 / next_retry_at / last_error_stage=journal_write
     |                     不可自动重试 -> failed / last_error_stage=journal_write
     |                     返回 error
     |                     code=2005 /
     |                     message=journal write failed /
     |                     data(request_id, round_id, session_id)]
     |
     v
[推进 append_cursor_round_index（P2）]
 - 将 append_cursor_round_index 更新为当前 round_index
 - 仅在 summary_round_journal 持久化成功后推进
 - CAS 条件必须是旧 cursor 仍等于读取时的 append_cursor_round_index；CAS 失败按并发冲突重新读取 task / journal 状态后分流
     |
     v
[尽力刷新 L1 派生缓存]
 - 写入范围：user_id × character_id × session_id 当前会话窗口
- 写入介质：Redis sorted set / window，默认保留最近 10 个完整轮次，并受 token budget 约束；轮次数与 token budget 均应配置化
 - P0 写入字段：round_id / round_index? / session_id / messages[] / timestamp
 - P2 额外写入字段：write_fence_version
 - 写入规则：
  · L1 默认缓存最近 10 个完整轮次，并按 token budget 裁剪
   · P2 entry 必须带 write_fence_version
   · P2 recall 只读取当前 fence 代的数据
 - P2 在真正写 L1 前再次读取 current_write_fence_version
 - P2 若 fence 已变化 -> 跳过本次 L1 刷新，不视为主链路失败
     |
     +--> cache write failed -> [记录 warn 日志
     |                          不改变 accepted round 真值
     |                          后续由缓存修复或自然重建收敛]
     |
     v
[dispatch memory.operation(op_type=extract)]
     |
     +--> dispatch failed -> [更新 task 状态
     |                     可自动重试 -> 保持 pending 并写入 retry_count + 1 / next_retry_at / last_error_stage=dispatch
     |                     不可自动重试 -> failed / last_error_stage=dispatch
     |                     返回 error
     |                     code=2004 /
     |                     message=dispatch failed /
     |                     data(request_id, round_id, session_id)]
     |
     v
[返回 accepted]
 code=0 /
 message=accepted /
 data(request_id, round_id, session_id)
     |
     v
[worker 领取任务]
 - 按 task_id 做 CAS / 行锁领取
 - 只允许 pending 被领取（含 retry_count > 0 的待重试任务）
 - 领取成功后 status -> running
 - P1/P2 写入 locked_at / lock_owner / heartbeat_at
 - CAS 失败 -> 当前 worker no-op 并 ack
     |
     v
[处理前校验 write fence（P2）]
 - 从 Redis 读取 current_write_fence_version
 - 要求 payload.write_fence_version == current_write_fence_version
 - 若不相等：
   -> 说明 task 创建后发生过 delete / rebuild
   -> 直接收敛 task 为 completed(outdated_noop)
   -> ack 消息后退出
   -> 不再读取 L2 / L3
 - 若相等：
   -> 继续执行
     |
     v
[读取当前完整轮次]
 - P0 从 durable round 按 round_id 读取当前 round
 - P2 从 summary_round_journal 按 round_id + write_fence_version 读取当前 round
 - 若缺失且 P2 fence 仍匹配
   -> failed(unrecoverable_missing_round_source)
     |
     v
[并行启动两个异步沉淀分支]
     |
     +--> 异步分支 A：L2 摘要生成 / 覆盖
     |    -> [P0 简化策略]
     |       按 user_id × character_id 读取尚未摘要的 durable round
     |       以 timestamp / round_index 稳定排序
     |       若 current_summary.summary_dirty=true
     |         -> l2_result=skipped(summary_dirty_wait_rebuild)，不生成候选摘要
     |         -> dirty 只能由 rebuild 基于删除/修复后的历史源清理
     |       若 current_summary.summary_stale=true
     |         -> 可继续滚动摘要；成功写回后清除 summary_stale
     |       达到阈值后用 current_summary + 新增轮次生成新摘要
     |       写回 current_summary，并记录 latest_source_round_id / updated_at
     |       不要求 fence、连续前缀、tombstone、CAS token
     |    -> [P2 强一致策略]
     |    -> [读取当前状态]
     |       从 PostgreSQL 读取 current_summary 及其 metadata.summary_cursor_round
     |       缺省视为：current_summary=null，summary_cursor_round=0
     |       说明：当 current_summary=null 时，首批摘要起点取当前 fence 代 journal 中最早可用 round_index
     |       若 current_summary.summary_dirty=true
     |         -> l2_result=skipped(summary_dirty_wait_rebuild)，不生成候选摘要
     |         -> 保留本次及后续 active 轮次在 summary_round_journal 中等待 rebuild 消费
     |    -> [判断是否达到摘要阈值]
     |       在 PostgreSQL summary_round_journal 中识别当前 fence 代、尚未纳入摘要的新增完整轮次
     |       查询条件：user_id / character_id / write_fence_version / round_index > summary_cursor_round
     |       排序规则：ORDER BY round_index ASC, timestamp ASC
     |       若 current_summary 已存在：只保留从 summary_cursor_round + 1 开始的连续前缀
     |       若 current_summary=null：以当前 fence 代最早可用 round_index 作为连续前缀起点
     |       若中间出现缺口且无 deleted_tombstone，则在缺口处停止
     |       deleted_tombstone 可用于推进连续性判断，但不进入摘要正文输入
     |       若连续前缀中的 active 轮次 < 10 轮
     |         -> l2_result=skipped(summary_threshold_not_reached)
     |    -> [生成候选摘要]
     |       若连续前缀中的 active 轮次 >= 10 轮
     |         -> 仅截取最早的 10 个 active 轮次作为本次摘要正文输入
     |         -> 若其间夹有 deleted_tombstone，只用于 cursor 推进，不进入摘要正文
     |         -> 基于 current_summary + 本批次 10 个新增 active 轮次
     |            生成候选摘要 summary_candidate
     |         -> 为候选摘要补齐 metadata：
     |            summary_cursor_round   = 本批次最后一轮 round_index
     |            latest_source_timestamp / latest_source_round_id
     |    -> [版本比较]
     |       写回前再次读取当前 current_summary metadata
     |       若数据库中的 summary_cursor_round
     |       已大于本次候选摘要的 summary_cursor_round
     |         -> l2_result=skipped(outdated_summary_candidate)
     |       若 summary_cursor_round 相同
     |       且当前摘要的 latest_source_timestamp / latest_source_round_id 更优
     |         -> l2_result=skipped(outdated_summary_candidate)
     |    -> [并发校验]
     |       实际写入前再次读取 current_write_fence_version
     |       若 payload.write_fence_version != current_write_fence_version
     |         -> l2_result=skipped(outdated_during_processing)，不写 L2
     |    -> [执行 CAS 写回]
     |       若摘要版本仍领先或持平且更优，且 fence 仍有效
     |         -> 以 summary_cursor_round / latest_source_timestamp /
     |            latest_source_round_id / summary_cas_token 做条件更新
     |         -> CAS 成功：覆盖写入 PostgreSQL current_summary
     |         -> 写入 metadata.write_fence_version
     |         -> 可异步清理本批实际参与摘要正文的 10 个 active round_id
     |         -> 夹在本批 cursor 覆盖范围内的 deleted_tombstone 可一并异步清理
     |         -> l2_result=completed
     |       若 CAS 失败
     |         -> 重新读取当前摘要版本
     |         -> 若当前摘要已等于本次候选
     |            则 l2_result=skipped(already_applied)
     |         -> 若当前摘要已领先于本次候选
     |            则 l2_result=skipped(outdated_summary_candidate)
     |         -> 若当前摘要仍未领先
     |            则视为瞬时竞争失败，按可恢复错误进入 retry
     |    -> 异常：l2_result=failed
     |
     +--> 异步分支 B：L3 长期记忆提取 / 覆盖
     |    -> [可选确定性跳过]
     |       只跳过确定不应进入长期记忆的输入，例如空内容、安全守卫已拒绝内容、系统指令、工具调试回显
     |       确定性跳过检查输入 = join(payload.messages[].normalized_content)
     |       注：本步骤只做安全和协议层面的硬跳过，不能承担“是否值得记”的语义判断；只要不是确定性跳过，就交给 Mem0 判断是否形成长期记忆
     |       命中确定性跳过规则
     |         -> l3_result=skipped
     |    -> [准备写入参数]
     |       准备 mem0_adapter.add 参数
     |         messages = map(payload.messages):
     |           - role=user      -> {"role": "user", "content": normalized_content}
     |           - role=assistant -> {"role": "assistant", "content": normalized_content}
     |         scope = {
     |           user_id:      payload.user_id,
     |           character_id: payload.character_id
     |         }
     |         metadata = {
     |           character_id:        payload.character_id,
     |           session_id:          payload.session_id,
     |           source_ref_id:       payload.round_id,
     |           write_fence_version: payload.write_fence_version,  // P2
     |         }
     |       说明：scope 是业务抽象，不是 Mem0 原生参数承诺；adapter 必须按当前 Mem0 OSS/Platform 实测映射到 entity、namespace、run、tenant 或组合 user key
     |       adapter 可在 Mem0 返回结果后补齐业务侧 metadata：
     |         backend_categories: Mem0 返回或 adapter 映射出的分类结果
     |         memory_type: profile|preference|event|relation（仅当产品需要稳定业务枚举；否则沿用 backend_categories）
     |         importance: 0.0~1.0，缺省 0.5（若 Mem0 已返回重要性或分类结果则优先沿用）
     |         recall_priority: event 缺省 medium，非 event 可省略（P2 decay 启用时）
     |    -> [并发校验]
     |       P0 可跳过本步骤
     |       实际调用 mem0_adapter.add 前再次读取 current_write_fence_version
     |       若 payload.write_fence_version != current_write_fence_version
     |         -> l3_result=skipped(outdated_during_processing)，不写 L3
     |    -> [执行写入]
     |       若 fence 仍有效，则调用 mem0_adapter.add(messages, scope=user_character_scope, metadata)
     |       P2 调用前后必须应用当前 user×character 作用域内仍有效的 suppression；命中已删除 memory 的同义事实时跳过本次写入，若 Mem0 已形成结果，则通过显式 delete 命令清理该结果
     |       Mem0 写入侧已提供的抽取、分类和去重能力优先复用；冲突通过 Mem0 能力加显式 update/delete/rebuild 收敛
     |       若当前 Mem0 SDK/Platform 的 add 返回 ADD-only 事件，adapter 不得假设 add 会原地更新或删除旧记忆
     |       仅当当前 Mem0 模式缺少稳定写入幂等时，adapter 才用 source_ref_id + content_hash
     |       或等价稳定键做业务侧幂等保护
     |       应用层不做先删后写，不绕过 Mem0 直接写底层向量存储
     |       Mem0 未形成长期记忆
     |         -> l3_result=skipped
     |       Mem0 写入返回成功（按当前版本可能是新增、复用已有或其他明确定义结果）
     |         -> l3_result=completed
     |    -> 异常：l3_result=failed
     |
     v
[汇总分支结果并收敛 task]
 l2_result / l3_result 全部为 completed 或 skipped
   -> completed(result={
        l2_result: completed|skipped(summary_threshold_not_reached)|skipped(outdated_during_processing)|skipped(outdated_summary_candidate)|skipped(already_applied),
        l3_result: completed|skipped|skipped(outdated_during_processing),
      })
 任一分支 failed 且可恢复
   -> pending(error_message, retry_count + 1, next_retry_at)
 任一分支 failed 且不可恢复
   -> failed(error_message)
 说明：
 - completed(outdated_noop) 仅表示任务所属 generation 已过期，不代表该 round 被当前代接纳

[补偿调度器（P1/P2 自动）]
 扫描：
 - status=pending 且 retry_count > 0 且 next_retry_at <= now
 - status=pending 且 pending 超时
 - status=running 且 heartbeat_at 超时
 说明：
 - 所有可自动恢复的错误（journal_write / dispatch / worker 瞬时异常）都由上游节点转成 pending + retry_count + next_retry_at 进入本扫描窗口
 - P2 last_error_stage=journal_write 的 append task 不能直接重新 dispatch；必须先用 memory_task.payload 恢复 summary_round_journal 与 append_cursor_round_index，恢复成功后才允许 dispatch
 - L1 派生缓存写失败不进入 P2 dead_letter 主链路
 - failed 是已判定不可自动恢复的终态，由管理面处理，不在本调度器职责内
 处理：
 - status=pending 且 next_retry_at 到期且 last_error_stage=journal_write
   -> 读取 memory_task.payload
   -> 重新校验 write_fence_version / round_fingerprint / append_cursor_round_index
   -> 若 fence 已过期，则收敛为 completed(outdated_noop)
   -> 若 journal 已存在且身份一致，则进入 dispatch
   -> 若 journal 不存在且 cursor 仍允许该 round_index，则原子写入 journal 并推进 cursor 后 dispatch
   -> 若身份冲突或 cursor 已无法恢复，则 failed 或 dead_letter，交由管理面处理
 - status=pending 且 next_retry_at 到期且 last_error_stage != journal_write -> 重新 dispatch
 - status=pending 且 retry_count = 0 且 pending 超时
   -> 写入 retry_count + 1 / next_retry_at / last_error_stage=queue_wait_timeout
 - status=running 且 heartbeat 超时 -> 释放锁并置回 pending
   并写入 retry_count + 1 / next_retry_at / last_error_stage=worker_timeout
 - retry_count < max_retries -> 继续重新 dispatch
 - retry_count >= max_retries -> dead_letter(error_message)

[管理面人工恢复]
 P2 能力，不属于自动补偿链路，由管理面显式触发：
 - 以 summary_round_journal 中已持久化的完整轮次为首选补齐源
 - journal 不可用时回源历史对话按 round_id 补齐完整轮次
 - 两者都不可读时保持 dead_letter 并标记 unrecoverable_missing_source
```

### 1.2 recall 工程参考流程

> 参考范围：这一节展开 recall 的完整执行细节。正文只需要把握“L1/L2/L3 融合召回 + P0 降级召回”。

召回闭环目标：同步读取目标记忆层，并在返回前完成安全过滤、跨层去重、冲突收敛与预算裁剪，输出可直接注入回复上下文的记忆结果。`L1 / L2` 默认参与召回；`L3` 根据 skip / recall 规则决定是否查询。召回结果不是“三层原样拼接”，而是按跨层融合规则收敛后的上下文材料。

当前推荐 P0 采用降级召回（degraded recall）：任一非关键层读取失败时返回其余可用层，并在响应中标记 `degraded_layers` 和 `failed_layer_reason`。只有输入协议错误、作用域隔离失败、安全过滤失败，或全部目标层均不可用时才整体失败。P2 若业务要求强一致回复，可单独启用失败关闭（fail-closed）模式。

P0 主路径可以简化理解为：

```text
+------------------------------+
| recall 请求                   |
| retrieval_query + scope       |
+--------------+---------------+
               |
               v
+------------------------------+
| 默认读取 L1 / L2              |
| 当前会话窗口 / 阶段摘要       |
+--------------+---------------+
               |
               v
+------------------------------+
| 按需检索 L3                   |
| Mem0 search + filters/rerank  |
+--------------+---------------+
               |
               v
+------------------------------+
| 融合与预算裁剪                |
| 安全过滤 / 去重 / 冲突收敛     |
+--------------+---------------+
               |
               v
+------------------------------+
| 返回 items                    |
| 当前输入 > L3 > L2            |
+------------------------------+
```

同步返回统一格式：`code(int) + message + data(request_id, items)`；失败时返回 `code(int) + message + data(request_id, items=[], failed_layer)`。

说明：P0 按架构主文 §3.2 的召回流程落地即可；`write fence / fail-closed / event touch-back` 属于增强治理或旁路优化。

L3 skip rules：

- `强制 skip`
  纯礼貌寒暄：`你好 / 在吗 / 晚安 / 谢谢`
- `强制 skip`
  纯流程控制：`继续 / 停止 / 重试 / 重新回答`
- `强制 skip`
  纯即时执行：`翻译这句 / 总结下面这段 / 改写这句话`
- `强制 skip`
  与用户长期信息无关的客观问答：`今天天气 / 1+1 / 通用百科事实`
- `强制 skip`
  query 极短且无记忆指向词：`嗯？ / 然后呢？ / 展开`
- `强制 recall`
  明确记忆追问：`你还记得 / 之前说过 / 我上次提到`
- `强制 recall`
  明确个人信息追问：`我的偏好 / 我的经历 / 我的家人 / 我的工作 / 我的计划`
- `默认 recall`
  未命中上述规则的其他 query

```text
[recall command
 request_id / user_id / character_id /
 retrieval_query / session_id?]
            |
            v
[输入契约校验]
 必填字段 / 类型 / 作用域校验
 必填：
 - request_id
 - user_id
 - character_id
 - retrieval_query
 校验：
 - character_id 必须非空
 - retrieval_query 必须是字符串
 - session_id 可选；提供时用于读取当前会话 L1，缺失时跳过 L1，只读取 L2/L3
            |
            +--> invalid -> [返回 rejected
                              code=1001 /
                              message=invalid contract /
                              data(request_id, items=[])]
            |
            v
[规范化 retrieval_query]
 - trim -> normalized_retrieval_query
 - 过长则截断到安全上限
            |
            +--> normalized_retrieval_query 为空
                 -> [返回 skipped
                     code=1002 /
                     message=empty retrieval query /
                     data(request_id, items=[])]
            |
            v
[L3 跳过规则判断]
 输入：normalized_retrieval_query
 输出：l3_action = recall / skip
 规则：命中强制 skip -> skip；命中强制 recall -> recall；否则默认 recall
            |
            v
[确定召回可见范围]
 - P0 不读取 write fence，L1 按 user_id × character_id × session_id 读取当前会话窗口，L2 按 user_id × character_id 读取当前摘要
 - P2 启用 write fence 后，读取 user_id × character_id 的 current_write_fence_version
 - P2 模式下 L1 / L2 只读取该 fence 代的数据
 - L3 不按当前 fence 做全局过滤；session/all 删除与重建通过目标范围物理治理收敛
            |
            +--> P2 fence read failed -> [返回 error
            |                    code=2101 /
            |                    message=recall failed /
            |                    data(request_id, items=[], failed_layer=scope_state)]
            |
            v
[并行读取三层候选]
     |
     +--> L1 默认召回
     |    -> P0 read L1 Redis window(user_id, character_id, session_id)
     |    -> P2 read current-fence L1 Redis window(user_id, character_id, session_id)
     |    -> 若 session_id 缺失：L1 候选为空，layer_status.l1=skipped(no_session_id)
     |    -> P2 只读取 write_fence_version 与当前 scope 一致的 entry
     |    -> 命中：返回短期记忆片段
     |       round_id / round_index / messages[] / timestamp / session_id
     |    -> 无命中：L1 候选为空
     |    -> P0 失败：记录 degraded_layers=[l1]，继续使用 L2/L3
     |    -> P2 fail-closed：返回 error
     |       code=2101 / message=recall failed /
     |       data(request_id, items=[], failed_layer=l1)
     |
     +--> L2 默认召回
     |    -> P0 get_current_summary(user_id, character_id)
     |    -> P2 get_current_summary(user_id, character_id, current_write_fence_version)
     |    -> 命中：返回 current_summary
     |       summary_id / content / updated_at / summary_dirty / summary_suppression?
     |    -> 若 summary_dirty=true
     |       -> L2 候选为空，并异步确保 L2 rebuild 已排队
     |       -> 不允许把 dirty summary 原样返回
     |       -> dirty 只由 rebuild 清除，不由滚动摘要自然覆盖
     |    -> 若 summary_stale=true
     |       -> L2 候选为空或只返回降级诊断
     |       -> 不把 stale summary 注入 prompt；后续可由滚动摘要或 rebuild 修复
     |    -> 无摘要：L2 候选为空
     |    -> P0 失败：记录 degraded_layers=[l2]，继续使用 L1/L3
     |    -> P2 fail-closed：返回 error
     |       code=2101 / message=recall failed /
     |       data(request_id, items=[], failed_layer=l2)
     |
     +--> l3_action=skip
     |    -> L3 候选为空
     |
     +--> l3_action=recall
          -> mem0_adapter.search(query=normalized_retrieval_query,
                                 scope=user_character_scope,
                                 filters=business_filters,
                                 top_k=mem0_top_k,
                                 threshold=score_threshold,
                                 rerank=rerank_config?)
          -> 命中：返回长期记忆条目
             memory_id / memory_type / content / score / importance / updated_at / recall_priority?
          -> 性能控制：score_threshold / timeout / rerank 参数优先透传 Mem0
          -> business_filters 可包含 character_id / session_id / memory_type 等治理条件，但主要隔离职责由 adapter 映射出的 scope 承担
          -> 若 Mem0 rerank 或过滤能力已满足排序需求，mem0_top_k 可等于最终 L3 返回预算
          -> 只有当业务侧 recall_priority 无法下推给 Mem0 时，才使用 overfetch_top_k 做最小放大后再裁剪
          -> 无命中：L3 候选为空
          -> P0 失败：记录 degraded_layers=[l3]，继续使用 L1/L2
          -> P2 fail-closed：返回 error
             code=2101 / message=recall failed /
             data(request_id, items=[], failed_layer=l3)
            |
            v
[层内标准化]
- L1 按 round_id 去重
- L2 只保留 current_summary
- L3 按 memory_id 去重
            |
            v
[L3 层内冲突收敛]
 - 只在当前 `user_id × character_id` 命名空间内处理
 - 对 Mem0 返回后仍表达同一长期事实的多条 L3 候选，先尊重 Mem0 返回顺序和 rerank 结果；只有当前 Mem0 模式未能稳定收敛明显冲突时，才按以下优先级做业务侧最终裁剪：
   · 明确否定/纠正语义 > 普通陈述
   · updated_at 更新者优先
   · importance 更高者优先
   · recall_priority 更高者优先
 - 对被淘汰的旧候选：
   · 本次召回不返回
   · 若 Mem0 当前模式无法自动收敛冲突，可异步记录 L3 conflict_review 信号，后续通过 Mem0 update/delete 或 rebuild 收敛
 - 若无法可靠判断为同一事实，只做并列保留，交给跨层融合与预算裁剪处理
            |
            v
[召回时安全过滤]
 - 对即将返回的 L1 / L2 / L3 内容执行最新记忆域安全策略
 - 命中禁止召回内容 -> 从候选中剔除
 - 若过滤后 items 为空 -> 返回空结果 code=0，不视为系统错误
 - 安全策略执行失败 -> 返回 error
   code=2102 /
   message=recall safety filter failed /
   data(request_id, items=[], failed_layer=safety_filter)
            |
            v
[跨层去重与冲突收敛]
 - 若 L2 摘要中的某项信息与 L3 明确长期记忆表达同一事实
   -> 以 L3 为真值来源
   -> L2 保留阶段叙事，但不重复返回同一事实表述
 - L1 作为近邻原文片段，不与 L3 竞争真值
 - 若三层内容只是互补，不视为冲突，允许共同保留
 - 若当前发现 L2 与 L3 明显冲突
   -> 本次返回以 L3 真值为准
   -> 可异步记录摘要待修正信号，但不阻塞本次召回
            |
            v
[排序与预算裁剪]
 先做层内排序：
 - L1：当前 session 片段优先，其次按 timestamp 倒序
 - L2：只保留 current_summary，无需排序
 - L3：`score_threshold` 是准入门槛，低于阈值的候选不得进入排序；优先保留 Mem0 返回顺序和 rerank 结果。若业务侧启用 `recall_priority` 且无法下推给 Mem0，再按 high > medium > low 做轻量分桶；桶内仍优先保留 Mem0 返回顺序。

 再做跨层组装（进入 prompt 的拼接顺序）：
 - 先放 L2 current_summary，提供阶段连续性
 - 再放 L1 最邻近短期片段，提供最近原文上下文
 - 最后放 L3 高价值长期记忆，作为稳定真值补充

 最后做总预算裁剪：
 - 优先裁掉 L1 中较远、较旧的片段
 - 再裁掉 L3 中排序靠后的条目
 - 最后才考虑压缩 L2 current_summary

 说明：
 - 跨层真值优先级仍是 当前输入 > L3 > L2
 - prompt 拼接顺序不等于真值优先级
 - 裁剪顺序只解决 token 预算，不改变真值口径
            |
            v
[返回同步召回结果]
 code=0 /
 message=ok /
 data(request_id, items)]
            |
            v
[异步 event touch-back]
 - 不阻塞召回响应
 - 只对最终进入 items 且 memory_type=event 的 L3 条目
 - 通过 mem0_adapter.update(memory_id, scope=user_character_scope, metadata={last_recalled_at, recall_count+1})
 - 写入失败只记录 warn 日志，不改变本次召回结果
 - 被安全过滤剔除的条目不能触发 touch-back
 - 未进入最终 items 的条目不能触发 touch-back
 - 注：touch-back 是旁路尽力而为机制，不登记 memory_task、不进入补偿链路、不触发 dead_letter
```

### 1.3 delete 工程参考流程

> 参考范围：这一节展开 delete 的完整执行细节。正文只需要把握“P0 基础删除和标脏，P2 再做强一致删除治理”。

删除闭环目标：按 `delete_scope` 精确清理 `L1 短期记忆`、`L2 当前摘要` 和 `L3 长期记忆`，并通过 `memory_task(op_type=delete)` 收敛为可追踪状态。

分阶段看，删除链路可以这样拆：

- 首版先做好：按 `memory/session/all` 完成目标 L3 删除、清理相关 L1；如果已生效 L2 摘要可能包含被删内容，标记为 dirty 并触发 rebuild；如果只是未摘要新轮次落后，标记为 stale，允许滚动摘要或 rebuild 修复。
- 先不引入：删除前 snapshot 固化、semantic suppression、fence 推进、tombstone、carry-forward。
- 风险出现后再接入：删除防复活 suppression、session/all cutoff fence、L2 精确 rebuild。

P0 主路径可以简化理解为：

```text
+------------------------------+
| delete 请求                   |
| memory / session / all        |
+--------------+---------------+
               |
               v
+------------------------------+
| 作用域与幂等校验              |
| scope + version / operation   |
+--------------+---------------+
               |
               v
+------------------------------+
| 删除目标 L3                   |
| Mem0 delete / delete_all      |
+--------------+---------------+
               |
               v
+------------------------------+
| 清理 L1 并标记 L2             |
| dirty 触发 rebuild，stale 等待 |
+--------------+---------------+
               |
               v
+------------------------------+
| 返回 task_id                  |
| task query 查询最终状态       |
+------------------------------+
```

同步返回统一格式：`code(int) + message + data(request_id, delete_scope, task_id)`。

说明：P0 按架构主文 §3.3 的删除流程落地即可，但任务身份不能只使用静态 scope_key；至少需要 `scope_key + p0_scope_version` 或显式 `operation_id`。`snapshot / suppression / fence / tombstone / carry-forward` 属于 P2 强治理能力。

```text
[delete command
 request_id / user_id / character_id /
 delete_scope / session_id? / memory_id?]
                |
                v
[输入契约校验]
 必填字段 / 类型 / 作用域校验
 必填：
 - request_id
 - user_id
 - character_id
 - delete_scope
 校验：
 - delete_scope in {memory, session, all}
 - delete_scope=memory  -> memory_id 必填
 - delete_scope=session -> session_id 必填
                |
                +--> invalid -> [返回 rejected
                                  code=1001 /
                                  message=invalid contract /
                                  data(request_id, delete_scope)]
                |
                v
[推导任务身份]
 scope_key =
 - delete_scope=memory  -> memory:{user_id}:{character_id}:{memory_id}
 - delete_scope=session -> session:{user_id}:{character_id}:{session_id}
 - delete_scope=all     -> all:{user_id}:{character_id}
                |
                v
[读取 scope version]
 来源：读取删除前的稳定内容快照；同一删除目标在内容未变化时必须派生出同一个 version
 P0 可使用轻量 `p0_scope_version`：memory 维度取目标 memory updated_at/content_hash，session/all 维度取历史源或 journal 中“删除前业务源内容”的 max_updated_at；若这些版本不可得，则由上游传入显式 operation_id。
 版本计算必须排除 delete 任务自身写入的 task 状态、审计记录、dirty/stale 标记、current_summary.updated_at 和 carry-forward 元数据，避免重复删除生成新 task。
 P2 使用更严格的 `scope_content_version`，用于区分“同一删除目标在内容变化前后”的不同治理任务。
- memory 维度：
    若 mem0_adapter.get(memory_id, scope=user_character_scope) 命中
      version = target_memory.updated_at + target_memory.content_hash
      并把 deleted_memory_snapshot / deleted_memory_content_hash 固化进 delete_task_payload
    若未命中
      version = missing:{memory_id}
      后续重复删除同一 missing memory 必须命中同一个 no-op task
- session 维度：version = max(
     历史源或 summary_round_journal 中该 session、write_fence_version <= 当前 fence 的最新 accepted round timestamp,
     mem0_adapter.get_all(scope=user_character_scope, filters={character_id, session_id}) 的 max(updated_at)
   )
- all 维度：    version = max(
     历史源或 summary_round_journal 当前 fence 内最新 accepted round timestamp,
     mem0_adapter.get_all(scope=user_character_scope, filters={character_id}) 的 max(updated_at)
   )
 推进规则（隐式）：
 - append / rebuild 写入新内容 -> 下游 Mem0 updated_at / journal timestamp / summary.updated_at 自然推进
 - delete 自身的清理、dirty/stale 标记、summary/journal/L1 carry-forward 不应推进 scope_content_version；否则同一删除请求重复提交会生成新 task，破坏删除幂等
 - P2 读取失败则返回 error
   code=2001 /
   message=scope version query failed /
   data(request_id, delete_scope)
                |
                v
[推导任务 ID]
 - P0：task_id = memory-delete:{scope_key}:v{p0_scope_version} 或 memory-delete:{scope_key}:op{operation_id}
 - P2：task_id = memory-delete:{scope_key}:{scope_content_version}
                |
                v
[查询 PG memory_task(task_id)]
                |
                +--> query failed -> [返回 error
                |                    code=2001 /
                |                    message=task query failed /
                |                    data(request_id, delete_scope, task_id)]
                |
                +--> task exists 且 status=completed
                |    -> [返回 already_done
                |        code=1004 /
                |        message=task already completed /
                |        data(request_id, delete_scope, task_id)]
                |
                +--> task exists 且 status in {pending, running}
                |    -> [返回 accepted
                |        code=0 /
                |        message=accepted /
                |        data(request_id, delete_scope, task_id)
                |        视为同一 task_id 的幂等重放]
                |
                +--> task exists 且 status in {failed, dead_letter}
                |    -> [返回 error
                |        code=2006 /
                |        message=task not resumable /
                |        data(request_id, delete_scope, task_id)]
                |
                +--> task not found
                |    -> [继续执行
                |        进入 task 登记 + dispatch]
                |
                v
[构造 delete_task_payload]
 request_id / user_id / character_id /
 delete_scope / session_id? / memory_id?
 scope_key / p0_scope_version? / operation_id? / scope_content_version?
 deleted_memory_snapshot? / deleted_memory_content_hash?
                |
                v
[登记或更新 PG memory_task
 task_id=P0(memory-delete:{scope_key}:v{p0_scope_version} 或 memory-delete:{scope_key}:op{operation_id}) 或 P2(memory-delete:{scope_key}:{scope_content_version})
 task_type=memory
 op_type=delete
 task_id 唯一约束
 task not found -> create(status=pending)
 retry_count / max_retries / next_retry_at
 payload=最新 delete_task_payload]
                |
                +--> upsert conflict -> [重新读取 task 状态
                |                       status=completed
                |                         -> 返回 already_done
                |                            code=1004 /
                |                            message=task already completed /
                |                            data(request_id, delete_scope, task_id)
                |                       status in {pending, running}
                |                         -> 返回 accepted
                |                            code=0 /
                |                            message=accepted /
                |                            data(request_id, delete_scope, task_id)
                |                       status in {failed, dead_letter}
                |                         -> 返回 error
                |                            code=2006 /
                |                            message=task not resumable /
                |                            data(request_id, delete_scope, task_id)
                |                       避免并发重复派发]
                |
                +--> persist failed -> [返回 error
                |                         code=2003 /
                |                         message=task persist failed /
                |                         data(request_id, delete_scope, task_id)]
                |
                v
[dispatch memory.operation(op_type=delete)]
                |
                +--> dispatch failed -> [更新 task 状态
                |                         可自动重试 -> 保持 pending 并写入 retry_count + 1 / next_retry_at
                |                         不可自动重试 -> failed
                |                         last_error_stage=dispatch
                |                         返回 error
                |                         code=2004 /
                |                         message=dispatch failed /
                |                         data(request_id, delete_scope, task_id)]
                |
                v
[返回 accepted]
 code=0 /
 message=accepted /
 data(request_id, delete_scope, task_id)
                |
                v
[worker 领取任务]
 - 按 task_id 做 CAS / 行锁领取
 - 只允许 pending 被领取（含 retry_count > 0 的待重试任务）
 - 领取成功后 status -> running
 - CAS 失败 -> 当前 worker no-op
 - 删除动作必须幂等，未命中按 count=0 处理
     |
     v
[按 delete_scope 分派]
     |
     +--> delete_scope = memory
     |    -> P0：
     |       校验 memory_id 属于 user_id × character_id
     |       幂等删除该 L3 memory
     |       若现有 L2 可能包含该 memory 对应事实，则标记 L2 dirty，并触发或等待 rebuild 修复摘要
     |       若未影响已生效摘要，只记录删除审计，不标记 dirty
     |    -> P2：
     |    -> 使用入口阶段固化的 deleted_memory_snapshot / deleted_memory_content_hash
     |       若 payload 缺失且 mem0_adapter.get 仍命中，则先补齐快照再继续
     |       mem0_adapter.get 必须校验业务作用域，作用域不匹配按未命中处理
     |    -> 再 mem0_adapter.delete(memory_id, scope=user_character_scope)
     |       若 SDK 只能按 memory_id 删除，adapter 必须先校验作用域后删除
     |    -> 若未命中且 task payload 中没有已固化 snapshot
     |       则 deleted_l3_count=0，summary_action=unchanged，不标记 summary_dirty
     |    -> 若命中或 task payload 中已有已固化 snapshot
     |       先幂等记录 suppression(memory_id / deleted_memory_content_hash / deleted_at / operator_context)
     |       使后续 L3 append/rebuild 立即避让同义事实
     |       标记 L2 summary_dirty=true
     |       后续 L2 rebuild 与 L3 append/rebuild 必须应用该 suppression，避免同义事实被重新写回
     |       并确保异步 L2 rebuild 已排队
     |       在 rebuild 完成前，recall 必须跳过 dirty current_summary
     |    -> 说明：memory 维度只删单条、不影响并发写入语义，因此不推进 fence
     |
     +--> delete_scope = session
     |    -> P0：
     |       清理该 session 对应 L1 短期片段
     |       删除该 session 来源的 L3 memory
     |       若 current_summary 覆盖范围可能包含该 session 历史轮次，则标记 L2 dirty，必须通过 rebuild 修复
     |       若只影响尚未摘要的新增轮次，则标记 L2 stale，可通过滚动摘要或 rebuild 修复
     |    -> P2：
     |    -> INCR user×character fence_version，记录 cutoff_write_fence_version = 推进前的值
     |    -> 清理该 session 的 L1 短期窗口
     |    -> 将 summary_round_journal 中 session_id 匹配、尚未摘要覆盖且 write_fence_version<=cutoff_write_fence_version 的 active 轮次
     |       标记为推进后 fence 可见的 deleted_tombstone
     |       说明：不直接物理删除，避免 L2 后续连续前缀判断被 round_index 缺口卡死
     |    -> mem0_adapter.delete_all(scope=user_character_scope, filters={character_id, session_id,
     |       write_fence_version__lte=cutoff_write_fence_version})
     |       cutoff_write_fence_version 保护操作开始后的新 append 不被误删
     |    -> 若删除范围可能影响当前 current_summary
     |       则触发 L2 rebuild：基于删除后的剩余完整轮次历史重建或清空 current_summary
     |    -> 若删除范围不影响当前 current_summary 正文
     |       则以 CAS 将 current_summary.write_fence_version 更新为推进后的 fence
     |       正文与 summary_cursor_round 不变，避免有效摘要因旧 fence 被 recall 过滤
     |    -> 对未受该 session 删除影响、仍需后续摘要的 active journal 轮次
     |       也必须 carry-forward 到推进后的 fence，避免后续 L2 增量消费只看当前 fence 时丢失待摘要轮次
     |
     +--> delete_scope = all
     |    -> P0：
     |       清理该 user×character 下全部 session 的 L1 窗口、L2 和 L3 可见记忆
     |       清空 current_summary
     |       后续 append 使用新的有效历史继续推进
     |    -> P2：
     |    -> INCR user×character fence_version，记录 cutoff_write_fence_version = 推进前的值
     |    -> 清理该 user×character 下全部 session 中 write_fence_version<=cutoff_write_fence_version 的 L1 短期记忆
     |    -> 清理该 user×character 下 write_fence_version<=cutoff_write_fence_version 的 summary_round_journal
     |    -> mem0_adapter.delete_all(scope=user_character_scope, filters={character_id,
     |       write_fence_version__lte=cutoff_write_fence_version})
     |    -> 清空 current_summary，并将 summary_cursor_round=0
     |    -> append_cursor_round_index 不回退；后续 append 仍必须使用更大的连续 round_index
     |
     v
[worker: 收敛 memory_task 状态
 completed(result:
   deleted_l1_count / deleted_l3_count /
   summary_action=unchanged|recomputed|cleared|marked_dirty) /
 pending(error_message, retry_count + 1, next_retry_at) /
 failed(error_message) /
 dead_letter(error_message)]

[补偿调度器]
 机制与写入闭环一致
```

### 1.4 rebuild 工程参考流程

> 参考范围：这一节展开 rebuild 的完整执行细节。正文只需要把握“历史对话源是真值，L1 不参与重建”。

重建闭环目标：重建 `L3 长期记忆`，并恢复 `L2 current_summary + summary_cursor_round + summary_round_journal` 的 steady state；`L1` 不作为真值参与重建写入。重建通过 `memory_task(op_type=rebuild)` 收敛。

分阶段看，重建链路可以这样拆：

- 首版先做好：重建以历史对话源为真值，L1 不参与重建；支持按 session 重建 L3，按 user×character 重算 L2；重建结果可追踪。
- 先不引入：history_snapshot_version、fence 推进、tombstone 连续性、suppression。
- 风险出现后再接入：历史快照版本、cutoff fence、L2 steady state 精确重写、suppression 防复活。

P0 主路径可以简化理解为：

```text
+------------------------------+
| rebuild 请求                  |
| session 或 all                |
+--------------+---------------+
               |
               v
+------------------------------+
| 任务身份                      |
| scope + history / operation   |
+--------------+---------------+
               |
               v
+------------------------------+
| 读取历史对话源                |
| 删除/修复后的有效完整轮次     |
+--------------+---------------+
               |
               v
+------------------------------+
| 重算 L2                       |
| user×character current_summary |
+--------------+---------------+
               |
               v
+------------------------------+
| 重写 L3                       |
| 按 scope 调用 Mem0            |
+--------------+---------------+
               |
               v
+------------------------------+
| 返回 task_id                  |
| task query 查询最终状态       |
+------------------------------+
```

同步返回统一格式：`code(int) + message + data(request_id, task_id)`。

说明：P0 按架构主文 §3.4 的重建流程落地即可，但任务身份不能只使用静态 rebuild_scope_key；至少需要 `rebuild_scope_key + p0_history_version` 或显式 `operation_id`。`history_snapshot_version / fence / tombstone / suppression` 属于 P2 强治理能力。

```text
[rebuild command
 request_id / user_id / character_id /
 rebuild_scope / session_id?]
                 |
                 v
[输入契约校验]
 必填字段 / 类型 / 作用域校验
 必填：
 - request_id
 - user_id
 - character_id
 - rebuild_scope
 校验：
 - rebuild_scope in {session, all}
 - rebuild_scope=session -> session_id 必填
                 |
                 +--> invalid -> [返回 rejected
                                   code=1001 /
                                   message=invalid contract /
                                   data(request_id, rebuild_scope)]
                 |
                 v
[推导重建作用域]
 rebuild_scope_key =
 - rebuild_scope=session -> session:{user_id}:{character_id}:{session_id}
 - rebuild_scope=all     -> all:{user_id}:{character_id}
                 |
                 v
[读取 history version]
 来源：历史对话源
 P0 可读取轻量 `p0_history_version`，例如目标 scope 历史源 max_updated_at、history latest visible version，或由上游传入显式 operation_id；只读取当前最新可见数据，不要求强一致 snapshot。
 `history_snapshot_version` 是 P2 用于强一致重建和幂等任务身份的增强字段。
 - rebuild_scope=session -> 读取该 session 的历史快照版本作为 l3_history_snapshot_version
                         -> 同时读取 user×character 的全量历史快照版本作为 l2_history_snapshot_version
 - rebuild_scope=all     -> 读取该 user×character 的全量历史快照版本，同时作为 l2_history_snapshot_version 与 l3_history_snapshot_version
 说明：
 - 这两个版本共同代表“本次重建所依据的历史材料版本”
 - 历史对话新增、删除、修正后，应推进对应 scope 的 history_snapshot_version
 - P2 读取失败则返回 error
   code=2001 /
   message=history snapshot query failed /
   data(request_id, rebuild_scope)
                 |
                 v
[推导任务身份]
 - P0：task_id = memory-rebuild:{rebuild_scope_key}:v{p0_history_version} 或 memory-rebuild:{rebuild_scope_key}:op{operation_id}
 - P2：task_id = memory-rebuild:{rebuild_scope_key}:l2hv{l2_history_snapshot_version}:l3hv{l3_history_snapshot_version}
                 |
                 v
[查询 PG memory_task(task_id)]
                 |
                 +--> query failed -> [返回 error
                 |                    code=2001 /
                 |                    message=task query failed /
                 |                    data(request_id, task_id)]
                 |
                 +--> task exists 且 status=completed
                 |    -> [返回 already_done
                 |        code=1004 /
                 |        message=task already completed /
                 |        data(request_id, task_id)]
                 |
                 +--> task exists 且 status in {pending, running}
                 |    -> [返回 accepted
                 |        code=0 /
                 |        message=accepted /
                 |        data(request_id, task_id)
                 |        视为同一 task_id 的幂等重放]
                 |
                 +--> task exists 且 status in {failed, dead_letter}
                 |    -> [返回 error
                 |        code=2006 /
                 |        message=task not resumable /
                 |        data(request_id, task_id)]
                 |
                 +--> task not found
                 |    -> [继续执行
                 |        进入 task 登记 + dispatch]
                 |
                 v
[构造 rebuild_task_payload]
 request_id / user_id / character_id /
 rebuild_scope / session_id? /
 rebuild_scope_key / p0_history_version? / operation_id? / l2_history_snapshot_version? / l3_history_snapshot_version?
                 |
                 v
[登记或更新 PG memory_task
 task_id=P0(memory-rebuild:{rebuild_scope_key}:v{p0_history_version} 或 memory-rebuild:{rebuild_scope_key}:op{operation_id}) 或 P2(memory-rebuild:{rebuild_scope_key}:l2hv{l2_history_snapshot_version}:l3hv{l3_history_snapshot_version})
 task_type=memory
 op_type=rebuild
 task_id 唯一约束
 task not found -> create(status=pending)
 retry_count / max_retries / next_retry_at
 payload=最新 rebuild_task_payload]
                 |
                 +--> upsert conflict -> [重新读取 task 状态
                 |                       status=completed
                 |                         -> 返回 already_done
                 |                            code=1004 /
                 |                            message=task already completed /
                 |                            data(request_id, task_id)
                 |                       status in {pending, running}
                 |                         -> 返回 accepted
                 |                            code=0 /
                 |                            message=accepted /
                 |                            data(request_id, task_id)
                 |                       status in {failed, dead_letter}
                 |                         -> 返回 error
                 |                            code=2006 /
                 |                            message=task not resumable /
                 |                            data(request_id, task_id)
                 |                       避免并发重复派发]
                 |
                 +--> persist failed -> [返回 error
                 |                         code=2003 /
                 |                         message=task persist failed /
                 |                         data(request_id, task_id)]
                 |
                 v
[dispatch memory.operation(op_type=rebuild)]
                 |
                 +--> dispatch failed -> [更新 task 状态
                 |                         可自动重试 -> 保持 pending 并写入 retry_count + 1 / next_retry_at
                 |                         不可自动重试 -> failed
                 |                         last_error_stage=dispatch
                 |                         返回 error
                 |                         code=2004 /
                 |                         message=dispatch failed /
                 |                         data(request_id, task_id)]
                 |
                 v
[返回 accepted]
 code=0 /
 message=accepted /
 data(request_id, task_id)
                 |
                 v
[worker 领取任务]
 - 按 task_id 做 CAS / 行锁领取
 - 只允许 pending 被领取（含 retry_count > 0 的待重试任务）
 - 领取成功后 status -> running
 - CAS 失败 -> 当前 worker no-op
     |
     v
[推进 fence（P2）]
 - INCR user×character fence_version，记录 cutoff_write_fence_version = 推进前的值
 - rebuild_scope=all / session 统一推进同一 fence
 - 阻止旧 extract worker 在重建后写回旧 L2 / L3
 - P0 可跳过 fence 推进；通过重建任务完成时间、来源版本或直接覆盖当前 L2/L3 有效状态收敛
     |
     v
[读取历史对话源]
 - P0 可读取当前历史对话源的最新有效数据，不要求显式 history_snapshot_version
 - P2 启用以下 snapshot 读取规则：
 - L3 重建：
     rebuild_scope=session -> 按 l3_history_snapshot_version 仅读目标 session_id 的历史对话
     rebuild_scope=all     -> 按 l3_history_snapshot_version 读取 user×character 下全部有效历史对话
 - L2 重建：
     无论 rebuild_scope=session 还是 all，都按 l2_history_snapshot_version 读取 user×character 下删除/修复后的全部有效历史对话
     并应用仍然有效的 summary_suppression
- 读取失败 -> pending(retry_count+1, next_retry_at) / failed / dead_letter
     |
     v
[重建 L2 steady state]
- 基于 user×character 下“删除/修复后的全量有效完整轮次历史”重建 `current_summary + summary_cursor_round + summary_round_journal`
- P0 可只重算 current_summary，并保留尚未摘要的最近轮次作为后续增量输入；不要求 tombstone 连续性
- P0 rebuild 成功后必须清除 `summary_dirty / summary_stale`，并记录本次修复使用的 `p0_history_version` 或 `operation_id`
- P2 若存在仍有效的 `summary_suppression`，摘要生成时必须抑制与 suppression 命中的已删 memory 语义等价的事实表达
- P2 写入 L2 重建结果前，必须清理或覆盖 `write_fence_version<=cutoff_write_fence_version` 的旧 L2 state；不得删除或覆盖推进 fence 后新 append 写入的 journal 轮次
- P2 先按 round_index 升序排列全部有效完整轮次，并保留因删除产生的必要 `deleted_tombstone`，用于维持原始 round_index 的连续推进语义
- P2 用有效完整轮次计算摘要正文批次；用有效完整轮次 + tombstone 共同推进 `summary_cursor_round`
- 设有效完整轮次总数为 `N`
- 令 `K = floor(N / 10) * 10`
- 令 `R = N - K`
- 若 `K = 0`
  -> 清空 current_summary
  -> summary_cursor_round = 0
  -> P2 全部有效轮次重新写入 summary_round_journal；若原始 round_index 存在缺口，必须同步写入必要 `deleted_tombstone`
- 若 `K > 0`
  -> 基于前 `K` 个有效轮次按每 10 轮一批折叠生成最终 current_summary
  -> summary_cursor_round = 第 `K` 个有效轮次的 round_index
  -> P2 尾部剩余 `R` 个有效轮次重新写入 summary_round_journal；从 summary_cursor_round+1 到尾部最大 round_index 之间如存在已删除缺口，必须写入 `deleted_tombstone`
- P2 rebuild 后的 journal 必须满足：从 `summary_cursor_round + 1` 到当前最大待摘要 `round_index`，每个缺失原始 round_index 要么有 active 轮次，要么有 deleted_tombstone；否则后续 L2 增量摘要会在缺口处永久停止
- 最终写入 metadata：summary_cursor_round / latest_source_timestamp / latest_source_round_id / 推进后的 write_fence_version
- P2 若重建成功且 suppression 已被应用，则清除对应 summary_dirty / summary_suppression
- P2 rebuild 不回退 append_cursor_round_index；若历史修复导致最大有效 round_index 大于当前 cursor，则只能将 cursor 前进到该最大值
     |
     v
[重建 L3]
- rebuild_scope=session
    mem0_adapter.delete_all(scope=user_character_scope, filters={character_id, session_id, write_fence_version__lte=cutoff_write_fence_version})
 - rebuild_scope=all
    mem0_adapter.delete_all(scope=user_character_scope, filters={character_id, write_fence_version__lte=cutoff_write_fence_version})
 - 若重建范围内无历史对话 -> rebuilt_l3_count=0
 - 若重建范围内有历史对话
     按 source_timestamp / round_id 稳定排序
     mem0_adapter.add(messages, scope=user_character_scope, metadata) 重新写入
     P2 写入前后必须应用仍有效的 suppression；命中已删除 memory 的同义事实时跳过本次写入，若 Mem0 已形成结果，则通过显式 delete 命令清理该结果
     Mem0 写入侧已提供的抽取、分类和去重能力优先复用；冲突通过 Mem0 能力加显式 update/delete/rebuild 收敛；删除仍由 rebuild 前的 delete_all 显式完成
     adapter 补齐业务 metadata：character_id / session_id / source_ref_id / backend_categories? / memory_type? / importance? / 推进后的 write_fence_version / recall_priority?
     |
     v
[worker: 收敛 memory_task 状态
 completed(result:
   source_fragment_count /
   rebuilt_l3_count /
   summary_action=recomputed|cleared) /
 pending(error_message, retry_count + 1, next_retry_at) /
 failed(error_message) /
 dead_letter(error_message)]

[补偿调度器]
机制与写入闭环一致
```

### 1.5 decay 工程参考流程

> 参考范围：这一节展开 decay 的完整执行细节。正文只需要把握“decay 只治理 L3.event 的召回优先级，不删除、不归档、不影响 P0 主链路”。
>
> 实施建议：首版可以完全跳过本节。只有当 L3 事件类记忆数量明显增长，并且“旧事件挤占新事实”开始影响召回质量时，再启用 decay。

```text
[decay schedule command
 request_id / schedule_slot / triggered_at /
 partition_count / partition_index / dry_run]
     |
     v
[输入契约校验]
 必填字段 / 类型校验
 必填：
 - request_id
 - schedule_slot
 - triggered_at
 - partition_count
 - partition_index
 校验：
 - partition_count >= 1
 - 0 <= partition_index < partition_count
 - dry_run 可选，默认 false
     |
     +--> invalid -> [记录 error 日志
                       code=1001 /
                       message=invalid contract /
                       data(request_id, schedule_slot, partition_index)]
     |
     v
[推导任务身份]
 task_id = memory-decay:{schedule_slot}:p{partition_index}-of-{partition_count}:dry_run={dry_run}
     |
     v
[查询 PG memory_task(task_id)]
     |
     +--> query failed -> [记录 error 日志
     |                    code=2001 /
     |                    message=task query failed /
     |                    data(request_id, task_id, schedule_slot, partition_index)]
     |
     +--> task exists 且 status=completed
     |    -> [返回 already_done，结束当前调度周期
     |        code=1004 /
     |        message=task already completed /
     |        data(request_id, task_id, schedule_slot, partition_index)]
     |
     +--> task exists 且 status in {pending, running}
     |    -> [结束当前调度周期
     |        code=0 /
     |        message=accepted /
     |        data(request_id, task_id, schedule_slot, partition_index)
     |        避免重复派发]
     |
     +--> task exists 且 status in {failed, dead_letter}
     |    -> [结束当前调度周期
     |        code=2006 /
     |        message=task not resumable /
     |        data(request_id, task_id, schedule_slot, partition_index)]
     |
     +--> task not found
     |    -> [继续执行
     |        进入 task 登记 + dispatch]
     |
     v
[构造 decay_task_payload]
 request_id / schedule_slot / triggered_at /
 partition_count / partition_index / dry_run
     |
     v
[登记或更新 PG memory_task
 task_id=memory-decay:{schedule_slot}:p{partition_index}-of-{partition_count}:dry_run={dry_run}
 task_type=memory
 op_type=decay
 task_id 唯一约束
 task not found -> create(status=pending)
 retry_count / max_retries / next_retry_at
 payload=最新 decay_task_payload]
     |
     +--> upsert conflict -> [重新读取 task 状态
     |                       status=completed
     |                         -> [返回 already_done，结束当前调度周期
     |                            code=1004 /
     |                            message=task already completed /
     |                            data(request_id, task_id, schedule_slot, partition_index)]
     |                       status in {pending, running}
     |                         -> [结束当前调度周期
     |                            code=0 /
     |                            message=accepted /
     |                            data(request_id, task_id, schedule_slot, partition_index)]
     |                       status in {failed, dead_letter}
     |                         -> [结束当前调度周期
     |                            code=2006 /
     |                            message=task not resumable /
     |                            data(request_id, task_id, schedule_slot, partition_index)]
     |                       避免并发重复派发]
     |
     +--> persist failed -> [记录 error 日志
     |                       code=2003 /
     |                       message=task persist failed /
     |                       data(request_id, task_id, schedule_slot, partition_index)]
     |
     v
[dispatch memory.operation(op_type=decay)]
     |
     +--> dispatch failed -> [更新 task 状态
     |                         可自动重试 -> 保持 pending 并写入 retry_count + 1 / next_retry_at
     |                         不可自动重试 -> failed
     |                         last_error_stage=dispatch
     |                         记录 error 日志
     |                         code=2004 /
     |                         message=dispatch failed /
     |                         data(request_id, task_id, schedule_slot, partition_index)]
     |
     v
[结束当前调度周期]
 code=0 /
 message=accepted /
 data(request_id, task_id, schedule_slot, partition_index)
     |
     v
[worker 领取任务]
 - 按 task_id 做 CAS / 行锁领取
 - 只允许 pending 被领取（含 retry_count > 0 的待重试任务）
 - 领取成功后 status -> running
 - CAS 失败 -> 当前 worker no-op
     |
     v
[扫描治理目标]
 仅扫描 `L3.event` 条目
 - 若按 user_id × character_id 分片：
     先从 PostgreSQL scope state 按 cursor/page_size 分页枚举命中当前 partition_index 的 scope
     再对每个 scope 调用 mem0_adapter.get_all(scope=user_character_scope, filters={character_id, memory_type=event}) 翻页扫描
 - 若按 memory_id hash 分片：
     由 adapter 分页返回命中当前 partition_index 的 event 及其 user_id / character_id / memory_id
 - 不直接访问底层向量存储 collection，交由 Mem0 或 adapter 适配
 仅处理命中当前 partition_index 的分片数据
 cursor / page_size / heartbeat 持续更新
     |
     +--> scan failed -> [worker: 更新 memory_task 状态
     |                    可自动重试 -> pending(retry_count+1, next_retry_at)
     |                    不可自动重试 -> failed
     |                    last_error_stage=scan]
     |
     v
[执行 event 优先级治理]
 - 不删除 `L3.event`
 - 不归档 `L3.event`
 - 只重算其 recall_priority
 - 推荐影响因子：
   - updated_at：越新优先级越高
   - importance：越高优先级越高
   - last_recalled_at：近期被召回过的事件优先级更高
   - recall_count：长期反复被命中的事件优先级更高
 - 输出：
   - high
   - medium
   - low
 - `low` 仅表示“召回排序靠后”
 - 不表示自动淘汰或自动删除
     |
     v
[批量写回]
 - 通过 mem0_adapter.update(memory_id, scope=user_character_scope, metadata={recall_priority}) 批量更新
 - 不直接写底层向量存储 payload，避免绕过 Mem0 索引一致性
 - 不做归档
 - 不做删除
 - 若 dry_run=true
   -> 不落库，只产出 preview_result
     |
     +--> batch write failed -> [worker: 更新 memory_task 状态
     |                          可自动重试 -> pending(retry_count+1, next_retry_at)
     |                          不可自动重试 -> failed
     |                          last_error_stage=batch_write]
     |
     v
[worker: 收敛 memory_task 状态
completed(result:
   processed_count / updated_count /
   preview_count / dry_run /
   partition_index / partition_count /
   priority_model_version) /
pending(error_message, retry_count + 1, next_retry_at) /
failed(error_message) /
dead_letter(error_message)]

[补偿调度器]
 机制与写入闭环一致
```

## 2. 服务化边界

> 当记忆能力独立成服务，除了业务流程，还要交代调用入口、任务状态和依赖健康。

当记忆能力从模块变成独立服务，只描述 `append / recall / delete / rebuild` 的业务流程还不够。调用入口、任务查询、健康检查、超时降级和历史源 adapter 都要提前说清楚，否则上游只能看到“请求已受理”，却不知道服务是否健康、任务是否完成、失败后从哪里恢复。

### 2.1 服务入口

统一使用版本化 API。首版推荐先固定 `v1`，后续兼容升级只新增字段，不删除字段、不改变既有字段语义。

| 能力 | 方法与路径 | 语义 | 返回方式 |
| --- | --- | --- | --- |
| 写入完整轮次 | `POST /v1/memory/append` | 受理完整 `user -> assistant` 轮次，刷新 L1 并异步沉淀 L2/L3 | 同步返回入口受理结果 |
| 融合召回 | `POST /v1/memory/recall` | 同步读取并融合 L1/L2/L3 | 同步返回召回结果，可降级成功 |
| 删除记忆 | `POST /v1/memory/delete` | 按 `memory/session/all` 删除或标脏 | 同步返回 `task_id` |
| 重建记忆 | `POST /v1/memory/rebuild` | 基于历史对话源重建 L2/L3 | 同步返回 `task_id` |
| 查询任务 | `GET /v1/memory/tasks/{task_id}` | 查询异步任务最终状态 | 同步返回任务状态 |
| 条件查询任务 | `GET /v1/memory/tasks` | 按 `request_id / op_type / status` 查询 | 同步返回任务列表 |
| 查询影响范围 | `GET /v1/memory/affected` | 按 `user_id / character_id / session_id?` 查询受影响记忆 | 治理查询 |
| 存活检查 | `GET /healthz` | 只判断进程是否存活 | 不访问下游依赖 |
| 就绪检查 | `GET /readyz` | 判断服务是否可接流量 | 检查关键依赖 |
| 指标暴露 | `GET /metrics` | 暴露 Prometheus 指标 | 不包含正文内容 |

首版不需要提供公网 API、开放式 SDK 或复杂管理 UI。若接入 API 网关，网关路径可以不同，但进入服务进程后的逻辑路由需要能映射到上述能力。

### 2.2 调用边界

记忆服务作为基础设施能力，主要服务上游编排层。鉴权、用户授权、租户权限和复杂配额更适合放在网关、服务治理或上游业务系统里；记忆服务只保留排障和治理所需的调用方标识。

这层边界可以收敛成几条：

- 通过服务发现、服务网格或 API 网关接入，不把记忆服务直接暴露成外部用户入口。
- 每次请求推荐携带稳定调用方标识，例如 `X-Caller-Service` 或等价服务名；该字段用于日志、指标和排障，不作为权限判断依据。
- `request_id` 贯穿业务请求体；`trace_id` 可来自 `X-Trace-Id`，缺失时由服务生成并写入日志。
- 记忆服务不重复实现终端用户权限模型，但输入契约会拒绝缺失 `user_id / character_id` 的记忆命令，避免跨用户或跨角色写错作用域。
- 服务内只保留保护性并发上限和超时，防止依赖抖动拖垮自身；不重复实现业务限流、配额和授权系统。

推荐请求头：

```text
X-Caller-Service: liaohe-orchestrator
X-Trace-Id: trace_xxx
X-Request-Id: req_xxx
```

请求头里的 `X-Request-Id` 只用于链路追踪；业务幂等仍以命令协议中的稳定业务键为准。

### 2.3 统一响应与错误边界

HTTP 状态表达传输层和服务可达性，响应体中的 `code / message / data` 表达记忆业务结果。不要只依赖 HTTP 状态判断业务是否完成。

| HTTP 状态 | 适用场景 | 业务体要求 |
| --- | --- | --- |
| `200` | 同步成功、跳过、业务拒绝、降级成功 | 返回 `code + message + data` |
| `202` | 异步任务已受理 | 返回 `task_id` 或可查询锚点 |
| `400` | 请求结构、字段类型、枚举值错误 | `code=1001` |
| `404` | 任务或治理查询目标不存在 | 返回稳定查询错误码 |
| `429` | 上游治理层或服务保护性并发限制 | 不登记业务任务 |
| `500` | 未分类服务异常 | 必须记录 `trace_id` |
| `503` | 关键依赖不可用或服务未就绪 | 不应伪装成业务成功 |

所有命令响应体保持同一外壳：

```json
{
  "code": 0,
  "message": "ok",
  "data": {},
  "trace_id": "trace_xxx"
}
```

`trace_id` 可放在响应头或响应体；若两者都存在，值必须一致。

### 2.4 任务查询闭环

凡返回 `task_id` 的命令，都需要能通过任务查询接口查到最终状态。`append` 可以不直接返回 `task_id`，但要能按 `request_id / round_id / op_type=extract` 定位对应任务。

首版任务状态保持简单即可：

- `pending`
- `running`
- `completed`
- `failed`

后续再增加 `retry_count / next_retry_at`；当失败类型变复杂，再引入 `dead_letter` 和人工处置。

任务查询响应可以长这样：

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "task_id": "memory-rebuild:session:user_xxx:char_xxx:sess_xxx:v20260419T100000Z",
    "request_id": "req_xxx",
    "op_type": "rebuild",
    "status": "running",
    "result": null,
    "affected_scope": {
      "user_id": "user_xxx",
      "character_id": "char_xxx",
      "session_id": "sess_xxx"
    },
    "retry_count": 0,
    "next_retry_at": null,
    "last_error_stage": null,
    "error_code": null,
    "error_message": null,
    "created_at": "2026-04-19T10:00:00Z",
    "updated_at": "2026-04-19T10:00:02Z",
    "completed_at": null
  },
  "trace_id": "trace_xxx"
}
```

查询不到任务时，返回 `code=4040 / message=task not found`，并按 HTTP `404` 或网关统一错误规范返回。

### 2.5 健康检查与依赖就绪

`/healthz` 只表示进程存活，不访问 PostgreSQL、Redis、Mem0 或底层向量存储。

`/readyz` 建议检查关键依赖，并返回可读状态：

| 依赖 | 首版就绪含义 | 失败影响 |
| --- | --- | --- |
| PostgreSQL | 任务、摘要、轮次账本等逻辑表可读写 | 不可接写入、删除、重建 |
| Redis / Queue | L1 缓存、任务派发或 worker 领取可用 | 写入受理和异步任务不可闭环 |
| Mem0 / 向量存储适配层 | L3 检索与写入可用 | recall 可降级，L3 写入/重建会失败或重试 |
| 历史对话源 adapter | rebuild 可读取完整轮次 | rebuild 不可用 |

推荐 `readyz` 返回三态：

- `ready`：关键依赖可用，可以接流量。
- `degraded`：非关键层不可用，例如 Mem0 或向量存储适配层临时异常；recall 可降级，但写入 worker 可能积压。
- `not_ready`：PG、队列或核心配置不可用，不应接主流量。

### 2.6 超时、重试与降级预算

独立服务需要有明确超时，不能把上游对话链路拖死。可以先用下面这组基线，具体值再通过配置调整。

| 链路 | 推荐总超时 | 分层预算 |
| --- | --- | --- |
| `append` 同步入口 | `300ms - 800ms` | 只做校验、journal、task 登记、dispatch；L2/L3 不在同步等待内 |
| `recall` | `800ms - 1500ms` | L1 `50ms`、L2 `100ms`、L3 `500ms - 1000ms`、融合裁剪 `100ms` |
| `delete` 同步入口 | `300ms - 800ms` | 只完成任务登记和派发 |
| `rebuild` 同步入口 | `300ms - 800ms` | 只完成任务登记和派发 |
| `task query` | `300ms` | 只查任务状态，不触发补偿 |

召回默认采用 degraded success：L3 超时或单层读取失败时，返回其余可用层并标记 `degraded=true`。只有输入协议错误、作用域隔离失败、安全过滤失败，或全部目标层不可用时才整体失败。

### 2.7 历史对话源 adapter

重建不能依赖 L1，因此独立服务需要一个只读历史源 adapter。首版不要求强一致 snapshot，但要保证完整轮次可按稳定顺序读取。

历史源 adapter 至少提供三类读取能力：

- `get_round(user_id, character_id, session_id, round_id)`：读取单个完整轮次。
- `list_rounds(user_id, character_id, session_id?, cursor?, limit?)`：分页读取某个用户角色下的完整轮次。
- `get_history_version(user_id, character_id, session_id?)`：P0 可返回当前最新可见版本；P2 才要求强一致 `history_snapshot_version`。

返回约束：

- 每个 round 至少包含 `round_id / session_id / messages[] / timestamp`。
- 若存在 `round_index`，优先按 `round_index` 稳定排序；否则按 `timestamp + round_id` 稳定排序。
- 缺失 assistant 回复的半轮消息不能进入记忆重建；应作为 `partial_round_skipped` 计数或确定性错误记录。
- adapter 读取失败时，rebuild 任务进入 `failed` 或 P1 retry，不允许用 L1 反推历史。

### 2.8 配置、备份与数据安全底线

首版先把这些配置项显式化：

- PostgreSQL DSN
- Redis / Queue DSN
- Mem0 模式与向量存储适配层连接配置
- L1 window 大小
- L1 缺失 `session_id` 时的召回策略，推荐跳过 L1，只使用 L2/L3
- L2 摘要触发阈值，例如首版默认每 10 个 active round 滚动生成一次摘要；该值建议配置化，不写死在代码里
- recall 总超时与 L3 timeout
- worker 并发数和 `max_retries`
- 是否启用 P2 能力开关：`write_fence / suppression / dead_letter / decay`

数据恢复底线：

- PostgreSQL 是任务、摘要和 journal 的关键状态，必须有备份。
- Mem0 / 向量存储适配层是 L3 长期记忆载体，必须能按 `user_id × character_id` 恢复或通过 rebuild 重建。
- Redis 中的 L1 是派生缓存，丢失后可由后续 append/recall 逐步恢复，不作为真值备份对象。

安全底线：

- 日志和指标不得记录完整消息正文、密钥、系统提示词或工具调试回显。
- 指标标签不得直接使用 `user_id / character_id / session_id / round_id / memory_id` 这类高基数字段；这些字段只进入日志、审计或查询条件，并按存储规范脱敏或哈希。
- 删除、重建、人工补偿必须进入审计。
- 密钥和 DSN 只通过环境变量或密钥系统注入，不写入文档示例和日志。

## 3. 标准命令协议

> 如果要落到接口实现，先看公共规则，再按命令查看请求和响应示例。`decay` 是后续可选协议，不影响首版核心链路。

这里定义推荐基准下的五个业务命令协议，并补充任务查询协议。`healthz / readyz / metrics` 属于服务运行接口，见第 2 章。

### 3.1 Mem0 Adapter 约束

工程版里的 `mem0_adapter.*` 都是业务侧适配器伪代码，不是 Mem0 SDK 或 Platform 的原生签名。

- `scope=user_character_scope` 表示业务隔离作用域，逻辑上等价于 `user_id × character_id`。
- adapter 必须按当前 Mem0 OSS/Platform 的实体、namespace、run、tenant、metadata filter 和返回字段实测映射该 scope。
- 不允许把 `user_id + agent_id=character_id` 写死成严格交集隔离。若当前 Mem0 模式不能稳定表达交集，使用组合用户键、组合 namespace、租户键或其他单一强隔离键。
- `filters={character_id, ...}` 只做二次校验、治理、审计和误写防护，不承担主要隔离职责。
- 写入侧优先复用 Mem0 的抽取、去重和分类；检索侧优先复用 Mem0 的 filters、top_k、threshold、rerank；显式更新/删除由业务命令调用 Mem0 update/delete/delete_all。冲突收敛按当前版本实测，能交给 Mem0 的交给 Mem0，不能稳定表达的部分才由业务侧显式 update/delete/rebuild 兜底。若某项参数或返回字段在当前 SDK/Platform 不可用，adapter 必须降级为等价可验证映射，不能在应用层重做记忆引擎。

公共规则：

- 所有业务命令都必须带 `request_id`。
- 所有记忆命令都必须带 `character_id`；P2 `decay` 为平台调度命令，不绑定单个 `user_id × character_id`。
- 所有同步返回只表示入口处理结果，不代表后台一定完成。
- 记忆服务不重复实现鉴权和终端用户授权，协议校验只负责字段完整性、作用域隔离和记忆域安全守卫。
- 对 `append`，`code` 必须是数值码，`message` 是稳定可读文案。
- `task not resumable(code=2006)` 是通用后台任务语义：命中同一幂等键下不可恢复的 `failed / dead_letter` 任务时，不通过同步入口隐式恢复，需要走治理入口处理。
- `delete / rebuild` 这类后台任务型命令推荐返回 `task_id`，便于后续查询最终状态；`decay` 是 P2 调度命令。
- `append` 入口只需返回 `code + message + data(request_id, round_id, session_id)`；是否返回 `task_id` 不是必需项。
- `append` 即使不返回 `task_id`，也必须能通过 `request_id / round_id / op_type=extract` 查询任务状态。

### 3.2 append 数值码表

> P0 重点关注 `accepted / invalid contract / empty round content / blocked by safety / round conflict / task not resumable`；`round index conflict` 属于 P2 连续账本场景。

- `0`
  `accepted`
- `1001`
  `invalid contract`
- `1002`
  `empty round content`
- `1003`
  `blocked by safety`
- `1004`
  `task already completed`
- `1005`
  `round conflict`
- `1006`
  `round index conflict`
- `2001`
  `scope state query failed`
- `2003`
  `task persist failed`
- `2004`
  `dispatch failed`
- `2005`
  `journal write failed`
- `2006`
  `task not resumable`

说明：

- `request_id` 只用于链路追踪
- `round_id` 是 append 的业务幂等锚点
- `accepted` 仅表示入口已成功受理，不代表后台 L2 / L3 已完成
- `task already completed` 表示命中同一 `round_id` 的已完成任务，不属于“拒绝”
- `round conflict` 表示同一 `round_id` 对应的轮次身份冲突，例如内容、作用域或 generation 不一致
- `round index conflict` 是 P2 连续账本错误，表示本次 append 没有满足连续轮次推进规则；P0 不应默认用 round_index 缺口拒绝 append
- `journal write failed` 表示 durable round 真值未成功持久化
- `task not resumable` 表示 P0 命中旧的 `failed` 任务，或 P2 命中旧的 `failed / dead_letter` 任务，不再按 accepted 语义处理

### 3.3 append

> 一个容易踩坑的点：append 同步返回只表示入口受理，不代表 L2/L3 已经沉淀完成。

请求：

```json
{
  "request_id": "req_xxx",
  "user_id": "user_xxx",
  "character_id": "char_xxx",
  "session_id": "sess_xxx",
  "round_id": "round_xxx",
  "round_index": 12,
  "messages": [
    {
      "message_id": "msg_user_xxx",
      "role": "user",
      "message_content": "我最近在评估换工作机会",
      "timestamp": "2026-04-19T09:59:50Z"
    },
    {
      "message_id": "msg_assistant_xxx",
      "role": "assistant",
      "message_content": "你更看重薪资、成长空间，还是城市机会？",
      "timestamp": "2026-04-19T10:00:00Z"
    }
  ],
  "timestamp": "2026-04-19T10:00:00Z"
}
```

说明：`round_index` 在 P0 为推荐字段，用于排序和排障；P2 启用连续账本时才作为强约束字段。若 P0 请求未提供 `round_index`，服务必须能退化为按 `timestamp + round_id` 稳定排序。

同步受理响应：

```json
{
  "code": 0,
  "message": "accepted",
  "data": {
    "request_id": "req_xxx",
    "round_id": "round_xxx",
    "session_id": "sess_xxx"
  }
}
```

同步跳过响应：

```json
{
  "code": 1002,
  "message": "empty round content",
  "data": {
    "request_id": "req_xxx",
    "round_id": "round_xxx",
    "session_id": "sess_xxx"
  }
}
```

安全拒绝响应：

```json
{
  "code": 1003,
  "message": "blocked by safety",
  "data": {
    "request_id": "req_xxx",
    "round_id": "round_xxx",
    "session_id": "sess_xxx"
  }
}
```

已完成幂等响应：

```json
{
  "code": 1004,
  "message": "task already completed",
  "data": {
    "request_id": "req_xxx",
    "round_id": "round_xxx",
    "session_id": "sess_xxx"
  }
}
```

轮次身份冲突响应：

```json
{
  "code": 1005,
  "message": "round conflict",
  "data": {
    "request_id": "req_xxx",
    "round_id": "round_xxx",
    "session_id": "sess_xxx"
  }
}
```

轮次顺序冲突响应：

```json
{
  "code": 1006,
  "message": "round index conflict",
  "data": {
    "request_id": "req_xxx",
    "round_id": "round_xxx",
    "session_id": "sess_xxx"
  }
}
```

不可恢复旧任务响应：

```json
{
  "code": 2006,
  "message": "task not resumable",
  "data": {
    "request_id": "req_xxx",
    "round_id": "round_xxx",
    "session_id": "sess_xxx"
  }
}
```

journal 持久化失败响应：

```json
{
  "code": 2005,
  "message": "journal write failed",
  "data": {
    "request_id": "req_xxx",
    "round_id": "round_xxx",
    "session_id": "sess_xxx"
  }
}
```

派发失败响应：

```json
{
  "code": 2004,
  "message": "dispatch failed",
  "data": {
    "request_id": "req_xxx",
    "round_id": "round_xxx",
    "session_id": "sess_xxx"
  }
}
```

### 3.4 recall

> 一个容易踩坑的点：首版 recall 允许降级返回。上游应读取 `items`，并根据需要关注 `degraded_layers` 一类诊断字段。

请求：

```json
{
  "request_id": "req_xxx",
  "user_id": "user_xxx",
  "character_id": "char_xxx",
  "session_id": "sess_xxx",
  "retrieval_query": "近期关于职业规划的记忆"
}
```

成功响应：

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "request_id": "req_xxx",
    "degraded": false,
    "degraded_layers": [],
    "layer_status": {
      "l1": "ok",
      "l2": "ok",
      "l3": "ok"
    },
    "items": [
      {
        "layer": "l2",
        "summary_id": "sum_xxx",
        "content": "近期阶段摘要：用户处于职业决策期，持续比较薪资、成长空间与城市机会，后续互动应围绕择业判断与行动方案继续推进。",
        "updated_at": "2026-04-19T10:01:00Z"
      },
      {
        "layer": "l1",
        "round_id": "round_xxx",
        "round_index": 12,
        "messages": [
          {
            "message_id": "msg_user_xxx",
            "role": "user",
            "normalized_content": "我最近在评估换工作机会",
            "timestamp": "2026-04-19T09:59:50Z"
          },
          {
            "message_id": "msg_assistant_xxx",
            "role": "assistant",
            "normalized_content": "你更看重薪资、成长空间，还是城市机会？",
            "timestamp": "2026-04-19T10:00:00Z"
          }
        ],
        "timestamp": "2026-04-19T10:00:00Z",
        "session_id": "sess_xxx"
      },
      {
        "layer": "l3",
        "memory_id": "mem_xxx",
        "memory_type": "event",
        "content": "用户近期在评估换工作机会",
        "score": 0.92,
        "importance": 0.73,
        "recall_priority": "high",
        "updated_at": "2026-04-19T10:01:05Z"
      }
    ]
  }
}
```

降级成功响应：

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "request_id": "req_xxx",
    "degraded": true,
    "degraded_layers": ["l3"],
    "layer_status": {
      "l1": "ok",
      "l2": "ok",
      "l3": "timeout"
    },
    "failed_layer_reason": {
      "l3": "mem0 search timeout"
    },
    "items": [
      {
        "layer": "l2",
        "summary_id": "sum_xxx",
        "content": "近期阶段摘要：用户处于职业决策期，持续比较薪资、成长空间与城市机会，后续互动应围绕择业判断与行动方案继续推进。",
        "updated_at": "2026-04-19T10:01:00Z"
      }
    ]
  }
}
```

空结果成功响应：

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "request_id": "req_xxx",
    "degraded": false,
    "degraded_layers": [],
    "items": []
  }
}
```

查询为空跳过响应：

```json
{
  "code": 1002,
  "message": "empty retrieval query",
  "data": {
    "request_id": "req_xxx",
    "degraded": false,
    "degraded_layers": [],
    "items": []
  }
}
```

失败响应：

```json
{
  "code": 2101,
  "message": "recall failed",
  "data": {
    "request_id": "req_xxx",
    "items": [],
    "failed_layer": "all",
    "failed_layer_reason": {
      "l1": "redis timeout",
      "l2": "postgres timeout",
      "l3": "mem0 search timeout"
    }
  }
}
```

召回安全过滤失败响应：

```json
{
  "code": 2102,
  "message": "recall safety filter failed",
  "data": {
    "request_id": "req_xxx",
    "items": [],
    "failed_layer": "safety_filter"
  }
}
```

补充说明：

- 返回的 `items` 是经过层内标准化、召回安全过滤、跨层去重与冲突收敛之后的结果，不等于三层原始命中的简单拼接
- `code=0` 不代表三层都成功；`degraded=true` 表示本次召回使用了可用层降级返回，上游可继续生成回复但应记录诊断信息
- 若 L2 与 L3 对同一事实表达冲突，以 L3 作为真值来源；L2 保留阶段叙事，但不重复返回同一事实表述
- L1 提供近邻原文上下文，不与 L3 竞争真值
- `L3 skip` 仅表示本次不发起长期记忆语义检索，不影响 L1 / L2 默认召回
- `event touch-back` 是召回后的异步旁路动作，不体现在同步响应中

### 3.5 delete

> 一个容易踩坑的点：delete 是异步受理语义。首版删除后，如果已生效摘要可能包含被删内容，标记 L2 dirty 并由 rebuild 修复；如果只是未摘要新轮次落后，标记 stale 并允许滚动摘要或 rebuild 修复。

请求：

```json
{
  "request_id": "req_xxx",
  "user_id": "user_xxx",
  "character_id": "char_xxx",
  "delete_scope": "session",
  "session_id": "sess_xxx"
}
```

同步成功响应：

```json
{
  "code": 0,
  "message": "accepted",
  "data": {
    "request_id": "req_xxx",
    "delete_scope": "session",
    "task_id": "memory-delete:session:user_xxx:char_xxx:sess_xxx:v20260419T100000Z"
  }
}
```

说明：P0 示例使用 `scope_key + p0_scope_version` 作为任务身份；如果历史源或 journal 暂时无法提供版本，也可以由上游传入显式 `operation_id`。P2 启用 `scope_content_version` 后，可扩展为 `memory-delete:{scope_key}:{scope_content_version}`。

### 3.6 rebuild

> 一个容易踩坑的点：rebuild 的输入来源是历史对话源，不是 L1 短期缓存。

请求：

```json
{
  "request_id": "req_xxx",
  "user_id": "user_xxx",
  "character_id": "char_xxx",
  "rebuild_scope": "session",
  "session_id": "sess_xxx"
}
```

同步成功响应：

```json
{
  "code": 0,
  "message": "accepted",
  "data": {
    "request_id": "req_xxx",
    "task_id": "memory-rebuild:session:user_xxx:char_xxx:sess_xxx:v20260419T100000Z"
  }
}
```

说明：P0 示例使用 `rebuild_scope_key + p0_history_version` 作为任务身份；如果历史源暂时无法提供版本，也可以由上游传入显式 `operation_id`。P2 启用 `history_snapshot_version` 后，可扩展为 `memory-rebuild:{rebuild_scope_key}:l2hv{l2_history_snapshot_version}:l3hv{l3_history_snapshot_version}`。

全量重建请求：

```json
{
  "request_id": "req_xxx_all",
  "user_id": "user_xxx",
  "character_id": "char_xxx",
  "rebuild_scope": "all"
}
```

### 3.7 decay（后续可选）

> 一个容易踩坑的点：decay 是定时治理任务，不进入首版核心链路。

调度触发载荷：

```json
{
  "request_id": "req_20260419_0100",
  "schedule_slot": "2026-04-19T01",
  "triggered_at": "2026-04-19T01:00:00Z",
  "partition_count": 16,
  "partition_index": 3,
  "dry_run": false
}
```

调度受理响应：

```json
{
  "code": 0,
  "message": "accepted",
  "data": {
    "request_id": "req_20260419_0100",
    "task_id": "memory-decay:2026-04-19T01:p3-of-16:dry_run=false",
    "schedule_slot": "2026-04-19T01",
    "partition_index": 3
  }
}
```

dry-run 预览请求：

```json
{
  "request_id": "req_20260419_0100_preview",
  "schedule_slot": "2026-04-19T01",
  "triggered_at": "2026-04-19T01:00:00Z",
  "partition_count": 16,
  "partition_index": 3,
  "dry_run": true
}
```

### 3.8 task query

> 一个容易踩坑的点：任务查询是异步闭环的一部分。凡入口返回 `task_id`，上游或治理入口都需要能查到最终状态。

按 `task_id` 查询：

```text
GET /v1/memory/tasks/memory-rebuild:session:user_xxx:char_xxx:sess_xxx:v20260419T100000Z
```

按条件查询：

```text
GET /v1/memory/tasks?request_id=req_xxx&op_type=rebuild&status=running
```

成功响应：

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "task_id": "memory-rebuild:session:user_xxx:char_xxx:sess_xxx:v20260419T100000Z",
    "request_id": "req_xxx",
    "op_type": "rebuild",
    "status": "completed",
    "result": {
      "l2_result": "completed",
      "l3_result": "completed"
    },
    "affected_scope": {
      "user_id": "user_xxx",
      "character_id": "char_xxx",
      "session_id": "sess_xxx"
    },
    "retry_count": 0,
    "next_retry_at": null,
    "last_error_stage": null,
    "error_code": null,
    "error_message": null,
    "created_at": "2026-04-19T10:00:00Z",
    "updated_at": "2026-04-19T10:00:20Z",
    "completed_at": "2026-04-19T10:00:20Z"
  }
}
```

任务不存在响应：

```json
{
  "code": 4040,
  "message": "task not found",
  "data": {
    "task_id": "memory-rebuild:session:user_xxx:char_xxx:sess_xxx:v20260419T100000Z"
  }
}
```

查询原则：

- `append` 未返回 `task_id` 时，必须支持按 `request_id / round_id / op_type=extract` 查询。
- `delete / rebuild / decay` 返回 `task_id` 时，优先按 `task_id` 查询。
- 查询接口不隐式重试、不恢复失败任务，只返回当前状态。
- `failed / dead_letter` 任务只能通过治理动作收敛，不能通过重复提交同一入口命令恢复。

协议原则：

- `append` 只有在入口受理成功时才返回 `code=0`。
- `append` 若命中空内容、安全拒绝、已完成任务、查询失败、落库失败、派发失败，则返回对应数值码。
- `recall` 默认是同步读语义，统一返回 `code + message + data(...)`。
- `delete / rebuild` 默认是异步 accepted 语义；P2 `decay` 也是异步 accepted 语义，入口返回格式统一为 `code + message + data(...)`。
- `delete / rebuild / decay` 命中已完成的同一 `task_id` 时返回 `already_done(code=1004)`，不按 rejected 处理。
- 若返回 `task_id`，治理入口需要能通过它查询最终状态。
- `task query` 只查询状态，不触发补偿或重试。
- `skipped` 与 `rejected` 的区别体现在数值码语义上：前者表示“协议合法但无需继续”，后者表示“协议或内容被拒绝”。

## 4. 可观测性设计

> 可观测性先服务于排障和运营。首版先保证请求、任务、召回、删除、重建可追踪；后续再补齐 dead-letter、decay、fence 等治理指标。

可观测性目标：

- 能查清一次请求是否被拒绝、跳过、受理、完成或失败；强治理阶段还要能查清是否进入死信。
- 能定位问题发生在 L1、L2、L3、调度层还是 worker 层。
- 能量化记忆系统的命中率、沉淀率和失败率；启用衰减后再量化衰减率。

### 4.1 日志

> 日志目标：一次请求出问题时，能定位是入口、L1、L2、L3、dispatch 还是 worker。

推荐日志最小字段：

- `trace_id`
- `request_id`
- `task_id`
- `op_type`
- `user_id`
- `character_id`
- `session_id`
- `round_id`
- `status`
- `error_message`

推荐日志节点：

- 输入契约校验失败
- 安全守卫拒绝
- summary_round_journal 持久化成功/失败
- 后续 append_cursor_round_index 推进成功/失败
- L1 派生缓存刷新成功/跳过/失败
- L2 摘要生成成功/失败
- L3 长期记忆抽取成功/失败
- task 派发成功/失败
- worker 完成/重试/失败；强治理阶段记录死信

### 4.2 指标

> 指标先看核心链路是否健康，再看增强治理是否有效。治理类指标可以先留好位置，不必在首版一次性铺满。

核心指标可以分为基础指标和增强治理指标。首版先接基础指标，增强治理指标随着对应能力一起接入。

指标标签保持低基数。推荐标签只使用 `op_type / status / layer / error_code / caller_service / algorithm_mode` 这类有限枚举；不要把 `user_id / character_id / session_id / round_id / memory_id / task_id / request_id` 放进 Prometheus label。需要按这些字段排障时，走日志、审计或任务查询接口。

- `memory_append_total`
- `memory_append_rejected_total`
- `memory_append_skipped_total`
- `memory_recall_total`
- `memory_recall_failed_total`
- `memory_recall_safety_filtered_total`
- `memory_recall_touchback_failed_total`
- `memory_write_fence_outdated_total`（P2）
- `memory_append_round_conflict_total`
- `memory_append_round_index_conflict_total`（P2）
- `memory_append_journal_write_failed_total`
- `memory_l1_cache_refresh_failed_total`
- `memory_l1_cache_refresh_skipped_total`
- `memory_delete_scope_idempotent_hit_total`
- `memory_task_not_resumable_total`
- `memory_task_retry_total`
- `memory_task_failed_total`
- `memory_task_dead_letter_total`（P2）
- `memory_l2_summary_generated_total`
- `memory_l2_summary_dirty_total`
- `memory_l2_summary_suppressed_total`（P2）
- `memory_l3_added_total`
- `memory_l3_explicit_updated_total`
- `memory_l3_explicit_deleted_total`
- `memory_l3_conflict_review_total`（P2）
- `memory_l3_recall_overfetch_total`（P1/P2）
- `memory_l3_algorithm_mode_total{mode=add_only|legacy_actions|custom_update_prompt}`
- `memory_fence_fail_closed_window_total`（P2）
- `memory_decay_processed_total`（P2）
- `memory_decay_priority_updated_total`（P2）
- `memory_decay_preview_total`（P2）
- `memory_decay_partition_total`（P2）

推荐时延指标：

- `memory_append_latency_ms`
- `memory_recall_latency_ms`
- `memory_dispatch_latency_ms`
- `memory_worker_latency_ms`

### 4.3 审计

> 审计目标：删除、重建、人工补偿这类会影响用户记忆结果的动作必须可追责。

必须审计的动作：

- 安全拒绝
- 记忆删除
- 长期记忆重建
- 死信任务补偿
- 人工关闭任务

审计最小字段：

- `request_id`
- `operator`
- `action`
- `target_scope`
- `target_id`
- `reason`
- `created_at`

### 4.4 告警

> 告警目标：优先发现核心链路失效，其次观察增强治理能力的失败趋势。

推荐告警场景：

- `memory_task_dead_letter_total` 突增
- `dispatch failed` 持续升高
- `recall failed` 持续升高
- `append rejected` 异常升高
- `L2` 长时间未生成新摘要
- `L3` 长时间无新增或更新，疑似抽取链路失效
- `decay` 连续失败

推荐告警原则：

- 先按错误类型区分是否可自动恢复。
- 可自动恢复的问题优先观测趋势。
- 不可自动恢复的问题应尽快升级到人工介入。

### 4.5 任务查询与人工处理

> 任务一旦异步化，就需要能查状态、定位失败，并在系统无法自动恢复时交给人工处理。

这部分能力可以先从查询开始：

- 按 `request_id` 查询整条链路
- 按 `task_id` 查询任务状态
- 按 `user_id / character_id / session_id` 查询受影响记忆
- 按 `status=failed/dead_letter` 查询待处理任务
- 对失败任务执行：
  - 跳过
  - 人工关闭

可观测性原则：

- 没有 trace_id 的日志，等于不可排障。
- 没有指标的失败，只能靠运气发现。
- 没有审计的删除与补偿，后续不可追责。
- 没有死信查询能力，补偿闭环就不完整。
