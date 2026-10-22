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


class ImporterTryResult(BaseModel):
    """导入预览真连接试调结果（O1）；样本已截断。"""

    model_config = ConfigDict(extra="ignore")

    ok: bool = True
    operation: str
    mode: str
    connection_env: str | None = None
    item_count: int = 0
    truncated: bool = False
    sample: Any = None
    next_cursor: str | None = None


class ImporterGenerateResult(BaseModel):
    """AI 生成的候选 OpenAPI（O3b）；须人工预览后再导入。"""

    model_config = ConfigDict(extra="ignore")

    document: str
    format: str = "openapi"
    title: str | None = None
    warnings: list[str] = Field(default_factory=list)
    operation_count: int = 0
