"""iam 领域事件（outbox）。紧急事件（停用、撤销、吊销）优先推送（architecture.md 4.4）。"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from aip.control_plane.iam import tables as t

URGENT = frozenset(
    {
        "user.disabled",
        "credential.revoked",
        "jwk.revoked",
        "binding.deleted",
        "org.disabled",
        "consumer.disabled",
    }
)


async def emit(
    conn: AsyncConnection,
    kind: str,
    company_id: uuid.UUID,
    subject_id: uuid.UUID | None,
    actor_id: uuid.UUID | None,
    payload: dict[str, Any] | None = None,
) -> None:
    await conn.execute(
        t.domain_event.insert().values(
            company_id=company_id,
            kind=kind,
            priority="urgent" if kind in URGENT else "normal",
            subject_id=subject_id,
            actor_id=actor_id,
            payload=payload or {},
        )
    )
