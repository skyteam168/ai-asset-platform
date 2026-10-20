"""控制台需要的只读查询接口：版本列表与详情、上限与使用申请列表、审计检索与完整性校验。"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from fastapi import APIRouter
from sqlalchemy import select, text

from aip.control_plane.db import DbScope
from aip.control_plane.grant import flows
from aip.control_plane.iam.api import ActorDep, DbDep
from aip.control_plane.registry import tables as t
from aip.libs.audit.store import verify
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.tokens.keys import platform_keys

V = "/api/v1"
MAX_AUDIT_ROWS = 500


def _out(row: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in dict(row).items():
        if isinstance(v, uuid.UUID | dt.datetime | dt.date):
            out[k] = str(v) if not isinstance(v, dt.datetime) else v.isoformat()
        elif isinstance(v, list):
            out[k] = [str(x) if isinstance(x, uuid.UUID) else x for x in v]
        else:
            out[k] = v
    return out


def router() -> APIRouter:
    r = APIRouter()

    @r.get(f"{V}/assets/{{id}}/versions", tags=["registry"], operation_id="listVersions")
    async def list_versions(id: uuid.UUID, actor: ActorDep, db: DbDep) -> dict[str, Any]:
        async with db.tx(actor.db_scope()) as conn:
            rows = (
                (
                    await conn.execute(
                        select(
                            t.asset_version.c.id,
                            t.asset_version.c.semver,
                            t.asset_version.c.state,
                            t.asset_version.c.content_hash,
                            t.asset_version.c.created_at,
                        )
                        .where(t.asset_version.c.asset_id == id)
                        .order_by(t.asset_version.c.created_at.desc())
                    )
                )
                .mappings()
                .all()
            )
        return {"items": [_out(x) for x in rows], "next_cursor": None, "has_more": False}

    @r.get(f"{V}/versions/{{id}}", tags=["registry"], operation_id="getVersion")
    async def get_version(id: uuid.UUID, actor: ActorDep, db: DbDep) -> dict[str, Any]:
        from aip.control_plane.gates import service as gates

        async with db.tx(actor.db_scope()) as conn:
            row = (await conn.execute(select(t.asset_version).where(t.asset_version.c.id == id))).mappings().first()
            if row is None:
                raise PlatformError(ErrorCode.NOT_FOUND, "版本不存在或不可见")
            cases = (
                (await conn.execute(select(gates.test_case).where(gates.test_case.c.version_id == id))).mappings().all()
            )
            sheet = (
                (await conn.execute(select(gates.acceptance_sheet).where(gates.acceptance_sheet.c.version_id == id)))
                .mappings()
                .first()
            )
        return {**_out(row), "test_cases": [_out(c) for c in cases], "acceptance": _out(sheet) if sheet else None}

    @r.get(f"{V}/grant-ceilings", tags=["grant"], operation_id="listCeilings")
    async def list_ceilings(actor: ActorDep, db: DbDep) -> dict[str, Any]:
        async with db.tx(actor.db_scope()) as conn:
            rows = (
                (await conn.execute(select(t.grant_ceiling).order_by(t.grant_ceiling.c.created_at.desc()).limit(200)))
                .mappings()
                .all()
            )
        return {"items": [_out(x) for x in rows], "next_cursor": None, "has_more": False}

    @r.get(f"{V}/usage-requests", tags=["grant"], operation_id="listUsageRequests")
    async def list_usage(actor: ActorDep, db: DbDep) -> dict[str, Any]:
        async with db.tx(actor.db_scope()) as conn:
            rows = (await conn.execute(select(flows.usage_request).limit(200))).mappings().all()
        return {"items": [_out(x) for x in rows], "next_cursor": None, "has_more": False}

    @r.get(f"{V}/audit-logs", tags=["audit"], operation_id="searchAudit")
    async def search_audit(
        actor: ActorDep,
        db: DbDep,
        kind: str | None = None,
        trace_id: str | None = None,
        since: dt.datetime | None = None,
        until: dt.datetime | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """审计员与平台管理员看全部；分公司管理员只看本公司（architecture.md 2.5）。只读。"""
        global_view = actor.has("auditor") or actor.has("platform_admin")
        companies = {g.scope_company_id for g in actor.roles if g.role == "company_admin"}
        actor.require(global_view or bool(companies), "需要审计员、平台管理员或分公司管理员角色")
        clauses = ["true"]
        params: dict[str, Any] = {"limit": min(max(limit, 1), MAX_AUDIT_ROWS)}
        if kind:
            clauses.append("kind = :kind")
            params["kind"] = kind
        if trace_id:
            clauses.append("record->>'trace_id' = :trace_id")
            params["trace_id"] = trace_id
        if since:
            clauses.append("occurred_at >= :since")
            params["since"] = since
        if until:
            clauses.append("occurred_at < :until")
            params["until"] = until
        if not global_view:
            clauses.append("company_id = ANY(CAST(:companies AS uuid[]))")
            params["companies"] = [str(c) for c in companies]
        # 条件只来自上面的固定片段，取值全部走绑定参数
        where = " AND ".join(clauses)
        sql = (
            "SELECT shard_id, seq, occurred_at, company_id, kind, record, hash FROM audit.audit_log "  # noqa: S608
            f"WHERE {where} ORDER BY occurred_at DESC, seq DESC LIMIT :limit"
        )
        async with db.tx(DbScope.system()) as conn:
            rows = (await conn.execute(text(sql), params)).mappings().all()
        return {"items": [_out(x) for x in rows], "next_cursor": None, "has_more": len(rows) == params["limit"]}

    @r.post(f"{V}/audit-logs/verify", tags=["audit"], operation_id="verifyAudit")
    async def verify_audit(actor: ActorDep, db: DbDep) -> dict[str, Any]:
        actor.require(actor.has("auditor") or actor.has("platform_admin"), "只有审计员与平台管理员可以校验审计链")
        async with db.tx(DbScope.system()) as conn:
            problems = await verify(conn, list(platform_keys()))
            shards = (await conn.execute(text("SELECT count(DISTINCT shard_id), count(*) FROM audit.audit_log"))).one()
        return {"ok": not problems, "problems": [str(p) for p in problems], "shards": shards[0], "records": shards[1]}

    return r
