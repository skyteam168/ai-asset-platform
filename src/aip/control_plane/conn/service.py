"""系统连接、传输例外审批、连接测试、Connector 实例（architecture.md 9.2、9.6、9.7）。"""

from __future__ import annotations

import datetime as dt
import json
import re
import uuid
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import jsonschema
from sqlalchemy import (
    ARRAY,
    REAL,
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    Table,
    Text,
    and_,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import CIDR, JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncConnection

from aip.connectors.credentials import CredentialManager, SecretStore
from aip.connectors.upstream_mcp import pin_from_tools_list
from aip.control_plane.iam.authz import Actor
from aip.control_plane.iam.orgs import get_org
from aip.control_plane.registry.service import SPECS, notify_change
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.common.ids import new_id, utcnow
from aip.libs.httpclient import ConnectionConfig, NetworkAllowlist, build_client, map_error, probe, trust_draft
from aip.libs.httpclient.config import Resolver

_REF_OK = re.compile(r"^kv/data/[a-z0-9][a-z0-9_./-]{1,180}$")


def _normalize_http_base_url(base_url: str, scheme: str) -> str:
    """http(s) 的 base_url 只能是 scheme://host[:port]，不能带登录页等路径。"""
    if scheme not in ("http", "https"):
        return base_url
    parts = urlsplit(base_url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "地址须为 http(s)://主机[:端口]")
    path = parts.path or ""
    if path not in ("", "/"):
        raise PlatformError(
            ErrorCode.INVALID_ARGUMENT,
            "地址请只填到主机和端口（例如 http://10.10.130.199:8082），不要带 /Index/login 等路径；接口路径在下一步单独填写",
        )
    if parts.query or parts.fragment:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "地址不要带查询参数或 # 片段")
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))

md = MetaData(schema="conn")
U = UUID(as_uuid=True)
system_connection = Table(
    "system_connection",
    md,
    Column("id", U, primary_key=True),
    Column("company_id", U),
    Column("system_key", Text),
    Column("env", Text),
    Column("base_url", Text),
    Column("scheme", Text),
    Column("tls_mode", Text),
    Column("ca_pem", Text),
    Column("pins", JSONB),
    Column("tls_check_hostname", Boolean),
    Column("pin_enforce_expiry", Boolean),
    Column("block_on_fingerprint_change", Boolean),
    Column("client_cert_ref", Text),
    Column("credential_ref", Text),
    Column("connect_timeout_s", REAL),
    Column("read_timeout_s", REAL),
    Column("transport_exception_reason", Text),
    Column("transport_exception_expires_at", DateTime(timezone=True)),
    Column("transport_approved_by", ARRAY(U)),
    Column("tls_last_seen_fingerprint", Text),
    Column("owner_user_id", U),
    Column("status", Text),
    Column("updated_at", DateTime(timezone=True)),
)
network_allowlist = Table("network_allowlist", md, Column("cidr", CIDR, primary_key=True), Column("description", Text))
connection_test_run = Table(
    "connection_test_run",
    md,
    Column("id", U, primary_key=True),
    Column("connection_id", U),
    Column("company_id", U),
    Column("run_at", DateTime(timezone=True)),
    Column("verdict", Text),
    Column("error_code", Text),
    Column("result", JSONB),
    Column("operator", U),
)
connector_instance = Table(
    "connector_instance",
    md,
    Column("id", U, primary_key=True),
    Column("name", Text),
    Column("company_id", U),
    Column("kind", Text),
    Column("system_connection_id", U),
    Column("config", JSONB),
    Column("concurrency", Integer),
    Column("rate_per_s", REAL),
    Column("burst", Integer),
    Column("status", Text),
)
upstream_mcp_source = Table(
    "upstream_mcp_source",
    md,
    Column("id", U, primary_key=True),
    Column("company_id", U),
    Column("name", Text),
    Column("system_connection_id", U),
    Column("mcp_path", Text),
    Column("tools_manifest_hash", Text),
    Column("pinned_tools", JSONB),
    Column("status", Text),
    Column("created_at", DateTime(timezone=True)),
)

MAX_EXCEPTION_DAYS = 90
_SCHEMA = jsonschema.Draft202012Validator(
    json.loads((SPECS / "system-connection.schema.json").read_text(encoding="utf-8"))
)


def _manages(actor: Actor) -> bool:
    return actor.has("platform_admin") or actor.has("asset_developer")


async def row(conn: AsyncConnection, connection_id: uuid.UUID) -> dict[str, Any]:
    r = (
        (await conn.execute(select(system_connection).where(system_connection.c.id == connection_id)))
        .mappings()
        .first()
    )
    if r is None:
        raise PlatformError(ErrorCode.NOT_FOUND, "连接不存在或不可见")
    return dict(r)


def to_config(r: dict[str, Any]) -> ConnectionConfig:
    return ConnectionConfig.from_dict({**r, "id": str(r["id"])})


async def allowlist(conn: AsyncConnection) -> NetworkAllowlist:
    return NetworkAllowlist.parse([str(c) for (c,) in (await conn.execute(select(network_allowlist.c.cidr))).all()])


async def create_connection(
    conn: AsyncConnection, actor: Actor, *, company_id: uuid.UUID, data: dict[str, Any]
) -> uuid.UUID:
    """传输例外（http、insecure）创建后处于 pending_approval，审批通过才 active。"""
    actor.require(_manages(actor), "连接由总部 IT 维护")
    errors = list(_SCHEMA.iter_errors(data))
    if errors:
        raise PlatformError(
            ErrorCode.INVALID_ARGUMENT,
            "连接配置不合法",
            [
                {"field": ".".join(str(p) for p in e.path) or "$", "code": "INVALID_ARGUMENT", "message": e.message}
                for e in errors[:10]
            ],
        )
    org = await get_org(conn, company_id)
    if org.type != "company":
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "连接按公司登记")
    data = {**data, "base_url": _normalize_http_base_url(str(data["base_url"]), str(data["scheme"]))}
    exception = data["scheme"] == "http" or data.get("tls_mode") == "insecure"
    expires = data.get("transport_exception_expires_at")
    if exception:
        expires_at = dt.datetime.fromisoformat(expires) if isinstance(expires, str) else expires
        if expires_at is None or expires_at > utcnow() + dt.timedelta(days=MAX_EXCEPTION_DAYS):
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"传输例外到期日不能超过 {MAX_EXCEPTION_DAYS} 天")
        data = {**data, "transport_exception_expires_at": expires_at}
    dup = (
        await conn.execute(
            select(system_connection.c.id).where(
                and_(
                    system_connection.c.company_id == company_id,
                    system_connection.c.system_key == data["system_key"],
                    system_connection.c.env == data["env"],
                )
            )
        )
    ).first()
    if dup is not None:
        raise PlatformError(
            ErrorCode.INVALID_ARGUMENT,
            f"该公司下系统「{data['system_key']}」环境「{data['env']}」已登记，请改系统标识或选用已有连接",
        )
    cid = new_id()
    await conn.execute(
        system_connection.insert().values(
            id=cid,
            company_id=company_id,
            system_key=data["system_key"],
            env=data["env"],
            base_url=data["base_url"],
            scheme=data["scheme"],
            tls_mode=data.get("tls_mode"),
            ca_pem=data.get("ca_pem"),
            pins=data.get("pins", []),
            tls_check_hostname=data.get("tls_check_hostname", True),
            pin_enforce_expiry=data.get("pin_enforce_expiry", False),
            block_on_fingerprint_change=data.get("block_on_fingerprint_change", data.get("env") == "prod"),
            client_cert_ref=data.get("client_cert_ref"),
            credential_ref=data.get("credential_ref"),
            transport_exception_reason=data.get("transport_exception_reason"),
            transport_exception_expires_at=data.get("transport_exception_expires_at"),
            transport_approved_by=[],
            owner_user_id=actor.user_id,
            status="pending_approval" if exception else "active",
        )
    )
    if exception:
        # 写入通用审批单 → 铃铛 / 审批待办可见，并走邮件/IM outbox
        await _ensure_transport_approval(conn, await row(conn, cid))
    await notify_change(conn, "connection.created", company_id, cid, actor)
    return cid


async def _pending_transport_request(conn: AsyncConnection, connection_id: uuid.UUID) -> dict[str, Any] | None:
    from aip.control_plane.approval import service as approval

    r = (
        await conn.execute(
            select(approval.approval_request)
            .where(
                and_(
                    approval.approval_request.c.kind == "transport_exception",
                    approval.approval_request.c.subject_id == connection_id,
                    approval.approval_request.c.status == "pending",
                )
            )
            .order_by(approval.approval_request.c.created_at.desc())
            .limit(1)
        )
    ).mappings().first()
    return dict(r) if r else None


async def _ensure_transport_approval(conn: AsyncConnection, connection: dict[str, Any]) -> uuid.UUID:
    """为待审批传输例外补建审批单（新登记必建；历史遗留 pending 连接懒补）。"""
    from aip.control_plane.approval import service as approval

    existing = await _pending_transport_request(conn, connection["id"])
    if existing is not None:
        return existing["id"]
    expires = connection.get("transport_exception_expires_at")
    return await approval.create(
        conn,
        kind="transport_exception",
        subject_id=connection["id"],
        company_id=connection["company_id"],
        submitter=connection.get("owner_user_id"),
        approver_role="data_owner",
        required=2 if connection["env"] == "prod" else 1,
        excluded=[connection["owner_user_id"]] if connection.get("owner_user_id") else [],
        payload={
            "system_key": connection["system_key"],
            "env": connection["env"],
            "scheme": connection["scheme"],
            "tls_mode": connection.get("tls_mode"),
            "base_url": connection["base_url"],
            "reason": connection.get("transport_exception_reason"),
            "expires_at": expires.isoformat() if isinstance(expires, dt.datetime) else expires,
        },
        sla_business_days=2,
    )


async def backfill_transport_approvals(conn: AsyncConnection) -> int:
    """把仍 pending_approval 但缺审批单的连接补进审批待办（铃铛可见）。"""
    rows = (
        await conn.execute(select(system_connection).where(system_connection.c.status == "pending_approval"))
    ).mappings().all()
    n = 0
    for r in rows:
        before = await _pending_transport_request(conn, r["id"])
        await _ensure_transport_approval(conn, dict(r))
        if before is None:
            n += 1
    return n


async def decide_transport_exception(
    conn: AsyncConnection,
    actor: Actor,
    request_id: uuid.UUID,
    approve: bool,
    comment: str = "",
    on_behalf_of: uuid.UUID | None = None,
) -> str:
    """审批待办入口：同步审批单与 system_connection 状态。"""
    from aip.control_plane.approval import service as approval

    req = await approval.request_row(conn, request_id)
    if req["kind"] != "transport_exception":
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "不是传输例外审批单")
    r = await row(conn, req["subject_id"])
    if r["status"] != "pending_approval":
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, "该连接不在待审批状态")
    status = await approval.decide(conn, actor, request_id, approve, comment, on_behalf_of)
    approvers = await approval.approvers_of(conn, request_id)
    if status == "approved":
        conn_status = "active"
    elif status == "rejected":
        conn_status = "disabled"
    else:
        conn_status = "pending_approval"
    await conn.execute(
        update(system_connection)
        .where(system_connection.c.id == r["id"])
        .values(transport_approved_by=approvers, status=conn_status, updated_at=utcnow())
    )
    await notify_change(
        conn,
        "connection.approved" if approve else "connection.rejected",
        r["company_id"],
        r["id"],
        actor,
        {"status": conn_status},
    )
    return status


async def approve_exception(conn: AsyncConnection, actor: Actor, connection_id: uuid.UUID) -> str:
    """数据 Owner / 安全审批；生产连接需两名不同审批人（双人复核），其他环境一名。创建者不能审批。"""
    actor.require(actor.has("data_owner"), "只有数据 Owner / 安全可以审批传输例外")
    r = await row(conn, connection_id)
    if r["status"] != "pending_approval":
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, "该连接不在待审批状态")
    actor.require(actor.user_id != r["owner_user_id"], "不能审批自己登记的连接")
    request_id = await _ensure_transport_approval(conn, r)
    status = await decide_transport_exception(conn, actor, request_id, approve=True)
    return "active" if status == "approved" else "pending_approval"


async def expire_exceptions(conn: AsyncConnection) -> int:
    """到期未复审的传输例外自动禁用（随快照发布执行）。"""
    res = await conn.execute(
        update(system_connection)
        .where(
            and_(
                system_connection.c.status == "active",
                system_connection.c.transport_exception_expires_at.is_not(None),
                system_connection.c.transport_exception_expires_at <= utcnow(),
            )
        )
        .values(status="disabled")
    )
    return res.rowcount or 0


async def test_connection(
    conn: AsyncConnection, actor: Actor, connection_id: uuid.UUID, resolver: Resolver | None = None
) -> dict[str, Any]:
    actor.require(_manages(actor) or actor.has("data_owner"))
    r = await row(conn, connection_id)
    cfg = to_config(r)
    if cfg.status == "pending_approval":
        cfg.status = "active"  # 审批前也允许测试连通性（不携带凭据）
    result = await probe(cfg, allowlist=await allowlist(conn), resolver=resolver)
    await conn.execute(
        connection_test_run.insert().values(
            id=new_id(),
            connection_id=connection_id,
            company_id=r["company_id"],
            verdict=result["verdict"],
            error_code=result["error_code"],
            result=result,
            operator=actor.user_id,
        )
    )
    return result


async def draft_trust(conn: AsyncConnection, actor: Actor, connection_id: uuid.UUID) -> dict[str, Any]:
    actor.require(_manages(actor))
    last = (
        await conn.execute(
            select(connection_test_run.c.result)
            .where(connection_test_run.c.connection_id == connection_id)
            .order_by(connection_test_run.c.run_at.desc())
            .limit(1)
        )
    ).first()
    if last is None:
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, "请先执行连接测试")
    return trust_draft(last[0], "pinned")


async def put_credentials(
    conn: AsyncConnection,
    actor: Actor,
    secrets: SecretStore,
    connection_id: uuid.UUID,
    *,
    username: str | None = None,
    password: str | None = None,
    api_key: str | None = None,
    token: str | None = None,
    credential_ref: str | None = None,
) -> dict[str, Any]:
    """写入 OpenBao；响应只回 credential_ref。口令不进审计与日志。"""
    actor.require(_manages(actor), "连接由总部 IT 维护")
    r = await row(conn, connection_id)
    ref = (credential_ref or r.get("credential_ref") or f"kv/data/{r['system_key']}/{r['env']}").strip()
    if not _REF_OK.match(ref):
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "credential_ref 须形如 kv/data/system/env")
    data: dict[str, str] = {}
    if username is not None:
        data["username"] = username
    if password is not None:
        data["password"] = password
    if api_key is not None:
        data["api_key"] = api_key
    if token is not None:
        data["token"] = token
    if not data:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "至少提供一项凭据字段")
    # 合并已有密钥字段，避免只更新 password 时丢掉 username
    try:
        existing = await secrets.get(ref)
    except PlatformError:
        existing = {}
    merged = {**existing, **data}
    await secrets.put(ref, merged)
    if r.get("credential_ref") != ref:
        await conn.execute(
            update(system_connection)
            .where(system_connection.c.id == connection_id)
            .values(credential_ref=ref, updated_at=utcnow())
        )
    await notify_change(
        conn,
        "connection.credentials_updated",
        r["company_id"],
        connection_id,
        actor,
        {"credential_ref": ref, "fields": sorted(data.keys())},
    )
    return {"id": str(connection_id), "credential_ref": ref}


async def test_auth(
    conn: AsyncConnection,
    actor: Actor,
    secrets: SecretStore,
    connection_id: uuid.UUID,
    *,
    auth: dict[str, Any],
) -> dict[str, Any]:
    """用连接上的 credential_ref + 传入 auth 配置测登录；不回传 Cookie/口令。"""
    actor.require(_manages(actor) or actor.has("data_owner"))
    if not isinstance(auth, dict):
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "auth 必须是对象")
    mode = str(auth.get("mode") or "none")
    if mode not in ("none", "api_key", "bearer", "login", "cookie"):
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"不支持的 auth.mode: {mode}")
    r = await row(conn, connection_id)
    cfg = to_config(r)
    if cfg.status == "pending_approval":
        cfg.status = "active"
    elif cfg.status != "active":
        raise PlatformError(ErrorCode.TRANSPORT_EXCEPTION_EXPIRED, "连接已停用")
    if cfg.scheme not in ("http", "https"):
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "仅 http/https 连接支持测登录")
    allow = await allowlist(conn)
    mgr = CredentialManager(auth, r.get("credential_ref"), secrets)
    try:
        result = await mgr.probe(lambda: build_client(cfg, allowlist=allow, trace_id="conn-test-auth"))
    except PlatformError as e:
        return {
            "ok": False,
            "mode": mode,
            "detail": e.detail,
            "error_code": str(e.code),
        }
    return result


async def delete_connector_instance(conn: AsyncConnection, actor: Actor, instance_id: uuid.UUID) -> None:
    """删除 Connector 实例及其 SQL 模板；被 Tool Spec 引用时由调用方自行处理快照失效。"""
    actor.require(_manages(actor), "连接器由总部 IT 维护")
    inst = (
        await conn.execute(select(connector_instance).where(connector_instance.c.id == instance_id))
    ).mappings().first()
    if inst is None:
        raise PlatformError(ErrorCode.NOT_FOUND, "连接器实例不存在或不可见")
    await conn.execute(
        text("DELETE FROM conn.sql_query_template WHERE connector_instance_id = :i"), {"i": instance_id}
    )
    await conn.execute(connector_instance.delete().where(connector_instance.c.id == instance_id))
    await notify_change(conn, "connector.deleted", inst["company_id"], instance_id, actor, {"name": inst["name"]})


async def delete_connection(conn: AsyncConnection, actor: Actor, connection_id: uuid.UUID) -> None:
    """删除系统连接。仍有 Connector 实例或上游 MCP 源绑定时拒绝。"""
    from aip.control_plane.approval import service as approval

    actor.require(_manages(actor), "连接由总部 IT 维护")
    r = await row(conn, connection_id)
    bound = (
        await conn.execute(
            select(connector_instance.c.id).where(connector_instance.c.system_connection_id == connection_id).limit(1)
        )
    ).first()
    if bound is not None:
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, "仍有连接器实例绑定该连接，请先删除实例")
    upstream = (
        await conn.execute(
            select(upstream_mcp_source.c.id).where(upstream_mcp_source.c.system_connection_id == connection_id).limit(1)
        )
    ).first()
    if upstream is not None:
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, "仍有上游 MCP 源绑定该连接，请先禁用或删除")
    pending = await _pending_transport_request(conn, connection_id)
    if pending is not None:
        await conn.execute(
            update(approval.approval_request)
            .where(approval.approval_request.c.id == pending["id"])
            .values(status="cancelled", decided_at=utcnow())
        )
    await conn.execute(text("DELETE FROM conn.connection_test_run WHERE connection_id = :c"), {"c": connection_id})
    await conn.execute(system_connection.delete().where(system_connection.c.id == connection_id))
    await notify_change(
        conn, "connection.deleted", r["company_id"], connection_id, actor, {"system_key": r["system_key"]}
    )


async def preview_upstream_mcp_hash(
    conn: AsyncConnection,
    actor: Actor,
    *,
    connection_id: uuid.UUID,
    mcp_path: str = "/mcp",
) -> dict[str, Any]:
    """对系统连接发起 tools/list，计算钉住哈希（ADR-0014）；出站经 build_client。"""
    actor.require(_manages(actor), "上游 MCP 源由总部 IT 维护")
    sc = await row(conn, connection_id)
    if sc["scheme"] not in ("https", "http"):
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "上游 MCP 预览须绑定 HTTP(S) 系统连接")
    path = (mcp_path or "/mcp").strip() or "/mcp"
    if not path.startswith("/"):
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "mcp_path 须以 / 开头")
    cfg = to_config(sc)
    if cfg.status == "pending_approval":
        cfg.status = "active"
    elif cfg.status != "active":
        raise PlatformError(ErrorCode.TRANSPORT_EXCEPTION_EXPIRED, "连接已停用")
    allow = await allowlist(conn)
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    try:
        async with build_client(cfg, allowlist=allow, trace_id="upstream-mcp-preview") as client:
            resp = await client.post(path, json=rpc)
    except Exception as e:
        raise map_error(e) from e
    if resp.status_code >= 400:
        raise PlatformError(ErrorCode.DOWNSTREAM_UNAVAILABLE, f"上游 MCP HTTP {resp.status_code}")
    try:
        payload = resp.json()
    except ValueError as e:
        raise PlatformError(ErrorCode.DOWNSTREAM_UNAVAILABLE, "上游 MCP 返回非 JSON") from e
    if not isinstance(payload, dict):
        raise PlatformError(ErrorCode.DOWNSTREAM_UNAVAILABLE, "上游 MCP 响应格式错误")
    if "error" in payload:
        err_obj = payload["error"] if isinstance(payload["error"], dict) else {}
        raise PlatformError(
            ErrorCode.DOWNSTREAM_UNAVAILABLE,
            f"上游 MCP 错误：{err_obj.get('message', err_obj)}",
        )
    result = payload.get("result")
    if not isinstance(result, dict):
        raise PlatformError(ErrorCode.DOWNSTREAM_UNAVAILABLE, "上游 MCP 缺少 result")
    tools_raw = result.get("tools")
    if not isinstance(tools_raw, list):
        raise PlatformError(ErrorCode.DOWNSTREAM_UNAVAILABLE, "上游 tools/list 无效")
    pin = pin_from_tools_list([t for t in tools_raw if isinstance(t, dict)])
    return {
        "tools_manifest_hash": pin["tools_manifest_hash"],
        "tools": pin["pinned_tools"],
        "mcp_path": path,
        "system_connection_id": str(connection_id),
    }


async def create_upstream_mcp_source(
    conn: AsyncConnection,
    actor: Actor,
    *,
    name: str,
    connection_id: uuid.UUID,
    tools_manifest_hash: str,
    mcp_path: str = "/mcp",
    pinned_tools: list[dict[str, Any]] | None = None,
) -> uuid.UUID:
    """登记上游 MCP 源（architecture.md 9.1 / ADR-0014）：钉住清单哈希。"""
    actor.require(_manages(actor), "上游 MCP 源由总部 IT 维护")
    key = name.strip()
    if not key or len(key) > 64:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "名称须 1–64 字符")
    digest = tools_manifest_hash.strip().lower()
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "tools_manifest_hash 须为 64 位小写十六进制")
    path = (mcp_path or "/mcp").strip() or "/mcp"
    if not path.startswith("/"):
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "mcp_path 须以 / 开头")
    sc = await row(conn, connection_id)
    if sc["scheme"] not in ("https", "http"):
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "上游 MCP 源须绑定 HTTP(S) 系统连接")
    sid = new_id()
    await conn.execute(
        upstream_mcp_source.insert().values(
            id=sid,
            company_id=sc["company_id"],
            name=key,
            system_connection_id=connection_id,
            mcp_path=path,
            tools_manifest_hash=digest,
            pinned_tools=pinned_tools or [],
            status="active",
            created_at=utcnow(),
        )
    )
    await notify_change(
        conn, "upstream_mcp.registered", sc["company_id"], sid, actor, {"name": key, "hash": digest[:12]}
    )
    return sid


async def list_upstream_mcp_sources(conn: AsyncConnection, actor: Actor) -> list[dict[str, Any]]:
    actor.require(_manages(actor) or actor.has("data_owner") or actor.has("auditor"), "无权查看上游 MCP 源")
    rows = (
        await conn.execute(select(upstream_mcp_source).order_by(upstream_mcp_source.c.name).limit(200))
    ).mappings().all()
    return [dict(x) for x in rows]


async def disable_upstream_mcp_source(conn: AsyncConnection, actor: Actor, source_id: uuid.UUID) -> None:
    actor.require(_manages(actor), "上游 MCP 源由总部 IT 维护")
    r = (
        await conn.execute(select(upstream_mcp_source).where(upstream_mcp_source.c.id == source_id))
    ).mappings().first()
    if r is None:
        raise PlatformError(ErrorCode.NOT_FOUND, "上游 MCP 源不存在或不可见")
    if r["status"] == "disabled":
        return
    await conn.execute(
        update(upstream_mcp_source).where(upstream_mcp_source.c.id == source_id).values(status="disabled")
    )
    await notify_change(conn, "upstream_mcp.disabled", r["company_id"], source_id, actor, {"name": r["name"]})


async def delete_upstream_mcp_source(conn: AsyncConnection, actor: Actor, source_id: uuid.UUID) -> None:
    """删除上游 MCP 源登记；不级联 Connector 实例（实例自持 hash）。"""
    actor.require(_manages(actor), "上游 MCP 源由总部 IT 维护")
    r = (
        await conn.execute(select(upstream_mcp_source).where(upstream_mcp_source.c.id == source_id))
    ).mappings().first()
    if r is None:
        raise PlatformError(ErrorCode.NOT_FOUND, "上游 MCP 源不存在或不可见")
    await conn.execute(upstream_mcp_source.delete().where(upstream_mcp_source.c.id == source_id))
    await notify_change(conn, "upstream_mcp.deleted", r["company_id"], source_id, actor, {"name": r["name"]})


async def create_connector_instance(
    conn: AsyncConnection,
    actor: Actor,
    *,
    name: str,
    kind: str,
    connection_id: uuid.UUID | None,
    config: dict[str, Any],
    concurrency: int = 8,
    rate_per_s: float = 20,
    burst: int = 40,
) -> uuid.UUID:
    actor.require(_manages(actor))
    company_id = (await row(conn, connection_id))["company_id"] if connection_id else None
    if company_id is None:
        if kind != "mock":
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, "非 mock Connector 必须绑定系统连接")
        company_id = actor.company_id or uuid.UUID(int=0)
    iid = new_id()
    await conn.execute(
        connector_instance.insert().values(
            id=iid,
            name=name,
            company_id=company_id,
            kind=kind,
            system_connection_id=connection_id,
            config=config,
            concurrency=concurrency,
            rate_per_s=rate_per_s,
            burst=burst,
            status="active",
        )
    )
    await notify_change(conn, "connector.created", company_id, iid, actor, {"name": name})
    return iid


def check_tool_on_connection(spec: dict[str, Any], r: dict[str, Any]) -> None:
    """发布前置检查（附录 B T-GEN-N6）：传输例外连接上只允许服务账号身份。"""
    exception = r["scheme"] == "http" or r["tls_mode"] == "insecure"
    if exception and spec.get("identity_mode") != "service_account":
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "http / insecure 连接上只允许 identity_mode=service_account")


async def check_release_connection(conn: AsyncConnection, spec: dict[str, Any], env: str) -> None:
    """发布 Tool 前检查其 Connector 绑定的连接（附录 B T-GEN-N6）。"""
    name = spec["connector_ref"]["connector"]
    inst = (await conn.execute(select(connector_instance).where(connector_instance.c.name == name))).mappings().first()
    if inst is None or inst["system_connection_id"] is None:
        return  # mock 或尚未登记实例：运行时找不到实例会直接拒绝
    check_tool_on_connection(spec, await row(conn, inst["system_connection_id"]))


async def _active_sql_templates(conn: AsyncConnection, instance_id: uuid.UUID) -> dict[str, Any]:
    rows = (
        await conn.execute(
            text(
                "SELECT template_key, engine, statement, param_schema, max_rows, "
                "statement_timeout_ms, explain_cost_cap, output_field_whitelist "
                "FROM conn.sql_query_template WHERE connector_instance_id = :iid AND status = 'active'"
            ),
            {"iid": instance_id},
        )
    ).mappings().all()
    out: dict[str, Any] = {}
    for r in rows:
        out[str(r["template_key"])] = {
            "engine": r["engine"],
            "statement": r["statement"],
            "param_schema": r["param_schema"] or {},
            "max_rows": r["max_rows"],
            "statement_timeout_ms": r["statement_timeout_ms"],
            "explain_cost_cap": float(r["explain_cost_cap"]) if r["explain_cost_cap"] is not None else None,
            "output_field_whitelist": list(r["output_field_whitelist"] or []),
        }
    return out


async def snapshot_part(conn: AsyncConnection) -> dict[str, Any]:
    """供发布快照使用：启用的 Connector 实例及其连接（只含凭据引用，不含明文）。"""
    await expire_exceptions(conn)
    conns = {r["id"]: r for r in (await conn.execute(select(system_connection))).mappings()}
    out = []
    for i in (await conn.execute(select(connector_instance).where(connector_instance.c.status == "active"))).mappings():
        c = conns.get(i["system_connection_id"])
        cfg = dict(i["config"] or {})
        if i["kind"] == "sql_readonly":
            cfg = {**cfg, "templates": await _active_sql_templates(conn, i["id"])}
        out.append(
            {
                "name": i["name"],
                "kind": i["kind"],
                "config": cfg,
                "concurrency": i["concurrency"],
                "rate_per_s": i["rate_per_s"],
                "burst": i["burst"],
                "connection": None
                if c is None
                else {
                    k: (str(v) if isinstance(v, uuid.UUID) else v.isoformat() if isinstance(v, dt.datetime) else v)
                    for k, v in c.items()
                    if k not in ("transport_approved_by", "owner_user_id", "updated_at")
                },
            }
        )
    allow = [str(c) for (c,) in (await conn.execute(select(network_allowlist.c.cidr))).all()]
    return {"connectors": sorted(out, key=lambda x: x["name"]), "network_allowlist": allow}
