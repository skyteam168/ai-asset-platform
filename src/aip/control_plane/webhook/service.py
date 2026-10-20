"""Webhook 订阅与投递（architecture.md 10.5）。

签名：HMAC-SHA256(secret, timestamp + "." + nonce + "." + body)；头带时间戳与 nonce。
出站必须经 libs/httpclient.build_client；密钥轮换后旧密钥立即失效（只保留当前密文）。
失败指数退避重试，超过上限标记 dead。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import logging
import secrets
import uuid
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from aip.control_plane.iam import consumers as cons
from aip.control_plane.iam.authz import Actor
from aip.control_plane.webhook import tables as t
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.common.ids import new_id, utcnow
from aip.libs.httpclient import DEFAULT_ALLOWLIST, ConnectionConfig, build_client
from aip.libs.tokens.secrets_util import seal, unseal

log = logging.getLogger(__name__)

EVENTS = frozenset({"catalog.changed", "run.finished", "risk.triggered"})
MAX_ATTEMPTS = 8
# 指数退避：2^attempt 秒，上限 1 小时
BACKOFF_CAP_S = 3600
# 接收方校验时间窗（秒）；过期时间戳应拒收
TIMESTAMP_SKEW_S = 300
HDR_TS, HDR_NONCE, HDR_SIG = "X-AIP-Timestamp", "X-AIP-Nonce", "X-AIP-Signature"


def new_secret() -> str:
    return secrets.token_urlsafe(32)


def sign(secret: str, timestamp: str, nonce: str, body: bytes) -> str:
    msg = f"{timestamp}.{nonce}.".encode() + body
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def verify_signature(
    secret: str,
    timestamp: str,
    nonce: str,
    body: bytes,
    signature: str,
    *,
    now: dt.datetime | None = None,
    max_skew_s: int = TIMESTAMP_SKEW_S,
) -> bool:
    """接收方校验：签名匹配、时间戳在窗口内、nonce 非空。篡改或过期返回 False。"""
    if not nonce or not timestamp or not signature:
        return False
    try:
        ts = dt.datetime.fromtimestamp(int(timestamp), tz=dt.UTC)
    except (ValueError, OverflowError, OSError):
        return False
    skew = abs(((now or utcnow()) - ts).total_seconds())
    if skew > max_skew_s:
        return False
    expected = sign(secret, timestamp, nonce, body)
    return hmac.compare_digest(expected, signature)


def _ser(row: Any) -> dict[str, Any]:
    d = dict(row)
    for k, v in list(d.items()):
        if isinstance(v, uuid.UUID | dt.datetime):
            d[k] = str(v)
    d.pop("secret_sealed", None)
    return d


def _require_consumer_admin(actor: Actor) -> None:
    actor.require(
        actor.has("platform_admin") or actor.has("company_admin"),
        "只有平台管理员或分公司管理员可以管理 webhook",
    )


async def create_subscription(
    conn: AsyncConnection,
    actor: Actor,
    *,
    consumer_id: uuid.UUID,
    events: list[str],
    url: str,
) -> dict[str, Any]:
    """登记订阅 + 回调端点；返回端点信息与明文密钥（仅此一次）。"""
    _require_consumer_admin(actor)
    ev = sorted({e for e in events if e in EVENTS})
    if not ev:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"events 须为 {sorted(EVENTS)} 的非空子集")
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "url 须为 http(s) 绝对地址")
    consumer = await cons.consumer_row(conn, consumer_id)
    company_id = consumer["company_id"]
    if not actor.has("platform_admin"):
        allowed = {r.scope_company_id for r in actor.roles if r.role == "company_admin"}
        if actor.company_id:
            allowed.add(actor.company_id)
        if company_id not in allowed:
            raise PlatformError(ErrorCode.PERMISSION_DENIED, "不能为其他公司的消费方登记 webhook")
    secret = new_secret()
    sub_id, ep_id = new_id(), new_id()
    now = utcnow()
    await conn.execute(
        t.subscription.insert().values(
            id=sub_id,
            company_id=company_id,
            consumer_id=consumer_id,
            events=ev,
            created_at=now,
            created_by=actor.user_id,
        )
    )
    await conn.execute(
        t.webhook_endpoint.insert().values(
            id=ep_id,
            company_id=company_id,
            subscription_id=sub_id,
            url=url,
            secret_sealed=seal(secret),
            status="active",
            created_at=now,
            updated_at=now,
        )
    )
    return {
        "subscription_id": str(sub_id),
        "endpoint_id": str(ep_id),
        "consumer_id": str(consumer_id),
        "events": ev,
        "url": url,
        "status": "active",
        "secret": secret,  # 仅创建时返回一次
    }


async def list_endpoints(conn: AsyncConnection, actor: Actor, consumer_id: uuid.UUID) -> list[dict[str, Any]]:
    _require_consumer_admin(actor)
    rows = (
        await conn.execute(
            select(t.webhook_endpoint, t.subscription.c.events, t.subscription.c.consumer_id)
            .select_from(
                t.webhook_endpoint.join(
                    t.subscription, t.subscription.c.id == t.webhook_endpoint.c.subscription_id
                )
            )
            .where(t.subscription.c.consumer_id == consumer_id)
            .order_by(t.webhook_endpoint.c.created_at.desc())
        )
    ).mappings().all()
    out = []
    for r in rows:
        d = _ser(r)
        d["events"] = list(r["events"] or [])
        out.append(d)
    return out


async def list_deliveries(
    conn: AsyncConnection, actor: Actor, endpoint_id: uuid.UUID, *, limit: int = 50
) -> list[dict[str, Any]]:
    _require_consumer_admin(actor)
    rows = (
        await conn.execute(
            select(t.webhook_delivery)
            .where(t.webhook_delivery.c.endpoint_id == endpoint_id)
            .order_by(t.webhook_delivery.c.created_at.desc())
            .limit(min(limit, 200))
        )
    ).mappings().all()
    return [_ser(r) for r in rows]


async def rotate_secret(conn: AsyncConnection, actor: Actor, endpoint_id: uuid.UUID) -> dict[str, Any]:
    """轮换密钥：覆盖密文，旧密钥立即失效；明文只返回一次。"""
    _require_consumer_admin(actor)
    row = (
        await conn.execute(select(t.webhook_endpoint).where(t.webhook_endpoint.c.id == endpoint_id))
    ).mappings().first()
    if row is None:
        raise PlatformError(ErrorCode.NOT_FOUND, "webhook 端点不存在")
    secret = new_secret()
    await conn.execute(
        update(t.webhook_endpoint)
        .where(t.webhook_endpoint.c.id == endpoint_id)
        .values(secret_sealed=seal(secret), updated_at=utcnow())
    )
    return {"endpoint_id": str(endpoint_id), "secret": secret}


async def set_status(conn: AsyncConnection, actor: Actor, endpoint_id: uuid.UUID, status: str) -> None:
    _require_consumer_admin(actor)
    if status not in ("active", "disabled"):
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "status 须为 active 或 disabled")
    res = await conn.execute(
        update(t.webhook_endpoint)
        .where(t.webhook_endpoint.c.id == endpoint_id)
        .values(status=status, updated_at=utcnow())
    )
    if res.rowcount == 0:
        raise PlatformError(ErrorCode.NOT_FOUND, "webhook 端点不存在")


async def enqueue(
    conn: AsyncConnection,
    *,
    event_kind: str,
    body: dict[str, Any],
    company_id: uuid.UUID | None = None,
    event_id: str | None = None,
    consumer_id: uuid.UUID | None = None,
) -> list[uuid.UUID]:
    """按事件种类找到匹配的 active 端点并写入 pending 投递；返回新建 delivery id。"""
    if event_kind not in EVENTS:
        return []
    q = (
        select(t.webhook_endpoint, t.subscription.c.events, t.subscription.c.consumer_id)
        .select_from(
            t.webhook_endpoint.join(t.subscription, t.subscription.c.id == t.webhook_endpoint.c.subscription_id)
        )
        .where(t.webhook_endpoint.c.status == "active")
    )
    if company_id is not None:
        q = q.where(t.webhook_endpoint.c.company_id == company_id)
    if consumer_id is not None:
        q = q.where(t.subscription.c.consumer_id == consumer_id)
    rows = (await conn.execute(q)).mappings().all()
    ids: list[uuid.UUID] = []
    now = utcnow()
    for r in rows:
        if event_kind not in (r["events"] or []):
            continue
        did = new_id()
        await conn.execute(
            t.webhook_delivery.insert().values(
                id=did,
                company_id=r["company_id"],
                endpoint_id=r["id"],
                event_kind=event_kind,
                event_id=event_id,
                body=body,
                status="pending",
                attempts=0,
                next_retry_at=now,
                created_at=now,
            )
        )
        ids.append(did)
    return ids


def _connection_for(url: str) -> ConnectionConfig:
    parsed = urlparse(url)
    scheme = "https" if parsed.scheme == "https" else "http"
    return ConnectionConfig(
        id=f"webhook-{hashlib.sha256(url.encode()).hexdigest()[:12]}",
        base_url=url,
        scheme=scheme,  # type: ignore[arg-type]
        tls_mode="system_ca" if scheme == "https" else None,
        transport_exception_reason="webhook 内网回调" if scheme == "http" else None,
        transport_exception_expires_at=dt.datetime(2099, 1, 1, tzinfo=dt.UTC) if scheme == "http" else None,
        connect_timeout_s=5.0,
        read_timeout_s=15.0,
        status="active",
    )


async def deliver_one(conn: AsyncConnection, delivery_id: uuid.UUID) -> dict[str, Any]:
    """投递一条；成功标 delivered，失败写 attempts / next_retry_at，超限标 dead。"""
    row = (
        await conn.execute(select(t.webhook_delivery).where(t.webhook_delivery.c.id == delivery_id))
    ).mappings().first()
    if row is None:
        raise PlatformError(ErrorCode.NOT_FOUND, "投递记录不存在")
    if row["status"] == "delivered":
        return _ser(row)
    ep = (
        await conn.execute(select(t.webhook_endpoint).where(t.webhook_endpoint.c.id == row["endpoint_id"]))
    ).mappings().first()
    if ep is None or ep["status"] != "active":
        await conn.execute(
            update(t.webhook_delivery)
            .where(t.webhook_delivery.c.id == delivery_id)
            .values(status="dead", last_error="端点已禁用或不存在")
        )
        return {"id": str(delivery_id), "status": "dead"}
    secret = unseal(ep["secret_sealed"])
    body = json.dumps(row["body"], ensure_ascii=False, separators=(",", ":")).encode()
    ts = str(int(utcnow().timestamp()))
    nonce = secrets.token_hex(16)
    sig = sign(secret, ts, nonce, body)
    headers = {
        HDR_TS: ts,
        HDR_NONCE: nonce,
        HDR_SIG: sig,
        "Content-Type": "application/json",
        "User-Agent": "aip-webhook/1",
    }
    attempts = int(row["attempts"]) + 1
    status_code: int | None = None
    err: str | None = None
    ok = False
    try:
        async with build_client(_connection_for(ep["url"]), allowlist=DEFAULT_ALLOWLIST) as client:
            resp = await client.post(ep["url"], content=body, headers=headers)
            status_code = resp.status_code
            ok = 200 <= resp.status_code < 300
            if not ok:
                err = f"HTTP {resp.status_code}"
    except Exception as e:  # 出站失败（TLS、超时、白名单等）记入投递结果并重试
        err = f"{type(e).__name__}: {e}"
        log.warning("webhook deliver failed endpoint=%s: %s", ep["id"], err)
    values: dict[str, Any] = {
        "attempts": attempts,
        "last_status_code": status_code,
        "last_error": err,
    }
    if ok:
        values.update(status="delivered", delivered_at=utcnow(), next_retry_at=None)
    elif attempts >= MAX_ATTEMPTS:
        values.update(status="dead", next_retry_at=None)
    else:
        delay = min(BACKOFF_CAP_S, 2**attempts)
        values.update(status="failed", next_retry_at=utcnow() + dt.timedelta(seconds=delay))
    await conn.execute(update(t.webhook_delivery).where(t.webhook_delivery.c.id == delivery_id).values(**values))
    return {"id": str(delivery_id), **{k: (str(v) if isinstance(v, dt.datetime) else v) for k, v in values.items()}}


async def process_due(conn: AsyncConnection, *, limit: int = 50) -> list[dict[str, Any]]:
    """处理到期的 pending/failed 投递（后台扫描与测试手动触发）。"""
    now = utcnow()
    rows = (
        await conn.execute(
            select(t.webhook_delivery.c.id)
            .where(
                and_(
                    t.webhook_delivery.c.status.in_(("pending", "failed")),
                    t.webhook_delivery.c.next_retry_at <= now,
                )
            )
            .order_by(t.webhook_delivery.c.next_retry_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    ).all()
    return [await deliver_one(conn, did) for did in (r[0] for r in rows)]
