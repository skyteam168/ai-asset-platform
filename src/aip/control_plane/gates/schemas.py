"""门禁 / 审批 / 使用申请响应模型（对齐 control-plane.openapi.yaml）。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class TestCase(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    kind: Literal["functional", "permission"]
    args: dict[str, Any] = Field(default_factory=dict)
    expect: dict[str, Any] = Field(default_factory=dict)
    principal: dict[str, Any] = Field(default_factory=dict)
    critical: bool = True


class SelectionEvalCase(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    question: str
    expect_capability_id: str | None = None


class SelectionEval(BaseModel):
    model_config = ConfigDict(extra="ignore")

    version_id: str
    model_profiles: list[str] = Field(default_factory=list)
    cases: list[SelectionEvalCase] = Field(default_factory=list)


class AcceptanceSheet(BaseModel):
    version_id: str
    template: dict[str, Any] = Field(default_factory=dict)


class PiiSuggestion(BaseModel):
    model_config = ConfigDict(extra="ignore")

    field: str | None = None
    kind: str | None = None
    source: str | None = None


class GateSubmitResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    gate: str
    passed: bool
    problems: list[str] = Field(default_factory=list)
    pii_suggestions: list[PiiSuggestion] = Field(default_factory=list)


class LabRunResult(BaseModel):
    """Test Lab / 组合门禁结果：字段随证据与 G4 变化，保留透传。"""

    model_config = ConfigDict(extra="allow")

    passed: bool


class GateResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    version_id: str | None = None
    company_id: str | None = None
    gate: str | None = None
    status: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    operator: str | None = None
    created_at: str | None = None


class ImpactItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    asset_id: str
    version_id: str
    asset_type: str
    name: str
    capability_id: str | None = None
    semver: str | None = None
    state: str | None = None
    refs: list[str] = Field(default_factory=list)


class VersionImpact(BaseModel):
    version_id: str
    items: list[ImpactItem] = Field(default_factory=list)


class CanaryStarted(BaseModel):
    release_id: str


class PromoteResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    passed: bool
    calls: int | None = None
    errors: int | None = None
    error_rate: float | None = None
    min_calls: int | None = None
    max_error_rate: float | None = None


class ApprovalPending(BaseModel):
    """待办审批行（异于历史契约里的简化 Approval）。"""

    model_config = ConfigDict(extra="ignore")

    id: str
    kind: str
    status: str
    subject: str | None = None
    subject_id: str | None = None
    company_id: str | None = None
    approver_role: str | None = None
    required_approvals: int | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    due_at: str | None = None
    created_at: str | None = None
    excluded_users: list[str] = Field(default_factory=list)


class ApprovalDecision(BaseModel):
    id: str
    status: str


class ApprovalDelegate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    delegate_user_id: str
    valid_to: str


class UsageRequestCreated(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    status: str
    consumer_id: str | None = None
    asset_id: str | None = None
    env: str | None = None
    reason: str | None = None


class AccessReviewOpened(BaseModel):
    id: str


class AccessReviewClosed(BaseModel):
    id: str
    revoked: int
