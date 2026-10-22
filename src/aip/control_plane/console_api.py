"""控制台需要的只读查询接口：版本列表与详情、上限与使用申请列表、审计检索与完整性校验。"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from fastapi import APIRouter
from sqlalchemy import select, text

from aip.control_plane import console_schemas as out
from aip.control_plane.db import DbScope
from aip.control_plane.grant import flows
from aip.control_plane.iam.api import ActorDep, DbDep
from aip.control_plane.registry import tables as t
from aip.control_plane.registry.schemas import GrantCeiling
from aip.libs.audit.store import verify
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.common.ids import utcnow
from aip.libs.common.paging import Page
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

    @r.get(
        f"{V}/assets/{{id}}/versions",
        tags=["registry"],
        operation_id="listVersions",
        response_model=Page[out.VersionSummary],
    )
    async def list_versions(id: uuid.UUID, actor: ActorDep, db: DbDep) -> Page[out.VersionSummary]:
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
        return Page(items=[out.VersionSummary.model_validate(_out(x)) for x in rows])

    @r.get(
        f"{V}/versions/{{id}}",
        tags=["registry"],
        operation_id="getVersion",
        response_model=out.VersionDetail,
    )
    async def get_version(id: uuid.UUID, actor: ActorDep, db: DbDep) -> out.VersionDetail:
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
        return out.VersionDetail.model_validate(
            {**_out(row), "test_cases": [_out(c) for c in cases], "acceptance": _out(sheet) if sheet else None}
        )

    @r.get(
        f"{V}/grant-ceilings",
        tags=["grant"],
        operation_id="listCeilings",
        response_model=Page[GrantCeiling],
    )
    async def list_ceilings(actor: ActorDep, db: DbDep) -> Page[GrantCeiling]:
        async with db.tx(actor.db_scope()) as conn:
            rows = (
                (await conn.execute(select(t.grant_ceiling).order_by(t.grant_ceiling.c.created_at.desc()).limit(200)))
                .mappings()
                .all()
            )
        items: list[GrantCeiling] = []
        for x in rows:
            d = _out(x)
            if d.get("asset_ids") is not None and not d.get("asset_set"):
                d["asset_set"] = {"asset_ids": d["asset_ids"]}
            items.append(GrantCeiling.model_validate(d))
        return Page(items=items)

    @r.get(
        f"{V}/usage-requests",
        tags=["grant"],
        operation_id="listUsageRequests",
        response_model=Page[out.UsageRequest],
    )
    async def list_usage(actor: ActorDep, db: DbDep) -> Page[out.UsageRequest]:
        async with db.tx(actor.db_scope()) as conn:
            rows = (await conn.execute(select(flows.usage_request).limit(200))).mappings().all()
        return Page(items=[out.UsageRequest.model_validate(_out(x)) for x in rows])

    @r.get(
        f"{V}/audit-logs",
        tags=["audit"],
        operation_id="searchAudit",
        response_model=Page[out.ConsoleAuditRecord],
    )
    async def search_audit(
        actor: ActorDep,
        db: DbDep,
        kind: str | None = None,
        trace_id: str | None = None,
        since: dt.datetime | None = None,
        until: dt.datetime | None = None,
        limit: int = 100,
    ) -> Page[out.ConsoleAuditRecord]:
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
        return Page(
            items=[out.ConsoleAuditRecord.model_validate(_out(x)) for x in rows],
            has_more=len(rows) == params["limit"],
        )

    @r.get(f"{V}/usage", tags=["audit"], operation_id="usageReport", response_model=out.UsageReport)
    async def usage(actor: ActorDep, db: DbDep, days: int = 7) -> out.UsageReport:
        """计量（12.4）：按日、公司、消费方、能力汇总调用、失败与写操作次数，来源是审计库的 tool.execution。"""
        global_view = actor.is_global
        companies = {g.scope_company_id for g in actor.roles if g.role == "company_admin"}
        actor.require(global_view or bool(companies), "需要平台角色或分公司管理员")
        params: dict[str, Any] = {"since": utcnow() - dt.timedelta(days=min(max(days, 1), 90))}
        scope = ""
        if not global_view:
            scope = "AND company_id = ANY(CAST(:companies AS uuid[]))"
            params["companies"] = [str(c) for c in companies]
        sql = (
            "SELECT date_trunc('day', occurred_at)::date AS day, company_id, record->>'consumer_id' AS consumer_id, "  # noqa: S608
            "record->>'capability_id' AS capability_id, count(*) AS calls, "
            "count(*) FILTER (WHERE record->>'status' IN ('failed','unknown')) AS failed, "
            "count(*) FILTER (WHERE record->>'write' = 'true') AS writes "
            "FROM audit.audit_log WHERE kind = 'tool.execution' AND occurred_at >= :since "
            f"{scope} GROUP BY 1, 2, 3, 4 ORDER BY 1 DESC, calls DESC LIMIT 2000"
        )
        async with db.tx(DbScope.system()) as conn:
            rows = (await conn.execute(text(sql), params)).mappings().all()
        return out.UsageReport(items=[out.UsageMeterRow.model_validate(_out(x)) for x in rows])

    @r.post(
        f"{V}/audit-logs/verify",
        tags=["audit"],
        operation_id="verifyAudit",
        response_model=out.AuditVerifyResult,
    )
    async def verify_audit(actor: ActorDep, db: DbDep) -> out.AuditVerifyResult:
        actor.require(actor.has("auditor") or actor.has("platform_admin"), "只有审计员与平台管理员可以校验审计链")
        async with db.tx(DbScope.system()) as conn:
            problems = await verify(conn, list(platform_keys()))
            shards = (await conn.execute(text("SELECT count(DISTINCT shard_id), count(*) FROM audit.audit_log"))).one()
        return out.AuditVerifyResult(
            ok=not problems, problems=[str(p) for p in problems], shards=shards[0], records=shards[1]
        )

    @r.get(
        f"{V}/console/catalog",
        tags=["catalog"],
        operation_id="consoleCatalog",
        response_model=out.ConsoleCatalog,
    )
    async def console_catalog(
        actor: ActorDep, db: DbDep, q: str | None = None
    ) -> out.ConsoleCatalog:
        """控制台资产目录：最新发布快照中的 Tool 与套件（运维视角；消费方可见性以数据面 GET /catalog 为准）。"""
        actor.require(
            actor.has("platform_admin")
            or actor.has("asset_developer")
            or actor.has("data_owner")
            or actor.has("company_admin"),
            "需要平台角色",
        )
        from aip.control_plane.registry.fts import match_in_memory, search_asset_ids, upsert_catalog_fts
        from aip.control_plane.registry.list_enrich import attach_system_fields, summary_from_spec

        async with db.tx(DbScope.system()) as conn:
            row = (
                await conn.execute(
                    select(t.release_snapshot.c.payload).order_by(t.release_snapshot.c.seq.desc()).limit(1)
                )
            ).first()
            releases = list((row[0] if row else {}).get("releases") or [])
            membership: dict[str, list[str]] = {}
            bundles: list[dict[str, Any]] = []
            for rel in releases:
                if rel.get("asset_type") != "bundle" or rel.get("status") != "active":
                    continue
                bname = (rel.get("spec") or {}).get("name") or rel.get("name")
                tools = list((rel.get("spec") or {}).get("tools") or [])
                bundles.append(
                    {
                        "name": bname,
                        "capability_id": rel.get("capability_id"),
                        "version": rel.get("version"),
                        "description": (rel.get("spec") or {}).get("description", ""),
                        "tools": tools,
                    }
                )
                for ref in tools:
                    cap, _, _ = str(ref).partition("@")
                    if cap:
                        membership.setdefault(cap, []).append(bname)
            asset_ids: list[uuid.UUID] = []
            for rel in releases:
                if rel.get("asset_type") != "tool" or rel.get("status") != "active":
                    continue
                aid = rel.get("asset_id")
                if aid:
                    try:
                        asset_ids.append(uuid.UUID(str(aid)))
                    except ValueError:
                        pass
            org_by_asset: dict[str, dict[str, str | None]] = {}
            if asset_ids:
                arows = (
                    await conn.execute(
                        select(t.asset.c.id, t.asset.c.company_id, t.asset.c.owner_org_id).where(
                            t.asset.c.id.in_(asset_ids)
                        )
                    )
                ).mappings()
                for ar in arows:
                    org_by_asset[str(ar["id"])] = {
                        "company_id": str(ar["company_id"]) if ar["company_id"] else None,
                        "owner_org_id": str(ar["owner_org_id"]) if ar["owner_org_id"] else None,
                    }
            items = []
            for rel in releases:
                if rel.get("asset_type") != "tool" or rel.get("status") != "active":
                    continue
                spec = rel.get("spec") or {}
                sm = summary_from_spec(spec)
                aid = str(rel["asset_id"]) if rel.get("asset_id") else None
                org = org_by_asset.get(aid or "") or {}
                items.append(
                    {
                        "capability_id": rel.get("capability_id"),
                        "version": rel.get("version"),
                        "title": sm.get("title") or rel.get("name"),
                        "description": sm.get("description") or "",
                        "risk_level": sm.get("risk_level") or "danger",
                        "egress_class": spec.get("egress_class", "none"),
                        "data_classification": spec.get("data_classification") or rel.get("classification"),
                        "bundles": sorted(set(membership.get(rel.get("capability_id", ""), []))),
                        "connector": sm.get("connector"),
                        "asset_id": aid,
                        "company_id": org.get("company_id") or rel.get("company_id"),
                        "owner_org_id": org.get("owner_org_id") or rel.get("owner_org_id"),
                    }
                )
            items = await attach_system_fields(conn, items)
            # H41：有 zhparser 时物化 search_text；q 优先走 FTS，否则子串降级
            for it in items:
                aid = it.get("asset_id")
                cid = it.get("company_id")
                if not aid or not cid:
                    continue
                try:
                    await upsert_catalog_fts(
                        conn,
                        asset_id=uuid.UUID(str(aid)),
                        company_id=uuid.UUID(str(cid)),
                        search_text=" ".join(
                            str(it.get(k) or "")
                            for k in ("capability_id", "title", "description", "name")
                        ),
                    )
                except ValueError:
                    pass
            qq = (q or "").strip()
            if qq:
                fts_ids = await search_asset_ids(conn, qq)
                if fts_ids is None:
                    items = match_in_memory(items, qq)
                else:
                    allow = {str(i) for i in fts_ids}
                    items = [
                        it
                        for it in items
                        if (it.get("asset_id") and str(it["asset_id"]) in allow)
                        or qq.lower() in str(it.get("capability_id") or "").lower()
                    ]
        items.sort(key=lambda x: x["capability_id"] or "")
        return out.ConsoleCatalog(
            items=[out.CatalogTool.model_validate(i) for i in items],
            bundles=[out.CatalogBundle.model_validate(b) for b in bundles],
        )

    @r.get(f"{V}/console/ops-summary", tags=["catalog"], operation_id="consoleOpsSummary")
    async def ops_summary(actor: ActorDep, db: DbDep) -> dict[str, Any]:
        """运营概览：待办相关计数、活跃漂移、近期风险（H47）。"""
        actor.require(
            actor.has("platform_admin")
            or actor.has("company_admin")
            or actor.has("data_owner")
            or actor.has("auditor"),
            "需要平台角色",
        )
        from aip.control_plane.probe import service as probe
        from aip.control_plane.risk import service as risk

        async with db.tx(actor.db_scope()) as conn:
            drifts = await probe.list_drifts(conn)
            events = await risk.list_events(conn, status="open", limit=20)
            open_risk = len(events)
        def _ts(v: Any) -> str | None:
            if v is None:
                return None
            if hasattr(v, "isoformat"):
                return str(v.isoformat())
            return str(v)

        return {
            "active_drifts": len(drifts),
            "open_risk_events": open_risk,
            "recent_risk_events": [
                {
                    "id": str(e.get("id")),
                    "rule_code": e.get("rule_code"),
                    "severity": e.get("severity"),
                    "status": e.get("status"),
                    "created_at": _ts(e.get("created_at")),
                }
                for e in events[:10]
            ],
            "drift_items": [
                {
                    "version_id": str(d.get("version_id")),
                    "capability_id": d.get("capability_id"),
                    "reason": d.get("reason"),
                    "marked_at": _ts(d.get("marked_at")),
                }
                for d in drifts[:20]
            ],
        }

    return r
