# Contributing to Thinkback

Thanks for your interest in contributing! This document explains how to get a development
environment running and what we look for in a pull request.

[English](CONTRIBUTING.md) · [中文](CONTRIBUTING.md#贡献指南)

---

## Ways to Contribute

- **Report bugs** — open an [issue](https://github.com/mcgrapeng/Thinkback/issues) with
  reproduction steps, expected vs. actual behavior, and your environment.
- **Improve docs** — fixes to README, guides, or code comments are always welcome.
- **Submit code** — bug fixes, tests, and small features first. For large changes, open an
  issue to discuss the design before writing code.
- **Share feedback** — tell us how you use Thinkback and where it falls short.

## Development Setup

### Prerequisites

| Tool | Version | Notes |
| --- | --- | --- |
| Python | 3.12 – 3.13 | see `.python-version` |
| [uv](https://github.com/astral-sh/uv) | latest | package manager |
| PostgreSQL | 14+ | via `make dev-up` or your own instance |
| Milvus | 2.x | external; not started by `docker-compose` |
| OpenAI-compatible LLM + embedding endpoint | any | OpenAI, DashScope, vLLM, … |

### Bootstrap

```bash
git clone https://github.com/mcgrapeng/Thinkback.git
cd Thinkback

# install dependencies (uses uv.lock)
uv sync --frozen --group dev

# local PostgreSQL via Docker Compose (optional)
make dev-up

# configure
cp .env.example .env   # fill in MEMORY_LLM_KEY, MILVUS_URL, LLM/embedding endpoints

# install git hooks
make hooks-install
```

### Run & Verify

```bash
make dev          # start backend (API) + admin web UI with auto port fallback
make test         # deterministic unit/integration suite
make check        # format-check + ruff + mypy strict + coverage
make lint         # ruff only
make typecheck    # mypy --strict
make fmt          # ruff format
```

The deterministic suite must pass without any real LLM, Milvus, or Mem0 service —
tests use in-memory repositories and a fake memory backend.

## Pull Request Guidelines

1. **One concern per PR.** Keep unrelated refactors out of bug fixes.
2. **Add or update tests** for behavior changes. Existing tests must stay green.
3. **Pass `make check`** before requesting review. CI runs the same gates
   (`ruff`, `ruff format --check`, `mypy --strict`, `pytest`).
4. **Write clear commit messages.** We follow
   [Conventional Commits](https://www.conventionalcommits.org/) —
   `feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`.
5. **Update docs** when you change public behavior, configuration, or API contracts.

### Code Style

- **Formatting / lint**: [ruff](https://docs.astral.sh/ruff/) (`line-length = 100`).
- **Types**: [mypy](https://mypy.readthedocs.io) in strict mode. No `Any` at API boundaries.
- **Architecture**: dependency direction is `api/rpc → memory → infra → domain`.
  The `domain` package must not import from any other layer.
- **Validation**: request/response models are Pydantic v2; keep `extra="forbid"` on
  API-facing models.

### Testing Expectations

- Prefer deterministic tests: no network, no real vector DB, no real LLM.
- Cover edge cases that matter in production: idempotent writes, empty IDs,
  degradation paths (`degraded=True`), and background-task state transitions.

## Reporting Security Issues

Please do **not** open a public issue for security vulnerabilities. Instead, report them
privately via [GitHub Security Advisories](https://github.com/mcgrapeng/Thinkback/security/advisories/new)
and we will respond before any public disclosure.

## Code of Conduct

Be respectful and constructive. We are committed to a harassment-free experience
for everyone, regardless of level of experience, gender, gender identity and expression,
sexual orientation, disability, personal appearance, body size, race, ethnicity, age,
religion, or nationality.

## License

By contributing, you agree that your contributions will be licensed under the
[Apache License 2.0](LICENSE), the same license that covers the project.

---

# 贡献指南

感谢你对 Thinkback 的关注！本文说明如何搭建开发环境，以及我们对 Pull Request 的要求。

## 贡献方式

- **报告缺陷** — 提 [Issue](https://github.com/mcgrapeng/Thinkback/issues)，附复现步骤、
  期望与实际行为、运行环境。
- **完善文档** — README、指南、代码注释的修正都欢迎。
- **提交代码** — 优先接受 bug 修复、测试与小功能。较大改动请先开 Issue 讨论设计。
- **反馈体验** — 告诉我们你的使用场景与痛点。

## 开发环境

### 前置要求

| 工具 | 版本 | 说明 |
| --- | --- | --- |
| Python | 3.12 – 3.13 | 见 `.python-version` |
| [uv](https://github.com/astral-sh/uv) | 最新 | 包管理器 |
| PostgreSQL | 14+ | `make dev-up` 或自备实例 |
| Milvus | 2.x | 外部服务，不由 `docker-compose` 启动 |
| OpenAI 兼容 LLM + Embedding 端点 | 任意 | OpenAI、DashScope、vLLM 等 |

### 初始化

```bash
git clone https://github.com/mcgrapeng/Thinkback.git
cd Thinkback

# 安装依赖（使用 uv.lock）
uv sync --frozen --group dev

# 本地 PostgreSQL（可选）
make dev-up

# 配置
cp .env.example .env   # 填写 MEMORY_LLM_KEY、MILVUS_URL、LLM/Embedding 端点

# 安装 git hooks
make hooks-install
```

### 运行与验证

```bash
make dev          # 启动后端 API + 管理台前端，端口占用自动顺延
make test         # 确定性单元/集成测试
make check        # 格式检查 + ruff + mypy strict + 覆盖率
make lint         # 仅 ruff
make typecheck    # mypy --strict
make fmt          # ruff 格式化
```

确定性测试套件必须在**无真实 LLM / Milvus / Mem0** 的情况下通过 ——
测试使用内存仓储与 fake 记忆后端。

## Pull Request 规范

1. **一个 PR 只做一件事**，不要把无关重构混进 bug 修复。
2. **行为变更需补测试**，且现有测试保持全绿。
3. **提交前跑 `make check`**。CI 跑同样的门禁
   （`ruff`、`ruff format --check`、`mypy --strict`、`pytest`）。
4. **提交信息清晰**，遵循 [Conventional Commits](https://www.conventionalcommits.org/)：
   `feat:`、`fix:`、`docs:`、`test:`、`refactor:`、`chore:`。
5. **同步更新文档**，尤其是公开行为、配置项与 API 契约变更时。

### 代码风格

- **格式/静态检查**：[ruff](https://docs.astral.sh/ruff/)（`line-length = 100`）。
- **类型**：[mypy](https://mypy.readthedocs.io) strict 模式，API 边界不出现 `Any`。
- **架构**：依赖方向为 `api/rpc → memory → infra → domain`，
  `domain` 不得反向依赖其他层。
- **校验**：请求/响应模型使用 Pydantic v2，API 模型保持 `extra="forbid"`。

### 测试要求

- 优先确定性测试：不联网、不依赖真实向量库与 LLM。
- 覆盖生产关键边界：幂等写入、空 ID、降级路径（`degraded=True`）、后台任务状态迁移。

## 安全问题

请**不要**公开提交安全漏洞 Issue。请通过
[GitHub Security Advisories](https://github.com/mcgrapeng/Thinkback/security/advisories/new)
私下报告，我们会在公开披露前响应。

## 行为准则

保持尊重与建设性。我们致力于为所有人提供无骚扰的参与体验，不论经验、性别、
性别认同与表达、性取向、外貌、身材、种族、族裔、年龄、宗教或国籍。

## 许可证

提交贡献即表示同意你的贡献以 [Apache License 2.0](LICENSE) 授权，与本项目许可证一致。
