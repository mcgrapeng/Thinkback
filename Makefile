# 常用工程命令入口；生产部署以 Kubernetes 为准，Compose 只用于本地调试。
# IMAGE：本地或生产镜像仓库名称，发布时可覆盖为完整 registry/repository。
IMAGE ?= thinkback
# VERSION：镜像标签，生产建议覆盖为 git SHA、语义版本或不可变构建号。
VERSION ?= 0.1.0
# K8S_DIR：Kustomize 部署目录，环境 overlay 可覆盖为其他目录。
K8S_DIR ?= k8s
.PHONY: fmt format-check lint typecheck test test-fast test-baseline coverage check test-ui test-e2e test-fuzz eval audit load-smoke mutation-audit hooks-install hooks-run dev dev-up dev-down image-build k8s-render k8s-apply k8s-delete proto-gen migration-up migration-downgrade-1 migration-status migration-history migration-revision

proto-gen:
	# 固定生成器版本：不固定时 grpcio-tools 升级会产出大量无关 diff。
	# 生成后跑 ruff format —— 仓库里的生成代码是格式化过的。
	uv run --with "grpcio-tools==1.69.0" python -m grpc_tools.protoc \
		-I proto \
		--python_out=src/thinkback/rpc \
		--grpc_python_out=src/thinkback/rpc \
		--pyi_out=src/thinkback/rpc \
		proto/memory.proto
	sed -i '' 's/^import memory_pb2 as memory__pb2$$/from thinkback.rpc import memory_pb2 as memory__pb2/' \
		src/thinkback/rpc/memory_pb2_grpc.py
	uv run --group dev ruff format src/thinkback/rpc/

fmt:
	uv run --group dev ruff format .

format-check:
	uv run --group dev ruff format --check .

lint:
	uv run --group dev ruff check .

typecheck:
	uv run --group dev mypy src

test:
	uv run --group dev pytest -v --junitxml=junit.xml

# 用例防回血基线：删掉一条用例必须是个显式动作（连带更新基线文件）。
# 变异基线盯的是「变异体有没有被杀掉」，看不见用例本身被删。
# 复用 test 那次跑的 junit 报告：门禁校验的是**同一场**跑的用例。再跑一遍
# 等于拿另一场的结果对基线 —— flaky 会被放大成两次不同的结论。
test-baseline: test
	uv run --group dev python tests/eval/check_pytest_baseline.py --junit junit.xml

# 本地快环：testmon 只跑受本次改动影响的用例（提交前仍以 make check 为准）
test-fast:
	uv run --group dev pytest --testmon -q

# 顺序依赖检查：把整份单测按随机顺序再跑一遍。
#
# 「单独跑绿、跟别人一起跑红」的用例是变异审计的隐形杀手：mutmut 按覆盖图
# 跑用例（按名字排序），顺序跟文件里的定义顺序不一样。第十轮实测：一条
# gRPC 用例在乱序下挑到了被下架的记忆 → UpdateMemory 报 not found → 那一批
# 变异体全被判成「被杀」，结论是假的
# （docs/report/2026-11-01-thinkback-round10-testing-sweep.md §2.4）。
#
# 不新增插件文件：先收集用例 id，按种子打乱，再逐条跑。
ORDER_SEEDS ?= 20261101 20261102

test-order:
	@for seed in $(ORDER_SEEDS); do \
		echo "== 顺序依赖检查 seed=$$seed"; \
		uv run --group dev pytest -o addopts= --collect-only -q tests/unit \
		| uv run --group dev python -c "import random,sys;items=[l.strip() for l in sys.stdin if '::' in l];random.Random($$seed).shuffle(items);sys.stdout.write('\0'.join(items))" \
		| xargs -0 uv run --group dev pytest -q || exit 1; \
	done

# 覆盖率观测（非阻断）。行覆盖率不再当门禁：_verify_secret 那个恒真函数
# 在 95.9% 覆盖率下活着——覆盖率衡量「这行被执行过」，不衡量「结果对不对」。
# COVERAGE_FLOOR 只作为观测水位打印，不让 make coverage 失败。
COVERAGE_FLOOR ?= 92

coverage:
	uv run --group dev pytest --cov=thinkback --cov-report=term-missing --cov-report=xml -v
	@uv run --group dev python -c "import xml.etree.ElementTree as ET;f=float('$(COVERAGE_FLOOR)');r=float(ET.parse('coverage.xml').getroot().get('line-rate',0))*100;print(f'行覆盖率 {r:.1f}%，观测水位 {f:.0f}%（非阻断）')" || echo "覆盖率报告不可用（非阻断）"

# 完成定义：格式 / lint / 类型 / 单元测试 / 顺序依赖 / 变异审计 / 评测 全绿。
# 单元测试进 check：否则单测红着 check 照样绿 —— 门禁漏门。
# test-order 进 check：顺序依赖的用例「单独跑绿、一起跑红」，不换顺序
# 发现不了；而它们会让变异审计的整批判定变假（见 test-order 的注释）。
# 全程离线确定：不触真实 PG / Milvus / LLM，不需要任何凭证。
check: format-check lint typecheck test test-baseline test-order mutation-audit eval

# 前端组件测试（vitest，确定性无网络）
test-ui:
	cd web && npm run test-ui

# 管理台端到端冒烟（Playwright + VITE_USE_MOCK，自动起 vite dev）
test-e2e:
	uv run --group dev pytest tests/e2e -v

# 自包含质量评测（确定性 fakes）+ 基线门禁；真实链路评测见 tests/script/
eval:
	uv run --group dev python tests/script/run_self_contained_evaluation.py
	uv run --group dev python tests/script/eval/check_baseline.py
	uv run --group dev python tests/script/eval/run_semiconductor_evaluation.py
	uv run --group dev python tests/script/eval/check_baseline.py \
		--report-dir docs/memory/report/semiconductor \
		--baseline tests/eval/baseline_semiconductor.json

# 真实栈门禁：本地必跑，不进 CI（真实 Mem0/Milvus 契约与质量）
# 需要真实 Milvus + 可达的 LLM/embedding endpoint + 可写的 PG。
gate-real:
	uv run --group dev python tests/script/gate_real.py

# API 契约模糊测试（schemathesis，审计层：不进 check，分钟级）
test-fuzz:
	uv run --group dev pytest tests/fuzz -q

# 供应链 CVE 扫描（需网络查漏洞库；进 CI 门禁，check 层保持离线）
audit:
	uv run --group dev pip-audit

# k6 冒烟压测：需真实后端（BASE_URL 指向被测服务）。
# 被测服务的单 IP 限流要调高：压测测的是服务容量，不是限流器
# （MEMORY_RATE_LIMIT_PER_MINUTE=600000 MEMORY_RATE_LIMIT_BURST=10000）。
load-smoke:
	k6 run tests/load/smoke.k6.js

# 变异测试审计：范围见 pyproject [tool.mutmut]。
# mutmut 3.8 即使有存活变异体也退出 0 —— 光跑 mutmut 不算门禁。
# 末步拿 tests/eval/mutation_survivors.json（已验收存活 + 理由）比对，
# 出现**新增**存活 / 无测试变异体即失败。
# 第一步先自愈：mutmut 的「函数↔测试」映射缓存不含测试文件内容，改了测试
# 映射还是旧的，会把有覆盖的变异体误判成「无测试」。
#
# **必须在自己的副本里跑**：mutmut 的状态与变异体都记在 mutants/ 树里，
# 两个 mutmut 实例共用同一棵树会互相写坏（实测：另一个 agent 的审计进程
# 在跑，本进程就静默卡在 5117/5126 不动，所有 worker 停在 0.3% CPU，
# 没有任何报错）。MUTATION_AUDIT_DIR 指向一个隔离副本即可；不设时用
# 仓库内的树（单机单 agent 时没问题）。
#
# **同一台机器上只跑一个审计**：两个审计同时跑（哪怕在不同副本里）会把机器
# 的内存吃干 —— 实测两轮各 8 个 worker 时 swap 用掉 20 GB / 21.5 GB，
# 物理内存只剩 38 MB，审计进程被 OOM 杀掉且**没有任何报错**（进度条停在
# 3802/5083，进程直接消失）。机器内存不够时把 --max-children 调小
# （本轮用 4），或者等另一个审计跑完再跑。
#
# 审计期间**不要改 tests/ 下的任何 .py**：门禁对整个测试树取内容指纹
# （tests/eval/refresh_mutation_stats.py），改了就得重跑整棵审计。
MUTATION_AUDIT_DIR ?= .

mutation-audit:
	cd $(MUTATION_AUDIT_DIR) && \
	  uv run --group dev python tests/eval/refresh_mutation_stats.py && \
	  uv run --group dev mutmut run --max-children 8 && \
	  uv run --group dev mutmut results && \
	  uv run --group dev python tests/eval/check_mutation_survivors.py

# 本地开发：后端 7002 / 前端 7001 起步，占用自动顺延（script/dev.py）
dev:
	uv run python script/dev.py

hooks-install:
	uv run --group dev pre-commit install

hooks-run:
	uv run --group dev pre-commit run --all-files

dev-up:
	docker compose up

dev-down:
	docker compose down

image-build:
	docker build -t $(IMAGE):$(VERSION) .

k8s-render:
	# 只渲染不应用：发布前先确认清单能过 kustomize 构建。
	kubectl kustomize $(K8S_DIR)

k8s-apply:
	kubectl apply -k $(K8S_DIR)

k8s-delete:
	kubectl delete -k $(K8S_DIR)

# 应用最新迁移（与启动时 lifespan 内自动迁移等价，便于手动/部署前使用）
migration-up:
	PYTHONPATH=src uv run alembic upgrade head

# 回退一个版本（默认回退 1 个 revision）
migration-downgrade-1:
	PYTHONPATH=src uv run alembic downgrade -1

# 查看当前 head 与 applied revision
migration-status:
	PYTHONPATH=src uv run alembic current

# 查看迁移历史
migration-history:
	PYTHONPATH=src uv run alembic history --verbose

# 生成新迁移：make migration-revision m="add xxx"
migration-revision:
	PYTHONPATH=src uv run alembic revision -m "$(m)"
