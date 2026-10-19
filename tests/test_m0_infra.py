"""M0 / P0：docker-compose 基础设施与 baseline 迁移（需 python tools/dev.py up）。"""

from __future__ import annotations

import pathlib

import asyncpg
import httpx
import pytest
import redis.asyncio as redis
from alembic.config import Config
from alembic.script import ScriptDirectory

from aip.libs.common.config import get_settings

pytestmark = pytest.mark.infra
S = get_settings()
ROOT = pathlib.Path(__file__).resolve().parents[1]


def _dsn() -> str:
    return S.database_url.replace("postgresql+asyncpg://", "postgresql://")


async def test_postgres_baseline_migration_applied() -> None:
    conn = await asyncpg.connect(_dsn())
    try:
        version = await conn.fetchval("SELECT version_num FROM alembic_version")
        exts = {r["extname"] for r in await conn.fetch("SELECT extname FROM pg_extension")}
        schemas = {r["nspname"] for r in await conn.fetch("SELECT nspname FROM pg_namespace")}
        ids = await conn.fetchval("SELECT public.app_company_ids()")
        async with conn.transaction():
            await conn.execute("SET LOCAL app.company_ids = '0192e3a0-0000-7000-8000-000000000001'")
            scoped = await conn.fetchval("SELECT public.app_company_ids()")
    finally:
        await conn.close()
    head = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini"))).get_current_head()
    assert version == head
    assert {"ltree", "pgcrypto", "pg_trgm"} <= exts
    assert {"iam", "registry", "grants", "conn", "exec", "audit"} <= schemas
    assert ids == []
    assert [str(x) for x in scoped] == ["0192e3a0-0000-7000-8000-000000000001"]


async def test_valkey() -> None:
    r = redis.from_url(S.valkey_url)
    try:
        assert await r.ping()
    finally:
        await r.aclose()


@pytest.mark.parametrize(
    ("name", "url", "ok"),
    [
        ("openbao", f"{S.openbao_addr}/v1/sys/health", {200}),
        ("opa", f"{S.opa_url}/health", {200}),
        ("seaweedfs-s3", f"{S.s3_endpoint}/", {200, 403}),
    ],
)
async def test_http_services(name: str, url: str, ok: set[int]) -> None:
    async with httpx.AsyncClient(timeout=5) as c:
        r = await c.get(url)
    assert r.status_code in ok, f"{name}: {r.status_code}"
