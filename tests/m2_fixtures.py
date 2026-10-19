"""M2 测试共用：一个已发布的读类 Tool、授权上限与授权、签名快照。"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

from aip.control_plane.db import Database, DbScope
from aip.control_plane.grant import service as grants
from aip.control_plane.iam import snapshot as iam_snapshot
from aip.control_plane.iam import tables as iam_t
from aip.control_plane.iam.authz import Actor
from aip.control_plane.registry import service as registry
from aip.control_plane.registry import snapshot as release_snapshot
from aip.libs.common.ids import utcnow
from aip.libs.tokens.keys import SigningKey
from iam_seed import Seed

KEY = SigningKey.generate()


def tool_spec(**over: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "capability_id": "production.mes.query_work_orders",
        "domain": "production",
        "name": "query_work_orders",
        "description": "查询指定工厂的生产工单，按状态过滤，返回工单号、计划数量和状态。",
        "input_schema": {"type": "object", "properties": {"factory_id": {"type": "string"}}},
        "output_schema": {"type": "object"},
        "connector_ref": {"connector": "mes", "operation": "query_work_orders"},
        "risk_level": "read",
        "timeout": {"connect_ms": 2000, "read_ms": 5000, "deadline_ms": 8000},
        "data_classification": 2,
        "identity_mode": "service_account",
        "confirm_policy": "auto",
        "scope_binding": {"factory_id": {"dimension": "company", "map": "by_code", "system_key": "mes"}},
    }
    spec.update(over)
    return spec


async def promote(conn: Any, actor: Actor, vid: uuid.UUID, to: str = "active") -> None:
    for state in ("testing", "in_review", "approved", "canary", "active"):
        await registry.transition(conn, actor, vid, state, "test")
        if state == to:
            return


@dataclass
class Published:
    asset_id: uuid.UUID
    version_id: uuid.UUID
    release_id: uuid.UUID
    ceiling_vn: uuid.UUID
    grant_vn: uuid.UUID


async def publish_tool(
    db: Database,
    s: Seed,
    *,
    spec: dict[str, Any] | None = None,
    classification: int = 2,
    scope: dict[str, Any] | None = None,
    exposure: list[str] | None = None,
    name: str = "mes.query_work_orders",
) -> Published:
    sys_ = Actor.system_actor()
    spec = spec or tool_spec(data_classification=classification)
    async with db.tx(DbScope.system()) as conn:
        aid = await registry.create_asset(
            conn, sys_, type_="tool", name=name, owner_org_id=s.hq, classification=classification
        )
        vid = await registry.create_version(conn, sys_, aid, "1.0.0", spec)
        await promote(conn, sys_, vid)
        rid = await registry.create_release(
            conn,
            sys_,
            vid,
            env="test",
            channel="stable",
            scope=scope or {"company_ids": [s.vn, s.kh]},
            exposure=exposure or ["rest", "mcp"],
        )
        # 旧系统编码：VN01 在 MES 里叫 F001，KH01 叫 F900
        for org, code in ((s.vn, "F001"), (s.kh, "F900")):
            await conn.execute(
                iam_t.org_external_code.insert().values(
                    org_id=org, system_key="mes", external_code=code, company_id=org
                )
            )
        ceiling = await grants.create_ceiling(conn, sys_, company_id=s.vn, asset_ids=[aid], max_classification=3)
        grant = await grants.create_grant(
            conn,
            sys_,
            asset_id=aid,
            grantee_type="consumer",
            grantee_id=s.consumers["vn-ai"],
            env="test",
            expires_at=utcnow() + dt.timedelta(days=30),
            ceiling_id=ceiling,
        )
    return Published(aid, vid, rid, ceiling, grant)


async def snapshots(db: Database) -> tuple[dict[str, Any], dict[str, Any]]:
    async with db.tx(DbScope.system()) as conn:
        await iam_snapshot.publish(conn, KEY)
        await release_snapshot.publish(conn, KEY)
        iam = await iam_snapshot.latest(conn)
        rel = await release_snapshot.latest(conn)
    assert iam and rel
    return iam, rel
