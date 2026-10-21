"""Skill 包入库、目录与下载（architecture.md 10.4）。"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from sqlalchemy import Column, DateTime, LargeBinary, MetaData, Table, Text, and_, or_, select
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncConnection

from aip.control_plane.iam.authz import Actor
from aip.control_plane.registry import service as registry
from aip.control_plane.registry import tables as rt
from aip.control_plane.skill import package as pkg
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.common.ids import utcnow
from aip.libs.tokens.keys import SigningKey, platform_key, platform_keys

md = MetaData(schema="registry")
U = UUID(as_uuid=True)
skill_package = Table(
    "skill_package",
    md,
    Column("version_id", U, primary_key=True),
    Column("company_id", U, nullable=False),
    Column("zip_bytes", LargeBinary, nullable=False),
    Column("manifest", JSONB, nullable=False),
    Column("signature", Text, nullable=False),
    Column("content_sha256", Text, nullable=False),
    Column("created_at", DateTime(timezone=True)),
)

RETRACTED_STATES = frozenset({"suspended", "retired"})


def _item(asset: dict[str, Any], ver: dict[str, Any], *, channel: str | None = None) -> dict[str, Any]:
    spec = ver["spec"] or {}
    return {
        "name": spec.get("name") or asset["name"],
        "version": ver["semver"],
        "description": spec.get("description", ""),
        "namespace": spec.get("namespace", "global"),
        "state": ver["state"],
        "retracted": ver["state"] in RETRACTED_STATES,
        "channel": channel,
        "tools": list(spec.get("tools") or []),
        "asset_id": str(asset["id"]),
        "version_id": str(ver["id"]),
    }


async def attach_package(
    conn: AsyncConnection,
    actor: Actor,
    version_id: uuid.UUID,
    zip_bytes: bytes,
    *,
    key: SigningKey | None = None,
) -> dict[str, Any]:
    """上传 zip：解析、扫清单、签名入库，并把 frontmatter/lock 写回版本 spec（须 draft）。"""
    actor.require(actor.has("asset_developer") or actor.has("platform_admin"))
    v = await registry.version_row(conn, version_id)
    a = await registry.asset_row(conn, v["asset_id"])
    if a["type"] != "skill":
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "只有 skill 类型资产可以上传 Skill 包")
    if v["state"] != "draft":
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, "只能在 draft 状态上传 Skill 包")
    files = pkg.unpack_zip(zip_bytes)
    described = pkg.validate_and_describe(files)
    described.pop("body_preview", None)
    described = {k: val for k, val in described.items() if val is not None}
    registry.validate_spec("skill", described)
    normalized = pkg.make_zip(files)
    manifest = pkg.build_manifest(files)
    signing = key or platform_key()
    signature = pkg.sign_manifest(manifest, signing)
    digest = hashlib.sha256(normalized).hexdigest()
    await conn.execute(skill_package.delete().where(skill_package.c.version_id == version_id))
    await conn.execute(
        skill_package.insert().values(
            version_id=version_id,
            company_id=v["company_id"],
            zip_bytes=normalized,
            manifest=manifest,
            signature=signature,
            content_sha256=digest,
            created_at=utcnow(),
        )
    )
    await conn.execute(
        rt.asset_version.update()
        .where(rt.asset_version.c.id == version_id)
        .values(spec=described, content_hash=registry.content_hash(described, v.get("dep_lock") or {}))
    )
    return {
        "version_id": str(version_id),
        "name": described["name"],
        "manifest": manifest,
        "signature": signature,
        "content_sha256": digest,
        "files": sorted(manifest),
    }


MAX_PACKAGE_BYTES = 8 * 1024 * 1024


async def save_source(
    conn: AsyncConnection,
    actor: Actor,
    version_id: uuid.UUID,
    source: dict[str, Any],
    *,
    key: SigningKey | None = None,
) -> dict[str, Any]:
    """在线编辑器保存：服务端组装标准目录 zip，再走与上传相同的 attach_package（解析、哈希、签名、写回 spec）。

    额外返回 G0 安全扫描的预检结果（warnings），让作者在提交前就能看到；正式判定仍在 G0。
    """
    from aip.control_plane.gates.service import scan_text

    files = pkg.build_source_files(source)
    raw = pkg.make_zip(files)
    if len(raw) > MAX_PACKAGE_BYTES:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "Skill 包不能超过 8MB")
    result = await attach_package(conn, actor, version_id, raw, key=key)
    texts = [files[pkg.SKILL_MD].decode("utf-8")]
    texts += [data.decode("utf-8") for path, data in files.items() if path.startswith("references/")]
    return {**result, "warnings": scan_text(*texts)}


async def load_source(conn: AsyncConnection, version_id: uuid.UUID) -> dict[str, Any]:
    """把已入库的包拆回编辑器表单（继续修改或作为新版本的起点）。"""
    row = await package_row(conn, version_id)
    files = pkg.unpack_zip(bytes(row["zip_bytes"]))
    return {"version_id": str(version_id), "content_sha256": row["content_sha256"], **pkg.read_source(files)}


async def package_row(conn: AsyncConnection, version_id: uuid.UUID) -> dict[str, Any]:
    row = (await conn.execute(select(skill_package).where(skill_package.c.version_id == version_id))).mappings().first()
    if row is None:
        raise PlatformError(ErrorCode.NOT_FOUND, "Skill 包不存在")
    return dict(row)


async def get_manifest(conn: AsyncConnection, version_id: uuid.UUID) -> dict[str, Any]:
    row = await package_row(conn, version_id)
    return {
        "version_id": str(version_id),
        "manifest": row["manifest"],
        "signature": row["signature"],
        "content_sha256": row["content_sha256"],
    }


async def download_zip(conn: AsyncConnection, version_id: uuid.UUID) -> tuple[bytes, str]:
    row = await package_row(conn, version_id)
    v = await registry.version_row(conn, version_id)
    name = (v["spec"] or {}).get("name") or "skill"
    return bytes(row["zip_bytes"]), f"{name}@{v['semver']}.zip"


async def list_skills(
    conn: AsyncConnection,
    *,
    q: str | None = None,
    channel: str = "stable",
    include_retracted: bool = True,
) -> list[dict[str, Any]]:
    """Skill 目录：优先取指定渠道有 release 的版本；否则取最新已进入发布态的版本（含撤回）。"""
    assets = (
        await conn.execute(select(rt.asset).where(rt.asset.c.type == "skill").order_by(rt.asset.c.name))
    ).mappings()
    out: list[dict[str, Any]] = []
    for asset in assets:
        rel = (
            await conn.execute(
                select(rt.asset_version, rt.release.c.channel)
                .select_from(rt.asset_version.join(rt.release, rt.release.c.version_id == rt.asset_version.c.id))
                .where(rt.asset_version.c.asset_id == asset["id"])
                .where(rt.release.c.channel == channel)
                .where(rt.release.c.status.in_(("active", "canary", "deprecated")))
                .order_by(rt.asset_version.c.created_at.desc())
                .limit(1)
            )
        ).mappings().first()
        if rel is not None:
            ver_dict = {k: rel[k] for k in rel.keys() if k != "channel"}
            item = _item(dict(asset), ver_dict, channel=rel["channel"])
        else:
            ver_row = (
                await conn.execute(
                    select(rt.asset_version)
                    .where(rt.asset_version.c.asset_id == asset["id"])
                    .where(rt.asset_version.c.state.in_(("active", "canary", "deprecated", "suspended", "retired")))
                    .order_by(rt.asset_version.c.created_at.desc())
                    .limit(1)
                )
            ).mappings().first()
            if ver_row is None:
                continue
            item = _item(dict(asset), dict(ver_row), channel=channel)
        if not include_retracted and item["retracted"]:
            continue
        if q and q.lower() not in f"{item['name']} {item['description']}".lower():
            continue
        out.append(item)
    return out


async def resolve_skill(conn: AsyncConnection, name: str, semver: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """按 name@semver 解析（资产名、spec.name 或 capability_id）。"""
    rows = (
        await conn.execute(
            select(rt.asset_version.c.id, rt.asset.c.id.label("asset_id"))
            .select_from(rt.asset_version.join(rt.asset, rt.asset.c.id == rt.asset_version.c.asset_id))
            .where(rt.asset.c.type == "skill")
            .where(rt.asset_version.c.semver == semver)
            .where(
                or_(
                    rt.asset.c.name == name,
                    rt.asset_version.c.spec["name"].astext == name,
                    rt.asset_version.c.spec["capability_id"].astext == name,
                )
            )
            .limit(1)
        )
    ).first()
    if rows is None:
        raise PlatformError(ErrorCode.NOT_FOUND, f"Skill 不存在：{name}@{semver}")
    vid, aid = rows[0], rows[1]
    return await registry.asset_row(conn, aid), await registry.version_row(conn, vid)


async def missing_tool_grants(
    conn: AsyncConnection, *, consumer_id: uuid.UUID, tool_refs: list[str], env: str = "test"
) -> list[str]:
    """安装时检查消费方是否已获 lock 中 Tool 的授权；返回缺失的 cap@ver。"""
    missing: list[str] = []
    for ref in tool_refs:
        cap, _, ver = ref.partition("@")
        tool_asset = (
            await conn.execute(
                select(rt.asset.c.id)
                .select_from(rt.asset_version.join(rt.asset, rt.asset.c.id == rt.asset_version.c.asset_id))
                .where(rt.asset.c.type == "tool")
                .where(rt.asset_version.c.semver == ver)
                .where(rt.asset_version.c.spec["capability_id"].astext == cap)
                .limit(1)
            )
        ).first()
        if tool_asset is None:
            missing.append(ref)
            continue
        grant = (
            await conn.execute(
                select(rt.grant.c.id).where(
                    and_(
                        rt.grant.c.asset_id == tool_asset[0],
                        rt.grant.c.grantee_type == "consumer",
                        rt.grant.c.grantee_id == consumer_id,
                        rt.grant.c.env == env,
                        rt.grant.c.status == "active",
                    )
                )
            )
        ).first()
        if grant is None:
            missing.append(ref)
    return missing


def verify_package_bytes(zip_bytes: bytes, manifest: dict[str, str], signature: str) -> list[str]:
    """CLI verify：验签 + 文件哈希。"""
    if not pkg.verify_manifest(manifest, signature, list(platform_keys())):
        return ["签名无效"]
    files = pkg.unpack_zip(zip_bytes)
    return pkg.local_verify(files, manifest)
