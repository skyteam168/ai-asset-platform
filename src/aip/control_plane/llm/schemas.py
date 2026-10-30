"""公司 LLM 白名单 / 预算 / 用量 / 提供方响应模型。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class LlmAllowlist(BaseModel):
    model_config = ConfigDict(extra="ignore")

    company_id: str
    names: list[str] = Field(default_factory=list)


class LlmBudget(BaseModel):
    model_config = ConfigDict(extra="ignore")

    company_id: str
    daily_tokens: int | None = None


class LlmUsage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    company_id: str
    usage_date: str
    tokens_used: int = 0
    daily_tokens: int | None = None


class LlmProvider(BaseModel):
    model_config = ConfigDict(extra="ignore")

    base_url: str = ""
    credential_ref: str = "kv/data/aip/llm/default"
    models_path: str = "/models"
    has_api_key: bool = False
    api_key_hint: str | None = None
    synced_at: str | None = None
    synced_count: int = 0


class LlmUpstreamModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    owned_by: str | None = None
    created: int | None = None
    suggested_profile: str


class LlmModelProfileRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    asset_id: str
    name: str
    version: str | None = None
    model: str | None = None
    base_url: str | None = None
    status: str | None = None
    company_id: str | None = None


class LlmModelProfileCreated(BaseModel):
    model_config = ConfigDict(extra="ignore")

    asset_id: str
    version_id: str
    name: str
    version: str
    ref: str
    spec: dict[str, Any] = Field(default_factory=dict)
