"""控制台登录与会话（architecture.md 3.4）：argon2id + TOTP，5 次失败锁定 15 分钟，30 分钟滑动、12 小时绝对过期。"""

from __future__ import annotations

import datetime as dt
import logging
import uuid

import pyotp
from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from aip.control_plane.iam import tables as t
from aip.control_plane.iam.events import emit
from aip.libs.common.config import get_settings
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.common.ids import utcnow
from aip.libs.tokens.secrets_util import new_opaque_token, token_hash, unseal, verify_password

log = logging.getLogger("aip.iam.auth")
MAX_FAILURES = 5
LOCK_MINUTES = 15
_INVALID = "用户名、口令或动态码错误"


async def login(
    conn: AsyncConnection, login_name: str, password: str, totp: str | None
) -> tuple[str, dt.datetime, uuid.UUID]:
    """成功返回 (session_token, expires_at, user_id)。错误信息不区分“用户不存在”与“口令错误”。"""
    s = get_settings()
    now = utcnow()
    u = (await conn.execute(select(t.iam_user).where(t.iam_user.c.login_name == login_name))).mappings().first()
    if u is None or not u["login_enabled"] or u["status"] != "active":
        raise PlatformError(ErrorCode.UNAUTHENTICATED, _INVALID)
    if u["locked_until"] and u["locked_until"] > now:
        raise PlatformError(ErrorCode.ACCOUNT_LOCKED, "连续失败次数过多，账号已临时锁定")
    if not verify_password(u["password_hash"], password):
        await _failure(conn, u["id"], u["company_id"], int(u["failed_logins"]) + 1)
        raise PlatformError(ErrorCode.UNAUTHENTICATED, _INVALID)
    if not totp:
        raise PlatformError(ErrorCode.MFA_REQUIRED, "需要动态码")
    if not pyotp.TOTP(unseal(u["totp_secret_enc"])).verify(totp, valid_window=1):
        await _failure(conn, u["id"], u["company_id"], int(u["failed_logins"]) + 1)
        raise PlatformError(ErrorCode.UNAUTHENTICATED, _INVALID)
    await conn.execute(
        update(t.iam_user)
        .where(t.iam_user.c.id == u["id"])
        .values(failed_logins=0, locked_until=None, last_seen_at=now)
    )
    token, digest = new_opaque_token("aips")
    expires = now + dt.timedelta(seconds=s.session_idle_s)
    await conn.execute(
        t.console_session.insert().values(
            token_hash=digest,
            user_id=u["id"],
            company_id=u["company_id"],
            expires_at=expires,
            absolute_expires_at=now + dt.timedelta(seconds=s.session_absolute_s),
            revoked=False,
        )
    )
    await emit(conn, "auth.login", u["company_id"], u["id"], u["id"])
    if u["is_break_glass"]:
        log.critical("BREAK-GLASS LOGIN user_id=%s", u["id"])
        await emit(conn, "auth.break_glass_login", u["company_id"], u["id"], u["id"])
    return token, expires, u["id"]


async def _failure(conn: AsyncConnection, user_id: uuid.UUID, company_id: uuid.UUID, failures: int) -> None:
    values: dict[str, object] = {"failed_logins": failures}
    if failures >= MAX_FAILURES:
        values = {"failed_logins": 0, "locked_until": utcnow() + dt.timedelta(minutes=LOCK_MINUTES)}
        log.warning("account locked user_id=%s", user_id)
        await emit(conn, "auth.locked", company_id, user_id, None)
    await conn.execute(update(t.iam_user).where(t.iam_user.c.id == user_id).values(**values))


async def resolve_session(conn: AsyncConnection, token: str) -> uuid.UUID:
    """校验并滑动续期会话；需在系统上下文中调用（此时还不知道操作者属于哪家公司）。"""
    s = get_settings()
    now = utcnow()
    row = (
        (await conn.execute(select(t.console_session).where(t.console_session.c.token_hash == token_hash(token))))
        .mappings()
        .first()
    )
    if row is None or row["revoked"] or row["expires_at"] <= now or row["absolute_expires_at"] <= now:
        raise PlatformError(ErrorCode.UNAUTHENTICATED, "会话无效或已过期")
    active = (await conn.execute(select(t.iam_user.c.status).where(t.iam_user.c.id == row["user_id"]))).scalar_one()
    if active != "active":
        raise PlatformError(ErrorCode.UNAUTHENTICATED, "会话无效或已过期")
    await conn.execute(
        update(t.console_session)
        .where(t.console_session.c.token_hash == row["token_hash"])
        .values(
            last_used_at=now, expires_at=min(now + dt.timedelta(seconds=s.session_idle_s), row["absolute_expires_at"])
        )
    )
    return uuid.UUID(str(row["user_id"]))


async def logout(conn: AsyncConnection, token: str) -> None:
    await conn.execute(
        update(t.console_session).where(and_(t.console_session.c.token_hash == token_hash(token))).values(revoked=True)
    )
