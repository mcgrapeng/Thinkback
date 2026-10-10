# 常用工程命令入口；生产部署以 Kubernetes 为准，Compose 只用于本地调试。
# IMAGE：本地或生产镜像仓库名称，发布时可覆盖为完整 registry/repository。
IMAGE ?= thinkback
# VERSION：镜像标签，生产建议覆盖为 git SHA、语义版本或不可变构建号。
VERSION ?= 0.1.0
# K8S_DIR：Kustomize 部署目录，环境 overlay 可覆盖为其他目录。
K8S_DIR ?= k8s
.PHONY: fmt format-check lint typecheck test test-fast coverage check test-ui test-e2e test-fuzz eval audit load-smoke mutation-audit hooks-install hooks-run dev dev-up dev-down image-build k8s-apply k8s-delete proto-gen migration-up migration-downgrade-1 migration-status migration-history migration-revision

proto-gen:
	uv run --group dev python -m grpc_tools.protoc \
		-I proto \
		--python_out=src/thinkback/rpc \
		--grpc_python_out=src/thinkback/rpc \
		--pyi_out=src/thinkback/rpc \
		proto/memory.proto
	sed -i '' 's/^import memory_pb2 as memory__pb2$$/from thinkback.rpc import memory_pb2 as memory__pb2/' \
		src/thinkback/rpc/memory_pb2_grpc.py

fmt:
	uv run --group dev ruff format .

format-check:
	uv run --group dev ruff format --check .

lint:
	uv run --group dev ruff check .

typecheck:
	uv run --group dev mypy src

test:
	uv run --group dev pytest -v

# 本地快环：testmon 只跑受本次改动影响的用例（提交前仍以 make check 为准）
test-fast:
	uv run --group dev pytest --testmon -q

# 覆盖率门禁：低于 92% 直接失败，防止测试退化无声通过。
# 提高门限时请同步补测试，不要下调阈值迁就缺失覆盖。
COVERAGE_FLOOR ?= 92

coverage:
	uv run --group dev pytest --cov=thinkback --cov-report=term-missing --cov-report=xml --cov-fail-under=$(COVERAGE_FLOOR) -v

check: format-check lint typecheck coverage

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

# API 契约模糊测试（schemathesis，审计层：不进 check，分钟级）
test-fuzz:
	uv run --group dev pytest tests/fuzz -q

# 供应链 CVE 扫描（需网络查漏洞库；进 CI 门禁，check 层保持离线）
audit:
	uv run --group dev pip-audit

# k6 冒烟压测：需真实后端 + API Key（BASE_URL/API_KEY 环境变量）
load-smoke:
	k6 run tests/load/smoke.k6.js

# 变异测试审计：范围见 pyproject [tool.mutmut]，结果用 mutmut results 查看
mutation-audit:
	uv run --group dev mutmut run --max-children 8
	uv run --group dev mutmut results

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
