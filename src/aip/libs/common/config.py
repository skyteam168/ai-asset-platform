"""配置：全部来自环境变量（前缀 AIP_），本地开发默认值对应 deploy/docker-compose.yml。"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AIP_", env_file=".env", extra="ignore")

    env: str = Field(default="dev", pattern="^(dev|test|staging|prod)$")
    version: str = "0.1.0"
    # 迁移使用 owner 账号；应用运行时使用非超级用户 aip_app，RLS 才会生效（architecture.md 2.6）
    database_url: str = "postgresql+asyncpg://aip:aip-dev-only@127.0.0.1:55432/aip"
    app_database_url: str = "postgresql+asyncpg://aip_app:aip-app-dev-only@127.0.0.1:55432/aip"
    valkey_url: str = "redis://127.0.0.1:56379/0"
    openbao_addr: str = "http://127.0.0.1:58200"
    openbao_token: str = "aip-dev-root-token"
    opa_url: str = "http://127.0.0.1:58181"
    opa_timeout_s: float = 2.0
    s3_endpoint: str = "http://127.0.0.1:58333"
    s3_access_key: str = "aip"
    s3_secret_key: str = "aip-dev-only"
    s3_region: str = "us-east-1"
    # 审计锚点 WORM 桶（architecture.md 12.4）；关闭时只写 audit_anchor 表
    worm_enabled: bool = False
    worm_bucket: str = "aip-audit-worm"

    # 身份（architecture.md 3）
    issuer: str = "https://api.aip.internal"
    mcp_resource: str = "https://mcp.aip.internal/mcp"
    token_endpoint_aud: str = "https://api.aip.internal/oauth/token"
    # 平台签名密钥（ES256 PEM）。生产由 OpenBao Transit 提供（M2）；为空时 dev / test 进程内生成临时密钥
    signing_key_pem: str = ""
    # local：本地密钥（signing_key_pem）；transit：OpenBao Transit 托管，私钥不出 OpenBao（生产）
    signing_backend: str = Field(default="local", pattern="^(local|transit)$")
    transit_signing_key: str = "aip-platform"
    # TOTP 种子等平台机密的加密：local（Fernet）或 transit（OpenBao Transit 加解密）
    secret_backend: str = Field(default="local", pattern="^(local|transit)$")
    transit_secret_key: str = "aip-secrets"
    # 执行快照信封加密（architecture.md 12.6）：local 派生 KEK；transit 按公司 Transit 密钥包装 DEK
    envelope_backend: str = Field(default="local", pattern="^(local|transit)$")
    transit_company_key_prefix: str = "aip-company-"
    # TOTP 种子加密密钥（Fernet，32 字节 urlsafe base64）。生产来自 OpenBao
    secret_box_key: str = "ZGV2LW9ubHktc2VjcmV0LWJveC1rZXktMzJieXRlcyE="
    # 服务间认证（生产为 mTLS + 短期服务令牌，architecture.md 3.3）；dev 用共享令牌
    internal_service_token: str = "aip-dev-internal-service-token"
    # static=仅共享令牌；jwt=仅短期 JWT；both=二者皆可（默认，兼容旧调用方）
    service_auth_mode: str = Field(default="both", pattern="^(static|jwt|both)$")
    service_token_ttl_s: int = Field(default=300, ge=60, le=3600)
    # 非空则 require_service_auth 额外校验客户端证书 PEM（由调用方/入口传入）
    service_mtls_ca_pem: str = ""
    control_plane_url: str = ""
    # 控制面经服务凭据调用数据面内部接口（平台待办：确认与人工任务）
    data_plane_url: str = "http://127.0.0.1:18082"
    # Workflow：本进程是否运行 Worker；启动 Run 后同步等待的秒数（预计 30 秒内完成的同步返回，architecture.md 8.5）
    workflow_worker: bool = True
    workflow_sync_wait_s: float = Field(default=2.0, ge=0, le=30)
    workflow_lease_s: float = Field(default=30.0, ge=1)
    workflow_stale_s: float = Field(default=30.0, ge=1)
    # 风险规则扫描间隔（秒）；0 表示不在本进程扫描
    risk_scan_interval_s: float = Field(default=30.0, ge=0)
    # G3 稳定性：同一用例重复执行次数（architecture.md 7.1，≥ 20）
    g3_repeats: int = Field(default=20, ge=2)
    # 数据面本地状态：快照落盘目录与审计 spool（architecture.md 4.4、12.4）
    data_dir: str = ".aip-data"
    # 控制台静态文件目录；为空时用仓库内 console/dist（镜像里显式设为 /app/console/dist）
    console_dist: str = ""
    instance_id: str = "dp-local"
    audit_spool_max_bytes: int = 512 * 1024 * 1024
    # 审计库（应用角色只有 INSERT / SELECT）；为空则只写本地 spool、不入库
    audit_database_url: str = ""
    # 数据面运行表（exec.tool_execution）；为空时用进程内存储（仅限开发）
    runtime_database_url: str = ""
    # sandbox：写操作只允许到 sandbox / test 连接；live：生产（architecture.md 7.2）
    runtime_mode: str = Field(default="sandbox", pattern="^(sandbox|live_read|live)$")
    audit_ship_interval_s: float = 1.0
    audit_anchor_interval_s: float = 3600.0
    access_token_ttl_s: int = Field(default=600, ge=300, le=900)
    assertion_max_ttl_s: int = 300
    session_idle_s: int = 1800
    session_absolute_s: int = 43200
    # OpenTelemetry：none | console | otlp | memory（测试）；OTLP 默认 http://127.0.0.1:4318/v1/traces
    otel_exporter: str = Field(default="none", pattern="^(none|console|otlp|memory)$")
    otel_endpoint: str = ""
    # LLM Gateway：上游 API Key（可选；本地 / 内网模型可为空）
    llm_api_key: str = ""
    # Skill Git bare 镜像根目录（空=禁用 H34）；clone URL 前缀（空则返回本地 bare 路径）
    skill_git_root: str = ""
    skill_git_public_base: str = ""
    # Skill 包对象存储（H35）：双写 SeaweedFS + bytea；下载 object 优先
    skill_bucket: str = "aip-skills"
    skill_object_storage: bool = False
    # Notifier（H43 / H56）：通用 IM Webhook / 飞书；SMTP 生产投递（空则 LogMail）
    notify_im_webhook_url: str | None = None
    notify_feishu_webhook_url: str | None = None
    notify_flush_interval_s: float = Field(default=2.0, ge=0.5, le=60)
    smtp_host: str = ""
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_starttls: bool = True
    # Connector worker（H57）：inline=同进程；rpc=独立池（骨架，未接线）
    connector_worker_mode: str = Field(default="inline", pattern="^(inline|rpc)$")
    connector_worker_url: str = ""
    # Langfuse（H50）：可选登记，不强制接线
    langfuse_host: str | None = None
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    # OIDC 登录插件（H51）：默认关闭；stub 供本地映射测例，不接真 IdP
    oidc_enabled: bool = False
    oidc_stub: bool = True
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_audience: str = ""
    # OAuth authorization_code + PKCE（H52）：默认关闭；stub 签发授权码供本地测例
    oauth_pkce_enabled: bool = False
    oauth_pkce_stub: bool = True
    oauth_pkce_code_ttl_s: int = Field(default=120, ge=30, le=600)
    # WebAuthn 可选第二因子（H53）：默认关闭；stub 跳过 attestation 签名校验
    webauthn_enabled: bool = False
    webauthn_stub: bool = True
    webauthn_rp_id: str = "localhost"
    webauthn_rp_name: str = "AIP Console"
    webauthn_challenge_ttl_s: int = Field(default=300, ge=60, le=600)

    @model_validator(mode="after")
    def _no_dev_secrets_outside_dev(self) -> Settings:
        """staging / prod 不允许沿用任何 dev 默认口令与密钥。"""
        if self.env in ("staging", "prod"):
            defaults = type(self).model_fields
            leaked = [
                n
                for n in ("openbao_token", "secret_box_key", "internal_service_token")
                if getattr(self, n) == defaults[n].default
            ]
            if self.signing_backend == "local" and not self.signing_key_pem:
                leaked.append("signing_key_pem（或 signing_backend=transit）")
            if self.secret_backend == "local" and "secret_box_key" in leaked:
                leaked[leaked.index("secret_box_key")] = "secret_box_key（或 secret_backend=transit）"
            if "dev-only" in self.database_url or "dev-only" in self.app_database_url:
                leaked.append("database_url")
            if leaked:
                raise ValueError(f"{self.env} 环境必须显式配置：{', '.join(leaked)}")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
