# Thinkback 当前架构、插件化与生产准入复审

日期：2026-10-28  
代码基线：`refactor/memory-architecture-rewrite@60c9023`，另含用户原有的 `tests/script/run_self_contained_evaluation.py` 未提交兼容修复  
评估方式：当前源码、确定性复现、工程门禁、指定模型端点 smoke、主流项目一手资料。

What this repo does: Thinkback 是 Mem0 之上的企业记忆服务：PostgreSQL 保存 L1 轮次与 L2 会话视图，Mem0 + Milvus 负责 L3 长期事实，HTTP/gRPC 与管理台提供接入和治理。本文按“多个企业助手接入、真实敏感数据、生产故障与重试”评估；单副本内网试点可以是一个边界，但不能外推成 HA 或通用 SaaS 能力。

## 0. 结论

**设计主线没有偏离主流，技术栈也没有脱节；当前不应签发生产就绪。**

- **不需要换 Mem0，也不需要重写三层。** L1 原始证据、L2 近期上下文、L3 派生长期事实，与 Zep episode/context、LangMem 类型化记忆、Cognee 分层写入状态的方向一致。
- **Thinkback 的合理价值在服务层。** 持久执行、幂等、namespace、来源与版本、上下文装配、删除审计、HTTP/gRPC/SDK，正是 Mem0 OSS 不做或做不完整的地方。
- **当前主要偏差是实现未完全遵守“引擎优先”。** 固定 P0 槽位、canonicalization、冲突和摘要逻辑仍与 Mem0 语义能力交织；类型注册表目前更像策略配置，不是完整的记忆类型插件。
- **当前存在可复现的数据正确性与协议 bug。** namespace 的 L1/L2 会串数据且删除可跨范围误删；L2 在特定时序可复活已删内容；崩溃恢复会丢类型/TTL；gRPC erase 任务查询会抛 `KeyError`；自定义类型在业务 list/get 响应中变成 `null`。这些比“是否再加引擎能力”更优先。
- **质量证据不够。** 自包含评测全绿，但最近多次真实垂类复跑为 68.8% 到 75%，最新一次为 75%（12/16），且当前发布 SHA 没有通过 real baseline 的归档结果。对记忆产品而言，模型抽取与召回质量就是系统质量，不能归为“模型侧问题”后放行。

建议路线：**保留现有服务，先修正确性和契约，再收窄并贯通插件模型；不做进程内通用库，不引入图数据库，不重写 Mem0。**

## 1. 与主流设计相比：哪些合理，哪些偏离

| 维度 | 主流参照 | 对 Thinkback 的判断 |
| --- | --- | --- |
| 三层模型 | Zep 保留 episode 与派生事实；近期原文与长期上下文分开装配 | 合理，应保留。Mem0 SQLite 的最近 messages 只是内部副本，Thinkback L1 才是权威账本 |
| 长期记忆引擎 | Mem0 OSS 提供 ADD-only 抽取、语义候选、BM25/entity 加分、CRUD、expiration、reranker | 固定 Mem0 合理。当前 2.2.1 仍是最新 Python release，不存在版本脱节 |
| 冲突与当前事实 | Mem0 ADD-only 只做精确文本去重，OSS 排序没有 recency/supersede 信号 | Thinkback 的 supersede、纠正、valid/recorded time 是必要补充，不是重复造轮子 |
| 类型系统 | LangMem 区分 collection/profile/episodic/procedural，类型决定读写路径 | 当前 registry 有准入/TTL/conflict，但缺独立抽取 prompt/schema 和完整协议表达，只是部分实现 |
| 持久执行 | Mem0 AsyncMemory 不是持久队列；add 可部分成功，async delete_all 可吞部分错误 | Thinkback 必须负责 operation、lease/attempt、恢复和逐项对账 |
| 时间语义 | Graphiti 区分事实时间、记录时间、参考时间与来源 episode | 当前补 recorded_at 是正确方向；不要只凭 valid_at/invalid_at 宣称完整双时态/as-of |
| 删除 | Mem0 expiration 只隐藏；delete 可留下 history/messages；其他项目也区分逻辑删除与物理残留 | suppress/erase 必须保留。物理保留清扫不能被 expiration 替代 |
| 接入 | MCP 只是传输协议；普通 API/SDK 仍需自己定义记忆与任务语义 | sidecar/service 正确。先做稳定版本化 API 和生成 SDK，MCP 后做适配，不先做 `/mcp` 外壳 |

详细一手来源见 `docs/report/2026-10-28-mainstream-memory-landscape.md`。

### 两个容易误判的 Mem0 边界

1. **安装 2.2.1 不等于混合检索已生效。** Milvus 老 collection 缺 `text`/`sparse` 字段会退回 semantic-only；BM25/entity 只重排 semantic 候选，不扩大召回。必须检查新 schema、`_entities` collection、NLP 依赖和 `score_details`。
2. **行业词表不会自动进入 Mem0 NER。** 检索侧实体抽取固定使用 `en_core_web_sm`，与 `add(prompt=...)` 是不同路径。中文半导体词表的收益必须实测，不能在配置里写了就宣称生效。

## 2. Must fix：当前发布阻断项

### 1. namespace 只隔离 L3，L1/L2 串数据，删除还会跨 namespace 误删

- **证据：** `_session_scope_id` 只使用 session_id（`service.py:393-397`），recall 的 L1/L2 也只按 session 读取（1645-1686），而 L3 使用 namespace。主线程用同一 user/session 写 alpha 与 beta，alpha recall 同时得到两边 L1 原文和合并后的 L2。
- **更严重的是：** 在 alpha 执行 ALL delete 后，beta journal 也变成 `deleted_tombstone`，beta L3 却仍 ACTIVE，形成跨范围数据丢失和索引/来源分叉。契约却承诺 namespace 隔离（`schemas.py:131-134,255-258`）。
- **最小修复：** 让 journal 查询和缓存键实际使用现有 `journal.namespace`，并把 namespace 纳入 summary、L1 缓存、delete/erase 的独立键；旧数据迁移到默认 namespace，补同 user/session、不同 namespace 的读写删矩阵。

### 2. L2 删除复活仍有 TOCTOU 窗口

- **证据：** `l2_refresh.py:220-244` 先读 rounds，再读 previous summary，最后才读 expected_row_version。若删除发生在 rounds 读取后、版本读取前，刷新会拿到删除后的新版本号，却仍拿旧 rounds 生成摘要，CAS 可成功并把已删内容写回 ACTIVE。受控时序已复现。
- **最小修复：** 在读取 rounds 前取得摘要和期望版本，最好使用同一数据库快照；写入前再复核排除集合，并补这个精确时序的回归测试。
- **不同的 SLO 问题：** 不足 N 轮不立即刷新是现有设计，最新轮次由 L1 承载，不属于删除复活或轮次丢失。若产品要求会话结束即新鲜，应先定义结束信号和允许延迟，再评估 trailing；直接改成 counter>0 会改变每 N 轮的成本模型。

### 3. 崩溃恢复会丢失原类型、TTL 和来源时间，且一次最多恢复 200 个任务

- **证据：** `recover_pending_writes` 只取 `limit=200`（`service.py:1375-1377`），journal 明明保存原策略，重放却传 `request_metadata={}`（1386-1409）。
- **影响：** accepted 的 `state` 写入恢复后变成 `profile`，TTL 消失，`valid_at` 变成 None，`recorded_at` 记录恢复时刻。来源时间缺失可能影响冲突新旧判定。队列容量为 256，超过 200 的 accepted 写入还可能不自动重放。
- **最小修复：** 复用已有 `_replay_metadata(entry)`，分页恢复全部 RUNNING/PENDING 写入；补自定义 TTL 类型恢复和 >200 任务测试。

### 4. P0 白名单与 `force_extract` 仍能制造不可召回、不可按条管理的暗数据

- **证据：** 长消息或 `force_extract` 可过前置门（`domain/slots/support.py:192-222`）；白名单不支持时保留 Mem0 记录但不写本地索引（`service.py:4667-4674`），recall 又只接受本地 ACTIVE id（1735-1739）。
- **影响：** 已复现响应 completed、backend=1、local=0、recall L3=0。记录占用存储，无法通过业务 memory_id 更新/擦除，只能依赖 ALL 清理。
- **最小修复：** 调用 Mem0 前后使用同一准入策略；不接受的类型应在抽取前拒绝，后置失败必须删除刚写入的 event ids 并返回明确 skipped/failed。

### 5. 类型声明的 restricted 可被请求降级为 normal

- **证据：** `service.py:508-518` 只在请求未给 classification 时才采用类型策略。主线程用 `classification=restricted` 的类型，请求传 `data_classification=normal`，journal 与 Mem0 均写入，最终分类为 normal。
- **影响：** 调用方可以绕过 namespace 类型策略，使本应禁存的数据落库并发给外部模型。
- **最小修复：** 分类取请求与类型策略中更严格的一级，或直接拒绝降级；安全策略不能由调用方自报覆盖。PERSONAL 的脱敏承诺问题另见第 12 项。

### 6. 当前 `make check` 仍红，且“确定性测试”会触真实基础设施

- **证据：** HEAD `60c9023` 的 format/ruff/mypy 通过；主线程两次 `make check` 均为 887/888，独立复审另一次 `make test` 为 888/888。`test_lifespan_shutdown_resets_service_singleton` 在后台恢复线程完成初始化前断言 service 非空，说明启动测试存在时序不稳定，而不是可忽略的环境噪声。
- **问题：** 当前 SHA 没有完整门禁全绿。该测试日志还执行真实 PostgreSQL migration 并尝试连接 localhost Milvus，和单元套件“无真实依赖”的约定冲突。另一个可复现性缺口是：HEAD 的 eval 脚本仍 import 已从 schema 删除的 `RecallIntent`；当前未提交补丁删掉该旧字段后 eval 才通过，纯 HEAD 不能直接复现这套绿色结果。
- **最小修复：** 明确启动时序契约，用条件等待或同步完成廉价初始化，修正测试；同时隔离 lifespan 的迁移、Milvus、gRPC 和恢复线程，不能让单测依赖本机服务。
- **若不修：** 无法证明同一个发布 SHA 可重复通过门禁，CI 与本机结果可能不同。

### 7. gRPC 查询 erase 任务稳定抛 `KeyError`

- **证据：** `OperationType.ERASE` 不在 proto/转换表（`src/thinkback/rpc/converters.py:143-151`）；对 erase `TaskResponse` 调 `task_response_to_pb` 已复现 `KeyError <OperationType.ERASE: 'erase'>`。
- **影响：** gRPC 的 erase 写后任务查询不可用；现有 gRPC 测试没有 erase/suppress 覆盖。
- **最小修复：** 补齐 OperationType 与 suppress/erase RPC/转换契约，做 append→erase→GetTask 真实往返测试；若 gRPC 不准备支持，明确从发布面撤掉而不是声称镜像 HTTP。

### 8. 生产镜像没有打包 profile，错误配置还会静默换默认策略

- **证据：** `Dockerfile:29-34` 没有 `COPY profiles/`；`src/thinkback/api/dependencies.py:143-162` 对缺失或解析失败回落默认 registry。
- **影响：** 容器里的 `semiconductor` 不会加载仓库中的 profile，`process_preference` 等类型变成未注册。配置错误被转换成另一套记忆策略，属于危险的静默行为。
- **最小修复：** 镜像打包只读 profile，容器启动验证目标 namespace；生产配置缺失/非法应 fail-visible，开发默认值可保留。

### 9. 自定义类型在业务管理协议中被抹掉

- **证据：** `ManagedMemoryItem.memory_type` 仍是固定五值枚举（`memory/schemas.py:426-442`）；`_managed_memory_item` 对未知类型返回 None（`service.py:3306-3328,3486-3494`）。实测 `state` 和 `process_preference` 都变成 `null`。
- **影响：** 新类型虽能写入，list/get/update 客户端却无法知道它是什么；gRPC 也只有固定五值。现有自定义 profile 测试只检查后端 metadata，没有覆盖管理 API。
- **最小修复：** 对外类型改为带版本约束的字符串，保留内部受控枚举；补自定义类型 append/list/get/update/recall/erase 全链路契约测试。

### 10. “通用事件写入”尚未成立，自定义 metadata 也被丢弃

- **证据：** `AppendMemoryRequest` 强制恰好 `user -> assistant` 两条消息（`schemas.py:117-187`）；`_l3_metadata` 只保留少数字段（`service.py:5032-5061`）。
- **影响：** 工具结果、业务事件、单向事实、文档、多消息回合无法走统一入口；配置允许 tool 来源也没有公开传输表达。企业自定义业务属性不能可靠进入后端和索引。
- **最小修复：** 先定义一个版本化 `SourceEvent`，保留 role/source/occurred_at/received_at/trust/provenance；自定义属性使用命名空间化字典，与安全策略字段分开。不要继续给两条消息模型加旁路字段。

### 11. 真实记忆质量未达到可签发生产的标准

- **证据：** 最近多次真实垂类复跑为 68.8% 到 75%，最新一次为 75%（12/16）；阈值 0.5 尚未标定；当前 real baseline 所要求的失败归因字段在旧报告中不全，本轮真实 erase/quality 也未在发布 SHA 重跑。
- **影响：** 自包含 20/20 与 16/16 只证明 fake 编排契约，不证明真实抽取和召回。16 条真实用例中约有 4 到 5 条失败，不能默认接受。
- **最小修复：** 用指定的 27B 模型优化 Mem0 原生 prompt/few-shot 和输出契约，标定 threshold；先建立无 rerank 基线，再对 bge reranker 做同语料 A/B。设定产品可接受的 per-category 下限，而不只是总通过率。

### 12. PERSONAL 的“自动脱敏”承诺与实现不符

- **证据：** `DataClassification.PERSONAL` 注释为“脱敏存储”（`domain/enums.py:47-53`），召回内容也称“脱敏后正文”（`memory/schemas.py:393`）；实际只对 SENSITIVE/RESTRICTED 做禁存（`domain/classification.py:38-51`），没有 PERSONAL 正文脱敏。
- **影响：** 默认类型大多是 personal，原文仍可进入 PG、Mem0/Milvus 和召回响应。调用方可能据此作出错误合规判断。
- **最小修复：** 若近期不实现脱敏，立即把契约改为“personal 允许原文存储，不自动脱敏，调用方负责前置处理”；若承诺自动脱敏，必须覆盖所有副本、摘要和日志，不能只在响应层遮罩。

### 13. 物理擦除证据不足，不能宣称所有残留已清除

- **证据：** 历史真实 erase 16/16 是积极证据，但本轮未重跑；部分断言检查 ACTIVE/查询不可见，不是全表密文扫描。MEMORY/SESSION erase 给 `purge_history` 传 `scopes=[]`（`service.py:2311-2319`），Mem0 原始 messages 可保留，普通响应却仍 completed。Mem0 delete 在 not-found 重试耗尽后也直接返回成功（`mem0_library.py:427-445`），暂不可见与真不存在没有区分。
- **影响：** 当前证据较强地支持“业务不可见”，不无条件支持“所有物理副本已擦除”。多 Pod 时一次 erase 只清当前副本的 Mem0 history；Milvus 物理回收依赖 compaction。
- **最小修复：** 按资源列删除矩阵：PG 全部文本列、主/entity collection、Mem0 history/messages、缓存、摘要、审计正文、日志和备份；分别声明不可召回、逻辑删除、物理擦除和备份过期。无法完成或无法判定时返回 failed/completed_with_residual，并进入持久对账，不能假完成。单副本边界写入发布说明。

### 14. 发布与部署门禁存在实际断点

- `make test-e2e` 默认缺 Playwright 依赖；临时补装后 6 条仍有 1 条等待已删除的 “API Keys” 文案，属于 E2E 契约漂移。
- Web `npm audit --audit-level=high` 有 source-map-js、undici、vite 三处 high。Vite 多项主要影响开发服务器，不能直接等同线上可利用，但供应链门禁仍红。
- CI 没有 mutation/eval、web 测试/构建、e2e/a11y、npm audit 或夜间任务，与 README 的 CI 口径及 `pyproject.toml:103-106` 的夜间变异注释不一致（`.github/workflows/ci.yml:15-101`）。
- `make k8s-apply` 使用 `kubectl apply -k k8s`，但目录没有 kustomization；`kubectl kustomize k8s` 已复现失败。

发布命令、CI 和实际执行项需要一致。

### 15. 负载正确性检查不会决定退出码，当前没有 SLO 证据

- **证据：** k6 有任务 completed、非降级、事实命中检查（`tests/load/smoke.k6.js:95-114`），thresholds 却只有 HTTP 错误率和延迟（21-28），没有 `checks: rate==1`。
- **影响：** 写进去却召不回来时，负载脚本仍可能成功；本轮又没有真实负载运行结果。
- **最小修复：** 先把正确性 checks 纳入 threshold，再在真实栈跑 baseline/stress/spike/soak/fault/replay，归档 append/recall p95/p99、任务完成时延、吞吐、错误率和恢复时间。

## 3. Should fix：引擎优先与插件设计的偏差

### 16. 固定 P0 语义层仍过重，类型配置尚未贯通抽取

- 当前 `MemoryService` 约 5,400 行，`domain/slots/` 约 1,450 行；P0 canonical/source gate 仍参与 L3 索引。
- `mem0` 与 `profile` extractor 行为相同，profile JSON 没有每类抽取 prompt/schema。新类型主要改变准入、TTL 和冲突标签，不是完整类型插件。
- 建议保留 P0 为一个内置类型实现，逐步删除全局硬编码；每类使用 Mem0 `add(prompt=...)` 的领域 prompt、输出版本和 eval，不在 `MemoryService` 增加行业分支。

### 17. 默认抽取提示词自相矛盾

`THINKBACK_CUSTOM_INSTRUCTIONS` 同时要求“跟随对话语言”和“只能纯中文及中文标点”（`memory/backends/mem0_library.py:41-49`）。英文对话、英文品牌或人名无法同时满足。应统一为“跟随来源语言，实体逐字保留”，并用中英文及混合实体用例验证指定 27B 模型。

### 18. 接入生态仍是 HTTP/gRPC 示例，不是完整插件分发

`docs/SDK.md` 是 httpx/fetch 示例，不是带版本、重试和兼容策略的 SDK；当前无 MCP 实现，HTTP/gRPC 在 metadata、任务字段、操作类型上也不等价。先稳定一个 canonical API 版本并生成 Python/TS SDK，再按真实消费者增加 MCP tools/resources；webhook 从自己的持久 outbox 做，Mem0 OSS 没有可依赖的回调。

另一个接入契约选择是全局 `round_id`：数据库迁移指南已明确它全局唯一，跨用户复用后冲突是在执行现行契约，不是当前实现 bug。企业助手若只能提供会话内局部 ID，应先做结构化键迁移；若可以接受全局 UUID，则更小的改动是把约束写进公开 schema/SDK 并自动生成。不要在未选定契约前直接改数据库。

### 19. 单副本可以是发布边界，但不能暗示 HA

当前 `replicas: 1 + Recreate` 是明确的保守选择，本身不是 bug。多副本会涉及进程内幂等/限流、L1 缓存、启动恢复、Mem0 history 和删除屏障；现有“双服务实例”测试主要共享内存仓储，不能证明 Pod 行为。HA、零停机与多副本应在真实集群验收后单独开放。

## 4. 引擎优先的最小职责划分

| 交给 Mem0 | Thinkback 必须拥有 | 应收窄或删除 |
| --- | --- | --- |
| 事实抽取、按类型 prompt、精确去重 | L1 权威 SourceEvent、L2 有界会话视图 | 全局 P0 正则/canonical 作为第二套事实真值 |
| 语义候选、可用的 BM25/entity 加分 | namespace、角色信任、分类与审批 | 语义召回后的词典硬过滤 |
| 单条 CRUD、expiration 停止召回 | 当前事实冲突、supersede、valid/recorded time | 与 Mem0 分数冲突的自造 L3 排序分 |
| 原生 reranker 及 provider 接口 | 持久 operation/outbox、幂等、lease、恢复与对账 | 只有“召回隐藏”重复的 decay；物理保留清扫仍保留 |
| AsyncMemory 的进程内非阻塞调用 | 上下文分层预算、来源证据、不可信数据边界 | 为每个行业新增 Python 分支 |
| 基础 user/agent/run 过滤 | suppress/erase、审计、SDK/webhook/MCP 适配 | 未经验证的“配置即生效”旋钮 |

关于指定的 bge reranker：Mem0 2.2.1 有原生 reranker，但没有 Xinference provider。可以写一个小 provider 适配，这是补引擎缺口；不需要为此升级大版本或自造检索管线。Mem0 rerank 失败会回原排序，Thinkback 需要暴露时延和回退指标。

## 5. 推荐插件形态

**服务/sidecar + 版本化网络契约**，不是嵌入企业助手进程的 Python 库。原因很实际：跨语言接入、独立升级、资源隔离，以及当前 Mem0 适配仍有进程全局 patch。

最小外部模型只需五类操作：

```text
append(SourceEvent[]) -> WriteReceipt（分层状态）
recall(Query, options) -> ContextPackage（来源、角色、预算、降级）
get_operation(id) -> OperationStatus
suppress(scope) / erase(scope) -> OperationReceipt
get_source(ref) -> SourceEvent / provenance
```

配置只声明已注册能力：类型名、Mem0 prompt/版本、来源角色、冲突、TTL、分类、召回路径和预算。新行业优先新增 JSON profile 与 prompt/eval，不改编排器。procedure 需要独立审批和 instruction-path 权限，不能和普通偏好共用信任等级。

## 6. 生产签发的最小验收矩阵

1. **同一发布 SHA 的确定性门禁：** `make check`、fuzz、web build/test、e2e/a11y、Python/npm CVE 全绿；CI 与文档口径一致。
2. **隔离与协议：** 同 user/session、不同 namespace 的 L1/L2/L3 读写删全矩阵；公开 schema/SDK 明确全局 ID 约束，或在局部键迁移后验证跨用户 ID 行为；所有 OperationType 做 proto 往返；HTTP/gRPC append→recall→update/suppress/erase→GetTask；自定义类型和 JSON metadata 不丢失。
3. **真实镜像：** 启动后确认 profile 存在且加载正确；非法生产配置不静默回默认；K8s 渲染、迁移、rollout、NetworkPolicy 允许/拒绝均验证。
4. **真实 PostgreSQL：** 目标版本升降级；两个仓储实例并发 claim/CAS；在 journal 后、L3 写中、erase 各阶段 SIGKILL 并恢复，无重复事实、无假 completed、无卡死 RUNNING。
5. **真实 Mem0 + Milvus：** 验证 collection schema、BM25/entity 是否真的启用、filters 不静默失效；执行写入→跨会话召回→更新→suppress→erase→recall，并按删除矩阵扫描残留。
6. **真实模型质量：** 在发布 SHA 重跑 real eval 与 baseline，包含全部失败归因桶；先出无 rerank 基线，再证明 rerank 的指标收益大于时延和故障成本。
7. **负载与故障：** checks 纳入退出码，完成真实栈压力、长稳和故障注入；明确单副本恢复时间，若开放多副本再做跨 Pod 全套测试。

## 7. 本次实际验证记录

| 命令/检查 | 结果 |
| --- | --- |
| HEAD `60c9023` `make check`（两次） | format/ruff/mypy 绿；单测均 **887 passed / 1 failed**，后续被中断；独立复审另次 `make test` 为 888/888，证明该启动用例不稳定 |
| `make eval` | 当前工作树含未提交 `RecallIntent` 清理时，自包含 **20/20**、垂类自包含 **16/16**；纯 HEAD 脚本仍引用已删除枚举，不能宣称纯 SHA 可复现 |
| `make mutation-audit` | 334 个变异体；110 survived + 2 no-tests 与已验收基线一致，无新增 |
| `make test-fuzz` | 通过；但测试隔离结构仍需修正 |
| `make audit` | Python 无已知漏洞 |
| `make test-ui` / `npm run build` | 22/22；构建通过 |
| `make test-e2e` | 默认缺 Playwright；补装后 **5 passed / 1 failed** |
| Web `npm audit --audit-level=high` | 3 个 high 依赖项，需升级/分流 |
| 指定对话模型 smoke | OpenAI 兼容调用通过 |
| 指定 embedding smoke | 实测 1024 维，与配置一致 |
| 指定 bge reranker smoke | `/v1/rerank` 可用，简单中文样例排序合理 |
| namespace 读写删 | 已复现 alpha 召回 beta L1/L2；alpha 删除后 beta journal 墓碑化、beta L3 仍 ACTIVE |
| 分类策略降级 | 已复现 restricted 类型被请求 normal 降级并写入 Mem0 |
| 全局 round_id 契约 | 已复现跨用户复用后冲突；这是现行全局唯一契约在执行，接入时需公开约束或另做局部键迁移 |
| L2 删除时序 / 恢复策略 | 独立复审用受控 fake 复现删除复活、state→profile/TTL 丢失 |
| gRPC erase task 转换 | 已复现 `KeyError` |
| 自定义类型管理响应 | 已复现 `state/process_preference -> null` |
| `kubectl kustomize k8s` | 已复现缺 kustomization 错误 |

审计期间曾出现 122 个单测失败的 WIP 中间快照，随后相关修改已提交并收敛为 888 中 1 失败。本文最终评级使用 HEAD `60c9023`，不把那 122 个失败当成独立产品 bug。

## 8. 未证明的范围

- 未在本轮启动真实 Milvus/PG 集群重跑完整 erase、real eval、SIGKILL、多 Pod 或负载；指定模型只做了端点 smoke。
- 未证明当前 Milvus collection 已启用 BM25/entity，也未证明 rerank 对生产语料有净收益。
- 未验证备份、日志归档、Milvus compaction 后的物理字节擦除。
- 未做其他记忆项目的同条件性能排名；主流比较只用于职责与设计校准。

**最终判定：架构方向可保留，Mem0 固定合理；当前是“有较强测试资产、但插件契约和生产证据尚未闭环”的系统。**

最小整改顺序：

1. **先堵数据正确性：** namespace 全层隔离、L2 删除屏障、分类不可降级。
2. **再堵执行与删除：** 恢复保留策略并分页、暗数据归零、erase 不虚报完成。
3. **再闭合插件契约：** profile 入镜像、类型/metadata/事件模型、HTTP/gRPC 对齐，并明确全局 ID 或局部键契约。
4. **最后签质量与运维：** 真实 eval/threshold/rerank A/B、稳定 CI、真实故障与负载证据。

在前三类问题修完前，不建议接入新的企业助手；第四类全部有证据前，不建议签发生产就绪。
