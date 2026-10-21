"""Skill Registry HTTP 接口：上传、目录、下载、清单；安装前授权检查。"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, File, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from aip.control_plane.iam.api import ActorDep, DbDep
from aip.control_plane.skill import schemas as out
from aip.control_plane.skill import service as skill
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.common.paging import Page
from aip.libs.tokens.keys import platform_key

V = "/api/v1"
SkillZip = Annotated[UploadFile, File()]


class InstallCheckIn(BaseModel):
    consumer_id: uuid.UUID
    name: str
    version: str
    env: str = Field(default="test", pattern="^(test|staging|prod)$")


class SkillTextFileIn(BaseModel):
    path: str = Field(max_length=256)
    content: str = Field(max_length=512_000)


class SkillAssetFileIn(BaseModel):
    path: str = Field(max_length=256)
    content_base64: str = Field(max_length=1_500_000)


class SkillSourceIn(BaseModel):
    """在线创建 Skill：只允许指令型内容（architecture.md 6.2、10.4），脚本后缀在组装时硬拒。"""

    name: str = Field(min_length=2, max_length=64)
    description: str = Field(min_length=10, max_length=1024)
    license: str | None = Field(default=None, max_length=200)
    compatibility: str | None = Field(default=None, max_length=500)
    allowed_tools: list[str] = Field(default_factory=list, max_length=50)
    body: str = Field(default="", max_length=200_000)
    tools: list[str] = Field(default_factory=list, max_length=100)
    references: list[SkillTextFileIn] = Field(default_factory=list, max_length=50)
    assets: list[SkillAssetFileIn] = Field(default_factory=list, max_length=20)


def router() -> APIRouter:
    r = APIRouter()

    @r.get(
        f"{V}/versions/{{id}}/skill-source",
        tags=["skill"],
        operation_id="getSkillSource",
        response_model=out.SkillSource,
    )
    async def get_source(id: uuid.UUID, actor: ActorDep, db: DbDep) -> out.SkillSource:
        async with db.tx(actor.db_scope()) as conn:
            return out.SkillSource.model_validate(await skill.load_source(conn, id))

    @r.put(
        f"{V}/versions/{{id}}/skill-source",
        tags=["skill"],
        operation_id="saveSkillSource",
        response_model=out.SkillSourceSaved,
    )
    async def save_source(id: uuid.UUID, body: SkillSourceIn, actor: ActorDep, db: DbDep) -> out.SkillSourceSaved:
        async with db.tx(actor.db_scope()) as conn:
            return out.SkillSourceSaved.model_validate(
                await skill.save_source(conn, actor, id, body.model_dump(), key=platform_key())
            )

    @r.post(
        f"{V}/versions/{{id}}/skill-package",
        tags=["skill"],
        operation_id="uploadSkillPackage",
        status_code=201,
        response_model=out.SkillPackageUploaded,
    )
    async def upload(id: uuid.UUID, actor: ActorDep, db: DbDep, file: SkillZip) -> out.SkillPackageUploaded:
        raw = await file.read()
        if len(raw) > 8 * 1024 * 1024:
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, "Skill 包不能超过 8MB")
        async with db.tx(actor.db_scope()) as conn:
            return out.SkillPackageUploaded.model_validate(
                await skill.attach_package(conn, actor, id, raw, key=platform_key())
            )

    @r.get(f"{V}/skills", tags=["skill"], operation_id="listSkills", response_model=Page[out.SkillCatalogItem])
    async def list_skills(
        actor: ActorDep,
        db: DbDep,
        q: str | None = None,
        channel: str = Query(default="stable", pattern="^(stable|beta)$"),
        include_retracted: bool = True,
    ) -> Page[out.SkillCatalogItem]:
        async with db.tx(actor.db_scope()) as conn:
            items = await skill.list_skills(conn, q=q, channel=channel, include_retracted=include_retracted)
        return Page(items=[out.SkillCatalogItem.model_validate(x) for x in items])

    @r.get(
        f"{V}/skills/{{name}}/versions/{{version}}/manifest",
        tags=["skill"],
        operation_id="getSkillManifest",
        response_model=out.SkillManifest,
    )
    async def manifest(name: str, version: str, actor: ActorDep, db: DbDep) -> out.SkillManifest:
        async with db.tx(actor.db_scope()) as conn:
            asset, ver = await skill.resolve_skill(conn, name, version)
            meta = await skill.get_manifest(conn, ver["id"])
            spec = ver["spec"] or {}
            return out.SkillManifest.model_validate(
                {
                    **meta,
                    "retracted": ver["state"] in skill.RETRACTED_STATES,
                    "name": spec.get("name") or asset["name"],
                    "version": ver["semver"],
                }
            )

    @r.get(f"{V}/skills/{{name}}/versions/{{version}}/download", tags=["skill"], operation_id="downloadSkill")
    async def download(name: str, version: str, actor: ActorDep, db: DbDep) -> Response:
        async with db.tx(actor.db_scope()) as conn:
            _, ver = await skill.resolve_skill(conn, name, version)
            data, filename = await skill.download_zip(conn, ver["id"])
        return Response(
            content=data,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @r.post(
        f"{V}/skills/install-check",
        tags=["skill"],
        operation_id="skillInstallCheck",
        response_model=out.SkillInstallCheck,
    )
    async def install_check(body: InstallCheckIn, actor: ActorDep, db: DbDep) -> out.SkillInstallCheck:
        """安装前检查：返回缺失的 Tool 授权（corp-skill install 调用）。"""
        async with db.tx(actor.db_scope()) as conn:
            _, ver = await skill.resolve_skill(conn, body.name, body.version)
            missing = await skill.missing_tool_grants(
                conn, consumer_id=body.consumer_id, tool_refs=list((ver["spec"] or {}).get("tools") or []), env=body.env
            )
            retracted = ver["state"] in skill.RETRACTED_STATES
        return out.SkillInstallCheck(
            name=body.name,
            version=body.version,
            retracted=retracted,
            missing_grants=missing,
            ok=not missing and not retracted,
        )

    return r
