# Thinkback Operational Scripts

本目录放运维、质量评测和生产前验证脚本。脚本默认面向真实依赖，不把 fake 模式伪装成真实结论。

## 数据库

| 脚本 | 用途 |
| --- | --- |
| `db_migrate.py` | Alembic 迁移辅助入口，适合本地和发布流程调用。 |

## 质量评测

| 脚本 | 用途 |
| --- | --- |
| `real_mem0_quality_regression.py` | 真实 Mem0/Qdrant/Postgres/Redis 质量主链路评测，包含槽位纠错、隔离、负样本、删除和重建质量门禁。 |
| `run_real_mem0_quality_evaluation.py` | 执行质量评测并在 `docs/report` 下生成 JSON/Markdown 对比报告。 |

## 生产前验证

| 脚本 | 用途 |
| --- | --- |
| `real_mem0_pressure.py` | 早期 5 轮真实主链路压力验证入口。 |
| `real_mem0_p0_short_pressure.py` | P0 短压测套件。 |
| `real_mem0_p0_preprod_pressure.py` | P0 生产前 baseline、stress、spike、soak 阶段报告汇总。 |
| `real_mem0_p0_fault_injection.py` | 依赖故障注入报告。 |
| `real_mem0_p0_representative_replay.py` | 代表性样本回放报告。 |
| `build_p0_pressure_final_report.py` | P0 压测最终报告生成。 |

## 本地调试建议

1. 从 `.env.example` 派生本地 `.env`，填入 `OPENAI_API_KEY`。
2. 使用 `make debug-api` 启动 18082 端口的本地 API。
3. 使用 `make debug-ready` 确认 readiness。
4. 使用 `make quality-real` 生成真实质量评测报告。

常用脚本环境变量说明：

| 变量 | 中文说明 |
| --- | --- |
| `OPENAI_API_KEY` | 模型服务密钥，供 Mem0 Library 抽取和向量化使用。 |
| `QDRANT_URL` | Qdrant 向量库地址。 |
| `QDRANT_API_KEY` | Qdrant 鉴权密钥；本地无鉴权可留空。 |
| `THINKBACK_API_URL` | 脚本访问 Thinkback API 的地址。 |
| `POSTGRES_DATABASE` | PostgreSQL 数据库名；本工程统一使用 `liaoriver_memory`。 |
| `QUALITY_EVALUATION_TIMEOUT_SECONDS` | 真实质量评测包装脚本的总超时。 |

稳定性与质量评测是两条不同的验证路径；如果只验证质量门禁，不要混入稳定性结论。
