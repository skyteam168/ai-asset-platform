"""Skill 包入库、目录与下载（architecture.md 10.4）。"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Column, DateTime, LargeBinary, MetaData, Table, Text, and_, or_, select, update
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncConnection

from aip.control_plane.iam.authz import Actor
from aip.control_plane.registry import service as registry
from aip.control_plane.registry import tables as rt
from aip.control_plane.skill import package as pkg
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.common.ids import new_id, utcnow
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
    Column("object_key", Text),
    Column("created_at", DateTime(timezone=True)),
)
skill_script_allowlist = Table(
    "skill_script_allowlist",
    md,
    Column("id", U, primary_key=True),
    Column("company_id", U, nullable=False),
    Column("version_id", U, nullable=False),
    Column("path_glob", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("requested_by", U, nullable=False),
    Column("approved_by", U),
    Column("reason", Text),
    Column("expires_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True)),
)

RETRACTED_STATES = frozenset({"suspended", "retired"})


def _git_url_for(name: str) -> str | None:
    from pathlib import Path

    from aip.control_plane.skill.git_export import bare_repo_path
    from aip.libs.common.config import get_settings

    s = get_settings()
    if not s.skill_git_root:
        return None
    bare = bare_repo_path(Path(s.skill_git_root), name)
    if not bare.exists():
        return None
    if s.skill_git_public_base:
        return f"{s.skill_git_public_base.rstrip('/')}/{name}.git"
    return str(bare)


def _item(asset: dict[str, Any], ver: dict[str, Any], *, channel: str | None = None) -> dict[str, Any]:
    spec = ver["spec"] or {}
    name = spec.get("name") or asset["name"]
    return {
        "name": name,
        "version": ver["semver"],
        "description": spec.get("description", ""),
        "namespace": spec.get("namespace", "global"),
        "state": ver["state"],
        "retracted": ver["state"] in RETRACTED_STATES,
        "channel": channel,
        "tools": list(spec.get("tools") or []),
        "asset_id": str(asset["id"]),
        "version_id": str(ver["id"]),
        "git_url": _git_url_for(name),
    }


def _allowlist_item(row: Any) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "version_id": str(row["version_id"]),
        "path_glob": row["path_glob"],
        "status": row["status"],
        "requested_by": str(row["requested_by"]),
        "approved_by": str(row["approved_by"]) if row["approved_by"] else None,
        "reason": row["reason"],
        "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


async def list_script_allowlist(conn: AsyncConnection, version_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = (
        await conn.execute(
            select(skill_script_allowlist)
            .where(skill_script_allowlist.c.version_id == version_id)
            .order_by(skill_script_allowlist.c.created_at)
        )
    ).mappings()
    return [_allowlist_item(r) for r in rows]


async def active_script_globs(conn: AsyncConnection, version_id: uuid.UUID) -> list[dict[str, Any]]:
    """已审批且未过期的白名单条目（供入库与 G0 证据）。"""
    now = utcnow()
    rows = (
        await conn.execute(
            select(skill_script_allowlist)
            .where(skill_script_allowlist.c.version_id == version_id)
            .where(skill_script_allowlist.c.status == "approved")
            .where(
                or_(
                    skill_script_allowlist.c.expires_at.is_(None),
                    skill_script_allowlist.c.expires_at > now,
                )
            )
        )
    ).mappings()
    return [_allowlist_item(r) for r in rows]


async def request_script_allowlist(
    conn: AsyncConnection,
    actor: Actor,
    version_id: uuid.UUID,
    *,
    path_globs: list[str],
    reason: str | None = None,
    expires_at: datetime | None = None,
) -> list[dict[str, Any]]:
    """资产开发者申请脚本路径白名单；须 data_owner 审批后才生效。"""
    actor.require(actor.has("asset_developer") or actor.has("platform_admin"))
    if not path_globs:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "path_globs 不能为空")
    v = await registry.version_row(conn, version_id)
    a = await registry.asset_row(conn, v["asset_id"])
    if a["type"] != "skill":
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "只有 skill 可以申请脚本白名单")
    if v["state"] != "draft":
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, "只能在 draft 状态申请脚本白名单")
    out: list[dict[str, Any]] = []
    now = utcnow()
    for raw_glob in path_globs:
        glob = pkg.validate_path_glob(raw_glob)
        eid = new_id()
        await conn.execute(
            skill_script_allowlist.insert().values(
                id=eid,
                company_id=v["company_id"],
                version_id=version_id,
                path_glob=glob,
                status="pending",
                requested_by=actor.user_id,
                approved_by=None,
                reason=reason,
                expires_at=expires_at,
                created_at=now,
                updated_at=now,
            )
        )
        row = (
            await conn.execute(select(skill_script_allowlist).where(skill_script_allowlist.c.id == eid))
        ).mappings().one()
        out.append(_allowlist_item(row))
    return out


async def decide_script_allowlist(
    conn: AsyncConnection,
    actor: Actor,
    entry_id: uuid.UUID,
    *,
    approve: bool,
    comment: str = "",
) -> dict[str, Any]:
    """数据 Owner 审批脚本白名单。"""
    actor.require(actor.has("data_owner") or actor.has("platform_admin"), "只有数据 Owner 可以审批脚本白名单")
    row = (
        await conn.execute(select(skill_script_allowlist).where(skill_script_allowlist.c.id == entry_id))
    ).mappings().first()
    if row is None:
        raise PlatformError(ErrorCode.NOT_FOUND, "白名单条目不存在")
    if row["status"] != "pending":
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, f"条目状态 {row['status']} 不可审批")
    actor.require(actor.user_id != row["requested_by"], "不能审批自己申请的脚本白名单")
    status = "approved" if approve else "rejected"
    reason = comment or row["reason"]
    await conn.execute(
        update(skill_script_allowlist)
        .where(skill_script_allowlist.c.id == entry_id)
        .values(status=status, approved_by=actor.user_id, reason=reason, updated_at=utcnow())
    )
    updated = (
        await conn.execute(select(skill_script_allowlist).where(skill_script_allowlist.c.id == entry_id))
    ).mappings().one()
    return _allowlist_item(updated)


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
    allow_rows = await active_script_globs(conn, version_id)
    globs = [r["path_glob"] for r in allow_rows]
    files = pkg.unpack_zip(zip_bytes, allowed_globs=globs)
    described = pkg.validate_and_describe(files)
    described.pop("body_preview", None)
    described = {k: val for k, val in described.items() if val is not None}
    registry.validate_spec("skill", described)
    normalized = pkg.make_zip(files)
    manifest = pkg.build_manifest(files)
    signing = key or platform_key()
    signature = pkg.sign_manifest(manifest, signing)
    digest = hashlib.sha256(normalized).hexdigest()
    from aip.control_plane.skill import storage as skill_store

    object_key = await skill_store.put_skill_zip(
        normalized,
        company_id=str(v["company_id"]),
        version_id=str(version_id),
        content_sha256=digest,
    )
    await conn.execute(skill_package.delete().where(skill_package.c.version_id == version_id))
    await conn.execute(
        skill_package.insert().values(
            version_id=version_id,
            company_id=v["company_id"],
            zip_bytes=normalized,
            manifest=manifest,
            signature=signature,
            content_sha256=digest,
            object_key=object_key,
            created_at=utcnow(),
        )
    )
    await conn.execute(
        rt.asset_version.update()
        .where(rt.asset_version.c.id == version_id)
        .values(spec=described, content_hash=registry.content_hash(described, v.get("dep_lock") or {}))
    )
    script_files = sorted(p for p in files if pkg.is_blocked_script(p))
    return {
        "version_id": str(version_id),
        "name": described["name"],
        "manifest": manifest,
        "signature": signature,
        "content_sha256": digest,
        "files": sorted(manifest),
        "script_allowlist": allow_rows if script_files else [],
        "script_files": script_files,
        "object_key": object_key,
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
    raw = await read_package_bytes(conn, version_id)
    files = pkg.unpack_zip(raw, check_scripts=False)
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


async def read_package_bytes(conn: AsyncConnection, version_id: uuid.UUID) -> bytes:
    """双读：object_key（SeaweedFS）优先，失败或未配置时回落 bytea。"""
    from aip.control_plane.skill import storage as skill_store

    row = await package_row(conn, version_id)
    key = row.get("object_key")
    if key:
        try:
            return await skill_store.get_skill_zip(str(key))
        except Exception:  # noqa: BLE001 — 回落 bytea
            pass
    return bytes(row["zip_bytes"])


async def download_zip(conn: AsyncConnection, version_id: uuid.UUID) -> tuple[bytes, str]:
    v = await registry.version_row(conn, version_id)
    name = (v["spec"] or {}).get("name") or "skill"
    data = await read_package_bytes(conn, version_id)
    return data, f"{name}@{v['semver']}.zip"


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
    files = pkg.unpack_zip(zip_bytes, check_scripts=False)
    return pkg.local_verify(files, manifest)


async def publish_git(
    conn: AsyncConnection,
    actor: Actor,
    version_id: uuid.UUID,
    *,
    git_root: str | None = None,
    public_base: str | None = None,
) -> dict[str, Any]:
    """将 Active/Canary Skill 导出为 bare repo 镜像（与 zip 哈希一致）。"""
    from pathlib import Path

    from aip.control_plane.skill.git_export import publish_skill_to_git
    from aip.libs.common.config import get_settings

    actor.require(actor.has("asset_developer") or actor.has("platform_admin"))
    v = await registry.version_row(conn, version_id)
    a = await registry.asset_row(conn, v["asset_id"])
    if a["type"] != "skill":
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "只有 skill 可以发布到 Git")
    if v["state"] not in ("active", "canary"):
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, "仅 active / canary 可发布 Git 镜像")
    s = get_settings()
    root = git_root if git_root is not None else s.skill_git_root
    if not root:
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, "未配置 AIP_SKILL_GIT_ROOT，Git 分发未启用")
    row = await package_row(conn, version_id)
    name = (v["spec"] or {}).get("name") or a["name"]
    base = public_base if public_base is not None else s.skill_git_public_base
    zip_bytes = await read_package_bytes(conn, version_id)
    return publish_skill_to_git(
        git_root=Path(root),
        name=name,
        semver=v["semver"],
        zip_bytes=zip_bytes,
        content_sha256=row["content_sha256"],
        public_base=base or "",
    )


async def get_git_url(conn: AsyncConnection, version_id: uuid.UUID) -> dict[str, Any]:
    v = await registry.version_row(conn, version_id)
    a = await registry.asset_row(conn, v["asset_id"])
    if a["type"] != "skill":
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "不是 skill 资产")
    name = (v["spec"] or {}).get("name") or a["name"]
    url = _git_url_for(name)
    if not url:
        raise PlatformError(ErrorCode.NOT_FOUND, "Git 镜像不存在或未启用（请先 publish-git）")
    return {"name": name, "version": v["semver"], "git_url": url, "tag": f"v{v['semver']}"}
