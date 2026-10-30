"""资产、版本、状态机、发布（architecture.md 6、7）。

状态迁移只能经 transition()，非法迁移抛错；P6 的门禁插件只是 transition() 的调用方。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import uuid
from typing import Any

import jsonschema
from sqlalchemy import and_, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

from aip.control_plane.iam.authz import Actor
from aip.control_plane.iam.orgs import get_org
from aip.control_plane.registry import tables as t
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.common.ids import new_id, utcnow
from aip.libs.tokens.keys import canonical_json

# draft 版本可物理删除（迁移触发器仅保护非 draft）；子表多数无 ON DELETE CASCADE，须按序清理。
_VERSION_CHILD_DELETES = (
    "DELETE FROM registry.selection_eval_run WHERE version_id = :v",
    "DELETE FROM registry.selection_eval_case WHERE version_id = :v",
    "DELETE FROM registry.selection_eval_settings WHERE version_id = :v",
    "DELETE FROM registry.skill_script_allowlist WHERE version_id = :v",
    "DELETE FROM registry.skill_package WHERE version_id = :v",
    "DELETE FROM registry.probe_result WHERE version_id = :v",
    "DELETE FROM registry.contract_drift WHERE version_id = :v",
    "DELETE FROM registry.test_case WHERE version_id = :v",
    "DELETE FROM registry.gate_result WHERE version_id = :v",
    "DELETE FROM registry.acceptance_sheet WHERE version_id = :v",
    "DELETE FROM registry.state_transition WHERE version_id = :v",
)

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


_SPEC_SCHEMAS = {
    "tool": "tool-spec",
    "workflow": "workflow-dsl",
    "bundle": "bundle-spec",
    "skill": "skill-spec",
    "model_profile": "model-profile",
}


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


async def _purge_version_children(conn: AsyncConnection, version_id: uuid.UUID) -> None:
    """清理版本附属表与以 version 为 subject 的审批（无 FK 级联）。"""
    for sql in _VERSION_CHILD_DELETES:
        await conn.execute(text(sql), {"v": version_id})
    await conn.execute(
        text(
            """
            DELETE FROM approval.approval_decision
            WHERE request_id IN (
              SELECT id FROM approval.approval_request WHERE subject_id = :v
            )
            """
        ),
        {"v": version_id},
    )
    await conn.execute(text("DELETE FROM approval.approval_request WHERE subject_id = :v"), {"v": version_id})


async def delete_draft_version(conn: AsyncConnection, actor: Actor, version_id: uuid.UUID) -> uuid.UUID:
    """丢弃草稿版本。已离开 draft 的版本不可删，应走 Deprecated → Retired。"""
    actor.require(actor.has("asset_developer") or actor.has("platform_admin"), "只有资产开发者可以丢弃草稿")
    v = await version_row(conn, version_id)
    if v["state"] != "draft":
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, "只有草稿版本可以删除；已提交版本请走弃用与退役")
    if (await conn.execute(select(t.release.c.id).where(t.release.c.version_id == version_id).limit(1))).first():
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, "该版本已有发布记录，不能删除")
    await _purge_version_children(conn, version_id)
    await conn.execute(t.asset_version.delete().where(t.asset_version.c.id == version_id))
    await _emit(
        conn,
        "version.discarded",
        v["company_id"],
        version_id,
        actor,
        {"asset_id": str(v["asset_id"]), "semver": v["semver"]},
    )
    return uuid.UUID(str(v["asset_id"]))


async def delete_asset(conn: AsyncConnection, actor: Actor, asset_id: uuid.UUID) -> None:
    """删除尚未发布、无授权的资产及其全部草稿版本。"""
    actor.require(actor.has("asset_developer") or actor.has("platform_admin"), "只有资产开发者可以删除资产")
    a = await asset_row(conn, asset_id)
    if (await conn.execute(select(t.release.c.id).where(t.release.c.asset_id == asset_id).limit(1))).first():
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, "资产已有发布记录，不能删除；请走弃用与退役")
    if (await conn.execute(select(t.grant.c.id).where(t.grant.c.asset_id == asset_id).limit(1))).first():
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, "资产仍有授权，不能删除")
    usage = (
        await conn.execute(
            text("SELECT id FROM grants.usage_request WHERE asset_id = :a LIMIT 1"),
            {"a": asset_id},
        )
    ).first()
    if usage is not None:
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, "资产仍有使用申请记录，不能删除")
    versions = (
        await conn.execute(
            select(t.asset_version.c.id, t.asset_version.c.state).where(t.asset_version.c.asset_id == asset_id)
        )
    ).all()
    if any(row[1] != "draft" for row in versions):
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, "存在非草稿版本，不能删除资产")
    for row in versions:
        await _purge_version_children(conn, row[0])
        await conn.execute(t.asset_version.delete().where(t.asset_version.c.id == row[0]))
    from aip.control_plane.registry.fts import zhparser_ready

    if await zhparser_ready(conn):
        await conn.execute(text("DELETE FROM registry.catalog_fts WHERE asset_id = :a"), {"a": asset_id})
    await conn.execute(t.asset.delete().where(t.asset.c.id == asset_id))
    await _emit(conn, "asset.deleted", a["company_id"], asset_id, actor, {"name": a["name"], "type": a["type"]})


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


_RETIRE_IDLE_DAYS = 30


async def deprecate_version(conn: AsyncConnection, actor: Actor, version_id: uuid.UUID, *, reason: str) -> str:
    """Active → Deprecated（architecture.md 7.4）；需平台管理员。"""
    actor.require(actor.has("platform_admin"), "弃用由平台管理员执行")
    why = reason.strip()
    if len(why) < 3:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "弃用理由至少 3 个字符")
    v = await version_row(conn, version_id)
    if v["state"] != "active":
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, f"只有 active 版本可以弃用（当前 {v['state']}）")
    return await transition(conn, actor, version_id, "deprecated", why)


async def retire_version(
    conn: AsyncConnection, actor: Actor, version_id: uuid.UUID, *, reason: str, force: bool = False
) -> str:
    """Deprecated → Retired。默认要求近期无调用；force=true 时总部强制下线并写入理由。"""
    actor.require(actor.has("platform_admin"), "退役由平台管理员执行")
    why = reason.strip()
    if len(why) < 3:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "退役理由至少 3 个字符")
    v = await version_row(conn, version_id)
    if v["state"] != "deprecated":
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, f"只有 deprecated 版本可以退役（当前 {v['state']}）")
    if not force:
        cutoff = utcnow() - dt.timedelta(days=_RETIRE_IDLE_DAYS)
        recent = (
            await conn.execute(
                select(t.grant.c.id)
                .where(
                    and_(
                        t.grant.c.asset_id == v["asset_id"],
                        t.grant.c.status == "active",
                        t.grant.c.last_used_at.is_not(None),
                        t.grant.c.last_used_at > cutoff,
                    )
                )
                .limit(1)
            )
        ).first()
        if recent is not None:
            raise PlatformError(
                ErrorCode.PRECONDITION_FAILED,
                f"近 {_RETIRE_IDLE_DAYS} 天仍有授权调用，不得退役；可 force=true 并记录强制下线原因",
            )
    note = why if not force else f"[force] {why}"
    return await transition(conn, actor, version_id, "retired", note)


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
