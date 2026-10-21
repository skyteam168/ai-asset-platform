"""Importer 响应模型（对齐 control-plane.openapi.yaml ImporterResult）。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ImporterDraft(BaseModel):
    model_config = ConfigDict(extra="ignore")

    asset_id: str
    version_id: str
    capability_id: str
    state: str | None = None


class ImporterResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    connector_config: dict[str, Any] = Field(default_factory=dict)
    tools: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    connector_instance_id: str | None = None
    drafts: list[ImporterDraft] = Field(default_factory=list)
