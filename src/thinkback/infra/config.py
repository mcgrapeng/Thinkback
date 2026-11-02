"""Runtime configuration for thinkback.

通过 pydantic-settings 从环境变量 / ``.env`` 文件加载配置，并集中校验。
生产环境（``ENVIRONMENT=production``）默认禁用 ``.env`` 兜底，强制
只用环境变量，避免开发配置污染线上。

R-0 修复：``DATABASE_URL``（云平台通用变量名）现在通过 validation_alias
同时支持环境变量与 ``.env`` 文件。此前它只在恰好有人（如 pymilvus 的
import 副作用 ``load_dotenv()``）把 ``.env`` 灌进 ``os.environ`` 时才生效，
导致数据库配置是否被读取取决于 settings 单例的创建时机。
"""

import os
from functools import lru_cache
from typing import Any, Literal
from urllib.parse import quote_plus

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全量运行配置。

    分组：
    - 应用基础：app_name / environment / debug / log_level
    - 存储：postgres / milvus
    - 记忆后端：LLM / Embedding / Mem0 / Milvus collection
    - 写入调度：L3 sync/async、线程池大小、队列上限
    - API 网关：worker 上限、等待时间
    - gRPC：监听地址、并发上限

    说明：短期记忆（L1/L2）只落 PostgreSQL；服务不依赖任何外部缓存或
    消息队列组件。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    def __init__(self, **values: Any) -> None:
        # 生产环境强制忽略 .env，避免开发配置泄漏
        if os.environ.get("ENVIRONMENT") == "production" and "_env_file" not in values:
            values["_env_file"] = None
        super().__init__(**values)

    # ── 应用基础 ──────────────────────────────────────────────────────
    app_name: str = "thinkback"
    app_version: str = "0.1.0"
    environment: Literal["development", "staging", "production"] = "development"
    debug: bool = False
    log_level: str = "INFO"

    # ── 存储 ──────────────────────────────────────────────────────────
    # AliasChoices 让 DATABASE_URL 在环境变量与 .env 文件两条通道都生效
    # （字段本名 raw_database_url 仍可作为 init kwarg / 环境变量名使用）。
    raw_database_url: str = Field(
        default="",
        validation_alias=AliasChoices("raw_database_url", "DATABASE_URL"),
    )
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "postgres"
    postgres_password: str = "postgres"
    postgres_database: str = "thinkback"

    milvus_url: str = "http://localhost:19530"
    milvus_database: str = "default"
    milvus_user: str = ""
    milvus_password: str = ""

    # ── 记忆后端 ──────────────────────────────────────────────────────
    memory_llm_key: str = Field(
        default="",
        description="LLM API key; optional — leave empty when the endpoint needs no auth",
    )
    memory_llm_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    memory_embedding_base_url: str = "http://embedding.example.internal:7345/v1"
    memory_embedding_api_key: str = Field(
        default="",
        description="Embedding API key; optional — leave empty when the endpoint needs no auth",
    )
    memory_milvus_collection: str = "thinkback"
    # Milvus ``text`` 字段的分析器。集合由 Thinkback 预建（ADR-0007）：
    # mem0 自建集合不带 analyzer_params，中文整句被当成一个词，BM25 对中文恒为 0。
    # Milvus 不允许原地改分析器，改不动就走迁移重建（script/migrate_milvus_collection.py）。
    memory_milvus_analyzer: str = "chinese"
    # 迁移窗口的次集合（双写 + 双读回落）。留空表示迁移不在窗口内。
    # 回滚只需清掉它、把 MEMORY_MILVUS_COLLECTION 改回旧集合名。
    memory_milvus_mirror_collection: str = ""
    # ── 部署形态 ──────────────────────────────────────────────────────
    # 单副本是当前的发布边界。多副本要 HA，五项前置必须先落地（infra.replica_guard）：
    # 前置没齐时服务在启动期硬失败，而不是静默双写。
    memory_multi_replica: bool = False
    # 进程内 L1 缓存。多副本部署必须关（关掉后走读穿透，每次回源 PG）。
    memory_l1_cache_enabled: bool = True
    # mem0 的 history.db 是否挂在共享卷上（多副本部署必须共享，否则擦除只清本副本）。
    memory_history_db_shared: bool = False
    # ── 保留期物理清理（ADR-0010 第三段）───────────────────────────────
    # 作业默认关闭：开了就要有 CronJob 或人工定期跑（script/run_retention_cleanup.py）。
    memory_retention_enabled: bool = False
    # 到期后再等几天才物理删除：让「用户改口」能挽回。
    memory_retention_grace_days: int = 7
    # 单批清理条数上限。
    memory_retention_batch_size: int = 500
    # 只清理某个 namespace；留空表示全部。
    memory_retention_namespace: str = ""
    memory_embedding_dims: int = 1024
    memory_llm_model: str = "qwen-plus-latest"
    memory_embedding_model: str = "default-embedding"
    mem0_history_db_path: str = ".mem0/history.db"
    # 记忆类型注册表 profile 目录：每个 namespace 一个 JSON（<namespace>.json）。
    # 可以是逗号分隔的多个目录，按顺序找第一个命中的 —— 企业自己的 profile
    # 挂在一个目录、镜像内置的在另一个目录，企业新增记忆类型不必重建镜像。
    # 所有目录都没有对应文件时回落到内置默认注册表。
    memory_type_profile_dir: str = "profiles"
    # mem0.add() 是否走 LLM 事实抽取（要 chat/completions endpoint）。
    # 设为 False 时退化为纯向量存储：raw message 落地为一条记忆，
    # thinkback 的 P0 槽位逻辑负责结构化抽取。
    # 适用场景：embedding-only endpoint、LLM 暂不可用、生产灰度观察。
    memory_infer_facts: bool = True

    # ── 写入调度 ──────────────────────────────────────────────────────
    memory_l3_write_mode: Literal["sync", "async"] = "async"
    memory_l3_executor_workers: int = 16
    memory_l3_max_pending_tasks: int = 256
    memory_l3_queue_wait_seconds: float = 5.0
    memory_backend_max_concurrent_calls: int = 4

    # ── L2 综合摘要（P0：LLM 化）───────────────────────────────────────
    # 关闭后 L2 保持拼接式降级实现（行为与 V1 相同）。
    memory_l2_llm_enabled: bool = True
    # 去抖间隔：每个会话作用域累计 N 个 append 触发一次后台 LLM 刷新。
    memory_l2_refresh_interval_rounds: int = 5
    # 单次 LLM 调用超时与输出上限（摘要场景无需长输出）。
    memory_l2_llm_timeout_seconds: float = 30.0
    memory_l2_llm_max_tokens: int = 512

    # ── 遗忘 decay（P2#7；默认关闭，按产品需求启用）─────────────────
    # decay = 老且久未召回的长尾记忆置 SUPPRESSED（不可达而非删除）。
    # 槽位关键事实/墓碑行受保护；被召回即强化（touch_memory_recalled）。
    memory_decay_enabled: bool = False
    # 事实年龄下限（天）：valid_at 早于 now-N 天才可能衰减。
    memory_decay_min_age_days: int = 90
    # 久未召回阈值（天）：last_recalled_at 为空或早于 now-N 天才可能衰减。
    memory_decay_unrecalled_days: int = 60
    # 全局清扫最小间隔（秒）：append 触发的时间门控，防止清扫风暴。
    memory_decay_sweep_interval_seconds: float = 3600.0

    # ── API 网关 ──────────────────────────────────────────────────────
    memory_api_worker_limit: int = 8
    # 单客户端 IP 的限流（业务面 /memory/*）。按 IP 分桶是因为内网免鉴权，
    # 没有调用方身份可用。压测要测的是服务容量、不是限流器，因此这两个值
    # 可配置：压测时调高，生产保持默认。
    memory_rate_limit_per_minute: int = 600
    memory_rate_limit_burst: int = 50
    # 读写路径的等待预算（各自独立的信号量与线程池，这里只是统一的超时值）。
    memory_api_worker_wait_seconds: float = 5.0
    # 本部署服务的 namespace 白名单（逗号分隔）。空 = 不限制。
    #
    # 为什么需要它：内网免鉴权（D10）意味着「能连到本服务就能读写任何
    # namespace 下的记忆」。插件形态下最安全的部署形状是**一个部署服务一个
    # 租户**；这个白名单把那条约定变成可执行的配置，而不是靠人记住。
    # 注意它**不是**访问控制：白名单内的 namespace 里，user_id 仍由调用方
    # 自报，能连到本服务的人仍能读到这个 namespace 的全部记忆。真正的边界
    # 仍然是网络边界（见 docs/API.md 的信任模型一节）。
    memory_allowed_namespaces: str = ""
    # 受信调用方身份头（D1 接入层身份绑定）：部署方前面的网关（带鉴权的
    # 反代 / service mesh）注入的 HTTP 头 / gRPC metadata 键名。空 = 不启用
    # （内网信任边界 = 网络边界，D10）。非空时 /v2/memory/* 里调用方自报的
    # user_id 必须与该头一致，头缺失或不一致一律 403 / PERMISSION_DENIED ——
    # 静默改写会掩盖「助手以为在写 A、实际写了 B」的配置错误，响亮拒绝更安全。
    trusted_user_header: str = ""
    readiness_timeout_seconds: float = 3.0
    # 启动回收阈值：running 任务超过该秒数无任何更新（updated_at 停滞），
    # 在服务启动时被回收为 failed（进程被硬杀留下的孤儿任务，否则客户端
    # 会永远轮询 running）。必须显著大于最长后台任务时长（L3 抽取分钟级）。
    task_orphan_running_seconds: float = 1800.0

    # P0 本地索引白名单：控制 mem0 抽出的 L3 事件是否进本地 MemoryRecord 表。
    # 留空（默认）= 所有 L3 记忆都进本地表，/items 与 /recall 全量可见。
    # 非空 = 只放行 slot 在该列表中的记忆，例如：
    #   MEMORY_P0_SLOTS=preferred_nickname,pet_name:cat,current_location
    # ⚠️ 注意：未列入白名单的 slot 仍写入 mem0，但 **不会进入 /recall 召回结果**
    # （recall 只返回本地索引中 ACTIVE 的记忆）。即白名单外的记忆在 Milvus 中
    # 是不可召回的存量数据；如需彻底不放行，应在准入校验处直接拒绝而非依赖白名单。
    # 注意：用 ``str`` 而非 ``list[str]``，避开 pydantic-settings 对 .env 里的空串
    # 走 ``json.loads`` 失败的坑；解析放在 ``memory_p0_slots_list`` 属性里。
    memory_p0_slots: str = ""

    # ── gRPC ──────────────────────────────────────────────────────────
    grpc_enabled: bool = True
    # ── 管理后台（治理台 M1）────────────────────────────────────────
    # 治理台 SPA 的 CORS 白名单（逗号分隔 origin，空 = 不启用 CORS 中间件）。
    admin_cors_allow_origins: str = ""
    grpc_host: str = "0.0.0.0"
    grpc_port: int = 50051
    grpc_max_workers: int = 8
    grpc_max_concurrent_rpcs: int = 0  # 0 = unlimited
    grpc_shutdown_grace_seconds: float = 5.0

    # ── 字段校验 ──────────────────────────────────────────────────────
    @field_validator("memory_l3_executor_workers")
    @classmethod
    def validate_memory_l3_executor_workers(cls, value: int) -> int:
        """L3 写线程数必须 >= 1，否则线程池无法启动。"""

        if value < 1:
            raise ValueError("memory_l3_executor_workers must be >= 1")
        return value

    @field_validator("memory_backend_max_concurrent_calls")
    @classmethod
    def validate_memory_backend_max_concurrent_calls(cls, value: int) -> int:
        """mem0 / Milvus 后端并发调用上限必须 >= 1，避免信号量零值。"""

        if value < 1:
            raise ValueError("memory_backend_max_concurrent_calls must be >= 1")
        return value

    @field_validator("memory_api_worker_limit")
    @classmethod
    def validate_memory_api_worker_limit(cls, value: int) -> int:
        """API 线程池大小必须 >= 1，否则永远取不到线程。"""

        if value < 1:
            raise ValueError("memory_api_worker_limit must be >= 1")
        return value

    @field_validator("memory_api_worker_wait_seconds")
    @classmethod
    def validate_memory_api_worker_wait_seconds(cls, value: float) -> float:
        """API 线程等待时间允许为 0（不等待直接报 busy），不允许为负。"""

        if value < 0:
            raise ValueError("memory_api_worker_wait_seconds must be >= 0")
        return value

    @field_validator("memory_rate_limit_per_minute")
    @classmethod
    def validate_memory_rate_limit_per_minute(cls, value: int) -> int:
        """限流速率必须 >= 1，否则等于关掉限流但看不出是配置写的。"""

        if value < 1:
            raise ValueError("memory_rate_limit_per_minute must be >= 1")
        return value

    @field_validator("memory_rate_limit_burst")
    @classmethod
    def validate_memory_rate_limit_burst(cls, value: int) -> int:
        """突发额度必须 >= 1，否则第一请求就被拒。"""

        if value < 1:
            raise ValueError("memory_rate_limit_burst must be >= 1")
        return value

    @field_validator("task_orphan_running_seconds")
    @classmethod
    def validate_task_orphan_running_seconds(cls, value: float) -> float:
        """孤儿任务回收阈值必须为正；过小会误回收健康执行中的任务。"""

        if value < 60:
            raise ValueError("task_orphan_running_seconds must be >= 60")
        return value

    # ── 派生属性（拼装 URL）────────────────────────────────────────────
    @property
    def database_url(self) -> str:
        """组装 SQLAlchemy 异步 URL：优先用 ``raw_database_url``，否则从分段配置拼装。

        把 ``postgres://`` 改写为 ``postgresql+asyncpg://``，与 SQLAlchemy 异步驱动对齐。
        """

        if self.raw_database_url:
            url = self.raw_database_url
            # Handle both postgres:// and postgresql:// prefixes
            if url.startswith("postgres://"):
                return url.replace("postgres://", "postgresql+asyncpg://", 1)
            elif url.startswith("postgresql://"):
                return url.replace("postgresql://", "postgresql+asyncpg://", 1)
            # If already has driver specified, return as-is
            return url
        encoded_password = quote_plus(self.postgres_password) if self.postgres_password else ""
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{encoded_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_database}"
        )

    @property
    def milvus_token(self) -> str:
        """组装 Milvus token；空凭据返回空串。"""

        if self.milvus_user and self.milvus_password:
            return f"{self.milvus_user}:{self.milvus_password}"
        return ""

    @property
    def llm_api_key(self) -> str:
        """LLM 调用使用的 API key。

        来自 ``MEMORY_LLM_KEY``；为空表示端点无需鉴权，调用方按 ``or "not-required"`` 兜底。
        """

        return self.memory_llm_key

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        """log_level 仅接受 loguru 标准五档大小写不敏感名。"""

        normalized = value.upper()
        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if normalized not in valid_levels:
            raise ValueError(f"Invalid log level: {value}")
        return normalized

    @property
    def memory_p0_slots_list(self) -> list[str]:
        """把 ``memory_p0_slots``（逗号分隔字符串）解析成 slot 列表。

        空串 / 缺失 = 空列表 = gate 关闭，/items 看到所有 L3 记忆。
        """

        if not self.memory_p0_slots:
            return []
        return [item.strip() for item in self.memory_p0_slots.split(",") if item.strip()]

    @property
    def memory_p0_slots_warning(self) -> str | None:
        """白名单非空时的启动提示；空则 ``None``（不提示）。

        为什么要它：白名单把「哪些事实能跨会话召回」收窄了，而症状是
        「助手忘了事」而不是报错 —— 没人会去看配置注释，所以启动时要说一遍。

        这条也是**全局**开关：一份部署服务多个 namespace（= 多个企业）时，
        一家调了白名单，另一家的召回覆盖面也一起变。
        """
        slots = self.memory_p0_slots_list
        if not slots:
            return None
        return (
            f"MEMORY_P0_SLOTS is set to {slots}: only these slots become recallable "
            "L3 memories; everything else stays in L1/L2 (session-scoped). "
            "This setting is global — it applies to every namespace."
        )

    @property
    def memory_allowed_namespaces_list(self) -> list[str]:
        """把 ``memory_allowed_namespaces``（逗号分隔字符串）解析成 namespace 列表。

        空串 / 缺失 = 空列表 = 不限制，保持内网免鉴权下的既有行为。
        """

        if not self.memory_allowed_namespaces:
            return []
        return [item.strip() for item in self.memory_allowed_namespaces.split(",") if item.strip()]

    def namespace_allowed(self, namespace: str) -> bool:
        """这个 namespace 本部署是否服务。

        白名单为空 = 不限制。白名单非空时只有名单内的 namespace 能被读写。
        """

        allowed = self.memory_allowed_namespaces_list
        return not allowed or namespace in allowed

    def require_namespace(self, namespace: str) -> None:
        """本部署不服务这个 namespace 时抛错（fail-closed）。

        错误文案带 ``fail-closed`` 前缀：HTTP 面映射成 403、gRPC 面映射成
        PERMISSION_DENIED（``api/errors.py`` 与 ``rpc/servicer.py`` 同口径）。
        措辞用「不服务」而不是「无权」—— 白名单是部署口径，不是权限体系。
        """

        if self.namespace_allowed(namespace):
            return
        raise ValueError(
            "fail-closed: namespace "
            f"{namespace!r} is not served by this deployment "
            f"(MEMORY_ALLOWED_NAMESPACES={self.memory_allowed_namespaces_list})"
        )

    @property
    def memory_namespace_boundary_warning(self) -> str | None:
        """生产部署且未设 namespace 白名单时的启动提示；否则 ``None``。

        为什么要它：本服务**不鉴权**（D10）。namespace 是业务分组标签，不是
        安全边界 —— 任何能连到本服务的人都能读写**任何** namespace 下的记忆。
        这是有意为之（内网部署），但接入方必须知道：把它当成有租户隔离的
        服务接进生产，是这份契约里最容易踩的坑。

        只在生产环境提示：开发与单测环境下这条是噪音。
        """

        if self.environment != "production":
            return None
        if self.memory_allowed_namespaces_list:
            return None
        return (
            "MEMORY_ALLOWED_NAMESPACES is not set: this deployment serves every "
            "namespace, and any caller that can reach it can read and write every "
            "memory it holds (no caller authentication — see docs/API.md "
            "'trust model'). Set MEMORY_ALLOWED_NAMESPACES to pin this deployment "
            "to the namespaces it is meant to serve."
        )


@lru_cache
def get_settings() -> Settings:
    """全局单例 ``Settings``；``lru_cache`` 保证只构造一次。"""

    return Settings()


settings = get_settings()
