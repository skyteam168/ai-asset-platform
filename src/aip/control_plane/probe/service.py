"""探针结果入库与契约漂移标记（architecture.md 7.3 第 6 条）。

连续 3 次失败或 schema 无效 → 写入 contract_drift、发领域事件、触发一次 G1 回归（不改版本状态、不 Suspended）。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import Boolean, Column, DateTime, Integer, MetaData, Table, Text, and_, select, update
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncConnection

from aip.control_plane.gates import service as gates
from aip.control_plane.iam.authz import Actor
from aip.control_plane.registry import service as registry
from aip.libs.common.ids import new_id, utcnow

log = logging.getLogger(__name__)
md = MetaData(schema="registry")
U = UUID(as_uuid=True)

probe_result = Table(
    "probe_result",
    md,
    Column("id", U, primary_key=True),
    Column("company_id", U, nullable=False),
    Column("version_id", U, nullable=False),
    Column("capability_id", Text, nullable=False),
    Column("run_at", DateTime(timezone=True), nullable=False),
    Column("outcome", Text, nullable=False),
    Column("latency_ms", Integer),
    Column("schema_valid", Boolean, nullable=False),
    Column("detail", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True)),
)

contract_drift = Table(
    "contract_drift",
    md,
    Column("version_id", U, primary_key=True),
    Column("company_id", U, nullable=False),
    Column("capability_id", Text, nullable=False),
    Column("marked_at", DateTime(timezone=True), nullable=False),
    Column("reason", Text, nullable=False),
    Column("consecutive_failures", Integer, nullable=False),
    Column("last_g1_at", DateTime(timezone=True)),
    Column("cleared_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True)),
)

FAIL_OUTCOMES = frozenset({"failed", "schema_invalid"})
STREAK = 3


async def record_probe(
    conn: AsyncConnection,
    *,
    version_id: uuid.UUID,
    capability_id: str,
    outcome: str,
    schema_valid: bool,
    latency_ms: int | None = None,
    detail: dict[str, Any] | None = None,
    secrets: Any = None,
    resolver: Any = None,
) -> dict[str, Any]:
    """写入探针结果；若连续失败达阈值则标记漂移并跑 G1 回归。"""
    v = await registry.version_row(conn, version_id)
    run_at = utcnow()
    pid = new_id()
    await conn.execute(
        probe_result.insert().values(
            id=pid,
            company_id=v["company_id"],
            version_id=version_id,
            capability_id=capability_id,
            run_at=run_at,
            outcome=outcome,
            latency_ms=latency_ms,
            schema_valid=schema_valid,
            detail=detail or {},
            created_at=run_at,
        )
    )
    failed = outcome in FAIL_OUTCOMES or not schema_valid
    if not failed:
        return {"probe_id": str(pid), "drift": False}

    streak = await _consecutive_failures(conn, version_id)
    if streak < STREAK:
        return {"probe_id": str(pid), "drift": False, "consecutive_failures": streak}

    reason = "schema_invalid" if not schema_valid or outcome == "schema_invalid" else "probe_failed"
    drift = await mark_drift(
        conn,
        version_id=version_id,
        capability_id=capability_id,
        reason=reason,
        consecutive_failures=streak,
        secrets=secrets,
        resolver=resolver,
    )
    return {"probe_id": str(pid), "drift": True, **drift}


async def _consecutive_failures(conn: AsyncConnection, version_id: uuid.UUID) -> int:
    rows = (
        await conn.execute(
            select(probe_result.c.outcome, probe_result.c.schema_valid)
            .where(probe_result.c.version_id == version_id)
            .order_by(probe_result.c.run_at.desc())
            .limit(STREAK)
        )
    ).all()
    n = 0
    for outcome, schema_valid in rows:
        if outcome in FAIL_OUTCOMES or not schema_valid:
            n += 1
        else:
            break
    return n


async def mark_drift(
    conn: AsyncConnection,
    *,
    version_id: uuid.UUID,
    capability_id: str,
    reason: str,
    consecutive_failures: int,
    secrets: Any = None,
    resolver: Any = None,
) -> dict[str, Any]:
    v = await registry.version_row(conn, version_id)
    now = utcnow()
    existing = (
        await conn.execute(select(contract_drift).where(contract_drift.c.version_id == version_id))
    ).mappings().first()
    if existing is None or existing.get("cleared_at") is not None:
        await conn.execute(contract_drift.delete().where(contract_drift.c.version_id == version_id))
        await conn.execute(
            contract_drift.insert().values(
                version_id=version_id,
                company_id=v["company_id"],
                capability_id=capability_id,
                marked_at=now,
                reason=reason,
                consecutive_failures=consecutive_failures,
                last_g1_at=None,
                cleared_at=None,
                created_at=now,
            )
        )
    else:
        await conn.execute(
            update(contract_drift)
            .where(contract_drift.c.version_id == version_id)
            .values(reason=reason, consecutive_failures=consecutive_failures, marked_at=now)
        )

    await registry.notify_change(
        conn,
        "contract.drift",
        v["company_id"],
        version_id,
        Actor.system_actor(),
        {"capability_id": capability_id, "reason": reason, "consecutive_failures": consecutive_failures},
    )

    g1: dict[str, Any] = {}
    if secrets is not None:
        g1 = await gates.run_g1_regression(
            conn, Actor.system_actor(), version_id, secrets=secrets, resolver=resolver
        )
        await conn.execute(
            update(contract_drift).where(contract_drift.c.version_id == version_id).values(last_g1_at=utcnow())
        )
    else:
        log.warning("drift marked without secrets; skip G1 regression version=%s", version_id)

    return {"version_id": str(version_id), "reason": reason, "g1": g1}


async def drift_row(conn: AsyncConnection, version_id: uuid.UUID) -> dict[str, Any] | None:
    row = (
        await conn.execute(
            select(contract_drift).where(
                and_(contract_drift.c.version_id == version_id, contract_drift.c.cleared_at.is_(None))
            )
        )
    ).mappings().first()
    return dict(row) if row else None


async def list_drifts(conn: AsyncConnection) -> list[dict[str, Any]]:
    rows = (
        await conn.execute(
            select(contract_drift)
            .where(contract_drift.c.cleared_at.is_(None))
            .order_by(contract_drift.c.marked_at.desc())
        )
    ).mappings()
    return [dict(r) for r in rows]


async def clear_drift(conn: AsyncConnection, actor: Actor, version_id: uuid.UUID) -> dict[str, Any]:
    """运维确认漂移已处理：标记 cleared_at（不自动改版本状态）。"""
    if not (actor.has("platform_admin") or actor.has("asset_developer") or actor.has("data_owner")):
        from aip.libs.common.errors import ErrorCode, PlatformError

        raise PlatformError(ErrorCode.PERMISSION_DENIED, "无权清除契约漂移")
    row = await drift_row(conn, version_id)
    if row is None:
        from aip.libs.common.errors import ErrorCode, PlatformError

        raise PlatformError(ErrorCode.NOT_FOUND, "无活跃契约漂移")
    await conn.execute(
        update(contract_drift)
        .where(and_(contract_drift.c.version_id == version_id, contract_drift.c.cleared_at.is_(None)))
        .values(cleared_at=utcnow())
    )
    row["cleared_at"] = utcnow()
    return row
