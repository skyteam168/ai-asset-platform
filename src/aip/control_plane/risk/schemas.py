"""风控响应模型（对齐 control-plane.openapi.yaml）。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class RiskRule(BaseModel):
    model_config = ConfigDict(extra="ignore")

    code: str
    params: dict[str, Any] = Field(default_factory=dict)
    action: Literal["alert", "suspend_asset"] | str
    name: str | None = None
    description: str | None = None
    source: Literal["audit", "domain"] | str | None = None
    severity: Literal["low", "medium", "high"] | str | None = None
    enabled: bool | None = None
    updated_by: str | None = None
    updated_at: str | None = None


class RiskEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    rule_code: str
    status: Literal["open", "acknowledged", "resume_requested", "resolved"] | str = "open"
    rule_name: str | None = None
    company_id: str | None = None
    group_key: dict[str, Any] = Field(default_factory=dict)
    observed: float | int | None = None
    threshold: float | int | None = None
    severity: str | None = None
    action: str | None = None
    action_result: dict[str, Any] = Field(default_factory=dict)
    note: str | None = None
    window_start: str | None = None
    window_end: str | None = None
    created_at: str | None = None
    resolved_by: str | None = None
    resolved_at: str | None = None


class RiskResumeRequested(BaseModel):
    event_id: str
    approval_id: str
    status: str


class RiskScanResult(BaseModel):
    created: list[RiskEvent] = Field(default_factory=list)
