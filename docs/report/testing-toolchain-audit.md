# 测试工具链审计（2026-10-10）

本地参考文档（docs/ 不入 git）。对应一次完整的测试工具链优化，结论与基线如下。

## tester-army/e2e 结论

不引入，列为可选试点。它是自然语言驱动的 e2e 框架（`agent.act`/`agent.assert` + locator 断言，
Playwright web 引擎，另有 iOS/Android 引擎，agent 步骤可零模型回放），npm 包 `e2e`，
`npx e2e init`。暂缓的四个理由：

1. 病因是覆盖空洞与测试纪律，不是 e2e 框架缺失；
2. pre-1.0，minor 版本间 API/配置会变（README Status 节）；
3. agent 步骤依赖模型 API，与「测试确定性」原则冲突；
4. 本机已有 @playwright/mcp、chrome-devtools-mcp、webapp-testing，AI 探索式测试能力不缺。

试点触发条件：想把自然语言探索固化为可版本管理的用例，或做移动端 e2e。试点时记得
`npx e2e telemetry disable`（默认开遥测）。

## 本次落地

| 项 | 变化 |
| --- | --- |
| 仓储契约测试 | 新增 `tests/unit/test_repository_contract.py`（28 例 × 双后端），SQL 跑 SQLite/aiosqlite |
| 行为漂移修复 | in_memory `save_round` 改首写胜出；in_memory `get_l1` 补 journal 回源；模型补 `sqlite_where` 部分唯一索引；recall 过滤 `excluded_source_refs`（防已删事实经 L1 回源复活） |
| 覆盖率 | `sqlalchemy.py` 45%→97%，`rpc/server.py` 0%→100%，`rpc/servicer.py` 62%→100%，TOTAL 83%→92% |
| 覆盖率门禁 | `make coverage` 加 `--cov-fail-under`（`COVERAGE_FLOOR=92`），生成代码 `memory_pb2*.py` 不计入 |
| gRPC 补测 | 错误映射矩阵、全部 RPC except 路径、UpdateMemory/GetMemory/GetTask happy path、server 生命周期 |
| 前端从零到一 | vitest 21 例（6 文件）+ Playwright e2e 冒烟 2 例（mock API 模式）；`make test-ui` / `make test-e2e` |
| 评测产品化 | `make eval` = 自包含评测（20 例）+ `tests/eval/baseline.json` 基线门禁（钉用例名单防删测回血） |
| 测试纪律 | 新建 `AGENTS.md`；`tests/unit` 为默认套件（e2e 显式跑） |
| 顺手修复 | 3 处存量 lint（死导入/导入排序）、20 处 `mypy src` 注解缺失；typecheck 门禁对齐 pre-commit（`mypy src`） |

## mutmut 变异审计基线

范围：`keys.py` / `summarization.py` / `decay.py` / `_l1_cache.py`（配置见 pyproject `[tool.mutmut]`）。
277 个变异体：191 killed / 84 survived / 2 no-tests（covered 变异体击杀率 69%）。

存活分布与处置：

- `MemoryDecaySweeper`（71 个）：`__init__`/`maybe_submit`/`_run_sweep` 线程调度编排，等价变异居多，接受存活；留给 CI 夜间全量。
- `build_structured_summary_user_prompt`（10 个）：LLM prompt 文本，不钉死（prompt 调优是常态）。
- `fingerprint` 编码名 `utf-8`→`UTF-8`：真等价变异。
- 其余纯函数存活已通过新增 `tests/unit/test_domain_pure_functions.py` 与 L1 语义用例清掉
  （指纹摘要钉死、四段渲染、`clear_l1` 三维键、`update_l1` 默认上限）。

复跑：`make mutation-audit`（结果 `uv run mutmut results`）。

## 全局工具去留

| 工具 | 处置 |
| --- | --- |
| k6 | 留。新增 `tests/load/smoke.k6.js` + `make load-smoke`（需真实后端 + API Key） |
| promptfoo | 留（全局其它用途）。Thinkback 未接：LLM 评审需要 key 与判分设计，确定性质量已由 `make eval` 覆盖 |
| venv playwright 1.63 | 已启用为 e2e 驱动（`tests/e2e/test_admin_smoke.py`，系统 Chrome 免下载） |
| mutmut | 已接入 dev 依赖 + `make mutation-audit` |

## 已知遗留

- `tests/unit` 历史用例未做严格 mypy（358 处存量注解债），typecheck 门禁只管 `src`；新用例保持注解完整。
- `service.py`（1500+ 行）未做变异审计，属 CI 夜间任务量级。
- 真实链路评测（`tests/script/real_*`、`zh_replay_eval`）仍手动触发，需真实服务。

## 测试天团补强（第二轮）

安装前全部经 skillspector 扫描 + 人工复核：409 条告警均为文档误报
（Vue 模板注释被判「隐藏指令」、`.gitignore` 示例被判「凭据窃取」），无真实风险。

| 成员 | 来源 | 用途 |
| --- | --- | --- |
| `hypothesis` 6.168 | dev 依赖 | 纯函数属性测试（`tests/unit/test_domain_properties.py`，8 条不变量） |
| `pytest-testmon` 2.2 | dev 依赖 | 测试影响分析，`make test-fast` 只跑受改动影响的用例 |
| `playwright-best-practices` | ~/.agents/skills | Playwright e2e 最佳实践（Currents.dev，91.7K installs） |
| `vitest` | ~/.agents/skills | Vitest 最佳实践（antfu，39.8K installs） |
| `python-testing-patterns` | ~/.agents/skills | pytest 模式（wshobson，35K installs） |
| `pytest-coverage` | ~/.agents/skills | 覆盖率纪律（GitHub 官方 awesome-copilot，12.7K installs） |

注：`npx skills -g` 在本机 harness 被拒，改项目安装后手工迁入 `~/.agents/skills`；
该 CLI 会顺手生成 CHANGELOG/.github 社区文件等脚手架，已清除。
testmon 按方法级哈希选测：注释级改动不触发（正确行为），语义改动实测选中 161/458 用例。

## 工业级补强（第三轮）

全局工具：`pip-audit 2.10`、`schemathesis 4.30`（uv tool，机器级 CLI）。
全局体系：`~/.agents/skills/ai-testing-system`（成熟度清单 10+1 条 + 工具矩阵 +
门禁分层 + AI 测试工作流）；全局 AGENTS.md 挂触发指针。

schemathesis 契约 fuzz（tests/fuzz，`make test-fuzz`）首轮即抓出并修复 6 类真问题：

1. 405 缺 RFC 9110 必需的 `Allow` 头（错误标准化层丢路由头）→ errors_standard 透传。
2. `IdStr`/update content 未声明 `minLength`（生成空串被拒 = 契约比实现宽松）。
3. v1 PUT/DELETE 的 `{memory_id}` 路径段被处理器忽略（契约未声明 + URL 可为任意值）
   → v1 包装层声明路径参数并做 path/body 一致性校验（顺带封掉 scope=ALL 误删口）。
4. `plan`/`env`/`statuses`/`action` 运行时枚举、schema 自由串 → json_schema_extra.pattern。
5. 未声明状态码（400/404/409/403）→ COMMON_ERROR_RESPONSES 各 router 声明。
6. pytest 8.4.2 CVE（PYSEC-2026-1845）→ `make audit` 抓获，升级 pytest 9.1.1 + pytest-asyncio 1.4。

fuzz 已知豁免（弱区留痕）：allow_header_conformance（FastAPI 分 method 路由的
Allow 集不全）、negative_data_rejection（pydantic 宽松 coercion，strict 化列为
API 策略 backlog）、positive_data_acceptance 放行 422（跨字段业务规则，单测钉死）。

a11y（axe 4.10.3 注入 e2e）修复 4 类：`role="button"` 覆盖 listitem、
`--foreground-muted/soft` 与 `--warning/error/info` 对比度差一口气（已加深）、
可滚 `<pre>` 不可聚焦、revoked 卡片 `opacity-60` 混色击穿 AA（改为纯视觉弱化）。
e2e 旅程补至 5 条（记忆浏览器/任务监控/治理操作）。

CI（ci.yml）新增 `supply-chain-audit`（pip-audit）与 `api-fuzz`（schemathesis）两个门禁任务。

### 后续 backlog

- 全模型 strict 化（negative_data_rejection 恢复启用）属 API 策略决策。
- 夜间 cron：全量变异审计（service.py）+ 真实链路评测（tests/script/real_*）。
- 视觉回归（截图基线）未做；迁移测试（alembic 升降级）需 PG 环境。
