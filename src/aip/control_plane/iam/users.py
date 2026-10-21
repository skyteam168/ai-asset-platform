"""用户、归属、角色、数据范围、密级（architecture.md 2.2 至 2.5）。"""

from __future__ import annotations

import csv
import datetime as dt
import io
import uuid
from dataclasses import dataclass
from typing import Any

import pyotp
from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from aip.control_plane.iam import tables as t
from aip.control_plane.iam.authz import GLOBAL_ROLES, Actor, RoleGrant
from aip.control_plane.iam.events import emit
from aip.control_plane.iam.orgs import descendants, get_org
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.common.ids import new_id, utcnow
from aip.libs.tokens.secrets_util import hash_password, seal

ROLES = frozenset({"platform_admin", "asset_developer", "data_owner", "business_owner", "company_admin", "auditor"})
HQ_ONLY_ROLES = frozenset({"platform_admin", "asset_developer", "data_owner", "auditor"})
# 平台角色之外的 user_role.role 视为业务角色（须先在 iam.business_role 登记）
MIN_PASSWORD = 12


@dataclass(frozen=True)
class CreatedUser:
    id: uuid.UUID
    totp_provisioning_uri: str | None  # 仅创建时返回一次


async def user_row(conn: AsyncConnection, user_id: uuid.UUID) -> dict[str, Any]:
    row = (await conn.execute(select(t.iam_user).where(t.iam_user.c.id == user_id))).mappings().first()
    if row is None:
        raise PlatformError(ErrorCode.NOT_FOUND, "用户不存在或不可见")
    return dict(row)


async def create_user(
    conn: AsyncConnection,
    actor: Actor,
    *,
    display_name: str,
    home_org_id: uuid.UUID,
    login_name: str | None = None,
    password: str | None = None,
    employee_no: str | None = None,
    email: str | None = None,
    login_enabled: bool = False,
    is_break_glass: bool = False,
    import_batch_id: uuid.UUID | None = None,
) -> CreatedUser:
    org = await get_org(conn, home_org_id)
    actor.require(actor.manages_org(home_org_id), "只能在自己管理的组织子树内创建用户")
    if org.status != "active" or org.type == "group":
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "主归属节点必须是启用的公司、部门或团队")
    totp_uri = None
    password_hash = totp_enc = None
    if login_enabled:
        if not login_name or not password or len(password) < MIN_PASSWORD:
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"可登录用户需要 login_name 与至少 {MIN_PASSWORD} 位口令")
        password_hash = hash_password(password)
        secret = pyotp.random_base32()
        totp_enc = seal(secret)
        totp_uri = pyotp.TOTP(secret).provisioning_uri(name=login_name, issuer_name="AIP")
    for col, val in (("login_name", login_name), ("employee_no", employee_no)):
        if val and (await conn.execute(select(t.iam_user.c.id).where(t.iam_user.c[col] == val))).first():
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"{col} 已存在")
    uid = new_id()
    await conn.execute(
        t.iam_user.insert().values(
            id=uid,
            login_name=login_name,
            display_name=display_name,
            employee_no=employee_no,
            email=email,
            home_org_id=home_org_id,
            company_id=org.company_id,
            login_enabled=login_enabled,
            password_hash=password_hash,
            totp_secret_enc=totp_enc,
            status="active",
            is_break_glass=is_break_glass,
            import_batch_id=import_batch_id,
            failed_logins=0,
        )
    )
    await conn.execute(
        t.user_org_membership.insert().values(
            user_id=uid, org_id=home_org_id, kind="primary", company_id=org.company_id
        )
    )
    await emit(conn, "user.created", org.company_id, uid, actor.user_id)
    return CreatedUser(uid, totp_uri)


async def set_status(conn: AsyncConnection, actor: Actor, user_id: uuid.UUID, status: str) -> None:
    u = await user_row(conn, user_id)
    actor.require(actor.manages_org(u["home_org_id"]))
    await conn.execute(update(t.iam_user).where(t.iam_user.c.id == user_id).values(status=status, updated_at=utcnow()))
    if status != "active":
        await conn.execute(update(t.console_session).where(t.console_session.c.user_id == user_id).values(revoked=True))
    await emit(conn, "user.disabled" if status != "active" else "user.enabled", u["company_id"], user_id, actor.user_id)


async def add_membership(conn: AsyncConnection, actor: Actor, user_id: uuid.UUID, org_id: uuid.UUID) -> None:
    u = await user_row(conn, user_id)
    org = await get_org(conn, org_id)
    actor.require(actor.manages_org(org_id) and actor.manages_org(u["home_org_id"]))
    await conn.execute(
        t.user_org_membership.insert().values(
            user_id=user_id, org_id=org_id, kind="secondary", company_id=org.company_id
        )
    )
    await emit(conn, "user.membership_added", u["company_id"], user_id, actor.user_id, {"org_id": str(org_id)})


async def assign_role(
    conn: AsyncConnection,
    actor: Actor,
    user_id: uuid.UUID,
    role: str,
    scope_org_id: uuid.UUID,
    expires_at: dt.datetime | None = None,
) -> uuid.UUID:
    from aip.control_plane.iam import business_roles as biz

    org = await get_org(conn, scope_org_id)
    is_platform = role in ROLES
    if not is_platform:
        if not await biz.exists(conn, org.company_id, role):
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, "未知角色（平台角色或本公司已登记的业务角色）")
    if role in HQ_ONLY_ROLES and org.type not in ("group", "company"):
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "总部角色的生效节点必须是集团或公司")
    if role == "company_admin" and org.type != "company":
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "分公司管理员的生效节点必须是公司节点")
    # 平台管理员可分配任意角色；分公司管理员可分配业务 Owner 与本公司业务角色
    can_company = actor.manages_org(scope_org_id) and (
        role == "business_owner" or (not is_platform and actor.manages_org(org.company_id))
    )
    actor.require(actor.has("platform_admin") or can_company)
    rid = new_id()
    await conn.execute(
        t.user_role.insert().values(
            id=rid,
            user_id=user_id,
            role=role,
            scope_org_id=scope_org_id,
            company_id=org.company_id,
            granted_by=actor.user_id,
            expires_at=expires_at,
        )
    )
    await emit(conn, "user.role_assigned", org.company_id, user_id, actor.user_id, {"role": role})
    return rid


async def grant_data_scope(
    conn: AsyncConnection, actor: Actor, user_id: uuid.UUID, org_id: uuid.UUID, expires_at: dt.datetime | None = None
) -> None:
    """额外数据范围：公司管理员只能扩大到本公司；跨公司只有总部（平台管理员）可授予。"""
    u = await user_row(conn, user_id)
    org = await get_org(conn, org_id)
    same_company = org.company_id == u["company_id"]
    actor.require(actor.has("platform_admin") or (same_company and actor.manages_org(org_id)))
    await conn.execute(
        t.user_data_scope.insert().values(
            user_id=user_id, org_id=org_id, company_id=u["company_id"], granted_by=actor.user_id, expires_at=expires_at
        )
    )
    await emit(conn, "user.data_scope_granted", u["company_id"], user_id, actor.user_id, {"org_id": str(org_id)})


async def request_clearance(
    conn: AsyncConnection, actor: Actor, user_id: uuid.UUID, level: int, reason: str, expires_at: dt.datetime
) -> uuid.UUID:
    u = await user_row(conn, user_id)
    actor.require(actor.user_id == user_id or actor.manages_org(u["home_org_id"]))
    if expires_at <= utcnow():
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "到期日必须晚于当前时间")
    rid = new_id()
    await conn.execute(
        t.clearance_request.insert().values(
            id=rid,
            user_id=user_id,
            company_id=u["company_id"],
            requested_level=level,
            reason=reason,
            expires_at=expires_at,
            status="pending",
            requested_by=actor.user_id or user_id,
        )
    )
    await emit(conn, "clearance.requested", u["company_id"], user_id, actor.user_id, {"level": level})
    return rid


async def decide_clearance(
    conn: AsyncConnection, actor: Actor, request_id: uuid.UUID, approve: bool, comment: str = ""
) -> str:
    """只有数据 Owner / 安全岗可审批；不能审批自己发起或针对自己的申请（职责分离）。"""
    actor.require(actor.has("data_owner"), "只有数据 Owner / 安全岗可以审批密级")
    req = (
        (await conn.execute(select(t.clearance_request).where(t.clearance_request.c.id == request_id)))
        .mappings()
        .first()
    )
    if req is None:
        raise PlatformError(ErrorCode.NOT_FOUND, "申请不存在")
    if req["status"] != "pending":
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, "申请已处理")
    actor.require(actor.user_id not in (req["requested_by"], req["user_id"]), "不能审批自己发起或针对自己的申请")
    status = "approved" if approve else "rejected"
    await conn.execute(
        update(t.clearance_request)
        .where(t.clearance_request.c.id == request_id)
        .values(status=status, approver=actor.user_id, decided_at=utcnow(), comment=comment)
    )
    if approve:
        await conn.execute(
            update(t.iam_user)
            .where(t.iam_user.c.id == req["user_id"])
            .values(
                clearance_override=req["requested_level"], clearance_expires_at=req["expires_at"], updated_at=utcnow()
            )
        )
    await emit(
        conn, f"clearance.{status}", req["company_id"], req["user_id"], actor.user_id, {"level": req["requested_level"]}
    )
    return status


async def effective_clearance(conn: AsyncConnection, user_id: uuid.UUID, now: dt.datetime | None = None) -> int:
    """个人覆盖值（未过期）→ 最近祖先节点默认值 → L1（architecture.md 2.4）。"""
    now = now or utcnow()
    u = await user_row(conn, user_id)
    if u["clearance_override"] and (u["clearance_expires_at"] is None or u["clearance_expires_at"] > now):
        return int(u["clearance_override"])
    row = (
        await conn.execute(
            select(t.org_unit.c.default_clearance)
            .select_from(t.org_closure.join(t.org_unit, t.org_unit.c.id == t.org_closure.c.ancestor_id))
            .where(and_(t.org_closure.c.descendant_id == u["home_org_id"], t.org_unit.c.default_clearance.is_not(None)))
            .order_by(t.org_closure.c.depth)
            .limit(1)
        )
    ).first()
    return int(row[0]) if row else 1


async def load_actor(conn: AsyncConnection, user_id: uuid.UUID) -> Actor:
    u = await user_row(conn, user_id)
    now = utcnow()
    rows = (
        await conn.execute(
            select(t.user_role.c.role, t.user_role.c.scope_org_id, t.user_role.c.company_id).where(
                and_(
                    t.user_role.c.user_id == user_id,
                    or_(t.user_role.c.expires_at.is_(None), t.user_role.c.expires_at > now),
                )
            )
        )
    ).all()
    grants = tuple(RoleGrant(r.role, r.scope_org_id, r.company_id) for r in rows)
    # 每个角色生效节点的后代集合：company_admin 管理范围、审批与待办的角色覆盖范围都用它判断
    subtrees = {g.scope_org_id: frozenset(await descendants(conn, g.scope_org_id)) for g in grants}
    return Actor(user_id=user_id, company_id=u["company_id"], roles=grants, subtrees=subtrees)


@dataclass
class ImportResult:
    batch_id: uuid.UUID
    dry_run: bool
    created: int
    unchanged: int
    errors: list[dict[str, Any]]


async def import_users_csv(conn: AsyncConnection, actor: Actor, content: str, dry_run: bool) -> ImportResult:
    """CSV 列：display_name, employee_no, home_org_code, email。导入是推送：已存在的 employee_no 视为 unchanged。"""
    batch = new_id()
    reader = csv.DictReader(io.StringIO(content))
    created = unchanged = 0
    errors: list[dict[str, Any]] = []
    org_ids = {r.code: r.id for r in (await conn.execute(select(t.org_unit.c.code, t.org_unit.c.id))).all()}
    for line, row in enumerate(reader, start=2):
        name, emp, code = (
            (row.get("display_name") or "").strip(),
            (row.get("employee_no") or "").strip(),
            (row.get("home_org_code") or "").strip(),
        )
        if not name or not emp or not code:
            errors.append({"line": line, "message": "display_name、employee_no、home_org_code 必填"})
            continue
        org_id = org_ids.get(code)
        if org_id is None or not actor.manages_org(org_id):
            errors.append({"line": line, "message": f"组织 {code} 不存在或不在管理范围内"})
            continue
        if (await conn.execute(select(t.iam_user.c.id).where(t.iam_user.c.employee_no == emp))).first():
            unchanged += 1
            continue
        created += 1
        if not dry_run:
            await create_user(
                conn,
                actor,
                display_name=name,
                home_org_id=org_id,
                employee_no=emp,
                email=(row.get("email") or None),
                import_batch_id=batch,
            )
    return ImportResult(batch, dry_run, created, unchanged, errors)


def is_global_role(role: str) -> bool:
    return role in GLOBAL_ROLES


async def rollback_import(conn: AsyncConnection, actor: Actor, batch_id: uuid.UUID) -> dict[str, int]:
    """按批次回滚导入：尚未被使用的用户删除；已有绑定或角色的只停用，保留历史（architecture.md 2.2）。"""
    rows = (
        await conn.execute(
            select(t.iam_user.c.id, t.iam_user.c.home_org_id, t.iam_user.c.company_id).where(
                t.iam_user.c.import_batch_id == batch_id
            )
        )
    ).all()
    if not rows:
        raise PlatformError(ErrorCode.NOT_FOUND, "导入批次不存在或不可见")
    deleted = disabled = 0
    for uid, home, company in rows:
        actor.require(actor.manages_org(home), "只能回滚自己管理范围内的导入")
        used = (
            await conn.execute(select(t.user_binding.c.id).where(t.user_binding.c.user_id == uid).limit(1))
        ).first() or (await conn.execute(select(t.user_role.c.id).where(t.user_role.c.user_id == uid).limit(1))).first()
        if used:
            await conn.execute(
                update(t.iam_user).where(t.iam_user.c.id == uid).values(status="disabled", updated_at=utcnow())
            )
            await emit(conn, "user.disabled", company, uid, actor.user_id, {"reason": "import_rollback"})
            disabled += 1
        else:
            await conn.execute(t.user_org_membership.delete().where(t.user_org_membership.c.user_id == uid))
            await conn.execute(t.iam_user.delete().where(t.iam_user.c.id == uid))
            await emit(conn, "user.deleted", company, uid, actor.user_id, {"reason": "import_rollback"})
            deleted += 1
    return {"deleted": deleted, "disabled": disabled}
