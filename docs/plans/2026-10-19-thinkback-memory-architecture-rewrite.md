# Thinkback 记忆架构重构计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (- [ ]) syntax for tracking.
> **Task 0.1 必须最先完成** —— 它解除后续所有删除任务对覆盖率门禁的阻塞。

**Goal:** 删掉 1,270 行硬编码正则槽位引擎和围绕它的冲突补偿机制，把记忆层换成
「档案 Profile + 叙事 Collection + 状态 TTL + 过程记忆 + 承诺」六个通道，
并修掉一批上线即出事的缺陷（鉴权恒真、数据不可逆销毁、重试预算失效、鉴权旁路）。

**Architecture:** PG 是唯一真值源。档案事实走每用户一行的 Profile，由 LLM 原地更新整份文档，
因此冲突协调问题不存在；叙事记忆复用 ins_memory 加 kind/emotion/importance/expires_at，
只追加不取代；状态记忆走 TTL 自动过期；过程记忆改的是系统提示词而不是记忆行；
承诺独立召回。召回返回一段组装好的、含「不该提的」标记的上下文。删除是物理删除。
Milvus 降级为叙事事件的**可选**翻旧账通道，默认关闭。

**Tech Stack:** Python 3.12 · FastAPI · SQLAlchemy(async) + asyncpg · Pydantic v2 ·
mem0 + Milvus（降级）· pytest + mutmut + schemathesis

**Spec:** 本文件。审计证据见文末「审计结论索引」，每条缺陷都带 file:line。

**前提条件:** 服务从未上线，因此**没有存量数据迁移负担、没有 API 兼容负担**。
这是做这次重构的唯一窗口期。

---

## Global Constraints

- **批次顺序不可交换。** Phase 1（评测）必须在 Phase 2（双跑）之前，
  Phase 3 的删除必须在切流之后
- **删除代码和删除断言该行为的测试必须在同一个 commit。** 分两个 commit 会让
  中间态既跑不动旧测试也没有新行为
- **AGENTS.md 的「覆盖率只升不降」在本次重构中作废**（Task 0.1 已改）。
  断言 extract_pet_name('...communication...') == ('cat', 'direct')
  不是回归护栏，是束腰带
- **单进程单副本起步。** 不引入 Redis / Celery。理由：现在每一块进程内状态都是
  承重的，给一个从未跑过的设计补基础设施是在掩盖问题
- **不动**：治理台 SPA（web/）· gRPC 接口面（Phase 0 只加鉴权，不改 proto）·
  long_term_scope_id 这个恒为 "thinkback" 的装饰性维度
- **验证命令**: make check（Task 0.1 之后含 mutation-audit + eval）、make test、make eval
- **commit 约定**：完成后立即 git add <files> && git commit，message 描述改动而非文件清单

---

## Phase 0 — 地基（与记忆模型无关，每条可独立验证）

这一批与记忆模型完全无关，自己就是一个可交付版本。做完之后，后续每一步都踩在可信地基上。

### Task 0.1: 解除覆盖率门禁阻塞，改完成定义

**Files:** AGENTS.md · Makefile · pyproject.toml

把 make check 从 format-check lint typecheck coverage 改成
format-check lint typecheck mutation-audit eval，并把 COVERAGE_FLOOR 从阻断门禁降级为
非阻断观测指标（保留 --cov-report=xml 生成报告，去掉 --cov-fail-under）。
同步改 AGENTS.md 的「完成定义」段落，明确写一句「覆盖率不是完成标准」。

**理由：** 仓库里**已经有**两个正确的门禁 —— make mutation-audit（mutmut，配置在
pyproject [tool.mutmut]）和 make eval + check_baseline.py（一个写得相当好的基线守卫，
连「删用例让通过率回升」都防了）。它们只是没进完成定义。行覆盖率已被证伪：
_verify_secret 那个恒真函数在 95.9% 覆盖率下活着，因为那一行被执行了，只是结果错了。

- [ ] make check 已含 mutation-audit 与 eval
- [ ] COVERAGE_FLOOR 不再阻断
- [ ] AGENTS.md 完成定义已更新
- [ ] make check 全绿

### Task 0.2: 修 API Key 校验恒真

**Files:** src/thinkback/api/auth.py · tests/unit/test_v1_integration.py

auth.py:149 的 _verify_secret 两侧是同一个表达式，恒返回 True：

    expected = hashlib.sha256(f"{record.key_id}:{secret}".encode()).hexdigest()
    return hmac.compare_digest(expected, hashlib.sha256(f"{record.key_id}:{secret}").hexdigest())

根因不是笔误：ApiKey（auth.py:39）**没有 secret 字段**，明文只在生成时返回一次、
从不落库，所以校验在结构上不可能实现。给 ApiKey 加 secret_hash，
generate_api_key 存 sha256(key_id:secret)，校验改成
compare_digest(record.secret_hash, expected)。同时把 key_id 从 secrets.token_hex(4)
（4 字节，可枚举）加长到 >=128 bit，并停止在日志里打印 key_id。

**补测试**（现在 tests/ 对 _verify_secret 零引用，现有「无效 key」用例在 key_id 不存在
那一步就 401 了，**根本没走到校验**）：合法 key_id + 错误 secret 必须 401 ·
空 secret 必须 401 · 已吊销 key 必须 401 · 跨租户 header 必须 401。

- [ ] 错误 secret 返回 401
- [ ] key_id >= 128 bit
- [ ] 日志不含 key_id
- [ ] mutation-audit 能杀出这行的 mutant

### Task 0.3: 管理端点鉴权 + 生产 fail-fast

**Files:** src/thinkback/api/integration_admin.py · src/thinkback/api/admin.py ·
src/thinkback/infra/config.py

- integration_admin.py:84/93/116 三个端点**完全没有 Depends**，任何人都能列举全部
  key_id、签发任意 scope 的新 key（返回明文）、吊销他人 key。对比同仓 admin.py 和
  mem0_config_admin.py 每个端点都挂了 Depends(require_admin_token)，只有这个文件漏了
  → 全部补上
- admin.py:47 的 require_admin_token 在 token 为空时直接 return（fail-open），而
  admin_token 默认空串（config.py:149），.env.example 里连这一项都没有 → 改为生产环境
  启动时缺失即失败
- require_admin_token 用 authorization != f"Bearer {token}" 做非常量时间比较
  → 改 secrets.compare_digest
- admin.py:390 的 admin_health_detail() 没挂鉴权，会泄露 alembic 版本、连接池、
  gRPC 地址、功能开关 → 补上

- [ ] 未授权请求三个端点全部 401
- [ ] 生产环境缺 ADMIN_TOKEN 时启动失败
- [ ] admin_health_detail 需要鉴权

### Task 0.4: 主链路鉴权 + 租户派生

**Files:** src/thinkback/api/memory.py · src/thinkback/api/v1_memory.py ·
src/thinkback/rpc/servicer.py · src/thinkback/rpc/server.py

- memory.py:47 的 router **零认证**，user_id 完全由请求体决定，/memory/delete 支持
  scope=ALL → 补鉴权，或干脆不注册只保留 /v1/*
- v1_memory.py 的 dependencies=[Depends(require_auth), ...] 把返回的 AuthContext
  **直接丢弃**，端点拿到的 user_id 仍来自 body；tenant_id 从未进入存储层
  （grep -rn "tenant" src/thinkback/memory/ src/thinkback/domain/ 返回空）
  → user_id 必须服务端从 AuthContext 派生
- gRPC add_insecure_port（server.py:58）+ reflection 全开，无 interceptor，无 API Key 校验
  → 加 metadata 鉴权
- ratelimit.py:80 对无效 key 直接放行且**不计入限流**，未认证流量可全速轰炸
  → 无效凭证走固定桶；self.buckets 是 defaultdict 永不淘汰 → 加 LRU 上限

- [ ] 携带他人 user_id 的请求返回 403
- [ ] 无效凭证也走限流
- [ ] gRPC 无凭证被拒

### Task 0.5: 停掉 Milvus 回写（唯一的不可逆数据销毁）

**Files:** src/thinkback/memory/service.py:3684,3714,3755 · src/thinkback/domain/slots/canonical.py

实测：

    原文: User's birthday is March 3rd, and her sister Lily lives in Boston.
    落库: User lives in Boston                    ← 生日和 Lily 永久消失

    原文: User moved to Hangzhou for a new job at Alibaba in 2024, leaving her old team.
    落库: User lives in Hangzhou                  ← 雇主、年份、团队全丢

_index_l3_event 用 canonical_memory_text_from_source() 把 mem0 抽出的多事实文本截断成
一个槽位，然后 _BackendUpdate **把截断后的文本覆写回 Milvus**。丢失的事实在 PG 和
Milvus 两边都没了，rebuild 也救不回来。

先把 _BackendUpdate 的调用去掉（截断后的文本只作本地比较用，不回写后端）。
这一条**最优先**，因为它每天都在销毁数据。

- [ ] 后端不再被回写
- [ ] 多事实记忆在本地索引保留原文

### Task 0.6: 删 mem0_config 死链

**Files:** src/thinkback/memory/mem0_config.py（266 行）·
src/thinkback/api/mem0_config_admin.py（128 行）· ins_mem0_config 表 · app.py 的 router 注册

mem0_library.py:45 的 _build_custom_instructions() 不接任何参数，直接
return THINKBACK_CUSTOM_INSTRUCTIONS 常量。**治理台改的抽取提示词从不生效**，
整条链是死代码。全部删除并写 alembic migration 删表。

- [ ] ins_mem0_config 表已删除
- [ ] 5 个 admin 端点已下线
- [ ] 治理台对应的调用方已清理

### Task 0.7: 删死代码

**Files:** src/thinkback/memory/schemas.py · src/thinkback/memory/service.py ·
src/thinkback/domain/slots/canonical.py · docs/ARCHITECTURE.md

- RecallIntent.SENSITIVE —— 枚举只有两个值，SENSITIVE 在 service.py:1237 无条件
  raise ValueError，连本地安全的 L1/L2 都不返回。这是 fail-useless 不是 fail-closed。
  DataClassification.SENSITIVE/RESTRICTED 已定义却从未在召回路径上用于过滤。
  删掉整个 intent 轴；隐私诉求在 Phase 2 用「负面记忆标记」实现
- conflict_partition / conflict_partition_from_metadata（canonical.py:23,27）——
  都 return ()，被 4 层包装转发。删
- service.py:3973-4040 的 14 个 _extract_* pass-through —— 纯转发，零信息量。删
- docs/ARCHITECTURE.md 的 1.1 / 2.1 / 2.2 节记录的 memory/caches.py（BoundedTTLCache）
  **文件不存在**，是「V2.1 瘦身」删掉后没同步的文档。删文档段落
- service.py:4041-4136 的 _l3_metadata / _classify_data 等静态包装 ——
  保留 _redact_event（有安全价值），其余按实际调用点决定

- [ ] RecallIntent 已从 schema 和 service 移除
- [ ] conflict_partition 已删
- [ ] ARCHITECTURE.md 不再引用不存在的组件

### Task 0.8: write-gate 拆分

**Files:** src/thinkback/domain/safety.py · src/thinkback/memory/service.py:500-504 ·
src/thinkback/memory/schemas.py

当前逻辑：命中词表 → 整轮 mark_round_deleted（连 L1 原文一起销毁）→ 返回
status="completed"。实测误杀率极高（set api_key: / the tool: / System: / token: 都会命中），
对抗性注入只需加两个空格（ignore␣␣all␣␣previous␣instructions）就全部放行。
**两件事一件没做到，只制造静默数据丢失。**

拆成两件事：

1. 「不写入长期记忆」→ 返回 skipped + 明确 reason，**保留 L1 原文**
2. 「删除」→ 独立的、需显式授权的操作（Phase 3 才做物理删除）

同时删掉 role in {system, tool} 的判定 —— 把工具遥测和 prompt injection 混为一谈。
词表本身降级为「写入前的可选过滤开关」，默认关；真正的注入防护在 Phase 2 的抽取层做
（把记忆当数据不当指令）。

- [ ] 命中词表返回 skipped 而非 completed
- [ ] L1 原文不再被 write-gate 销毁
- [ ] system / tool 角色不再触发拦截

### Task 0.9: 重试预算终态化

**Files:** src/thinkback/memory/service.py:472（及 1427/1735/1948/2591 共 5 处同款）

    was_retrying_failed_task = previous_task is not None and previous_task.status is TaskStatus.FAILED
    ...
    task.status = TaskStatus.RUNNING   # 无条件覆盖

DEAD_LETTER != FAILED，所以 retry_count 不自增、预算检查不执行、状态被覆写回 RUNNING。
实测连打 19 次：retry_count 涨到 11，MAX_RETRIES = 5 只挡了 6 次，
每个失败重试都触发一次完整 mem0 + LLM 抽取。

5 处统一改为只认 FAILED → DEAD_LETTER 视作终态。补回归测试：
**连打 20 次必须稳定停在 dead_letter**。

- [ ] 5 处统一
- [ ] 20 次连打后 retry_count 不超过 MAX_RETRIES

### Task 0.10: CAS 耗尽不再谎报成功

**Files:** src/thinkback/memory/service.py:811-844

三次 CAS 全冲突时只 logger.warning + return None，而 append 的终态 COMPLETED 写入
走的正是这条路径（service.py:604），调用方**丢弃返回值**照常返回 completed。
task 行留在 RUNNING、result 为空，之后每次重试都命中 already_done，把不一致永久固化。

改为：耗尽时抛出可识别的异常（不是 RuntimeError 字符串协议），
让 append 返回 5xx 而不是假成功。

- [ ] CAS 耗尽时 append 不返回 completed

### Task 0.11: 召回计数与预算裁剪的顺序

**Files:** src/thinkback/memory/service.py:1340 · src/thinkback/domain/recall_policy.py:93

self._touch_recalled_entries(recalled_entries) 在
clipped_items = self._dedupe_and_clip(...) **之前**执行
→ 被预算裁掉、根本没返回给模型的记忆，一样 recall_count += 1。
这是曝光偏差闭环（召回 → 计数涨 → strength 涨 → 排名提前 → 更容易召回）的一部分。

把 touch 移到裁剪之后，只对真正返回的记忆计数。同时 recall_policy.py:93 的排序键
(-层优先级, -strength) 完全不含 score
→ score 至少必须是主项。

**注意：** Mem0 官方用 RRF 而非加权分数融合，理由是不同检索器的分数不归一化就不可比
——所以「排序不用绝对 score」本身不是反模式。真正的问题是排序发生在预算裁剪之前，
且主导项是一个由召回动作自己写回数据库的计数器。

- [ ] 被裁掉的记忆不计数
- [ ] score 进入排序键

### Task 0.12: token 估算与只读健康检查

**Files:** src/thinkback/domain/recall_policy.py:102 · src/thinkback/memory/backends/mem0_library.py

- cost = max(1, len(normalized) // 2) —— 注释写「中文一字约等于一个 token」，
  代码对中文**低估约 2 倍**、对英文**高估约 2.4 倍**。名为预算的约束两头都不守。
  接入真实 tokenizer，或按字符集分档
- cost 超预算时用的是 continue 不是 break，超支后继续往下塞
  → 改 break，或改成每层配额
- Mem0LibraryMemoryBackend.health_check 真的调 add 写一条探针记忆再 delete。
  k8s 探针默认 10s 一次 = 一次 LLM + 一次 embedding + 一次 Milvus 写 + 一次删。
  改用 list_collections 或一条预置只读查询

- [ ] 中文 token 估算误差 < 20%
- [ ] health_check 不写数据

---

## Phase 1 — 评测先行（一行生产代码都不动）

**目标：在旧系统上跑红。** 不红说明负向集没测到真问题。

### Task 1.1: 真人陪伴语料

**Files:** tests/eval/corpus/companionship/*.jsonl

真人写 50 段多轮陪伴对话，覆盖：情绪状态 · 事件叙事 · 关系动态 · 承诺与跟进 ·
偏好与雷区 · 生活阶段。**不覆盖当前任何一个槽位** ——
现有 8 题评测语料就是 8 个槽位本身（引擎为答这 8 题而造，评测为确认它答对），属于自证。

实测证据 —— 当前引擎对真实陪伴内容的识别率是 0：

    slot=None  <- 今天面试没通过，我有点难过
    slot=None  <- 我妈上周做了手术，需要住院复查
    slot=None  <- 我跟男朋友吵架了，他总是回避问题
    slot=None  <- 你答应我周五提醒我去交房租
    slot=None  <- 最近总是失眠，凌晨三点还醒着
    slot=None  <- 我讨厌别人敷衍我，宁可听难听的真话

连现有评测集自己的第 6 题「我对花生过敏」也返回 slot=None。

- [ ] 50 段语料完成
- [ ] 每段标注期望召回 / 期望不召回

### Task 1.2: 负向评测集

**Files:** tests/eval/negative/*.jsonl · tests/script/eval/

四条硬阈值，每条都要能在旧系统上跑红：

1. **不该召回的必须召不回** —— 包括「用户刚说妈妈住院，召回结果里不能出现宠物名」
   这类时序不相关
2. **该被压后的必须压后** —— 排序质量。现有 zh_replay_eval.py 自己写着
   「不衡量排序质量」
3. **注入量硬阈值** —— 现有评测统计了 total_injected_chars 但没设阈值
4. **同一事实在一次响应里不得出现多次** —— 现有 dedupe_and_clip 只按精确字符串去重，
   L1/L2/L3 三层几乎必然都留下

- [ ] 四类负向用例都有至少 5 条
- [ ] 在旧系统上跑红

### Task 1.3: 判分口径

**Files:** tests/script/eval/zh_replay_eval.py · tests/eval/baseline.json

现有判分是「期望实体是否出现在召回 L3 内容里」，保守且不衡量排序。
改为分级判分：正确性 / 排序 / 注入量 / 假阳性率四维。
同时把 8 题自证评测降级为 regression fixture，移出主门禁。

- [ ] 四维判分
- [ ] baseline.json 更新并纳入 check_baseline.py 守护

---

## Phase 2 — 双跑（两套并存，数字可比）

代价：这期间 LLM 调用翻倍。

### Task 2.1: user_profile 表

**Files:** src/thinkback/infra/database/models.py · alembic/versions/* ·
src/thinkback/domain/entities.py

每用户一行 + Pydantic schema。字段按陪伴场景设计（称呼、居住地、生日、过敏、宠物、
工作状态、沟通雷区），**schema 化之后冲突协调问题不存在** ——
LLM 看着当前档案和新对话，输出一份新档案。

**依据：** LangMem Profile 形态 —— "When new information arrives, it updates the existing
document rather than creating a new one. This approach is ideal when you only care about
the latest state and want to avoid remembering extraneous information... easy to present
to a user for manual editing."

- [ ] 表与 Pydantic schema 就位
- [ ] 单测：同一输入产出稳定档案

### Task 2.2: ins_memory 加列

**Files:** src/thinkback/infra/database/models.py · alembic/versions/*

加 kind(event|state|commitment) · emotion · importance · expires_at · due_at · status。
**不新建表** —— 删掉 8 槽位不是删表，是把 ins_memory 从「槽位索引」改造成
「叙事记忆存储」，它已经有 user_id / memory_scope_id / source_refs / memory_text / metadata。

- [ ] 加列 + 索引
- [ ] kind 有约束

### Task 2.3: Profile 抽取器

**Files:** src/thinkback/domain/profile.py（新）· src/thinkback/infra/llm.py

LLM 抽取，不再用正则。固定 prompt 版本 + 强 schema 输出。

**替换掉的东西：** _backfill_p0_slots_from_round_source
（同一段文本被抽两次然后调和分歧 —— 这是 1,270 行正则存在的唯一理由）、
_supersede_conflicting_memories、_is_older_than_active_conflicting_memory、
_memory_supported_by_source 的关键词否定。

- [ ] 抽取确定性可复现（固定 prompt + 录制 fixture）
- [ ] 断言 schema 和不变量，不断言具体文本

### Task 2.4: 影子写入

**Files:** src/thinkback/memory/service.py

append 同时写旧路径和新路径。对同一批评测跑两套模型，数字可比。

- [ ] 同一 round_id 在两条路径都有记录
- [ ] 可对比报告

### Task 2.5: 新召回 API

**Files:** src/thinkback/api/v1_memory.py · src/thinkback/memory/service.py（新 build_context）

POST /v1/memory/context，返回**一段组装好的、按可注入性排序的上下文**，
字段：档案 · 未过期状态 · 最近 N 条事件 · 待办 · **负面标记（不该主动提起的）** · 实际注入量。
旧的 /memory/* 一行不动。

**「不该提的」是这个场景的核心能力，OSS 无先例。** 我查过 Mem0 / LangMem / Zep / Letta，
没有任何一个有 selective disclosure 这一层 —— 隐私就是删除/范围过滤。必须自己设计。
做法：召回时返回 proactive=false 的条目 + 触发原因（用户刚说过相关创伤 / 状态已恢复 /
用户要求别提），由调用方决定用不用。把「提不提」的判断权交给最了解上下文的那个 agent，
比在记忆服务里用正则猜「这个话题现在安不安全」靠谱。

同时：RecallIntent 已删（Task 0.7），现在没有「隐私模式」的表达位置，这个 API 就是它。

- [ ] 返回负面标记 + 原因
- [ ] 注入量真实计量
- [ ] 调用方能只取 proactive=true 的子集

---

## Phase 3 — 切流 + 一次性删除

新评测在 Task 2.5 上达标后切流，然后**在同一个 commit 里**删掉下列内容。

### Task 3.1: 切流

**Files:** 调用方配置 · web/

- [ ] 新 API 承载全部流量
- [ ] 旧 /memory/* 保持可用但零流量

### Task 3.2: 删 domain/slots/ 整层

**Files:** src/thinkback/domain/slots/（5 文件 1,270 行）· src/thinkback/memory/service.py 中约 550 行引用

删 _backfill_p0_slots_from_round_source · _supersede_conflicting_memories ·
_is_older_than_active_conflicting_memory · _mark_superseded_slot_sources_deleted ·
_same_source_local_p0_memory · _backfill_matching_slot_memories ·
_should_skip_backend_search（含它的 items 参数）· _context_terms · 14 个 pass-through。

**同时删除**断言这些行为的测试。注意 test_slot_extraction_real.py 只有 2 个测试函数
10 条参数化用例，全部是正向路径、全部来自作者已知的失败 fixture、**零条负向测试**
—— 它们保护的是错误行为。

- [ ] domain/slots/ 目录消失
- [ ] service.py 中无 slots. 引用

### Task 3.3: 物理删除，删掉墓碑/cutoff

**Files:** src/thinkback/memory/service.py · src/thinkback/memory/repositories/sqlalchemy.py

当前是墓碑 + cutoff（数据不删，只在读取时过滤），13 个函数约 108 行。
这不是遗忘权，只是自我安慰 —— 用户说「忘掉我告诉你的」，数据还在就等于没忘。

改为物理删除：journal 原文行、Profile、ins_memory 行、（若向量库开着）Milvus 行，
三处都删，留一份不含原文的删除审计（时间、条数、范围）。

删：_delete_tombstone_cutoff · _delete_all_cutoff · _session_delete_cutoff ·
_RebuildDeleteCutoffs · _add_delete_all_tombstone · _add_session_delete_tombstone ·
_add_deleted_source_ref_tombstone(s) · excluded_source_refs · _source_refs_excluded ·
_round_is_after_* 三个。

**依据：** 所有 OSS 在「删除」上做的都是 status flag + filter。陪伴场景不同 ——
用户一旦知道数据还在谈信任就无从谈起，所以这里必须做物理删除并明确告诉用户数据已删。
这是**故意偏离主流**的地方。

- [ ] 删除后三处存储都查不到
- [ ] 审计记录不含原文

### Task 3.4: 删 rebuild，缩成 reconcile

**Files:** src/thinkback/memory/service.py:1879-2180（302 行）

rebuild() 四个职责在新模型下逐个失效：

- 外部历史源一致性校验 —— api/dependencies.py:157-171 构造 MemoryService 时**根本没传
  history_source**，恒为 None，**从来没跑过**
- 清理来源已删的 L3 —— 删除改物理后没有「来源已删」这个状态
- 覆盖不足重放 L3 —— Profile 每次 append 原地更新、叙事记忆带 source_refs，
  不存在「覆盖不足」
- 重建 L2 —— L2 已删

只保留「来源对账」，降级成幂等的 reconcile CLI 命令。如果你们自己有对话存储且允许用户
删消息，就保留它；否则连它一起删。

- [ ] rebuild 从在线 API 移除
- [ ] reconcile 可重复执行且幂等

### Task 3.5: 删 L2，换 sleep-time consolidation

**Files:** src/thinkback/memory/l2_refresh.py · src/thinkback/domain/summarization.py（336 行）

现状两种模式都站不住：concat 是「最近 10 轮 user 消息用分号拼接」（不是摘要，是原文副本）；
llm 是把该 session **全部**轮次喂给模型、每 5 次 append 刷一次（O(N^2) 写路径，
_upsert_default_summary 无 LIMIT 拉全部行）。而且它的 4 段输出和 Profile + state 高度重叠。

换成 sleep-time consolidation：触发条件改成「该用户对话闲置 N 分钟」，
产出**只改写 Profile 和 procedural 指令，不产出摘要文本**。

**依据：** LangMem subconscious formation —— "after it has been inactive for some period"。
触发条件是闲置时长，不是累计次数。

顺带删掉 _upsert_default_summary 这条 O(N^2) 热路径，以及 L2BackgroundRefresher 的三个
无界容器（_append_counters / _llm_refreshed_scopes / _pending_scopes
全部按 (user_id, scope) 累积、永不回收）。

- [ ] L1 只保留「最近几轮原文直接读 journal」，且过滤掉 assistant 消息
- [ ] sleep-time 触发条件是闲置时长

### Task 3.6: 删 decay，换 state TTL

**Files:** src/thinkback/memory/decay.py（155 行）

状态记忆用 expires_at TTL，不够就后台清扫。两处不能靠原实现：
maybe_submit 的闸门是**进程级全局**（每小时只有第一个 append 的用户被扫，多租户下
其余永远不进队列）；if last_recalled is None: return True 把「从未被召回」当作
「久未召回」，配合 Task 0.11 修好前的曝光偏差，系统会系统性删除用户知识的尾部。

- [ ] 状态按 TTL 过期
- [ ] 清扫按用户维度，不是进程级全局闸门

### Task 3.7: 过程记忆独立成 procedural

**Files:** src/thinkback/domain/procedural.py（新）· prompt 配置

communication_preference 现在被当成一条会被 supersede 的语义事实存进记忆表，这是分类错误
—— 它在 LangMem 的分类里是 **Procedural Memory**（how an agent should behave and respond）。

做法：从真实对话提取失败模式 → LLM 改写沟通指令 → stage gate 校验 → 只在分数提升时发布。
改的是提示词，不是记忆行。

**依据：** LangMem Procedural Memory —— "starts with system prompts that define core behavior,
then evolves through feedback and experience... it refines these instructions, learning which
approaches work best for different situations."

- [ ] 沟通偏好从 ins_memory 移到 procedural
- [ ] 有 stage gate，失败不发布

### Task 3.8: 承诺与待办

**Files:** src/thinkback/memory/schemas.py · src/thinkback/api/v1_memory.py

AppendMemoryRequest 现在要求**恰好 2 条消息**（user 然后 assistant），
而 safety.py 曾把 role 为 system/tool 直接判为不安全整轮丢弃。结果是：
工具调用轨迹记不下来，被中断的回合（有 user 无 assistant）写不进去，
「你答应我周五提醒我交房租」这种跨天承诺没有任何地方能存。

Task 0.8 已经删掉了 role 判定。现在放开 append 的消息序列约束（任意 role，含 tool /
中断回合），并新增 commitment 通道：due_at + status，走**独立召回通道**，
不混进事实记忆的预算池。

陪伴产品里「你答应过我的事」是核心信任机制，这条能力现在是零。

- [ ] 可写入中断回合与 tool 轨迹
- [ ] 承诺有 due 和完成状态
- [ ] 到期提醒走独立通道

---

## Phase 4 — 收尾

### Task 4.1: Milvus 默认关

**Files:** src/thinkback/memory/backends/mem0_library.py · src/thinkback/infra/config.py

降级为叙事事件的可选翻旧账通道，默认关闭。陪伴场景 90% 的 recall 由 Profile + state 决定，
向量检索只在「我之前提过我妈手术是什么时候」这类翻旧账时才必需。关掉之后 Task 3.3 的
「三处都删」变成「一处都删」，延迟降一个 embedding 调用，还能纯 PG 跑通整套评测。

**依据：** Zep memoryMode=OFF 是官方开关，保留图结构、完全关掉语义检索层。

顺带删掉 mem0_library.py:805 的 _mem0_inline_threadpool —— 它是**进程级全局、
非 thread-local** 地改写 mem0_main.concurrent，一个线程进入 patch 期间所有并发调用
都被强制内联；mem0 升级改了这个符号名就静默失效。

- [ ] 向量后端默认 none
- [ ] 纯 PG 可跑通全部评测

### Task 4.2: 单副本

**Files:** k8s/deployment-api.yaml · AGENTS.md · docs/ARCHITECTURE.md

k8s/deployment-api.yaml:9 是 replicas: 2，而 ARCHITECTURE.md 写
「单进程承担 HTTP + gRPC + L3 后台抽取」，文档里没有任何关于副本的说明。
这个矛盾有实打实的代价：_API_KEYS · RateLimitMiddleware.buckets · IdempotencyCache ·
L1CacheMixin.l1_cache · _index_mutation_lock 全是进程内状态，2 副本下幂等在 3 个请求里
有 2 个会重复执行，召回结果取决于请求落到哪个 pod。

改成 1 副本并同步文档。等它跑起来有了真实并发数据再决定要不要 Redis。

- [ ] replicas: 1
- [ ] ARCHITECTURE.md 与实际一致

### Task 4.3: 剩余工程债

- _index_mutation_lock 是跨用户单锁，且在锁内做 DB 全表扫描
  （_backfill_p0_slots 删除后大幅缓解，但仍需按 (user_id, memory_scope_id) 分片，
  锁内零 DB 查询）
- l1_cache 键无界（_l1_cache.py:32 普通 dict，只截断值到 10 条，从不淘汰键，无 TTL）
  → 加 LRU 上界
- _active_memories 每次 recall 无 LIMIT 全量扫
  → 改 WHERE backend_memory_id IN (:search_results)
- rpc/converters.py:148 的 l3_score_threshold 直传，proto3 未设置是 0.0 而 pydantic 默认 0.5
  → HTTP 与 gRPC 语义不一致
- rpc/converters.py:147,149 的 x or default 会吃掉合法的 0
- rpc/server.py:58 add_insecure_port 返回值未检查 → 绑定失败被静默吞掉，日志还打 "bound"
- api/memory.py:57-66 模块级 asyncio.BoundedSemaphore + 两个从不 shutdown 的
  ThreadPoolExecutor
- errors.py 与 rpc/servicer.py 各写了一份 str(exc) 子串匹配错误分类且不一致 → 改类型化异常
- proto/memory.proto 里 repeated string l3_events / TaskResponse.scope 是 JSON 字符串
  → 定义真正的 message
- readiness.py 绑死 Milvus，但 service 设计明确是「L3 挂了降级为只返 L1/L2」→ 自相矛盾，摘掉
- service.py 里 4 处 from thinkback.infra.config import settings 在函数体内 → 分层破口，改注入

---

## 删除清单（总计约 3,160 行 / src 的 19%）

| 内容 | 行数 | 批次 |
| --- | --- | --- |
| domain/slots/ 整层（5 文件） | 1,270 | 3.2 |
| service.py 槽位引用 | ~550 | 3.2 |
| rebuild() + 9 个 helper | 325 | 3.4 |
| mem0_config 死链 | 394 | 0.6 |
| L2 层 | 336 | 3.5 |
| decay.py | 155 | 3.6 |
| 墓碑 / cutoff（13 个函数） | 108 | 3.3 |
| safety.py | 47 | 0.8 |

src 16,422 → ~13,200；memory/service.py 4,397 → ~2,800。

---

## 门禁（Task 0.1 之后）

    make check  ==  format-check + lint + typecheck + mutation-audit + eval

| 门禁 | 状态 | 作用 |
| --- | --- | --- |
| mutation-audit | **已有**，只是不在完成定义里 | 恒真函数、恒返回 () 的占位符、没被读的配置，全都会被 mutmut 杀出来 |
| eval + check_baseline.py | **已有**，连「删用例让通过率回升」都防了 | 挂入 Phase 1 的负向集 |
| 负向断言测试 | 新建 | 「必须拒绝的输入必须被拒绝」 |
| format / ruff / mypy --strict | 保留 | 真信号 |
| line coverage | **降级为非阻断观测指标** | 已证伪：恒真函数在 95.9% 覆盖率下活着 |

LLM 抽取的测试改为**断言 schema 和不变量**，不断言具体文本
（固定 prompt 版本 + 录制 fixture + Pydantic 校验）。

---

## 明确不做

- **多副本与 Redis** —— 等真实并发数据
- **改治理台 SPA**（web/）—— 记忆质量没跑通之前是过早投资
- **gRPC 接口面改造** —— Phase 0 只加鉴权，不动 proto
- **long_term_scope_id** —— 恒为 "thinkback" 的装饰性维度，隔离靠 user_id。
  要么把它做成真的 scope，要么删掉这个字段
- **引入新的依赖框架**

---

## 参考

| 主题 | 出处 |
| --- | --- |
| Semantic(Collection vs Profile) / Episodic / Procedural 分类 | https://github.com/langchain-ai/langmem/blob/main/docs/docs/concepts/conceptual_guide.md |
| Profile 原地更新 + 可供用户手动编辑 | 同上 |
| Conscious（热路径）vs Subconscious（对话闲置后后台反思） | 同上 |
| importance 由后台 LLM 批判标注，是内容属性 | https://github.com/langchain-ai/langmem/blob/main/docs/docs/background/extraction.md |
| 冲突协调 = reviewer LLM 输出 ADD/UPDATE/DELETE/NOOP | https://docs.mem0.ai/core-concepts/memory-update |
| re-ranking non-negotiable；down-ranking 与 up-ranking 同等重要 | 同上 |
| RRF 用 rank 不用 score 的理由（分数不归一化不可比） | 同上 |
| memoryMode=OFF 只保留图结构、关掉语义检索层 | https://docs.getzep.com/graphiti/working-with-data/hybrid-search-and-retrieval |
| bi-temporal 只作用于图边，不作用于节点属性 | https://github.com/getzep/graphiti |
| 只删被矛盾的边，保留用户原话节点 | 同上 |

**三条确认的 OSS 空白**（必须自己设计，不要指望抄）：

1. **没有任何 OSS 把召回频率放进排序权重。** Thinkback 当前的
   strength = (1+log1p(recall_count)) * 0.5^(hours/72) 是曝光偏差闭环。
   LangMem 文档确实提到 strength 是「recently/frequently used」的函数，
   但它是与 similarity、importance **并列组合**，不是主导项
2. **没有任何 OSS 有「不该主动提起」的概念。** 隐私就是删除/范围过滤，
   没有 selective disclosure 这一层
3. **没有任何 OSS 区分会过期的 state 和永久的 event。** Letta 的 sleep-time 是后台整合，
   mem0 可选 TTL，都不做语义判断。这一刀只能自己设计，且只能用 TTL
   （LLM 判断不可复现、不可测试、不可审计）

---

## 审计结论索引

| 结论 | 证据 |
| --- | --- |
| API Key 校验恒真 | api/auth.py:149 实测：任意伪造 secret → True；ApiKey 无 secret 字段 |
| key 管理端点零鉴权 | api/integration_admin.py:84/93/116 三个端点 require_* 出现 0 次 |
| 管理面默认敞开 | api/admin.py:47 空 token 放行；config.py:149 默认空串；.env.example 无此项 |
| 租户隔离未入存储层 | grep -rn "tenant" src/thinkback/memory/ src/thinkback/domain/ 返回空 |
| 主链路无鉴权 | api/memory.py:47 router 零 require_*；rpc/server.py:58 零认证 |
| 不可逆数据销毁 | service.py:3684/3714/3755 回写截断文本到 Milvus；实测生日/Lily/雇主永久丢失 |
| mem0_config 死链 | backends/mem0_library.py:45 返回硬编码常量，DB 配置从不读取 |
| DEAD_LETTER 复活 | service.py:472 等 5 处；实测连打 19 次 retry_count 涨到 11 |
| CAS 静默成功 | service.py:841 return None，调用方丢弃返回值照常返回 completed |
| 槽位覆盖率 | 8 个槽位 vs 真实陪伴内容 7/7 返回 None；连自有评测集的「花生过敏」也是 None |
| 词法门禁杀跨语言 | 实测 How is my project going? vs User current work status: preparing for promotion → allowed=False |
| 槽位误判 | extract_pet_name('我养了狗，我的朋友叫我老大') → ('dog', '我老大')，规范化后变成 User prefers to be called 老大 |
| 覆盖率门禁无效 | test_slot_extraction_real.py 只有 2 个函数 10 条用例，全正向，零负向；query_conflict_slot 在 tests/ 零引用 |
| history_source 从未接线 | api/dependencies.py:157-171 构造 MemoryService 时没传该参数 |
| 单副本 vs 2 副本矛盾 | k8s/deployment-api.yaml:9 replicas: 2 vs ARCHITECTURE.md「单进程」 |

**一条已推翻的结论**（留档防止以后被重新提出）：
「decay 在生产写路径上是死代码，因为 valid_at 永远是 NULL」—— **不成立**。
sqlalchemy.py:639 有 effective_valid_at = valid_at or datetime.now(UTC)，写入时会被填上。
连带「slot 回填的 max() 返回最旧」也不成立。decay 的真实问题在 Task 3.6。
