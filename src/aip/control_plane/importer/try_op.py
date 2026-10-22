"""导入预览阶段：用已登记系统连接对声明式 operation 做真连接试调（O1）。

对齐 SQL 模板 try：不经 Runtime / PDP 消费路径，仅供资产开发者确认映射；
写操作须 allow_write，且连接 env 为 sandbox/test，mode=sandbox（架构 7.2 / 9.7）。
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from aip.connectors.base import ExecContext
from aip.connectors.credentials import CredentialManager, SecretStore
from aip.connectors.declarative import DeclarativeConnector
from aip.control_plane.conn import service as conn_svc
from aip.control_plane.iam.authz import Actor
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.httpclient import build_client

_MAX_SAMPLE_ITEMS = 5
_MAX_SAMPLE_BYTES = 8_192


def _require_dev(actor: Actor) -> None:
    actor.require(
        actor.has("asset_developer") or actor.has("platform_admin"),
        "只有资产开发者或平台管理员可以试调",
    )


def _is_write(mapping: dict[str, Any]) -> bool:
    if mapping.get("mode") == "write":
        return True
    if mapping.get("mode") == "read":
        return False
    method = str((mapping.get("request") or {}).get("method") or "GET").upper()
    return method not in ("GET", "HEAD", "OPTIONS")


def _sample(data: Any) -> tuple[Any, int, bool]:
    """截断样本，避免把大包业务数据带回控制台。"""
    truncated = False
    if isinstance(data, list):
        count = len(data)
        sample: Any = data[:_MAX_SAMPLE_ITEMS]
        if count > _MAX_SAMPLE_ITEMS:
            truncated = True
    else:
        count = 1 if data is not None else 0
        sample = data
    raw = json.dumps(sample, ensure_ascii=False, default=str)
    if len(raw.encode("utf-8")) > _MAX_SAMPLE_BYTES:
        truncated = True
        if isinstance(sample, list):
            while sample and len(json.dumps(sample, ensure_ascii=False, default=str).encode()) > _MAX_SAMPLE_BYTES:
                sample = sample[:-1]
        else:
            sample = {"_truncated": True, "preview": raw[:512]}
    return sample, count, truncated


async def try_operation(
    conn: AsyncConnection,
    actor: Actor,
    secrets: SecretStore,
    *,
    connection_id: uuid.UUID,
    connector_config: dict[str, Any],
    operation: str,
    params: dict[str, Any] | None = None,
    allow_write: bool = False,
) -> dict[str, Any]:
    _require_dev(actor)
    ops = connector_config.get("operations")
    if not isinstance(ops, list) or not ops:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "connector_config.operations 不能为空")
    if len(ops) > 200:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "单次试调 operations 过多")
    mapping = next((o for o in ops if isinstance(o, dict) and o.get("operation") == operation), None)
    if mapping is None:
        raise PlatformError(ErrorCode.NOT_FOUND, f"connector_config 中没有操作 {operation}")

    write = _is_write(mapping)
    if write and not allow_write:
        raise PlatformError(
            ErrorCode.SANDBOX_WRITE_BLOCKED,
            "写操作须显式 allow_write=true，且仅允许 sandbox/test 环境连接",
        )

    crow = await conn_svc.row(conn, connection_id)
    cfg = conn_svc.to_config(crow)
    if cfg.scheme not in ("http", "https"):
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "仅支持 http/https 系统连接的声明式试调")
    if cfg.status == "pending_approval":
        cfg.status = "active"  # 与连接测试一致：审批前也可探活/试调
    elif cfg.status != "active":
        raise PlatformError(ErrorCode.TRANSPORT_EXCEPTION_EXPIRED, "连接已停用")

    env = str(crow.get("env") or "")
    if write and env not in ("sandbox", "test"):
        raise PlatformError(ErrorCode.SANDBOX_WRITE_BLOCKED, "写操作试调仅允许 sandbox/test 环境连接")

    mode: str = "sandbox" if write else "live_read"
    allow = await conn_svc.allowlist(conn)
    creds = CredentialManager(connector_config.get("auth") or {}, crow.get("credential_ref"), secrets)
    connector = DeclarativeConnector("importer_try", connector_config, creds)
    ctx = ExecContext(
        trace_id="importer-try",
        deadline=time.monotonic() + 30,
        mode=mode,  # type: ignore[arg-type]
        connection_env=env,
        http=lambda: build_client(cfg, allowlist=allow, trace_id="importer-try"),
    )
    result = await connector.execute(operation, params or {}, ctx)
    sample, count, truncated = _sample(result.data)
    return {
        "ok": True,
        "operation": operation,
        "mode": mode,
        "connection_env": env,
        "item_count": count,
        "truncated": truncated,
        "sample": sample,
        "next_cursor": result.next_cursor,
    }
