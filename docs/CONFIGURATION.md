# 配置与敏感信息管理

这份文档讲两件事：**所有环境变量集中在哪里**，以及**凭据怎么保证不外泄**。

一句话规则：

> **凭据只进 `.env`（已 gitignore）或 Secret 管理系统；变量清单只进 `.env.example`。**

---

## 1. 环境变量的四个位置

| 位置 | 装什么 | 进版本库？ |
| --- | --- | --- |
| `.env` | **本机的真凭据**（LLM key、数据库密码、内网端点地址） | **否**（`.gitignore` 已挡） |
| `.env.example` | **全部变量名的清单 + 注释**，值留空或写占位符 | 是 |
| `k8s/configmap.yaml` | 非敏感的生产配置（端口、并发、开关） | 是 |
| `k8s/secret.example.yaml` | 敏感配置的**空模板**（key / 密码） | 是（值必须为空） |

服务配置只有一条读取路径：`thinkback.infra.config.Settings`（pydantic-settings）。
它同时读环境变量与 `.env`（`ENVIRONMENT=production` 时禁用 `.env` 兜底，
强制只用环境变量 —— 防止开发机上的 `.env` 污染线上）。

**变量的环境变量名 = 字段名的大写**（`case_sensitive=False`）：
`memory_llm_model` → `MEMORY_LLM_MODEL`。

---

## 2. 本地怎么配

```bash
cp .env.example .env      # 清单复制成本地配置
$EDITOR .env              # 填 MEMORY_LLM_KEY / 端点地址等
```

`.env` 与 `.env.example` 的**变量集合必须一致** —— 这条由
`tests/unit/test_env_centralization_contract.py` 守着（清单改了本地没改、
或者反过来，都会让门禁红）。清单缺一个变量 = 运维看不见那个旋钮；
本地多一个变量 = 换台机器就少一个配置。

---

## 3. 凭据不外泄的几道保证

### 3.1 `.env` 永不进版本库

`.gitignore` 里有 `.env`。`test_env_centralization_contract.py` 断言：

- `.env` **不在** `git ls-files` 里；
- `.env` **在** `git check-ignore` 里。

所以「有人 `git add .env` 了」会当场被门禁抓住，而不是等到推送。

### 3.2 `.env.example` 里不能出现真凭据

清单是公开仓库的一部分。契约测试断言：

- key / password 类变量的值**必须为空**或占位符；
- 不能出现**私网地址**（`10.` / `192.168.` / `172.16-31.` / `169.254.`）。

注意「模型名」不是凭据 —— `MEMORY_LLM_MODEL=zhiman38_27b` 这类写在清单里
没问题；key 才是。

### 3.3 清单里的名字必须被服务认领

**这是最隐蔽的一类失效**：变量名写错，配置**静默不生效**。

`Settings` 是 `extra="ignore"` —— 不认识的名字被直接丢弃，不报错、不打日志。
于是「改了 ConfigMap / `.env`，重启后配置没变，而日志里什么都没有」。

契约测试因此双向断言：

- `Settings` 的**每个字段**都要在 `.env.example` 里露面（写漏了 = 运维看不见）；
- `.env.example` 与 `k8s/configmap.yaml` 里的**每个名字**都要被服务认领
  （写错了 = 改了不生效）。

**真实踩过**：k8s ConfigMap 写的是 `MEMORY_BACKEND_MAX_CONCURRENT_CALLS`，
而真正的字段是 `MEMORY_BACKEND_MAX_CONCURRENT_WRITES` / `_READS`
（读写分开两把信号量，见 `config.py` 的注释）。那个名字不存在 → **静默丢弃**；
两把信号量都没被这份 ConfigMap 覆盖。当时它碰巧等于默认值，所以没暴露 ——
隐患是「将来改了默认值，生产不会跟着变」。现已修正，并由契约测试守着。

### 3.4 端点不存在时，门禁要说「配置错了」而不是「记忆系统坏了」

真实栈门禁（`make gate-real`）与垂类评测（`--real`）在 CI 里跑。模型名写错时，
端点返回 404，抽取全空，门禁会以「召回 0 条」的形式红 —— 而根因是配置。

所以 CI 的 `real-stack-gate` job 里加了一步**模型存在性预检**：
拿 `.env` 里生效的模型名去问端点「你有哪些模型」，没有就立刻报
「端点上没有模型 X —— 配置错了，不是记忆系统坏了」。

同理，**模型名必须写进 `.env`**：产品默认值（`qwen-plus-latest` /
`default-embedding`）面向公有云端点，自建端点上没有这两个模型。

### 3.5 历史也要干净

凭据曾经进过版本库时，光删当前文件不够 —— 历史里还在。仓库用
`git filter-repo` 抹过（见 `docs/report/` 里的隐私清零记录）。

日常用 `gitleaks detect` 扫全历史确认：

```bash
gitleaks detect --no-banner --redact
```

### 3.6 管理台不回显凭据

`/admin/api/config` 返回运行时配置，但带敏感标记的字段（名字含
`password` / `token` / `secret` / `key` / `url`）会被掩掉 —— LLM 密钥
不会随配置接口明文出去。有用例钉着（`web/src/pages/integration.test.tsx`
断言页面里不出现 `ADMIN_TOKEN`）。

---

## 4. 生产环境怎么配

k8s 部署用 `envFrom` 把 ConfigMap 与 Secret 都灌进容器：

```yaml
envFrom:
  - configMapRef: { name: thinkback-config }
  - secretRef: { name: thinkback-secret }
```

- **非敏感**（端口、并发、开关、端点地址）→ `k8s/configmap.yaml`
- **敏感**（key、密码）→ `k8s/secret.yaml`（**不进版本库**），
  模板见 `k8s/secret.example.yaml`，值必须留空

`ENVIRONMENT=production` 时服务**不读** `.env` —— 容器里也没有 `.env`，
配置只能来自环境变量。这是「本地配置污染线上」的第一道闸门。

CI 的对应机制：`secrets.*`（凭据）+ `vars.*`（非敏感配置，如模型名），
凭据只经 env 传进脚本，不拼进命令文本。

---

## 5. 工具链变量

有些环境变量**不是服务配置**，而是脚本/前端直接读 `os.environ` 的：

| 变量 | 谁读 |
| --- | --- |
| `BACKEND_PORT` / `FRONTEND_PORT` | `script/dev.py`、vite |
| `VITE_USE_MOCK` / `VITE_API_TARGET` | `web/vite.config.ts` |
| `THINKBACK_MCP_*` | `src/thinkback/mcp/__main__.py` |
| `THINKBACK_MEMORY_API_URL` | `tests/script/real_mem0_*.py`（驱动运行中的服务） |

它们同样列在 `.env.example` 里（这样「所有旋钮在一处可见」），但由
`test_env_centralization_contract.py` 的 `_TOOLCHAIN_VARS` 显式豁免
「必须被 `Settings` 认领」这一条 —— 因为它们本来就不经过 `Settings`。

---

## 6. 加一个新配置项时

1. 在 `src/thinkback/infra/config.py` 加字段（带默认值与说明）；
2. 在 `.env.example` 加同名条目 + 注释；
3. 在本地 `.env` 加同一行；
4. 生产需要改 → 加进 `k8s/configmap.yaml`（非敏感）或
   `k8s/secret.example.yaml`（敏感）；
5. 跑 `make test` —— 契约测试会告诉你漏了哪一步。

第 2、3 步忘了，`make test` 会红（不是「部署时才发现」）。
