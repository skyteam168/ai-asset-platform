"""资产、版本、状态机、发布（architecture.md 6、7）。

状态迁移只能经 transition()，非法迁移抛错；P6 的门禁插件只是 transition() 的调用方。
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import uuid
from typing import Any

import jsonschema
from sqlalchemy import and_, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

from aip.control_plane.iam.authz import Actor
from aip.control_plane.iam.orgs import get_org
from aip.control_plane.registry import tables as t
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.common.ids import new_id, utcnow
from aip.libs.tokens.keys import canonical_json

SPECS = pathlib.Path(__file__).resolve().parents[4] / "docs" / "specs"
ASSET_TYPES = frozenset({"tool", "skill", "workflow", "bundle", "prompt", "model_profile", "connector_package"})

# architecture.md 第 7 章状态机；suspended 只能回到 suspended_from
TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"testing"}),
    "testing": frozenset({"draft", "in_review"}),
    "in_review": frozenset({"draft", "approved"}),
    "approved": frozenset({"canary"}),
    "canary": frozenset({"active", "approved", "suspended"}),
    "active": frozenset({"deprecated", "suspended"}),
    "deprecated": frozenset({"retired", "suspended"}),
    "retired": frozenset(),
    "suspended": frozenset(),
}
RELEASABLE = frozenset({"approved", "canary", "active", "deprecated"})


def _validator(name: str) -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(json.loads((SPECS / f"{name}.schema.json").read_text(encoding="utf-8")))


_SPEC_SCHEMAS = {"tool": "tool-spec", "workflow": "workflow-dsl", "bundle": "bundle-spec"}


def content_hash(spec: dict[str, Any], dep_lock: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json({"spec": spec, "dep_lock": dep_lock})).hexdigest()


def validate_spec(asset_type: str, spec: dict[str, Any]) -> None:
    name = _SPEC_SCHEMAS.get(asset_type)
    if name is None:
        return
    errors = sorted(_validator(name).iter_errors(spec), key=lambda e: list(e.path))
    if errors:
        raise PlatformError(
            ErrorCode.INVALID_ARGUMENT,
            f"{asset_type} spec 不符合 {name}.schema.json",
            [
                {"field": "spec." + ".".join(str(p) for p in e.path), "code": "INVALID_ARGUMENT", "message": e.message}
                for e in errors[:20]
            ],
        )


async def _emit(
    conn: AsyncConnection,
    kind: str,
    company_id: uuid.UUID,
    subject_id: uuid.UUID | None,
    actor: Actor,
    payload: dict[str, Any] | None = None,
) -> None:
    await conn.execute(
        t.domain_event.insert().values(
            company_id=company_id, kind=kind, subject_id=subject_id, actor_id=actor.user_id, payload=payload or {}
        )
    )


async def notify_change(
    conn: AsyncConnection,
    kind: str,
    company_id: uuid.UUID,
    subject_id: uuid.UUID | None,
    actor: Actor,
    payload: dict[str, Any] | None = None,
) -> None:
    """其他模块（连接、Connector 实例）的变更也要触发新的发布快照。"""
    await _emit(conn, kind, company_id, subject_id, actor, payload)


async def asset_row(conn: AsyncConnection, asset_id: uuid.UUID) -> dict[str, Any]:
    row = (await conn.execute(select(t.asset).where(t.asset.c.id == asset_id))).mappings().first()
    if row is None:
        raise PlatformError(ErrorCode.NOT_FOUND, "资产不存在或不可见")
    return dict(row)


async def version_row(conn: AsyncConnection, version_id: uuid.UUID) -> dict[str, Any]:
    row = (await conn.execute(select(t.asset_version).where(t.asset_version.c.id == version_id))).mappings().first()
    if row is None:
        raise PlatformError(ErrorCode.NOT_FOUND, "资产版本不存在或不可见")
    return dict(row)


async def create_asset(
    conn: AsyncConnection, actor: Actor, *, type_: str, name: str, owner_org_id: uuid.UUID, classification: int
) -> uuid.UUID:
    """资产由总部 IT 与开发集中创建（architecture.md 1）。"""
    actor.require(actor.has("asset_developer") or actor.has("platform_admin"), "只有资产开发者可以创建资产")
    if type_ not in ASSET_TYPES or not 1 <= classification <= 4:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "资产类型或密级不合法")
    org = await get_org(conn, owner_org_id)
    if (await conn.execute(select(t.asset.c.id).where(t.asset.c.name == name))).first():
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"资产名 {name} 已存在")
    aid = new_id()
    await conn.execute(
        t.asset.insert().values(
            id=aid,
            type=type_,
            name=name,
            owner_org_id=owner_org_id,
            owner_user_id=actor.user_id,
            company_id=org.company_id,
            classification=classification,
        )
    )
    await _emit(conn, "asset.created", org.company_id, aid, actor, {"name": name, "type": type_})
    return aid


async def create_version(
    conn: AsyncConnection,
    actor: Actor,
    asset_id: uuid.UUID,
    semver: str,
    spec: dict[str, Any],
    dep_lock: dict[str, Any] | None = None,
) -> uuid.UUID:
    actor.require(actor.has("asset_developer") or actor.has("platform_admin"))
    a = await asset_row(conn, asset_id)
    validate_spec(a["type"], spec)
    if a["type"] == "tool" and spec["data_classification"] != a["classification"]:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "Tool Spec 的 data_classification 必须与资产密级一致")
    dep_lock = dep_lock or {}
    if (
        await conn.execute(
            select(t.asset_version.c.id).where(
                and_(t.asset_version.c.asset_id == asset_id, t.asset_version.c.semver == semver)
            )
        )
    ).first():
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"版本 {semver} 已存在，已发布版本不可覆盖")
    vid = new_id()
    try:
        await conn.execute(
            t.asset_version.insert().values(
                id=vid,
                asset_id=asset_id,
                company_id=a["company_id"],
                semver=semver,
                content_hash=content_hash(spec, dep_lock),
                spec=spec,
                dep_lock=dep_lock,
                state="draft",
                submitted_by=actor.user_id,
            )
        )
    except DBAPIError as e:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "版本号格式不合法（需语义化版本）") from e
    await _emit(conn, "version.created", a["company_id"], vid, actor, {"semver": semver})
    return vid


async def update_draft(conn: AsyncConnection, actor: Actor, version_id: uuid.UUID, spec: dict[str, Any]) -> None:
    """应用层保护：只有 draft 可改；数据库触发器是第二道保护。"""
    actor.require(actor.has("asset_developer") or actor.has("platform_admin"))
    v = await version_row(conn, version_id)
    if v["state"] != "draft":
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, "已提交的版本不可修改，请发布新版本")
    a = await asset_row(conn, v["asset_id"])
    validate_spec(a["type"], spec)
    await conn.execute(
        update(t.asset_version)
        .where(t.asset_version.c.id == version_id)
        .values(spec=spec, content_hash=content_hash(spec, v["dep_lock"]), updated_at=utcnow())
    )


async def transition(conn: AsyncConnection, actor: Actor, version_id: uuid.UUID, to_state: str, reason: str) -> str:
    v = await version_row(conn, version_id)
    current = v["state"]
    allowed = TRANSITIONS[current] | (
        {v["suspended_from"]} if current == "suspended" and v["suspended_from"] else set()
    )
    if to_state not in allowed:
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, f"非法状态迁移：{current} → {to_state}")
    values: dict[str, Any] = {"state": to_state, "updated_at": utcnow()}
    if to_state == "suspended":
        values["suspended_from"] = current
    elif current == "suspended":
        values["suspended_from"] = None
    await conn.execute(update(t.asset_version).where(t.asset_version.c.id == version_id).values(**values))
    await conn.execute(
        t.state_transition.insert().values(
            version_id=version_id,
            company_id=v["company_id"],
            from_state=current,
            to_state=to_state,
            reason=reason,
            actor_id=actor.user_id,
        )
    )
    await _emit(
        conn,
        "version.state_changed",
        v["company_id"],
        version_id,
        actor,
        {"from": current, "to": to_state, "reason": reason},
    )
    return to_state


def _check_scope(scope: dict[str, Any]) -> dict[str, Any]:
    keys = set(scope)
    if keys - {"group", "company_ids", "consumer_ids"} or not keys:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "scope 只能是 group、company_ids、consumer_ids")
    return {
        "group": bool(scope.get("group")),
        "company_ids": sorted(str(x) for x in scope.get("company_ids", [])),
        "consumer_ids": sorted(str(x) for x in scope.get("consumer_ids", [])),
    }


async def create_release(
    conn: AsyncConnection,
    actor: Actor,
    version_id: uuid.UUID,
    *,
    env: str,
    channel: str,
    scope: dict[str, Any],
    exposure: list[str] | None = None,
) -> uuid.UUID:
    actor.require(actor.has("platform_admin"), "发布由平台管理员执行（审批流在 P7 接入）")
    v = await version_row(conn, version_id)
    if v["state"] not in RELEASABLE:
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, f"版本状态 {v['state']} 不能发布")
    exposure = sorted(set(exposure or ["rest"]))
    if set(exposure) - {"rest", "mcp", "a2a"}:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "exposure 只能是 rest、mcp、a2a")
    risk = v["spec"].get("risk_level")
    if risk == "danger" and exposure != ["rest"]:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "danger 级别默认不对外暴露（architecture.md 10.2）")
    if v["spec"].get("connector_ref"):
        from aip.control_plane.conn.service import check_release_connection

        await check_release_connection(conn, v["spec"], env)
    norm = _check_scope(scope)
    prev = (
        await conn.execute(
            select(t.release.c.id).where(
                and_(
                    t.release.c.asset_id == v["asset_id"],
                    t.release.c.env == env,
                    t.release.c.channel == channel,
                    t.release.c.status == "active",
                )
            )
        )
    ).first()
    if prev is not None:
        await conn.execute(update(t.release).where(t.release.c.id == prev[0]).values(status="superseded"))
    rid = new_id()
    await conn.execute(
        t.release.insert().values(
            id=rid,
            version_id=version_id,
            asset_id=v["asset_id"],
            company_id=v["company_id"],
            env=env,
            channel=channel,
            scope=norm,
            exposure=exposure,
            status="active",
            prev_release_id=prev[0] if prev else None,
            created_by=actor.user_id,
        )
    )
    await _emit(conn, "release.created", v["company_id"], rid, actor, {"env": env, "channel": channel})
    return rid


async def rollback(conn: AsyncConnection, actor: Actor, release_id: uuid.UUID) -> uuid.UUID:
    """一键回滚到上一个发布；运行中的执行实例钉住启动时的版本（architecture.md 7.4）。"""
    actor.require(actor.has("platform_admin"))
    r = (await conn.execute(select(t.release).where(t.release.c.id == release_id))).mappings().first()
    if r is None:
        raise PlatformError(ErrorCode.NOT_FOUND, "发布不存在")
    if r["status"] != "active" or r["prev_release_id"] is None:
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, "只有存在上一个发布的当前发布可以回滚")
    await conn.execute(update(t.release).where(t.release.c.id == release_id).values(status="rolled_back"))
    await conn.execute(update(t.release).where(t.release.c.id == r["prev_release_id"]).values(status="active"))
    await _emit(
        conn, "release.rolled_back", r["company_id"], release_id, actor, {"restored": str(r["prev_release_id"])}
    )
    return uuid.UUID(str(r["prev_release_id"]))
