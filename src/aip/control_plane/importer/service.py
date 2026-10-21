"""OpenAPI / Postman → 声明式 Connector / Tool 草稿（预览或落库）。"""

from __future__ import annotations

import re
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from aip.control_plane.conn import service as conn_svc
from aip.control_plane.iam.authz import Actor
from aip.control_plane.importer import openapi as oa
from aip.control_plane.importer import postman as pm
from aip.control_plane.importer.draft import build_drafts
from aip.control_plane.registry import service as registry
from aip.libs.common.errors import ErrorCode, PlatformError

_NAME = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_SEG = re.compile(r"^[a-z][a-z0-9_]*$")


def _require_dev(actor: Actor) -> None:
    actor.require(
        actor.has("asset_developer") or actor.has("platform_admin"),
        "只有资产开发者或平台管理员可以导入",
    )


def _validate_names(connector_name: str, domain: str, system: str) -> None:
    if not _NAME.match(connector_name):
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "connector_name 须为小写标识（3–64）")
    if not _SEG.match(domain) or not _SEG.match(system):
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "domain / system 须为小写标识段")


async def _persist_drafts(
    conn: AsyncConnection,
    actor: Actor,
    *,
    connector_name: str,
    connector_config: dict[str, Any],
    tools: list[dict[str, Any]],
    connection_id: uuid.UUID | None,
    owner_org_id: uuid.UUID | None,
) -> dict[str, Any]:
    if connection_id is None:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "persist=true 时必须提供 connection_id")
    conn_row = await conn_svc.row(conn, connection_id)
    org_id = owner_org_id or conn_row["company_id"]
    iid = await conn_svc.create_connector_instance(
        conn,
        actor,
        name=connector_name,
        kind="declarative",
        connection_id=connection_id,
        config=connector_config,
    )
    drafts: list[dict[str, str]] = []
    for tool in tools:
        aid = await registry.create_asset(
            conn,
            actor,
            type_="tool",
            name=tool["capability_id"],
            owner_org_id=org_id,
            classification=int(tool["data_classification"]),
        )
        vid = await registry.create_version(conn, actor, aid, "0.1.0", tool)
        drafts.append(
            {
                "asset_id": str(aid),
                "version_id": str(vid),
                "capability_id": tool["capability_id"],
                "state": "draft",
            }
        )
    return {"connector_instance_id": str(iid), "drafts": drafts}


async def import_openapi(
    conn: AsyncConnection,
    actor: Actor,
    *,
    document: str,
    connector_name: str,
    domain: str = "imported",
    system: str = "imported",
    persist: bool = False,
    connection_id: uuid.UUID | None = None,
    owner_org_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """解析 OpenAPI，生成声明式 config 与 Tool Spec；可选写入 connector_instance + draft 资产版本。"""
    _require_dev(actor)
    _validate_names(connector_name, domain, system)
    doc = oa.load_document(document)
    operations, warnings = oa.parse_operations(doc)
    connector_config, tools = build_drafts(
        doc, operations, connector_name=connector_name, domain=domain, system=system
    )
    out: dict[str, Any] = {
        "connector_config": connector_config,
        "tools": tools,
        "warnings": warnings,
    }
    if persist:
        out.update(
            await _persist_drafts(
                conn,
                actor,
                connector_name=connector_name,
                connector_config=connector_config,
                tools=tools,
                connection_id=connection_id,
                owner_org_id=owner_org_id,
            )
        )
    return out


async def import_postman(
    conn: AsyncConnection,
    actor: Actor,
    *,
    document: str,
    connector_name: str,
    domain: str = "imported",
    system: str = "imported",
    persist: bool = False,
    connection_id: uuid.UUID | None = None,
    owner_org_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """解析 Postman Collection，生成声明式 config 与 Tool Spec；可选落库。"""
    _require_dev(actor)
    _validate_names(connector_name, domain, system)
    doc = pm.load_collection(document)
    operations, warnings = pm.parse_operations(doc)
    connector_config, tools = build_drafts(
        {}, operations, connector_name=connector_name, domain=domain, system=system
    )
    out: dict[str, Any] = {
        "connector_config": connector_config,
        "tools": tools,
        "warnings": warnings,
    }
    if persist:
        out.update(
            await _persist_drafts(
                conn,
                actor,
                connector_name=connector_name,
                connector_config=connector_config,
                tools=tools,
                connection_id=connection_id,
                owner_org_id=owner_org_id,
            )
        )
    return out
