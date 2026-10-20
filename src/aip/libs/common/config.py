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
    # TOTP 种子加密密钥（Fernet，32 字节 urlsafe base64）。生产来自 OpenBao
    secret_box_key: str = "ZGV2LW9ubHktc2VjcmV0LWJveC1rZXktMzJieXRlcyE="
    # 服务间认证（生产为 mTLS + 短期服务令牌，architecture.md 3.3）；dev 用共享令牌
    internal_service_token: str = "aip-dev-internal-service-token"
    control_plane_url: str = ""
    # 数据面本地状态：快照落盘目录与审计 spool（architecture.md 4.4、12.4）
    data_dir: str = ".aip-data"
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
