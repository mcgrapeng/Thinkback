# 常用工程命令入口；生产部署以 Kubernetes 为准，Compose 只用于本地调试。
# IMAGE：本地或生产镜像仓库名称，发布时可覆盖为完整 registry/repository。
IMAGE ?= innies-memory
# VERSION：镜像标签，生产建议覆盖为 git SHA、语义版本或不可变构建号。
VERSION ?= 0.1.0
# K8S_DIR：Kustomize 部署目录，环境 overlay 可覆盖为其他目录。
K8S_DIR ?= k8s
.PHONY: fmt format-check lint typecheck test coverage check hooks-install hooks-run dev-up dev-down image-build k8s-apply k8s-delete proto-gen migration-up migration-downgrade-1 migration-status migration-history migration-revision

proto-gen:
	uv run --extra dev python -m grpc_tools.protoc \
		-I proto \
		--python_out=src/innies_memory/rpc \
		--grpc_python_out=src/innies_memory/rpc \
		--pyi_out=src/innies_memory/rpc \
		proto/memory.proto
	sed -i '' 's/^import memory_pb2 as memory__pb2$$/from innies_memory.rpc import memory_pb2 as memory__pb2/' \
		src/innies_memory/rpc/memory_pb2_grpc.py

fmt:
	uv run --extra dev ruff format .

format-check:
	uv run --extra dev ruff format --check .

lint:
	uv run --extra dev ruff check .

typecheck:
	uv run --extra dev mypy .

test:
	uv run --extra dev pytest -v

coverage:
	uv run --extra dev pytest --cov=innies_memory --cov-report=term-missing --cov-report=xml -v

check: format-check lint typecheck coverage

hooks-install:
	uv run --extra dev pre-commit install

hooks-run:
	uv run --extra dev pre-commit run --all-files

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
