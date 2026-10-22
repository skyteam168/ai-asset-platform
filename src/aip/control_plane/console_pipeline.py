"""接入管道汇总（O4）：按公司 / 系统看 Tool 草稿→门禁→发布→Grant 进度。"""

from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from aip.control_plane.conn import service as conn_svc
from aip.control_plane.iam import tables as iam_t
from aip.control_plane.registry import list_enrich
from aip.control_plane.registry import tables as t
from aip.libs.common.ids import utcnow

_STAGES = ("draft", "gating", "published", "granted")


def _empty_counts() -> dict[str, int]:
    return {s: 0 for s in _STAGES}


def _stage(
    asset_id: uuid.UUID,
    state: str | None,
    published: set[uuid.UUID],
    granted: set[uuid.UUID],
) -> str | None:
    """每个资产只记最远阶段。"""
    if asset_id in granted:
        return "granted"
    if asset_id in published:
        return "published"
    if state in ("testing", "in_review", "approved"):
        return "gating"
    if state == "draft":
        return "draft"
    return None


async def access_pipeline(conn: AsyncConnection) -> dict[str, Any]:
    """汇总 type=tool 资产的接入进度（受 RLS / actor.db_scope 约束）。"""
    assets = (
        await conn.execute(select(t.asset).where(t.asset.c.type == "tool").order_by(t.asset.c.created_at.desc()))
    ).mappings().all()
    if not assets:
        active = conn_svc.system_connection.c.status == "active"
        n_conn = len((await conn.execute(select(conn_svc.system_connection.c.id).where(active))).all())
        return {"totals": _empty_counts(), "active_connections": n_conn, "by_company": []}

    asset_ids = [r["id"] for r in assets]
    heads = await list_enrich.latest_versions_by_asset(conn, asset_ids)

    published_rows = (
        await conn.execute(
            select(t.release.c.asset_id)
            .where(t.release.c.status.in_(("active", "canary")))
            .distinct()
        )
    ).all()
    published = {r[0] for r in published_rows}

    now = utcnow()
    granted_rows = (
        await conn.execute(
            select(t.grant.c.asset_id).where(
                and_(t.grant.c.status == "active", t.grant.c.expires_at > now)
            ).distinct()
        )
    ).all()
    granted = {r[0] for r in granted_rows}

    connector_names: list[str] = []
    for aid in asset_ids:
        ver = heads.get(aid)
        if not ver:
            continue
        summ = list_enrich.summary_from_spec(ver.get("spec") if isinstance(ver.get("spec"), dict) else {})
        if summ.get("connector"):
            connector_names.append(str(summ["connector"]))
    systems = await list_enrich.resolve_connector_systems(conn, connector_names)

    company_ids = {r["company_id"] for r in assets if r["company_id"]}
    org_meta: dict[uuid.UUID, tuple[str, str]] = {}
    if company_ids:
        for o in (
            await conn.execute(
                select(iam_t.org_unit.c.id, iam_t.org_unit.c.code, iam_t.org_unit.c.name).where(
                    and_(iam_t.org_unit.c.id.in_(company_ids), iam_t.org_unit.c.type == "company")
                )
            )
        ).mappings():
            org_meta[o["id"]] = (str(o["code"]), str(o["name"]))

    # company_id -> system_key -> counts；system_key 缺省为 "-"
    nested: dict[uuid.UUID, dict[str, dict[str, int]]] = defaultdict(lambda: defaultdict(_empty_counts))
    totals = _empty_counts()

    for a in assets:
        aid = a["id"]
        ver = heads.get(aid)
        state = ver.get("state") if ver else None
        stage = _stage(aid, state, published, granted)
        if stage is None:
            continue
        cid = a["company_id"]
        if cid is None:
            continue
        summ = list_enrich.summary_from_spec(ver.get("spec") if ver and isinstance(ver.get("spec"), dict) else {})
        cname = summ.get("connector")
        sys_key = "-"
        if cname and cname in systems and systems[cname].get("system_key"):
            sys_key = str(systems[cname]["system_key"])
        nested[cid][sys_key][stage] += 1
        totals[stage] += 1

    by_company: list[dict[str, Any]] = []
    for cid, by_sys in sorted(nested.items(), key=lambda x: org_meta.get(x[0], ("", ""))[0]):
        code, name = org_meta.get(cid, (str(cid)[:8], ""))
        company_counts = _empty_counts()
        systems_out: list[dict[str, Any]] = []
        for sk, counts in sorted(by_sys.items(), key=lambda x: x[0]):
            for s in _STAGES:
                company_counts[s] += counts[s]
            systems_out.append({"system_key": sk, **counts})
        by_company.append(
            {
                "company_id": str(cid),
                "company_code": code,
                "company_name": name,
                **company_counts,
                "by_system": systems_out,
            }
        )

    # 顺带给出连接数（便于运营看「有连接但无草稿」）
    active = conn_svc.system_connection.c.status == "active"
    conn_count = (await conn.execute(select(conn_svc.system_connection.c.id).where(active))).all()

    return {
        "totals": totals,
        "active_connections": len(conn_count),
        "by_company": by_company,
    }
