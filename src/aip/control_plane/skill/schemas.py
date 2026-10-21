"""Skill Registry 响应模型。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SkillPackageUploaded(BaseModel):
    model_config = ConfigDict(extra="ignore")

    version_id: str
    name: str
    manifest: dict[str, Any] = Field(default_factory=dict)
    signature: str | None = None
    content_sha256: str | None = None
    files: list[str] = Field(default_factory=list)


class SkillCatalogItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    version: str
    description: str = ""
    namespace: str | None = None
    state: str | None = None
    retracted: bool = False
    channel: str | None = None
    tools: list[str] = Field(default_factory=list)
    asset_id: str | None = None
    version_id: str | None = None


class SkillManifest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    version_id: str | None = None
    manifest: dict[str, Any] = Field(default_factory=dict)
    signature: str | None = None
    content_sha256: str | None = None
    retracted: bool = False
    name: str | None = None
    version: str | None = None


class SkillInstallCheck(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    version: str
    retracted: bool = False
    missing_grants: list[str] = Field(default_factory=list)
    ok: bool
