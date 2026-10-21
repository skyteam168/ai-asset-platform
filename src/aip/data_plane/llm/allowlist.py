"""公司 × model_profile 白名单。"""

from __future__ import annotations

import uuid
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.common.ids import utcnow


def _validate_names(names: list[str]) -> list[str]:
    clean = sorted({n.strip() for n in names if n and n.strip()})
    for n in clean:
        if not n.replace("_", "").replace(".", "").isalnum() or not n[0].islower():
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"model_profile_name 不合法：{n}")
    return clean


class AllowlistStore(Protocol):
    async def is_allowed(self, company_id: uuid.UUID, model_profile_name: str) -> bool: ...

    async def list_names(self, company_id: uuid.UUID) -> list[str]: ...

    async def set_names(self, company_id: uuid.UUID, names: list[str]) -> None: ...


class MemoryAllowlistStore:
    def __init__(self) -> None:
        self._by_company: dict[uuid.UUID, set[str]] = {}

    async def is_allowed(self, company_id: uuid.UUID, model_profile_name: str) -> bool:
        allowed = self._by_company.get(company_id)
        if not allowed:
            return False
        return model_profile_name in allowed

    async def list_names(self, company_id: uuid.UUID) -> list[str]:
        return sorted(self._by_company.get(company_id, set()))

    async def set_names(self, company_id: uuid.UUID, names: list[str]) -> None:
        self._by_company[company_id] = set(_validate_names(names))


class PgAllowlistStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    async def is_allowed(self, company_id: uuid.UUID, model_profile_name: str) -> bool:
        async with self.engine.connect() as conn:
            await conn.execute(text("SELECT set_config('app.bypass_rls', 'on', true)"))
            row = (
                await conn.execute(
                    text(
                        "SELECT 1 FROM llm.company_model_allowlist "
                        "WHERE company_id = :c AND model_profile_name = :n"
                    ),
                    {"c": company_id, "n": model_profile_name},
                )
            ).first()
            return row is not None

    async def list_names(self, company_id: uuid.UUID) -> list[str]:
        async with self.engine.connect() as conn:
            await conn.execute(text("SELECT set_config('app.bypass_rls', 'on', true)"))
            rows = (
                await conn.execute(
                    text(
                        "SELECT model_profile_name FROM llm.company_model_allowlist "
                        "WHERE company_id = :c ORDER BY model_profile_name"
                    ),
                    {"c": company_id},
                )
            ).all()
            return [str(r[0]) for r in rows]

    async def set_names(self, company_id: uuid.UUID, names: list[str]) -> None:
        clean = _validate_names(names)
        async with self.engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.bypass_rls', 'on', true)"))
            await conn.execute(
                text("DELETE FROM llm.company_model_allowlist WHERE company_id = :c"), {"c": company_id}
            )
            for n in clean:
                await conn.execute(
                    text(
                        "INSERT INTO llm.company_model_allowlist (company_id, model_profile_name, created_at) "
                        "VALUES (:c, :n, :t)"
                    ),
                    {"c": company_id, "n": n, "t": utcnow()},
                )


async def set_names_on_conn(conn: AsyncConnection, company_id: uuid.UUID, names: list[str]) -> None:
    """控制面事务内写入。"""
    clean = _validate_names(names)
    await conn.execute(text("DELETE FROM llm.company_model_allowlist WHERE company_id = :c"), {"c": company_id})
    for n in clean:
        await conn.execute(
            text(
                "INSERT INTO llm.company_model_allowlist (company_id, model_profile_name, created_at) "
                "VALUES (:c, :n, :t)"
            ),
            {"c": company_id, "n": n, "t": utcnow()},
        )


async def list_names_on_conn(conn: AsyncConnection, company_id: uuid.UUID) -> list[str]:
    rows = (
        await conn.execute(
            text(
                "SELECT model_profile_name FROM llm.company_model_allowlist "
                "WHERE company_id = :c ORDER BY model_profile_name"
            ),
            {"c": company_id},
        )
    ).all()
    return [str(r[0]) for r in rows]
