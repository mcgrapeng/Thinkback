import subprocess
from pathlib import Path

TARGET_MEMORY_DOCS = [
    Path("docs/AI虚拟社交三层记忆工程实现版.md"),
    Path("docs/AI虚拟社交三层记忆架构.md"),
    Path("docs/AI虚拟社交记忆服务性能与稳定性测试方案.md"),
    Path("docs/AI虚拟社交记忆服务技术栈选型.md"),
    Path("docs/AI虚拟社交记忆服务数据库迁移指南.md"),
    Path("docs/AI虚拟社交记忆服务部署指南.md"),
    Path("docs/AI虚拟社交记忆服务项目结构.md"),
    Path("docs/AI虚拟社交记忆服务首版主链路质量评测报告字段.md"),
    Path("docs/AI虚拟社交记忆服务首版主链路质量评测报告模板.md"),
    Path("docs/AI虚拟社交记忆服务首版主链路质量评测方案.md"),
]


def test_alembic_scaffold_wires_sqlalchemy_metadata() -> None:
    env_py = Path("alembic/env.py")
    alembic_ini = Path("alembic.ini")

    assert env_py.exists()
    assert alembic_ini.exists()
    content = env_py.read_text(encoding="utf-8")
    assert "from infra.database.base import Base" in content
    assert "target_metadata = Base.metadata" in content


def test_db_migration_script_exists() -> None:
    script = Path("script/db_migrate.py")

    assert script.exists()
    assert "alembic" in script.read_text(encoding="utf-8")


def test_compose_declares_expected_services_without_object_storage() -> None:
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")

    for service in ["app", "postgres", "redis"]:
        assert f"  {service}:" in compose
    assert "  worker:" not in compose
    assert "qdrant:" not in compose
    assert "QDRANT_URL: ${DOCKER_QDRANT_URL:-http://host.docker.internal:6333}" in compose
    assert "POSTGRES_DB: ${POSTGRES_DATABASE:-liaoriver_memory}" in compose
    assert "minio" not in compose.lower()


def test_compose_app_overrides_container_internal_middleware_endpoints() -> None:
    compose_config = subprocess.run(
        ["docker", "compose", "config"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert "POSTGRES_HOST: postgres" in compose_config
    assert 'POSTGRES_PORT: "5432"' in compose_config
    assert "REDIS_HOST: redis" in compose_config
    assert 'REDIS_PORT: "6379"' in compose_config
    assert "QDRANT_URL: http://host.docker.internal:6333" in compose_config
    assert "POSTGRES_DATABASE: liaoriver_memory" in compose_config


def test_makefile_declares_core_commands() -> None:
    makefile = Path("Makefile").read_text(encoding="utf-8")

    for target in ["install:", "dev:", "run:", "test:", "lint:", "format:"]:
        assert target in makefile
    assert "worker:" not in makefile


def test_makefile_declares_local_debug_commands() -> None:
    makefile = Path("Makefile").read_text(encoding="utf-8")

    assert "-include .env" in makefile
    assert ".env.local" not in makefile
    assert "export" in makefile
    for target in [
        "debug-api:",
        "debug-ready:",
        "quality-real:",
        "verify-local:",
    ]:
        assert target in makefile
    assert "debug-worker:" not in makefile
    assert "POSTGRES_DATABASE=liaoriver_memory" in makefile
    assert "script/run_real_mem0_quality_evaluation.py --docs-dir docs/report" in makefile


def test_env_example_declares_runtime_settings() -> None:
    env_example = Path(".env.example").read_text(encoding="utf-8")

    for variable in [
        "POSTGRES_HOST=",
        "REDIS_HOST=",
        "QDRANT_URL=",
        "OPENAI_API_KEY=",
        "MEMORY_OPENAI_BASE_URL=",
        "MEMORY_QDRANT_COLLECTION=",
        "MEMORY_EMBEDDING_DIMS=",
        "MEMORY_LLM_MODEL=",
        "MEMORY_EMBEDDING_MODEL=",
        "MEM0_HISTORY_DB_PATH=",
        "READINESS_TIMEOUT_SECONDS=",
        "MEMORY_API_WORKER_LIMIT=",
        "MEMORY_L3_EXECUTOR_WORKERS=",
        "MEMORY_L3_MAX_PENDING_TASKS=",
        "MEMORY_L3_QUEUE_WAIT_SECONDS=",
        "QUALITY_EVALUATION_TIMEOUT_SECONDS=",
    ]:
        assert variable in env_example
    assert "QDRANT_API_KEY=" in env_example
    assert "POSTGRES_DATABASE=liaoriver_memory" in env_example
    assert "CELERY_BROKER_URL=" not in env_example
    assert "CELERY_RESULT_BACKEND=" not in env_example
    assert "MEM0_API_URL=" not in env_example
    assert "MEM0_API_KEY=" not in env_example
    assert "MEM0_HTTP_TIMEOUT_SECONDS=" not in env_example
    assert "MEMORY_LLM_API_KEY=" not in env_example
    assert "THINKBACK_API_URL=http://127.0.0.1:18082" in env_example
    assert "QDRANT_URL=http://localhost:6333" in env_example
    assert "MEMORY_L3_WRITE_MODE=sync" in env_example


def test_runtime_config_files_include_chinese_operator_comments() -> None:
    config_files = [
        Path(".env.example"),
        Path("docker-compose.yml"),
        Path("k8s/configmap.yaml"),
        Path("k8s/secret.example.yaml"),
        Path("k8s/deployment-api.yaml"),
        Path("k8s/job-migrate.yaml"),
        Path(".github/workflows/ci.yml"),
    ]

    for path in config_files:
        content = path.read_text(encoding="utf-8")
        assert "# 中文注释：" in content, path


def test_env_example_documents_every_variable_with_chinese_comment() -> None:
    missing_comments = _variables_without_previous_chinese_comment(Path(".env.example"), separator="=")

    assert missing_comments == []


def test_runtime_config_templates_document_every_environment_variable() -> None:
    missing_comments: dict[str, list[str]] = {}
    for path in [
        Path("docker-compose.yml"),
        Path("k8s/configmap.yaml"),
        Path("k8s/secret.example.yaml"),
    ]:
        missing = _variables_without_previous_chinese_comment(path, separator=":")
        if missing:
            missing_comments[str(path)] = missing

    assert missing_comments == {}


def _variables_without_previous_chinese_comment(path: Path, *, separator: str) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    missing_comments: list[str] = []

    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or separator not in stripped:
            continue
        variable = stripped.split(separator, maxsplit=1)[0]
        if not variable.isupper() or not all(char.isalnum() or char == "_" for char in variable):
            continue
        previous = lines[index - 1].strip() if index > 0 else ""
        if not previous.startswith("# 中文注释："):
            missing_comments.append(variable)
    return missing_comments


def test_quality_and_runtime_docs_explain_referenced_environment_variables() -> None:
    docs_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in [
            Path("README.md"),
            Path("k8s/README.md"),
            Path("script/README.md"),
            Path("docs/AI虚拟社交记忆服务数据库迁移指南.md"),
            Path("docs/AI虚拟社交记忆服务部署指南.md"),
            Path("docs/AI虚拟社交记忆服务性能与稳定性测试方案.md"),
            Path("docs/AI虚拟社交记忆服务首版主链路质量评测方案.md"),
        ]
    )

    for variable in [
        "OPENAI_API_KEY",
        "QDRANT_URL",
        "QDRANT_API_KEY",
        "DOCKER_QDRANT_URL",
        "THINKBACK_API_URL",
        "PYTHONPATH",
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_DATABASE",
        "REDIS_DB",
        "REDIS_PASSWORD",
        "MEMORY_QDRANT_COLLECTION",
        "MEMORY_LLM_MODEL",
        "MEMORY_EMBEDDING_MODEL",
        "MEM0_HISTORY_DB_PATH",
        "MEMORY_L3_WRITE_MODE",
        "MEMORY_API_WORKER_LIMIT",
        "MEMORY_L3_EXECUTOR_WORKERS",
        "MEMORY_L3_MAX_PENDING_TASKS",
        "MEMORY_L3_QUEUE_WAIT_SECONDS",
        "READINESS_TIMEOUT_SECONDS",
        "QUALITY_EVALUATION_TIMEOUT_SECONDS",
    ]:
        assert f"| `{variable}` |" in docs_text, variable


def test_single_env_example_uses_liaoriver_memory_and_local_middleware() -> None:
    env_example = Path(".env.example").read_text(encoding="utf-8")

    assert not Path(".env.local.example").exists()

    for expected in [
        "POSTGRES_HOST=localhost",
        "POSTGRES_PORT=5432",
        "POSTGRES_DATABASE=liaoriver_memory",
        "REDIS_HOST=localhost",
        "REDIS_PORT=6379",
        "POSTGRES_PASSWORD=postgres",
        "REDIS_PASSWORD=",
        "QDRANT_URL=http://localhost:6333",
        "MEMORY_QDRANT_COLLECTION=memories_qwen_1024",
        "MEMORY_EMBEDDING_DIMS=1024",
        "MEMORY_L3_WRITE_MODE=sync",
        "THINKBACK_API_URL=http://127.0.0.1:18082",
    ]:
        assert expected in env_example


def test_github_actions_ci_runs_quality_gates() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

    for expected in [
        "poetry install",
        "poetry run ruff check src test script",
        "poetry run mypy src",
        "poetry run pytest",
        "docker build -t thinkback:ci -f Dockerfile .",
        "POSTGRES_DB: liaoriver_memory",
        'pg_isready -U postgres -d liaoriver_memory',
    ]:
        assert expected in workflow
    assert "postgres:" in workflow
    assert "redis:" in workflow


def test_ide_workspace_files_are_not_tracked() -> None:
    result = subprocess.run(
        ["git", "ls-files", ".idea"],
        check=True,
        capture_output=True,
        text=True,
    )

    tracked_files = [line for line in result.stdout.splitlines() if line.strip()]
    assert tracked_files == []


def test_k8s_manifests_cover_api_service_and_migration_job() -> None:
    api_deployment = Path("k8s/deployment-api.yaml").read_text(encoding="utf-8")
    service = Path("k8s/service.yaml").read_text(encoding="utf-8")
    migration = Path("k8s/job-migrate.yaml").read_text(encoding="utf-8")

    assert "name: thinkback-api" in api_deployment
    assert "path: /health/ready" in api_deployment
    assert "path: /health/live" in api_deployment
    assert "timeoutSeconds: 3" in api_deployment
    assert "name: thinkback" in service
    assert "name: thinkback-migrate" in migration
    assert not Path("k8s/deployment-worker.yaml").exists()


def test_k8s_has_production_apply_entrypoint() -> None:
    kustomization = Path("k8s/kustomization.yaml").read_text(encoding="utf-8")

    for expected in [
        "configmap.yaml",
        "secret.example.yaml",
        "serviceaccount.yaml",
        "job-migrate.yaml",
        "deployment-api.yaml",
        "service.yaml",
        "poddisruptionbudget.yaml",
        "networkpolicy.yaml",
    ]:
        assert expected in kustomization


def test_k8s_manifests_include_production_controls() -> None:
    api_deployment = Path("k8s/deployment-api.yaml").read_text(encoding="utf-8")
    pdb = Path("k8s/poddisruptionbudget.yaml").read_text(encoding="utf-8")
    service_account = Path("k8s/serviceaccount.yaml").read_text(encoding="utf-8")
    network_policy = Path("k8s/networkpolicy.yaml").read_text(encoding="utf-8")
    migrate_job = Path("k8s/job-migrate.yaml").read_text(encoding="utf-8")

    for manifest in [api_deployment, migrate_job]:
        assert "serviceAccountName: thinkback" in manifest
        assert "allowPrivilegeEscalation: false" in manifest
        assert "readOnlyRootFilesystem: true" in manifest
        assert "resources:" in manifest
        assert "requests:" in manifest
        assert "limits:" in manifest

    assert "kind: PodDisruptionBudget" in pdb
    assert "minAvailable: 1" in pdb
    assert "kind: ServiceAccount" in service_account
    assert "kind: NetworkPolicy" in network_policy
    assert "kind: Job" in migrate_job
    assert "alembic" in migrate_job
    assert "upgrade" in migrate_job


def test_k8s_config_and_secret_examples_include_runtime_settings() -> None:
    configmap = Path("k8s/configmap.yaml").read_text(encoding="utf-8")
    secret = Path("k8s/secret.example.yaml").read_text(encoding="utf-8")

    assert "QDRANT_URL" in configmap
    assert "POSTGRES_DATABASE: liaoriver_memory" in configmap
    assert "emptyDir: {}" in Path("k8s/deployment-api.yaml").read_text(encoding="utf-8")
    assert "emptyDir: {}" in Path("k8s/job-migrate.yaml").read_text(encoding="utf-8")
    assert "QDRANT_API_KEY" in secret
    assert "MEM0_BACKEND_MODE" not in configmap
    assert "MEM0_API_URL" not in configmap
    assert "MEM0_HTTP_TIMEOUT_SECONDS" not in configmap
    assert "OPENAI_API_KEY" in secret
    assert "MEMORY_OPENAI_BASE_URL" in configmap
    assert "MEMORY_QDRANT_COLLECTION" in configmap
    assert "MEMORY_LLM_MODEL" in configmap
    assert "MEMORY_L3_EXECUTOR_WORKERS" in configmap
    assert "MEMORY_L3_MAX_PENDING_TASKS" in configmap
    assert "MEMORY_L3_QUEUE_WAIT_SECONDS" in configmap
    assert "MEMORY_API_WORKER_LIMIT" in configmap
    assert "READINESS_TIMEOUT_SECONDS" in configmap
    assert "QUALITY_EVALUATION_TIMEOUT_SECONDS" not in configmap
    assert "CELERY_BROKER_URL" not in configmap
    assert "CELERY_RESULT_BACKEND" not in configmap
    assert "POSTGRES_PASSWORD" in secret
    assert "MEM0_API_KEY" not in secret
    assert "MEMORY_LLM_API_KEY" not in secret


def test_deployment_guide_keeps_quality_timeout_out_of_api_runtime_config() -> None:
    guide = Path("docs/AI虚拟社交记忆服务部署指南.md").read_text(encoding="utf-8")

    assert "生产 API 运行环境必须显式设置" in guide
    assert "评测脚本运行环境" in guide
    api_runtime_section = guide.split("评测脚本运行环境", maxsplit=1)[0]
    assert "QUALITY_EVALUATION_TIMEOUT_SECONDS" not in api_runtime_section
    assert "QUALITY_EVALUATION_TIMEOUT_SECONDS=900" in guide
    assert "POSTGRES_DATABASE=liaoriver_memory" in api_runtime_section
    assert "| `REDIS_DB` | Redis DB 编号；首版默认使用 `0`。" in api_runtime_section
    assert "| `REDIS_PASSWORD` | Redis 密码，生产如启用鉴权必须放在 Secret。" in api_runtime_section


def test_deployment_guide_prioritizes_quality_gate_over_smoke_script() -> None:
    guide = Path("docs/AI虚拟社交记忆服务部署指南.md").read_text(encoding="utf-8")
    validation_section = guide.split("## 6. 真实主链路验证", maxsplit=1)[1]

    assert validation_section.index("make quality-real") < validation_section.index("script/real_mem0_pressure.py")
    assert "如果只想做早期 5 轮 smoke 验证" in validation_section


def test_script_directory_documents_operational_entrypoints() -> None:
    readme = Path("script/README.md").read_text(encoding="utf-8")

    for expected in [
        "db_migrate.py",
        "run_real_mem0_quality_evaluation.py",
        "real_mem0_quality_regression.py",
        "real_mem0_p0_preprod_pressure.py",
    ]:
        assert expected in readme
    assert "稳定性" in readme
    assert "质量评测" in readme


def test_core_runtime_files_include_chinese_reasoning_comments() -> None:
    core_files = [
        Path("src/api/memory.py"),
        Path("src/infra/config.py"),
        Path("src/memory/service.py"),
    ]

    for path in core_files:
        content = path.read_text(encoding="utf-8")
        assert "# 中文注释：" in content, path


def test_logger_messages_remain_english() -> None:
    source_files = [*Path("src").rglob("*.py"), *Path("script").rglob("*.py")]

    for path in source_files:
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("logger.") or " logger." in stripped:
                assert not any("\u4e00" <= char <= "\u9fff" for char in line), (
                    path,
                    line_number,
                    line,
                )


def test_docs_do_not_reference_removed_worker_or_stability_entrypoint() -> None:
    docs_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in [
            Path("README.md"),
            Path("docs/AI虚拟社交记忆服务技术栈选型.md"),
            Path("docs/AI虚拟社交记忆服务部署指南.md"),
            Path("docs/AI虚拟社交记忆服务项目结构.md"),
            Path("docs/AI虚拟社交记忆服务性能与稳定性测试方案.md"),
        ]
    )

    assert "Redis + Celery" not in docs_text
    assert "Celery 已有" not in docs_text
    assert "real_mem0_stability_preprod.py" not in docs_text
    assert "tasks/celery_app.py" not in docs_text
    assert "CELERY_BROKER_URL" not in docs_text
    assert "CELERY_RESULT_BACKEND" not in docs_text


def test_target_memory_docs_use_current_runtime_contract() -> None:
    docs_text = "\n".join(path.read_text(encoding="utf-8") for path in TARGET_MEMORY_DOCS)

    for removed_contract in [
        "/v1/memory",
        "/healthz",
        "/readyz",
        "/memory/affected",
        ".env.local",
        "env.local",
        "real_mem0_stability_preprod.py",
        "CELERY_BROKER_URL",
        "CELERY_RESULT_BACKEND",
        "Redis + Celery",
        "message_content",
        "retrieval_query",
        "delete_scope",
        "rebuild_scope",
        "degraded_layers",
        "failed_layer_reason",
    ]:
        assert removed_contract not in docs_text

    for current_contract in [
        "POST /memory/append",
        "POST /memory/recall",
        "POST /memory/delete",
        "POST /memory/rebuild",
        "GET /memory/tasks/{task_id}",
        "GET /memory/l3/background-status",
        "GET /health",
        "GET /health/live",
        "GET /health/ready",
        "POSTGRES_DATABASE=liaoriver_memory",
    ]:
        assert current_contract in docs_text


def test_docs_document_current_l3_background_status_fields() -> None:
    docs_text = "\n".join(path.read_text(encoding="utf-8") for path in TARGET_MEMORY_DOCS)

    for field in [
        "write_mode",
        "executor_workers",
        "max_pending_tasks",
        "pending_write_tasks",
        "cleanup_tasks",
        "available_capacity",
    ]:
        assert field in docs_text


def test_quality_docs_match_current_report_directory_and_wrapper_fields() -> None:
    docs_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in [
            Path("docs/AI虚拟社交记忆服务首版主链路质量评测报告字段.md"),
            Path("docs/AI虚拟社交记忆服务首版主链路质量评测报告模板.md"),
            Path("docs/AI虚拟社交记忆服务首版主链路质量评测方案.md"),
        ]
    )

    assert "--docs-dir docs/report" in docs_text
    assert "postgres_database" in docs_text
    assert "liaoriver_memory" in docs_text
    assert "YYYYMMDD-递增序号" in docs_text


def test_quality_docs_explain_metric_rationale_without_overclaiming() -> None:
    docs_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in [
            Path("docs/AI虚拟社交记忆服务首版主链路质量评测报告字段.md"),
            Path("docs/AI虚拟社交记忆服务首版主链路质量评测报告模板.md"),
            Path("docs/AI虚拟社交记忆服务首版主链路质量评测方案.md"),
        ]
    )

    for expected in [
        "符合主流方向",
        "没有过度设计",
        "没有明显遗漏 P0 质量风险",
        "指标值合理",
        "不代表广义生产级泛化覆盖已经充分",
        "`known_drift_regression_pass_rate` | 硬门禁",
        "`delete_session_residue_rate`",
        "`delete_all_residue_rate`",
        "`precision_at_10` 与 `item_precision_at_10` 的区别",
    ]:
        assert expected in docs_text

    assert "已覆盖生产级质量风险" not in docs_text


def test_removed_celery_runtime_is_not_a_dependency() -> None:
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8").lower()
    lockfile = Path("poetry.lock").read_text(encoding="utf-8").lower()

    assert "celery" not in pyproject
    assert "name = \"celery\"" not in lockfile


def test_readme_documents_local_debug_and_production_scaffold() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")

    for expected in [
        "cp .env.example .env",
        "make debug-api",
        "make debug-ready",
        "make quality-real",
        "make verify-local",
        "GitHub Actions",
        "PodDisruptionBudget",
        "NetworkPolicy",
        "job-migrate.yaml",
        "kubectl apply -k k8s",
        "liaoriver_memory",
    ]:
        assert expected in readme
