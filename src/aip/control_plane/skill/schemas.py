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
    script_allowlist: list[dict[str, Any]] = Field(default_factory=list)
    script_files: list[str] = Field(default_factory=list)


class SkillSourceSaved(SkillPackageUploaded):
    warnings: list[str] = Field(default_factory=list)


class SkillTextFile(BaseModel):
    path: str
    content: str


class SkillAssetFile(BaseModel):
    path: str
    content_base64: str
    size: int | None = None


class SkillSource(BaseModel):
    """在线编辑器的表单形态：frontmatter + SKILL.md 正文 + Tool 引用 + references/assets。"""

    model_config = ConfigDict(extra="ignore")

    version_id: str | None = None
    content_sha256: str | None = None
    name: str
    description: str = ""
    license: str | None = None
    compatibility: str | None = None
    allowed_tools: list[str] = Field(default_factory=list)
    body: str = ""
    tools: list[str] = Field(default_factory=list)
    references: list[SkillTextFile] = Field(default_factory=list)
    assets: list[SkillAssetFile] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


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
    git_url: str | None = None


class SkillGitPublished(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    version: str
    git_url: str
    bare_path: str | None = None
    tag: str | None = None
    content_sha256: str | None = None


class SkillGitUrl(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    version: str
    git_url: str
    tag: str | None = None


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


class SkillScriptAllowlistItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    version_id: str
    path_glob: str
    status: str
    requested_by: str | None = None
    approved_by: str | None = None
    reason: str | None = None
    expires_at: str | None = None
    created_at: str | None = None


class SkillScriptAllowlistPage(BaseModel):
    items: list[SkillScriptAllowlistItem] = Field(default_factory=list)
