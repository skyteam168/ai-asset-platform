"""连接响应模型（对齐 OpenAPI SystemConnection / ConnectionTestResult / ConfigDraft）。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class SystemConnection(BaseModel):
    model_config = ConfigDict(extra="ignore")

    system_key: str
    env: Literal["sandbox", "test", "staging", "prod"]
    base_url: str
    scheme: Literal["https", "http", "postgres", "mysql"]
    id: str | None = None
    company_id: str | None = None
    tls_mode: Literal["system_ca", "custom_ca", "pinned", "insecure"] | None = None
    tls_check_hostname: bool | None = None
    pins: list[dict[str, Any]] = Field(default_factory=list)
    pin_enforce_expiry: bool | None = None
    block_on_fingerprint_change: bool | None = None
    credential_ref: str | None = None
    status: str | None = None
    transport_exception_reason: str | None = None
    transport_exception_expires_at: str | None = None
    transport_approved_by: list[str] = Field(default_factory=list)


class ConnectionTestResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    connection_id: str
    verdict: Literal["pass", "fail"]
    resolved_ip: str | None = None
    allowlisted: bool | None = None
    tls_version: str | None = None
    chain: list[dict[str, Any]] = Field(default_factory=list)
    fingerprint_cert_sha256: str | None = None
    fingerprint_spki_sha256: str | None = None
    hostname_match: bool | None = None
    error_code: str | None = None
    detail: str | None = None


class ConfigDraft(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tls_mode: Literal["custom_ca", "pinned"] | None = None
    pins: list[dict[str, Any]] = Field(default_factory=list)
    ca_pem: str | None = None
    approval_id: str | None = None


class TransportExceptionStatus(BaseModel):
    id: str
    status: str


class ConnectorInstance(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    kind: Literal["declarative", "kb", "mock", "upstream_mcp", "sql_readonly"]
    company_id: str | None = None
    system_connection_id: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    concurrency: int | None = None
    rate_per_s: float | None = None
    burst: int | None = None
    status: str | None = None
