"""Registry / Grant 响应模型（对齐 control-plane.openapi.yaml）。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

AssetType = Literal["tool", "skill", "workflow", "bundle", "prompt", "model_profile", "connector_package"]
VersionState = Literal[
    "draft",
    "testing",
    "in_review",
    "approved",
    "canary",
    "active",
    "deprecated",
    "retired",
    "suspended",
]


class Asset(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: AssetType
    name: str
    owner_org_id: str
    classification: int = Field(ge=1, le=4)
    id: str | None = None
    owner_user_id: str | None = None
    company_id: str | None = None
    created_at: str | None = None
    # 列表摘要（来自最新版本 + 连接器关联，便于运营辨识）
    latest_semver: str | None = None
    latest_state: VersionState | None = None
    title: str | None = None
    description: str = ""
    risk_level: str | None = None
    connector: str | None = None
    system_key: str | None = None
    system_host: str | None = None


class AssetVersion(BaseModel):
    model_config = ConfigDict(extra="ignore")

    semver: str
    spec: dict[str, Any]
    id: str | None = None
    asset_id: str | None = None
    content_hash: str | None = None
    state: VersionState | None = None
    dep_lock: dict[str, Any] | None = None
    company_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class Grant(BaseModel):
    model_config = ConfigDict(extra="ignore")

    asset_id: str
    grantee_type: Literal["consumer", "agent_profile", "org_unit", "user"]
    grantee_id: str
    env: str
    expires_at: str
    id: str | None = None
    ceiling_id: str | None = None
    company_id: str | None = None


class GrantCeiling(BaseModel):
    model_config = ConfigDict(extra="ignore")

    company_id: str
    max_classification: int = Field(ge=1, le=4)
    id: str | None = None
    asset_set: dict[str, Any] = Field(default_factory=dict)
    asset_ids: list[str] = Field(default_factory=list)
    quota: dict[str, Any] = Field(default_factory=dict)
    status: str | None = None
    created_at: str | None = None
