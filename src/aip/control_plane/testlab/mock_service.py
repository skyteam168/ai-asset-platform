"""进程内 Prism Mock 会话登记（单控制面实例；重启即失效）。"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from aip.control_plane.iam.authz import Actor
from aip.control_plane.testlab import prism as prism_lib
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.common.ids import new_id, utcnow

_LOCK = threading.Lock()
_SESSIONS: dict[uuid.UUID, MockRecord] = {}

DEFAULT_TTL = 1800
MAX_TTL = 7200


@dataclass
class MockRecord:
    id: uuid.UUID
    session: prism_lib.PrismSession
    created_at: datetime
    expires_at: datetime
    created_by: uuid.UUID | None


def _require_dev(actor: Actor) -> None:
    actor.require(
        actor.has("asset_developer") or actor.has("platform_admin"),
        "只有资产开发者或平台管理员可以管理 Prism Mock",
    )


def _purge_expired_unlocked(now: datetime) -> None:
    dead = [mid for mid, rec in _SESSIONS.items() if rec.expires_at <= now]
    for mid in dead:
        rec = _SESSIONS.pop(mid)
        prism_lib.stop_mock(rec.session)


def create_mock(
    actor: Actor,
    *,
    document: str,
    ttl_seconds: int = DEFAULT_TTL,
    ready_path: str | None = None,
) -> dict[str, Any]:
    _require_dev(actor)
    if ttl_seconds < 60 or ttl_seconds > MAX_TTL:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"ttl_seconds 须在 60–{MAX_TTL}")
    now = utcnow()
    with _LOCK:
        _purge_expired_unlocked(now)
        sid = new_id()
        session = prism_lib.start_mock(document, ready_path=ready_path, session_id=sid)
        expires = now + timedelta(seconds=ttl_seconds)
        _SESSIONS[sid] = MockRecord(
            id=sid,
            session=session,
            created_at=now,
            expires_at=expires,
            created_by=actor.user_id,
        )
        return _public(_SESSIONS[sid])


def get_mock(actor: Actor, mock_id: uuid.UUID) -> dict[str, Any]:
    _require_dev(actor)
    now = utcnow()
    with _LOCK:
        _purge_expired_unlocked(now)
        rec = _SESSIONS.get(mock_id)
        if rec is None:
            raise PlatformError(ErrorCode.NOT_FOUND, "Prism Mock 不存在或已过期")
        return _public(rec)


def delete_mock(actor: Actor, mock_id: uuid.UUID) -> None:
    _require_dev(actor)
    with _LOCK:
        rec = _SESSIONS.pop(mock_id, None)
        if rec is None:
            raise PlatformError(ErrorCode.NOT_FOUND, "Prism Mock 不存在或已过期")
        prism_lib.stop_mock(rec.session)


def list_mocks(actor: Actor) -> list[dict[str, Any]]:
    _require_dev(actor)
    now = utcnow()
    with _LOCK:
        _purge_expired_unlocked(now)
        return [_public(r) for r in sorted(_SESSIONS.values(), key=lambda x: x.created_at, reverse=True)]


def _public(rec: MockRecord) -> dict[str, Any]:
    return {
        "id": str(rec.id),
        "base_url": rec.session.base_url,
        "container_name": rec.session.container_name,
        "expires_at": rec.expires_at.isoformat().replace("+00:00", "Z"),
        "ready_url": rec.session.ready_url,
    }


def reset_for_tests() -> None:
    """单测清理：停掉全部会话。"""
    with _LOCK:
        for rec in list(_SESSIONS.values()):
            prism_lib.stop_mock(rec.session)
        _SESSIONS.clear()
