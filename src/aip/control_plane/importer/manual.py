"""手工登记 REST 操作 → 声明式 Connector / Tool 草稿（简易接入向导）。"""

from __future__ import annotations

import re
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from aip.control_plane.iam.authz import Actor
from aip.control_plane.importer.openapi import to_snake
from aip.control_plane.importer.service import _persist_drafts, _require_dev, _validate_names
from aip.libs.common.errors import ErrorCode, PlatformError

_OP = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_PATH = re.compile(r"^/")
_AUTH_MODES = frozenset({"none", "api_key", "bearer", "login", "cookie"})


def _normalize_auth(auth: dict[str, Any] | None) -> dict[str, Any] | None:
    if not auth:
        return None
    if not isinstance(auth, dict):
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "auth 必须是对象")
    mode = str(auth.get("mode") or "none")
    if mode not in _AUTH_MODES:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"不支持的 auth.mode: {mode}")
    if mode == "none":
        return {"mode": "none"}
    return dict(auth)


def build_manual_drafts(
    *,
    connector_name: str,
    domain: str,
    system: str,
    operations: list[dict[str, Any]],
    auth: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    """返回 (connector_config, tool_specs, warnings)。"""
    if not isinstance(operations, list) or not operations:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "operations 不能为空")
    if len(operations) > 100:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "单次最多 100 个操作")
    warnings: list[str] = []
    conn_ops: list[dict[str, Any]] = []
    tools: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in operations:
        if not isinstance(raw, dict):
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, "operation 必须是对象")
        op_name = str(raw.get("operation") or "").strip()
        if not _OP.match(op_name):
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"非法 operation 名: {op_name}")
        if op_name in seen:
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"重复 operation: {op_name}")
        seen.add(op_name)
        method = str(raw.get("method") or "GET").upper()
        if method not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"):
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"不支持的 method: {method}")
        path = str(raw.get("path") or "").strip()
        if not _PATH.match(path):
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, "path 必须以 / 开头")
        mode = str(raw.get("mode") or ("read" if method in ("GET", "HEAD") else "write"))
        if mode not in ("read", "write"):
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, "mode 须为 read 或 write")
        success_when = raw.get("success_when")
        data_path = str(raw.get("data") or "$.data")
        query = raw.get("query") or {}
        if query and not isinstance(query, dict):
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, "query 必须是对象")
        req: dict[str, Any] = {"method": method, "path": path, "headers": {"Accept": "application/json"}}
        if query:
            req["query"] = {str(k): str(v) for k, v in query.items()}
        resp: dict[str, Any] = {"data": data_path}
        if success_when:
            resp["success_when"] = str(success_when)
        mapping: dict[str, Any] = {
            "connector": connector_name,
            "operation": op_name,
            "mode": mode,
            "request": req,
            "response": resp,
        }
        if mode == "write":
            mapping["errors"] = []
        conn_ops.append(mapping)

        title = str(raw.get("title") or op_name).strip()
        desc = str(raw.get("description") or title).strip()
        if len(desc) < 20:
            desc = f"{desc}（简易接入向导登记的只读/写操作草稿）".strip()
            warnings.append(f"{op_name}: 描述过短，已补齐占位说明")
        cap = f"{domain}.{system}.{op_name}"
        tool: dict[str, Any] = {
            "capability_id": cap,
            "domain": domain,
            "name": (title or op_name)[:64],
            "description": desc[:2000],
            "input_schema": {"type": "object", "properties": {}, "additionalProperties": True},
            "output_schema": {"type": "object"},
            "connector_ref": {"connector": connector_name, "operation": op_name},
            "risk_level": "read" if mode == "read" else "write",
            "timeout": {"connect_ms": 2000, "read_ms": 8000, "deadline_ms": 12000},
            "data_classification": 2,
            "identity_mode": "service_account",
            "confirm_policy": "auto",
            "app_identity_allowed": True,
        }
        if tool["risk_level"] == "write":
            tool["idempotency"] = {"idempotent": True}
            tool["recovery"] = "manual"
            tool["reconcile"] = {"operation": op_name, "by": "idempotency_key"}
            tool["app_identity_allowed"] = False
        tools.append(tool)

    config: dict[str, Any] = {"operations": conn_ops}
    norm_auth = _normalize_auth(auth)
    if norm_auth and norm_auth.get("mode") != "none":
        config["auth"] = norm_auth
    return config, tools, warnings


async def import_manual(
    conn: AsyncConnection,
    actor: Actor,
    *,
    connector_name: str,
    domain: str,
    system: str,
    operations: list[dict[str, Any]],
    connection_id: uuid.UUID | None,
    owner_org_id: uuid.UUID | None = None,
    auth: dict[str, Any] | None = None,
    persist: bool = False,
) -> dict[str, Any]:
    _require_dev(actor)
    _validate_names(connector_name, domain, system)
    # system 段允许连字符转 snake
    sys = to_snake(system) if not re.match(r"^[a-z][a-z0-9_]*$", system) else system
    if not re.match(r"^[a-z][a-z0-9_]*$", sys):
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "system 须为小写标识段")
    config, tools, warnings = build_manual_drafts(
        connector_name=connector_name,
        domain=domain,
        system=sys,
        operations=operations,
        auth=auth,
    )
    out: dict[str, Any] = {
        "connector_config": config,
        "tools": tools,
        "warnings": warnings,
        "drafts": [],
        "connector_instance_id": None,
    }
    if persist:
        persisted = await _persist_drafts(
            conn,
            actor,
            connector_name=connector_name,
            connector_config=config,
            tools=tools,
            connection_id=connection_id,
            owner_org_id=owner_org_id,
        )
        out.update(persisted)
    return out
