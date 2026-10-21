"""Webhook 响应模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class WebhookCreated(BaseModel):
    model_config = ConfigDict(extra="ignore")

    subscription_id: str
    endpoint_id: str
    consumer_id: str
    events: list[str] = Field(default_factory=list)
    url: str
    status: str
    secret: str


class WebhookEndpoint(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    company_id: str | None = None
    subscription_id: str | None = None
    consumer_id: str | None = None
    url: str | None = None
    status: str | None = None
    events: list[str] = Field(default_factory=list)
    created_at: str | None = None
    updated_at: str | None = None


class WebhookSecretRotated(BaseModel):
    endpoint_id: str
    secret: str


class WebhookStatus(BaseModel):
    id: str
    status: Literal["active", "disabled"] | str


class WebhookDelivery(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    endpoint_id: str | None = None
    company_id: str | None = None
    event_kind: str | None = None
    status: str | None = None
    attempt: int | None = None
    next_attempt_at: str | None = None
    last_error: str | None = None
    http_status: int | None = None
    created_at: str | None = None
    delivered_at: str | None = None


class WebhookProcessResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    results: list[dict[str, Any]] = Field(default_factory=list)
