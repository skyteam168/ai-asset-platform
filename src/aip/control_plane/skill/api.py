"""Skill Registry HTTP 接口：上传、目录、下载、清单；安装前授权检查。"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, File, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from aip.control_plane.iam.api import ActorDep, DbDep
from aip.control_plane.skill import service as skill
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.tokens.keys import platform_key

V = "/api/v1"
SkillZip = Annotated[UploadFile, File()]


class InstallCheckIn(BaseModel):
    consumer_id: uuid.UUID
    name: str
    version: str
    env: str = Field(default="test", pattern="^(test|staging|prod)$")


def router() -> APIRouter:
    r = APIRouter()

    @r.post(f"{V}/versions/{{id}}/skill-package", tags=["skill"], operation_id="uploadSkillPackage", status_code=201)
    async def upload(id: uuid.UUID, actor: ActorDep, db: DbDep, file: SkillZip) -> dict[str, Any]:
        raw = await file.read()
        if len(raw) > 8 * 1024 * 1024:
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, "Skill 包不能超过 8MB")
        async with db.tx(actor.db_scope()) as conn:
            return await skill.attach_package(conn, actor, id, raw, key=platform_key())

    @r.get(f"{V}/skills", tags=["skill"], operation_id="listSkills")
    async def list_skills(
        actor: ActorDep,
        db: DbDep,
        q: str | None = None,
        channel: str = Query(default="stable", pattern="^(stable|beta)$"),
        include_retracted: bool = True,
    ) -> dict[str, Any]:
        async with db.tx(actor.db_scope()) as conn:
            items = await skill.list_skills(conn, q=q, channel=channel, include_retracted=include_retracted)
        return {"items": items, "next_cursor": None, "has_more": False}

    @r.get(f"{V}/skills/{{name}}/versions/{{version}}/manifest", tags=["skill"], operation_id="getSkillManifest")
    async def manifest(name: str, version: str, actor: ActorDep, db: DbDep) -> dict[str, Any]:
        async with db.tx(actor.db_scope()) as conn:
            asset, ver = await skill.resolve_skill(conn, name, version)
            meta = await skill.get_manifest(conn, ver["id"])
            spec = ver["spec"] or {}
            return {
                **meta,
                "retracted": ver["state"] in skill.RETRACTED_STATES,
                "name": spec.get("name") or asset["name"],
                "version": ver["semver"],
            }

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

    @r.post(f"{V}/skills/install-check", tags=["skill"], operation_id="skillInstallCheck")
    async def install_check(body: InstallCheckIn, actor: ActorDep, db: DbDep) -> dict[str, Any]:
        """安装前检查：返回缺失的 Tool 授权（corp-skill install 调用）。"""
        async with db.tx(actor.db_scope()) as conn:
            _, ver = await skill.resolve_skill(conn, body.name, body.version)
            missing = await skill.missing_tool_grants(
                conn, consumer_id=body.consumer_id, tool_refs=list((ver["spec"] or {}).get("tools") or []), env=body.env
            )
            retracted = ver["state"] in skill.RETRACTED_STATES
        return {
            "name": body.name,
            "version": body.version,
            "retracted": retracted,
            "missing_grants": missing,
            "ok": not missing and not retracted,
        }

    return r
