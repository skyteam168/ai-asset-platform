"""公司 LLM 白名单 / 预算 / 用量响应模型。"""

from __future__ import annotations

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
