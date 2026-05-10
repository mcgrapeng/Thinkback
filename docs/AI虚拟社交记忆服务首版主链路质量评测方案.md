# AI虚拟社交记忆服务首版主链路记忆质量评测方案

本文档定义 Thinkback 记忆服务首版的质量评测方案。
目标是判断记忆服务是否达到首版主链路可用所需的记忆质量标准。

本文只定义评测方法、指标、门禁和报告要求，不记录任何本轮评测数据。
具体执行结果应写入独立报告：
[首版记忆质量评测报告模板](AI虚拟社交记忆服务首版主链路质量评测报告模板.md)。

性能、容量、并发、soak、故障注入和资源观测不属于本文范围。
相关内容见 [性能与稳定性测试方案](AI虚拟社交记忆服务性能与稳定性测试方案.md)。

## 1. 评测目标

首版记忆服务的质量标准可以概括为一句话：
用户告诉系统的当前事实要能被正确记住、正确召回、正确修正、正确删除，并且不能串到其他作用域。

| 目标 | 直白解释 | 首版要求 |
| --- | --- | --- |
| 写入可信 | 用户说过的重要事实能进入记忆。 | 核心事实必须能在后续召回中出现。 |
| 召回可信 | 问到已知事实时能找回当前事实。 | 当前事实应进入 top10 召回结果。 |
| 纠错可信 | 用户改口后，旧事实不能继续污染召回。 | 新旧事实不得同时作为有效记忆出现。 |
| 删除可信 | 删除后的事实不能继续被召回。 | 删除后复查不得命中被删事实。 |
| 重建可信 | 重建不能把已删除或已废弃事实复活。 | 重建后不得出现旧事实复活或 active 膨胀。 |
| 隔离可信 | 不同用户、角色、现实/剧情上下文不能串扰。 | 隔离用例不得出现跨作用域召回。 |

## 2. 评测范围

| 范围 | 是否纳入 | 说明 |
| --- | --- | --- |
| L3 长期记忆写入与召回 | 是 | 首版质量评测的核心对象。 |
| 事实纠错与旧值收敛 | 是 | 验证用户修正后是否仍召回旧事实。 |
| 负样本召回 | 是 | 验证系统不会把无关记忆当成已知事实。 |
| 删除与重建 | 是 | 验证删除语义和重建边界。 |
| 用户 / 角色 / 现实 / 剧情隔离 | 是 | 验证记忆不会跨作用域泄漏。 |
| 延迟、并发、容量、资源趋势 | 否 | 放入性能与稳定性测试方案。 |
| 线上 SLO 与长期运行报告 | 否 | 本文只定义首版质量评测，不定义线上运营指标。 |

## 3. 评测流程

| 步骤 | 做什么 | 产出 |
| --- | --- | --- |
| 1. 准备环境 | 使用真实 Thinkback API、Mem0 Library、OpenAI、Qdrant、PostgreSQL。 | 可执行的真实评测环境。 |
| 2. 写入事实 | 写入昵称、宠物、地点、生日、偏好等核心事实。 | 初始长期记忆。 |
| 3. 修正事实 | 对部分事实进行纠错，例如猫名、地点、饮品、生日。 | 新旧事实冲突样本。 |
| 4. 执行召回 | 对当前事实、未知事实、隔离事实分别发起 recall。 | 每个 case 的召回结果。 |
| 5. 删除与重建 | 删除目标记忆后复查，再执行 rebuild 后复查。 | 删除残留和重建复活证据。 |
| 6. 汇总指标 | 计算命中、污染、误召回、重复 active、隔离等指标。 | 指标摘要和失败列表。 |
| 7. 输出报告 | 按报告模板记录 run_id、结果、失败证据和结论。 | 独立质量评测报告。 |

## 4. 必测场景

| 场景 | 必测内容 | 失败表现 |
| --- | --- | --- |
| 核心槽位纠错 | 昵称、宠物名、生日、地点、工作状态、偏好。 | 漏召回当前事实，或仍召回旧事实。 |
| 多槽位并存 | 猫和狗、饮品和食物、地点和工作状态同时存在。 | 不同槽位互相覆盖。 |
| 旧值污染 | 用户改口后复查旧昵称、旧宠物名、旧地点、旧生日等。 | 新旧事实同时出现。 |
| 负样本 | 问用户从未提供过的鸟、兔等信息。 | 系统返回猫狗等无关记忆。 |
| 跨用户隔离 | 另一个 user 写入相似事实后，当前 user 召回。 | 召回其他 user 的事实。 |
| 跨角色隔离 | 同一 user 的另一个 character 写入相似事实后召回。 | 召回其他 character 的事实。 |
| 现实/剧情隔离 | real_user 与 roleplay 写入相似事实后分别召回。 | 现实猫和剧情猫混用。 |
| 删除与重建 | 删除一条长期记忆，复查，再 rebuild。 | 删除后残留，或 rebuild 后复活。 |
| 表达漂移 | Mem0 抽取出英文、转写、同义表达。 | 语义正确但脚本无法识别，或错误归一化。 |

## 5. 指标建议值总览

所有质量指标都需要有明确处理方式，但不都适合设成数值硬门禁。
首版只把会直接破坏记忆可信度的指标设为硬门禁；
排序、条目级精度和表达漂移先作为观察或复核指标。

建议值解释规则：

| 规则 | 说明 |
| --- | --- |
| 核心样本优先看失败个数 | 首版核心样本规模较小时，明确失败 case 不应被比例均值稀释。 |
| 比例阈值用于扩展样本 | 当样本量扩大后，再用 `>= 0.95`、`<= 0.02` 这类比例阈值衡量整体趋势。 |
| 生命周期和隔离按不变量处理 | 删除残留、重建复活、跨作用域泄漏不按平均值放宽。 |
| 观察指标不阻断首版 | top1、MRR、条目级精度受模型、rerank、top_k 影响大，先记录基线。 |

数值读法：

| 写法 | 直白解释 |
| --- | --- |
| `1.0` | 100% 通过，核心样本不能有失败。 |
| `>= 0.95` | 扩展样本中至少 95% 达标。 |
| `<= 0.02` | 扩展样本中问题比例最多 2%。 |
| `0` | 不允许出现这类问题。 |
| `null` | 当前不具备稳定自动化结果，不能当作 `0`。 |
| top10 | 一次召回最多看前 10 条结果。 |

| 指标 | 类型 | 首版建议值 | 报告处理 | 直白解释 |
| --- | --- | --- | --- | --- |
| `case_pass_rate` | 硬门禁 | `1.0` | 未达即未通过。 | 核心 case 不能失败。 |
| `recall_at_10` | 硬门禁 | 核心无漏召回；扩展 `>= 0.95`。 | 未达即未通过。 | 正确事实进入 top10。 |
| `precision_at_10` | 硬门禁 | 核心无污染；扩展 `>= 0.95`。 | 未达即未通过。 | 本方案的 case 级 top10 干净率。 |
| `conflict_pollution_rate` | 硬门禁 | 核心为 `0`；扩展 `<= 0.02`。 | 未达即未通过。 | 旧值不能污染召回。 |
| `false_positive_rate` | 硬门禁 | 负样本为 `0`；扩展 `<= 0.02`。 | 未达即未通过。 | 未知事实不能补成记忆。 |
| `duplicate_active_rate` | 硬门禁 | `0`。 | 未达即未通过。 | 同槽位不能有多个 active 版本。 |
| `delete_memory_residue_rate` | 硬门禁 | 本轮样本内为 `0`。 | 未达即未通过。 | 删除后不能召回被删事实。 |
| `rebuild_resurrection_rate` | 硬门禁 | 本轮样本内为 `0`。 | 未达即未通过。 | rebuild 不能复活旧事实。 |
| `cross_user_leak_rate` | 硬门禁 | 本轮样本内为 `0`。 | 未达即未通过。 | 不能召回其他用户的事实。 |
| `cross_character_leak_rate` | 硬门禁 | 本轮样本内为 `0`。 | 未达即未通过。 | 不能召回其他角色的事实。 |
| `roleplay_real_mix_rate` | 硬门禁 | 本轮样本内为 `0`。 | 未达即未通过。 | 现实和剧情记忆不能混用。 |
| `top1_hit_rate` | 观察 | 不设硬门禁，记录本地基线。 | 低于基线时复核。 | 正确事实是否排第一。 |
| `mrr` | 观察 | 不设硬门禁，记录本地基线。 | 低于基线时复核。 | 正确事实整体排序位置。 |
| `item_precision_at_10` | 观察 | 不设硬门禁，记录本地基线。 | 辅助解释召回噪声。 | top10 条目级相关比例。 |
| `irrelevant_l3_per_query` | 观察 | 不设硬门禁，越低越好。 | 异常升高时排查。 | 每次 query 的无关 L3 数。 |
| `known_drift_regression_pass_rate` | 复核 | 首版可为 `null`。 | 有样本后再设门禁。 | 表达漂移样本通过率。 |

## 6. 指标依据与阈值合理性

本文采用“通用检索 / RAG 指标 + 记忆服务语义指标”的组合。
这样既符合常见检索评测方法，也覆盖记忆服务特有风险。

公开资料可以证明指标类型是通用做法，但不能证明存在统一的记忆服务验收阈值。
Mem0、Zep 等公开资料更多给出 benchmark 分数、检索深度、模型配置和延迟，
没有发布可直接套用到所有业务系统的生产验收门禁。
因此，本文阈值按“公开评测方法 + 首版核心质量契约”制定。

| 指标类别 | 对应指标 | 依据 | 为什么适合记忆服务 |
| --- | --- | --- | --- |
| top-k 召回 | `recall_at_10` | 信息检索和 BEIR 常用 `Recall@k`。 | 记忆召回首先要找得到当前事实。 |
| case 级精度 | `precision_at_10` | 检索常用 `P@k`，RAG 评测也常看上下文精度。 | 判断 top10 是否干净。 |
| 条目级精度 | `item_precision_at_10` | 检索常用 `P@k`。 | 观察 top10 条目相关比例。 |
| 排序质量 | `top1_hit_rate`、`mrr` | 常用 `MRR@k`、Top-K Accuracy。 | 正确事实越靠前越可用。 |
| 负样本控制 | `false_positive_rate` | 检索评测中的误召回控制思路。 | 用户没说过的事实不能被补出来。 |
| 记忆更新 | 污染率、重复 active 率。 | 记忆服务需要处理事实更新。 | 验证旧事实是否失效。 |
| 删除与重建 | 删除残留、重建复活。 | 记忆系统需要验证生命周期语义。 | 错误会直接破坏用户信任。 |
| 作用域隔离 | 跨用户、跨角色、现实/剧情混用。 | 多作用域服务基本要求。 | 记忆串扰不可接受。 |

公开 benchmark 参考：

| 来源 | 公开结果 | 对本文的参考意义 |
| --- | --- | --- |
| Mem0 Memory Evaluation | LoCoMo `91.6`、LongMemEval `93.4`。 | 覆盖召回、更新、时间推理、多轮推理。 |
| Mem0 memory-benchmarks | 支持 LoCoMo、LongMemEval、BEAM。 | 分数受 top_k、模型、judge 和数据规模影响。 |
| Zep 公开论文与产品资料 | DMR `94.8%`。 | 强调事实更新、时间关系和跨会话检索。 |

这些公开结果不能直接换算成本系统的通过线。
公开 benchmark 的数据集、问题类型、judge 模型、top_k 和系统实现均不同；
本文只把它们作为“优秀记忆系统通常达到的量级”和“应覆盖的评测维度”参考。

第 5 节给出所有指标的建议值和报告处理方式。
本节只解释指标选择依据，不再重复阈值。

`precision_at_10` 在本文是 case 级门禁：
top10 中既要命中当前事实，也不能包含旧值或无关事实。
`item_precision_at_10` 用于观察 top10 条目级相关比例。

公开参考资料：

| 来源 | 本文采用方式 |
| --- | --- |
| Stanford IR ranked retrieval evaluation | 支持 top-k、precision、recall 等检索评测思想。 |
| BEIR metrics | 支持 `Recall@k`、`P@k`、`MRR@k`、Top-K Accuracy 等常用检索指标。 |
| RAGAS context precision | 支持 RAG 场景下的上下文精度评估。 |
| RAGAS context recall | 支持 RAG 场景下的上下文召回评估。 |
| LangSmith RAG evaluation | 支持 correctness、groundedness、retrieval relevance 等评测维度。 |
| Mem0 Memory Evaluation | 支持记忆系统按准确率、成本、性能组合评估。 |
| Mem0 memory-benchmarks | 支持 LoCoMo、LongMemEval、BEAM 等记忆增强系统 benchmark。 |
| Zep Agent Memory paper | 支持记忆服务按事实更新、时间关系和跨会话检索评测。 |

[Stanford IR ranked retrieval evaluation]:
https://nlp.stanford.edu/IR-book/html/htmledition/evaluation-of-ranked-retrieval-results-1.html
[BEIR metrics]: https://github.com/beir-cellar/beir/wiki/Metrics-available
[RAGAS context precision]:
https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/context_precision/
[RAGAS context recall]:
https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/context_recall/
[LangSmith RAG evaluation]: https://docs.langchain.com/langsmith/evaluate-rag-tutorial
[Mem0 Memory Evaluation]: https://docs.mem0.ai/core-concepts/memory-evaluation
[Mem0 memory-benchmarks]: https://github.com/mem0ai/memory-benchmarks
[Zep Agent Memory paper]:
https://blog.getzep.com/zep-a-temporal-knowledge-graph-architecture-for-agent-memory/

## 7. 评测边界

| 边界 | 说明 |
| --- | --- |
| 本文评测首版主链路质量 | 不评价长期线上运营质量，也不替代性能与稳定性测试。 |
| 本文门禁适用于受控核心样本 | 公开 benchmark 分数、线上大盘指标和本地核心样本不能直接互相替换。 |
| 评测数据写入独立报告 | 本文只定义方案、指标和门禁，不记录本轮结果。 |
| 阈值调整必须有样本依据 | 扩展样本后可以按场景重新分层，但不能降低生命周期和隔离不变量。 |

## 8. 执行方法

首版质量评测应使用真实依赖执行，不使用 fake 模式替代。

```bash
env OPENAI_API_KEY=${OPENAI_API_KEY:?set OPENAI_API_KEY} \
QDRANT_URL=${QDRANT_URL:?set QDRANT_URL} \
THINKBACK_API_URL=${THINKBACK_API_URL:?set THINKBACK_API_URL} \
PYTHONPATH=src .venv/bin/python script/real_mem0_quality_regression.py
```

执行要求：

| 要求 | 说明 |
| --- | --- |
| 环境真实 | API、Mem0 Library、OpenAI、Qdrant、PostgreSQL 必须可用。 |
| 数据隔离 | 每次执行使用唯一 `run_id`、user、character、session。 |
| 结果留痕 | 保存原始 JSON、失败 case、召回文本和 active memory 列表。 |
| 报告分离 | 本文不写入任何本轮数据，结果写入独立评测报告。 |

## 9. 报告要求

每次执行后，应生成一份独立质量评测报告。
报告模板见 [首版记忆质量评测报告模板](AI虚拟社交记忆服务首版主链路质量评测报告模板.md)。
稳定字段见 [首版记忆质量评测报告字段规范](AI虚拟社交记忆服务首版主链路质量评测报告字段.md)。

报告至少包含：

| 部分 | 内容 |
| --- | --- |
| 结论摘要 | 通过/未通过、run_id、执行时间、失败 case、失败指标。 |
| 指标摘要 | 本轮指标值、门禁、结论。 |
| 场景覆盖 | 核心槽位、负样本、隔离、删除、重建的覆盖情况。 |
| 失败证据 | query、期望事实、禁用事实、召回文本、active memory。 |
| 自动化状态 | 已自动化字段、由 case 覆盖字段、暂未输出字段。 |
| 结论边界 | 本次报告能证明什么，不能证明什么。 |

## 10. 失败处理

| 失败类型 | 优先排查方向 |
| --- | --- |
| 召回不到当前事实 | 检查 append、Mem0 抽取、L3 写入、Qdrant 检索和 top_k。 |
| 旧值污染 | 检查槽位归一化、active/superseded 状态、冲突替换逻辑。 |
| 误召回 | 检查负样本过滤、召回阈值、上下文过滤和相似槽位混淆。 |
| 重复 active | 检查同槽位去重、状态转换和本地补偿逻辑。 |
| 删除后残留 | 检查 delete 目标、Mem0 delete、业务索引状态和后续 recall 输入。 |
| 重建后复活 | 检查 rebuild 输入范围、被删事实过滤和 active count 变化。 |
| 作用域泄漏 | 检查 user、character、session、context_type、roleplay_mode 过滤条件。 |
| 外部依赖失败 | 记录 request 指标和错误原因；未恢复前不得判定质量通过。 |

阈值调整必须先更新本文档，再调整脚本或报告逻辑。
不得通过降低语义正确性要求来获得通过结论。

## 11. 结论判定

| 结论 | 判定标准 |
| --- | --- |
| 通过 | 首版硬门禁全部满足，失败 case 为空。 |
| 未通过 | 任一硬门禁失败，或存在未解释的核心 case 失败。 |
| 需复核 | 指标达标，但存在表达漂移、抽取歧义或人工判断不一致。 |

通过结论只代表首版记忆质量达标。
它不代表性能、容量、长期稳定性或线上 SLO 已达标。
