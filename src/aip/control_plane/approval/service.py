"""通用审批引擎（architecture.md 4.3、7.1）：门禁 G5 / G6、使用申请等共用。

- 审批人按角色（总部角色全局生效；company_admin 按生效节点子树）确定；
- excluded_users 实现职责分离（提交人不能审批、G5 验收人不能再做 G6 审核等）；
- required_approvals 支持双人复核；审批代理人在有效期内可代为审批，记录代理关系；
- 到期时间按工作日计算（周末顺延；分公司工作日历后续接入）。
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import ARRAY, Column, DateTime, MetaData, SmallInteger, Table, Text, and_, func, select, update
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncConnection

from aip.control_plane.iam.authz import Actor
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.common.ids import new_id, utcnow

md = MetaData(schema="approval")
U = UUID(as_uuid=True)
TS = DateTime(timezone=True)
approval_request = Table(
    "approval_request",
    md,
    Column("id", U, primary_key=True),
    Column("company_id", U),
    Column("kind", Text),
    Column("subject_id", U),
    Column("submitter", U),
    Column("approver_role", Text),
    Column("approver_scope_org_id", U),
    Column("required_approvals", SmallInteger),
    Column("excluded_users", ARRAY(U)),
    Column("status", Text),
    Column("payload", JSONB),
    Column("due_at", TS),
    Column("created_at", TS),
    Column("decided_at", TS),
)
approval_decision = Table(
    "approval_decision",
    md,
    Column("id", U, primary_key=True),
    Column("request_id", U),
    Column("company_id", U),
    Column("approver", U),
    Column("on_behalf_of", U),
    Column("decision", Text),
    Column("comment", Text),
    Column("decided_at", TS),
)
delegate = Table(
    "delegate",
    md,
    Column("user_id", U, primary_key=True),
    Column("delegate_user_id", U, primary_key=True),
    Column("company_id", U),
    Column("valid_from", TS, primary_key=True),
    Column("valid_to", TS),
)


def business_due(start: dt.datetime, days: int) -> dt.datetime:
    due = start
    left = days
    while left > 0:
        due += dt.timedelta(days=1)
        if due.weekday() < 5:
            left -= 1
    return due


async def create(
    conn: AsyncConnection,
    *,
    kind: str,
    subject_id: uuid.UUID,
    company_id: uuid.UUID,
    submitter: uuid.UUID | None,
    approver_role: str,
    scope_org_id: uuid.UUID | None = None,
    required: int = 1,
    excluded: list[uuid.UUID] | None = None,
    payload: dict[str, Any] | None = None,
    sla_business_days: int = 1,
) -> uuid.UUID:
    rid = new_id()
    excluded_users = sorted({*(excluded or []), *([submitter] if submitter else [])})
    await conn.execute(
        approval_request.insert().values(
            id=rid,
            company_id=company_id,
            kind=kind,
            subject_id=subject_id,
            submitter=submitter,
            approver_role=approver_role,
            approver_scope_org_id=scope_org_id,
            required_approvals=required,
            excluded_users=excluded_users,
            status="pending",
            payload=payload or {},
            due_at=business_due(utcnow(), sla_business_days),
        )
    )
    return rid


async def request_row(conn: AsyncConnection, request_id: uuid.UUID) -> dict[str, Any]:
    r = (await conn.execute(select(approval_request).where(approval_request.c.id == request_id))).mappings().first()
    if r is None:
        raise PlatformError(ErrorCode.NOT_FOUND, "审批单不存在或不可见")
    return dict(r)


def _qualifies(actor: Actor, req: dict[str, Any]) -> bool:
    role = req["approver_role"]
    if role == "company_admin":
        return req["approver_scope_org_id"] is not None and any(
            g.role == "company_admin"
            and req["approver_scope_org_id"] in actor.subtrees.get(g.scope_org_id, frozenset())
            for g in actor.roles
        )
    return actor.has(role) and not actor.system


async def _active_principals(conn: AsyncConnection, actor: Actor) -> list[uuid.UUID]:
    """actor 当前可以代为审批的人（含自己以外的委托人）。"""
    if actor.user_id is None:
        return []
    now = utcnow()
    rows = await conn.execute(
        select(delegate.c.user_id).where(
            and_(delegate.c.delegate_user_id == actor.user_id, delegate.c.valid_from <= now, delegate.c.valid_to > now)
        )
    )
    return [r[0] for r in rows]


async def decide(
    conn: AsyncConnection,
    actor: Actor,
    request_id: uuid.UUID,
    approve: bool,
    comment: str = "",
    on_behalf_of: uuid.UUID | None = None,
) -> str:
    """返回审批单的新状态：pending（还需更多人审批）/ approved / rejected。"""
    from aip.control_plane.iam.users import load_actor

    req = await request_row(conn, request_id)
    if req["status"] != "pending":
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, "审批单已处理")
    acting_as = actor
    if on_behalf_of is not None:
        if on_behalf_of not in await _active_principals(conn, actor):
            raise PlatformError(ErrorCode.PERMISSION_DENIED, "不是该审批人的有效代理人")
        acting_as = await load_actor(conn, on_behalf_of)
    actor.require(_qualifies(acting_as, req), f"需要 {req['approver_role']} 角色")
    excluded = set(req["excluded_users"] or [])
    actor.require(
        actor.user_id not in excluded and acting_as.user_id not in excluded,
        "提交人或已承担其他职责的人不能审批（职责分离）",
    )
    done = {
        r[0]
        for r in await conn.execute(
            select(approval_decision.c.approver).where(approval_decision.c.request_id == request_id)
        )
    }
    if actor.user_id in done or acting_as.user_id in done:
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, "同一人不能重复审批")
    await conn.execute(
        approval_decision.insert().values(
            id=new_id(),
            request_id=request_id,
            company_id=req["company_id"],
            approver=acting_as.user_id,
            on_behalf_of=acting_as.user_id if on_behalf_of else None,
            decision="approve" if approve else "reject",
            comment=comment,
        )
    )
    if not approve:
        status = "rejected"
    else:
        n = (
            await conn.execute(
                select(func.count())
                .select_from(approval_decision)
                .where(and_(approval_decision.c.request_id == request_id, approval_decision.c.decision == "approve"))
            )
        ).scalar_one()
        status = "approved" if n >= req["required_approvals"] else "pending"
    if status != "pending":
        await conn.execute(
            update(approval_request)
            .where(approval_request.c.id == request_id)
            .values(status=status, decided_at=utcnow())
        )
    return status


async def approvers_of(conn: AsyncConnection, request_id: uuid.UUID) -> list[uuid.UUID]:
    rows = await conn.execute(
        select(approval_decision.c.approver).where(
            and_(approval_decision.c.request_id == request_id, approval_decision.c.decision == "approve")
        )
    )
    return [r[0] for r in rows]


async def pending_for(conn: AsyncConnection, actor: Actor) -> list[dict[str, Any]]:
    rows = (
        (
            await conn.execute(
                select(approval_request)
                .where(approval_request.c.status == "pending")
                .order_by(approval_request.c.due_at)
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows if _qualifies(actor, dict(r)) and actor.user_id not in (r["excluded_users"] or [])]


async def add_delegate(conn: AsyncConnection, actor: Actor, delegate_user_id: uuid.UUID, valid_to: dt.datetime) -> None:
    if actor.user_id is None or actor.company_id is None:
        raise PlatformError(ErrorCode.PERMISSION_DENIED, "只能为自己设置代理人")
    if valid_to <= utcnow() or valid_to > utcnow() + dt.timedelta(days=60):
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "代理有效期须在 60 天以内")
    await conn.execute(
        delegate.insert().values(
            user_id=actor.user_id,
            delegate_user_id=delegate_user_id,
            company_id=actor.company_id,
            valid_from=utcnow(),
            valid_to=valid_to,
        )
    )
