"""IAM 响应模型（对齐 docs/api/control-plane.openapi.yaml 中 Session / User / OrgUnit 等）。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class User(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    display_name: str
    home_org_id: str
    login_enabled: bool
    status: Literal["active", "disabled", "left"]
    login_name: str | None = None
    employee_no: str | None = None
    email: str | None = None
    effective_clearance: int | None = Field(default=None, ge=1, le=4)
    totp_provisioning_uri: str | None = None
    timezone: str | None = None
    locale: str | None = None


class RoleGrant(BaseModel):
    role: str
    scope_org_id: str


class Me(User):
    """当前用户：User + 生效角色列表。"""

    roles: list[RoleGrant] = Field(default_factory=list)


class Session(BaseModel):
    session_token: str
    expires_at: str
    user: User


class OrgUnit(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    type: Literal["group", "company", "department", "team"]
    code: str
    name: str
    company_id: str
    status: Literal["active", "disabled"]
    parent_id: str | None = None
    default_clearance: int | None = Field(default=None, ge=1, le=4)
    timezone: str | None = None
    locale: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None


class ImportErrorItem(BaseModel):
    line: int
    message: str


class ImportResult(BaseModel):
    batch_id: str
    dry_run: bool = False
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    errors: list[ImportErrorItem] = Field(default_factory=list)


class ImportRollback(BaseModel):
    batch_id: str
    deleted: int = 0
    disabled: int = 0


class RoleAssignment(BaseModel):
    id: str
    user_id: str
    role: str
    scope_org_id: str


class BusinessRole(BaseModel):
    model_config = ConfigDict(extra="ignore")

    company_id: str
    code: str
    name: str
    description: str = ""
    created_at: str | None = None


class Consumer(BaseModel):
    model_config = ConfigDict(extra="ignore")

    client_id: str
    company_id: str
    clearance_cap: int = Field(ge=1, le=4)
    bind_policy: Literal["strict", "by_employee_no", "app_only"]
    id: str | None = None
    status: str | None = None
    contact: str | None = None


class CredentialIssued(BaseModel):
    id: str
    kind: str
    expires_at: str
    token: str | None = None


class UserBinding(BaseModel):
    model_config = ConfigDict(extra="ignore")

    consumer_id: str
    external_subject: str
    user_id: str
    id: str | None = None
    expires_at: str | None = None


class ClearanceRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    user_id: str | None = None
    requested_level: int | None = Field(default=None, ge=1, le=4)
    reason: str | None = None
    expires_at: str | None = None
    status: Literal["pending", "approved", "rejected"] | None = None


class ClearanceDecision(BaseModel):
    id: str
    status: str


class AssertionDebugResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    signature_valid: bool = False
    claims_valid: bool = False
    binding: str | None = None
    errors: list[str] = Field(default_factory=list)
    principal: dict[str, Any] | None = None
