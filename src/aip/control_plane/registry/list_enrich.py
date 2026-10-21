"""资产 / 目录列表摘要：描述、连接器、系统连接（运营可读）。"""

from __future__ import annotations

import uuid
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from aip.control_plane.conn import service as conn_svc
from aip.control_plane.registry import tables as t

_PREFERRED_STATES = ("active", "canary", "approved", "in_review", "testing", "draft")


def _host_from_url(url: str | None) -> str | None:
    if not url:
        return None
    try:
        p = urlparse(url)
        return p.netloc or None
    except Exception:  # noqa: BLE001
        return None


def summary_from_spec(spec: dict[str, Any] | None) -> dict[str, Any]:
    s = spec or {}
    cref = s.get("connector_ref") if isinstance(s.get("connector_ref"), dict) else {}
    connector = cref.get("connector") if isinstance(cref, dict) else None
    return {
        "title": s.get("title") or s.get("name"),
        "description": (s.get("description") or "") if isinstance(s.get("description"), str) else "",
        "risk_level": s.get("risk_level"),
        "connector": str(connector) if connector else None,
    }


async def resolve_connector_systems(
    conn: AsyncConnection, connector_names: list[str]
) -> dict[str, dict[str, str | None]]:
    """连接器名 → {system_key, system_host}。"""
    names = sorted({n for n in connector_names if n})
    if not names:
        return {}
    rows = (
        await conn.execute(
            select(
                conn_svc.connector_instance.c.name,
                conn_svc.system_connection.c.system_key,
                conn_svc.system_connection.c.base_url,
            )
            .select_from(
                conn_svc.connector_instance.outerjoin(
                    conn_svc.system_connection,
                    conn_svc.system_connection.c.id == conn_svc.connector_instance.c.system_connection_id,
                )
            )
            .where(conn_svc.connector_instance.c.name.in_(names))
        )
    ).mappings()
    out: dict[str, dict[str, str | None]] = {}
    for r in rows:
        out[str(r["name"])] = {
            "system_key": r["system_key"],
            "system_host": _host_from_url(r["base_url"]),
        }
    return out


def _state_pref(state: str | None) -> int:
    try:
        return _PREFERRED_STATES.index(state or "")
    except ValueError:
        return len(_PREFERRED_STATES)


async def latest_versions_by_asset(
    conn: AsyncConnection, asset_ids: list[uuid.UUID]
) -> dict[uuid.UUID, dict[str, Any]]:
    """每个资产一条「最新」版本（优先 active/canary…，同级取 created_at 最新）。"""
    if not asset_ids:
        return {}
    rows = (
        await conn.execute(
            select(t.asset_version)
            .where(t.asset_version.c.asset_id.in_(asset_ids))
            .order_by(t.asset_version.c.created_at.desc())
        )
    ).mappings()
    best: dict[uuid.UUID, dict[str, Any]] = {}
    for r in rows:
        aid = r["asset_id"]
        cur = best.get(aid)
        if cur is None:
            best[aid] = dict(r)
            continue
        pref_new, pref_old = _state_pref(r["state"]), _state_pref(cur.get("state"))
        if pref_new < pref_old or (
            pref_new == pref_old and (r["created_at"] or 0) > (cur.get("created_at") or 0)
        ):
            best[aid] = dict(r)
    return best


async def enrich_asset_rows(conn: AsyncConnection, assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """给资产列表行附加最新版本摘要与系统连接信息。"""
    ids = [a["id"] for a in assets if a.get("id")]
    versions = await latest_versions_by_asset(conn, ids)
    summaries = {aid: summary_from_spec(v.get("spec")) for aid, v in versions.items()}
    connectors = [str(s["connector"]) for s in summaries.values() if s.get("connector")]
    systems = await resolve_connector_systems(conn, connectors)
    out: list[dict[str, Any]] = []
    for a in assets:
        aid = a["id"]
        row = dict(a)
        ver = versions.get(aid)
        sm = summaries.get(aid) or summary_from_spec(None)
        if ver:
            row["latest_semver"] = ver.get("semver")
            row["latest_state"] = ver.get("state")
        row["title"] = sm.get("title")
        row["description"] = sm.get("description") or ""
        row["risk_level"] = sm.get("risk_level")
        conn_name = sm.get("connector")
        row["connector"] = conn_name
        sys = systems.get(conn_name or "") if conn_name else None
        row["system_key"] = (sys or {}).get("system_key")
        row["system_host"] = (sys or {}).get("system_host")
        out.append(row)
    return out


async def attach_system_fields(conn: AsyncConnection, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """目录条目已有 connector 时补 system_key / system_host。"""
    names = [str(i["connector"]) for i in items if i.get("connector")]
    systems = await resolve_connector_systems(conn, names)
    out: list[dict[str, Any]] = []
    for i in items:
        row = dict(i)
        sys = systems.get(str(row["connector"])) if row.get("connector") else None
        row["system_key"] = (sys or {}).get("system_key")
        row["system_host"] = (sys or {}).get("system_host")
        out.append(row)
    return out
