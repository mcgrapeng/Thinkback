# Thinkback 生产就绪报告（2026-10-28）

> 范围：按 `docs/plans/2026-10-28-thinkback-production-hardening.md` 的 14 项任务逐项整改，
> 并在真实栈（PostgreSQL + Milvus + zhiman38_27b + zhiman-embedding + bge-reranker-v2-m3）
> 上取证。引擎固定 Mem0 2.2.1，三层 L1/L2/L3 架构未改写。

## 一、结论

**代码侧达到生产可发布状态**：14 项任务全部完成，`make check` 全绿（format-check + lint +
typecheck + 963 条单测 + 变异审计 + 自包含评测），真实栈四类证据全绿。

**但生产上线前有两个必须由部署方确认的外部前提**（见第五节「上线前必须确认」）：

1. 单实例部署（`k8s/deployment-api.yaml` 固定 `replicas: 1` + `Recreate`）。进程内状态
   （幂等缓存、限流桶、L1 缓存、Mem0 history.db）不跨副本共享，扩副本前必须先把这些状态外移。
2. 模型端点容量。实测本地 27B 端点在 4 路并发下单轮 L3 抽取耗时 76s —— 这是端点容量事实，
   不是服务缺陷；服务侧已通过读写并发预算分离保证召回不被写入积压饿死（见 Task 12/性能段）。

## 二、14 项任务与证据

| # | 任务 | 提交 | 关键证据 |
|---|---|---|---|
| 1 | namespace 全层隔离（L1/L2/删除/擦除） | `bcca65d` | `ins_current_summary` 加 namespace 列；唯一约束改 `(user_id, namespace, memory_scope_id)`；迁移 `20261028_0016_summary_namespace.py` 在真实 PG 上 upgrade/downgrade/upgrade 通过 |
| 2 | L2 刷新删除竞态（TOCTOU） | `eccfc08` | 先取 `row_version` 再读轮次，写入前复核排除集 |
| 3 | 数据分类不可被请求降级 | `a56f65d` | `domain/classification.py::strictest_classification` |
| 4 | 崩溃恢复保留策略 + 分页 | `cef89d4` | `_collect_pending_write_tasks` 先收集后处理；`_replay_metadata` 还原类型/TTL |
| 5 | 暗数据归零（抽取前后同一准入） | `060c531` | 准入不通过且无本地索引时删后端记录 |
| 6 | 擦除语义诚实化 | `2da58bc` | `purge_history(user_id, memory_ids, namespace, message_texts, purge_all_messages)`；新增 `_residual_backend_ids` / `_erased_message_texts` |
| 7 | 契约诚实化（PERSONAL / 抽取语言） | `a314900` | 提示词改为「跟随对话语言 + 实体逐字保留」；PERSONAL 明确「不自动脱敏，由调用方负责」 |
| 8 | gRPC 生命周期契约 | `2c2595d` | proto 新增 `OPERATION_TYPE_ERASE`、`Suppress`/`Erase` 两个 RPC；此前擦除任务查询直接 `KeyError` |
| 9 | 自定义类型与自定义属性在协议层保留 | `2c2595d` | `memory_type_name` 贯穿 HTTP/gRPC；`custom_metadata` 随事件落库并进后端 |
| 10 | profile 进镜像 + 生产 fail-visible | `876ff35` | `Dockerfile` 复制 `profiles/`；生产环境缺 profile 或 profile 损坏直接报错，不再静默换策略 |
| 11 | 单测隔离 | `b5eeff3` | `create_app(run_startup_tasks=False)`；实测 `DATABASE_URL` 指向不可达端口时 963 条单测全绿（此前必红） |
| 12 | 前端/E2E/供应链门禁 | `d804d84` + `b22a2f8` | `npm audit` 0 漏洞；6 条 e2e（含 axe）通过；CI 新增 mutation-and-eval / web-gates / nightly |
| 13 | 部署入口 + 负载门禁 | `a914491` | 新增 `k8s/kustomization.yaml`（`kubectl kustomize k8s` 实测渲染成功）；k6 `checks: ["rate==1.0"]` 让正确性决定退出码 |
| 14 | 真实栈证据 | `6b1cae0` / `b22a2f8` / `5687065` | 见第三节 |

Task 14 期间真实栈又暴露了 4 个**计划外但真实的缺陷**，已全部修复（`6b1cae0` / `b22a2f8` /
`5687065`），逐条在第三节给出复现与证据。

## 三、真实栈四类证据

栈：PostgreSQL（docker `thinkback-postgres`）+ Milvus（docker `thinkback-milvus`，宿主端口
19540）+ `zhiman38_27b`（对话）+ `zhiman-embedding`（1024 维）+ `bge-reranker-v2-m3`（重排）。

### 3.1 擦除（物理不可恢复）

```
uv run --group dev python tests/script/real_mem0_erase_verification.py
→ 16 项断言全 PASS
```

覆盖：PG 索引行 / 轮次原文 / 摘要正文、Milvus 向量行与目标 ID、Mem0 事实抽取历史、
Mem0 原始消息、审计记录存在性与「审计不含原文」。

> 修复前真实栈擦除**必然失败**：`get_all(limit=10000)` 在 mem0 里按 `top_k * 4` 取数，
> 超过 Milvus 单次 query 的 16384 窗口 → 报 `invalid max query result window` → 擦除复核把
> 「查不到」当成「还残留」→ 每个目标都算残留。单页上限统一到 `domain/ports.py`
> 的 `MEMORY_LIST_PAGE_LIMIT = 4096`（服务层与后端实现共用一个数），复核遇满页判「无法证明
> 已删」→ 任务失败重试，不谎报完成。

### 3.2 故障注入（降级可见 + 恢复）

```
uv run --group dev python tests/script/real_mem0_p0_fault_injection.py
→ passed: true，failed_scenarios: []
```

三个场景全部「注入故障 → 就绪探针报不可用 → 恢复后探针 200」：
`mem0_library_model_unavailable`、`milvus_unavailable`、`postgres_unavailable`。

> 顺带修掉两个脚本自身缺陷：脚本用 `.env` 覆盖调用方环境（本地 `.env` 的 Milvus 端口过期时
> 「配好了却连不上」且无提示）；PG 故障注入只改 `POSTGRES_HOST/PORT`，而 `DATABASE_URL`
> 存在时它才是生效开关 —— 注入等于没注入，readiness 仍报 database ready。

### 3.3 真实链路质量

```
THINKBACK_MEMORY_API_URL=http://127.0.0.1:7002 \
  uv run --group dev python tests/script/real_mem0_quality_regression.py
→ passed: true
  recall_at_10 = 1.0   precision_at_10 = 1.0   top1_hit_rate = 1.0   mrr = 1.0
  12 个用例全过（11 正例 + 1 负例），0 次非瞬时失败、0 次重试
```

用例覆盖：昵称纠正、宠物名纠正、住址纠正、工作状态更新、沟通偏好更新、生日纠正、
饮品/食物偏好纠正、睡眠提醒、跨用户隔离（负例）。

> 这条评测在修复前连续三轮失败，暴露四个互相叠加的真实缺陷（全部已修）：
>
> 1. **中文昵称第三人称措辞不认**：模型抽出「用户喜欢别人叫他阿鹏」，词表只认「叫我/称呼」
>    → 纠正后旧昵称永远不被 supersede，继续 ACTIVE、继续被召回。
> 2. **P0 补齐的记忆拿不到来源时间戳**：冲突仲裁的来源游标按「时间戳来源」分档
>    （调用方给的 4 档 / 库里读出的 3 档 / round_id 数字 1 档），档位优先于时间 → 源规范化
>    出的新偏好被判「更旧」直接跳过（`indexed_count=0`），用户最新的沟通偏好进不了索引。
> 3. **提问侧槽位识别只认第一人称**：治理台口径的「用户希望你怎么给建议？」落不到槽位
>    → 只在本地索引、不在向量库的 P0 记忆永远召不回来。
> 4. **中文沟通偏好 / 工作状态措辞不认**：同一条事实被 mem0 抽取与源规范化各存一份
>    （`duplicate_active_rate` 0.15）。

### 3.4 负载（真实栈 + 4 并发）

```
BASE_URL=http://127.0.0.1:7002 k6 run tests/load/smoke.k6.js
→ 168/168 checks 通过
  append p95 = 178ms（阈值 1000ms）   recall p95 = 198ms（阈值 500ms）
  http_req_failed = 0.00%
```

六条正确性断言全过：`append 2xx`、`append task completed`、`recall 2xx`、`recall 未降级`、
`recall 命中本轮事实`、`recall 命中 L3 长期记忆`。

> 修复前压测本身是失真的，三处都改在服务侧或脚本的诚实口径上：
>
> - **读写共用一把后端并发预算** → 写积压饿死读：4 路并发写入时 mem0 `add` 耗时 76s，
>   排在后面的 `search` 等了 69s，召回直接 503。召回在助手关键路径上，不能被后台写入
>   队列拖死 → 读、写两把独立预算（读只要 embedding + 向量检索）。
> - **mem0 客户端懒构造** → 首个请求等 3-4 秒（实测首个 append 4.5s，其中 3.7s 花在
>   `Memory.from_config`）→ 启动期预热，预热失败不挡启动。
> - **单 IP 限流硬编码** → 压测单 IP 打满 600/分钟被 429 挡成「服务故障」→ 限流改成
>   配置驱动（生产默认不变），压测显式调高：压测测服务容量，不是限流器。
>
> 压测脚本自身两处名不副实也一并纠正：round_id 跨轮复用会命中幂等重试路径（测的是
> 「重试对账」）；所有迭代共用一个 user 时，后端把后面几轮判定成「已知」而跳过抽取
> （`event_count=0`，L3 压根没记忆），而「召回到」被 L2 拼接摘要兜底 —— 校验名存实亡。
> 现改为每轮独立 user + 头部唯一标记，并单独盯 L3 层断言。

## 四、本地门禁

| 门禁 | 命令 | 结果 |
|---|---|---|
| 格式 / lint / 类型 | `make format-check lint typecheck` | 通过（ruff + mypy strict，63 个源文件 0 错） |
| 单测（无外部依赖） | `make test` | 963 passed（`DATABASE_URL` 指向不可达端口时同样全绿） |
| 变异审计 | `make mutation-audit` | 无新增存活变异体 |
| 自包含评测 + 基线 | `make eval` | 通过率 100%，基线达标 |
| 前端单测 / 构建 | `npm run test-ui` / `npm run build` | 22 passed / 构建通过 |
| 端到端（含 axe a11y） | `make test-e2e` | 6 passed |
| 供应链 | `npm audit --audit-level=high` | 0 vulnerabilities |
| 契约模糊测试 | `make test-fuzz` | 通过 |
| 部署清单 | `make k8s-render` | 渲染成功 |
| 镜像 | `docker build` | 构建成功，镜像内 `/app/profiles/semiconductor.json` 可解析 |

CI 已对齐（`.github/workflows/ci.yml`）：`lint-and-typecheck`、`unit-tests`、
`supply-chain-audit`、`api-fuzz`、`docker-build-verify`、`mutation-and-eval`、
`web-gates`（npm audit + 前端用例 + 构建 + e2e）、`nightly-full`（`make check` + fuzz），
每日 03:00 定时。此前 README/pyproject 声称 CI 跑 `make check`，实际并没有。

## 五、上线前必须确认（外部前提，非代码问题）

1. **单实例**。`k8s/deployment-api.yaml` 固定 `replicas: 1` + `Recreate`，注释写明原因：
   幂等缓存、限流桶、L1 缓存、Mem0 history.db 都在进程内。要扩副本必须先把这些状态外移到
   Redis / PostgreSQL，再改滚动策略。本次真实栈证据全部来自单实例。
2. **模型端点容量**。本地 27B 端点 4 路并发下单轮抽取 76s；服务已保证召回不被饿死
   （读写预算分离），但 L3 落盘时延由端点决定。若生产端点并发能力不同，需重跑
   `tests/load/smoke.k6.js` 校准阈值。
3. **本地 `.env` 的 Milvus 端口过期**（`MILVUS_URL=http://localhost:19530`，实际容器在
   19540）。该文件是本地配置、不入库，但会影响直接跑真实栈脚本的人。
4. **本机 SOCKS 代理**。本机 `ALL_PROXY/http_proxy` 指向 SOCKS 代理，httpx 缺 `socksio`
   时构造即失败（mem0 调用报 ImportError）。生产 Pod 无这些变量；本地跑真实栈需
   `env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY ...`。

## 六、仍然存在的已知限制（诚实清单）

1. **多副本/多 Pod 证据缺失**：没有多实例集群实测（进程内状态限制见第五节第 1 条）。
2. **真实评测只跑过单次**：LLM 改写有方差，单次 100% 不等于稳定 100%。阈值按 12 用例
   全过记录，未做多次重复的统计基线；发布 SHA 上需重跑。
3. **`rebuild_resurrection_rate` 仍为 null**：V1 不暴露 `/memory/rebuild` 端点，重建相关
   断言被跳过（脚本已显式打印 `[v1-skip]`）。
4. **e2e 是 mock 模式**：UI 集成（路由、组件树、API 客户端接线）由 e2e 覆盖，真实链路
   证据来自 `tests/script/` 下的脚本，两者不重叠。
5. **变异审计覆盖范围有限**：`pyproject [tool.mutmut]` 限定了审计范围，范围外的代码
   没有变异证据。
6. **槽位词表要跟模型输出一起维护**：本轮四个真实缺陷里有三个来自「服务端槽位词表没有
   覆盖 zhiman38_27b 实际产出的中文措辞」。换模型或改抽取提示词时，槽位词表需要同步复核
   （`src/thinkback/domain/slots/extractors.py`）。
