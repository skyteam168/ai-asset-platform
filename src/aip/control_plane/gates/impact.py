"""依赖影响分析（architecture.md 6.4 / G4）：反向枚举受影响的上层资产。"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from aip.control_plane.registry import tables as rt
from aip.libs.workflow.dsl import split_ref

# 参与 G4 回归检查的上层版本状态（已发布或门禁中）
_IMPACT_STATES = frozenset({"testing", "in_review", "approved", "canary", "active", "deprecated"})


def _refs_of(asset_type: str, spec: dict[str, Any]) -> list[str]:
    if asset_type == "workflow":
        return [
            n[k]
            for n in spec.get("nodes", [])
            for k in ("ref", "compensation")
            if n.get("type") == "tool" and n.get(k)
        ]
    if asset_type in ("bundle", "skill"):
        return list(spec.get("tools") or [])
    return []


def _ref_hits_capability(refs: list[str], capability_id: str) -> bool:
    return any(split_ref(r)[0] == capability_id for r in refs)


async def list_impact(conn: AsyncConnection, version_id: uuid.UUID) -> list[dict[str, Any]]:
    """列出因本版本变更而需回归的上层资产版本（每资产取最新非 retired）。"""
    row = (
        await conn.execute(
            select(
                rt.asset_version.c.id,
                rt.asset_version.c.spec,
                rt.asset_version.c.semver,
                rt.asset.c.type,
                rt.asset.c.name,
                rt.asset.c.id.label("asset_id"),
            )
            .select_from(rt.asset_version.join(rt.asset, rt.asset.c.id == rt.asset_version.c.asset_id))
            .where(rt.asset_version.c.id == version_id)
        )
    ).mappings().one()
    asset_type = row["type"]
    if asset_type in ("workflow", "bundle", "skill"):
        return []
    if asset_type == "tool":
        cap = str((row["spec"] or {}).get("capability_id") or row["name"])
        return await _dependents_of_tool(conn, cap, exclude_version=version_id)
    if asset_type == "connector_package":
        # 包名以资产名为准；Tool 经 connector_ref.connector 引用实例名时也可能同名
        return await _dependents_of_connector(conn, str(row["name"]), exclude_version=version_id)
    return []


async def _dependents_of_tool(
    conn: AsyncConnection, capability_id: str, *, exclude_version: uuid.UUID
) -> list[dict[str, Any]]:
    rows = (
        await conn.execute(
            select(
                rt.asset_version.c.id,
                rt.asset_version.c.semver,
                rt.asset_version.c.state,
                rt.asset_version.c.spec,
                rt.asset_version.c.created_at,
                rt.asset.c.id.label("asset_id"),
                rt.asset.c.type,
                rt.asset.c.name,
            )
            .select_from(rt.asset_version.join(rt.asset, rt.asset.c.id == rt.asset_version.c.asset_id))
            .where(rt.asset.c.type.in_(("workflow", "bundle", "skill")))
            .where(rt.asset_version.c.state.in_(_IMPACT_STATES))
            .where(rt.asset_version.c.id != exclude_version)
            .order_by(rt.asset.c.id, rt.asset_version.c.created_at.desc())
        )
    ).mappings().all()
    best: dict[uuid.UUID, dict[str, Any]] = {}
    for r in rows:
        aid = r["asset_id"]
        if aid in best:
            continue  # 已按 created_at desc，第一条即最新
        refs = _refs_of(r["type"], r["spec"] or {})
        if not _ref_hits_capability(refs, capability_id):
            continue
        best[aid] = {
            "asset_id": str(aid),
            "version_id": str(r["id"]),
            "asset_type": r["type"],
            "name": r["name"],
            "capability_id": (r["spec"] or {}).get("capability_id") or r["name"],
            "semver": r["semver"],
            "state": r["state"],
            "refs": refs,
        }
    return sorted(best.values(), key=lambda x: (x["asset_type"], x["name"]))


async def _dependents_of_connector(
    conn: AsyncConnection, connector_name: str, *, exclude_version: uuid.UUID
) -> list[dict[str, Any]]:
    rows = (
        await conn.execute(
            select(
                rt.asset_version.c.id,
                rt.asset_version.c.semver,
                rt.asset_version.c.state,
                rt.asset_version.c.spec,
                rt.asset_version.c.created_at,
                rt.asset.c.id.label("asset_id"),
                rt.asset.c.type,
                rt.asset.c.name,
            )
            .select_from(rt.asset_version.join(rt.asset, rt.asset.c.id == rt.asset_version.c.asset_id))
            .where(rt.asset.c.type == "tool")
            .where(rt.asset_version.c.state.in_(_IMPACT_STATES))
            .where(rt.asset_version.c.id != exclude_version)
            .order_by(rt.asset.c.id, rt.asset_version.c.created_at.desc())
        )
    ).mappings().all()
    best: dict[uuid.UUID, dict[str, Any]] = {}
    for r in rows:
        aid = r["asset_id"]
        if aid in best:
            continue
        ref = ((r["spec"] or {}).get("connector_ref") or {}).get("connector")
        if ref != connector_name:
            continue
        best[aid] = {
            "asset_id": str(aid),
            "version_id": str(r["id"]),
            "asset_type": "tool",
            "name": r["name"],
            "capability_id": (r["spec"] or {}).get("capability_id") or r["name"],
            "semver": r["semver"],
            "state": r["state"],
            "refs": [],
        }
    return sorted(best.values(), key=lambda x: x["name"])
