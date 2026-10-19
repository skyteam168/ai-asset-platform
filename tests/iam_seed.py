"""M1 测试共用：独立测试库、清理、种子组织树。"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import asyncpg
import pyotp

from aip.control_plane.db import Database, DbScope
from aip.control_plane.iam import consumers, orgs, users
from aip.control_plane.iam import tables as t
from aip.control_plane.iam.authz import Actor
from aip.libs.common.config import get_settings
from aip.libs.tokens.secrets_util import unseal

ROOT = Path(__file__).resolve().parents[1]
TEST_DB = "aip_test"
PASSWORD = "correct horse battery"


def owner_dsn(db: str = TEST_DB) -> str:
    base = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")
    return base.rsplit("/", 1)[0] + f"/{db}"


def app_url(db: str = TEST_DB) -> str:
    return get_settings().app_database_url.rsplit("/", 1)[0] + f"/{db}"


async def create_test_database() -> None:
    conn = await asyncpg.connect(owner_dsn("aip"))
    try:
        await conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        await conn.execute(f"CREATE DATABASE {TEST_DB}")
    finally:
        await conn.close()
    env = {**os.environ, "AIP_DATABASE_URL": get_settings().database_url.rsplit("/", 1)[0] + f"/{TEST_DB}"}
    await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
    )


async def truncate() -> None:
    conn = await asyncpg.connect(owner_dsn())
    try:
        names = ", ".join(f"iam.{tb.name}" for tb in t.md.sorted_tables)
        await conn.execute(f"TRUNCATE {names} RESTART IDENTITY CASCADE")
    finally:
        await conn.close()


@dataclass
class Seed:
    group: uuid.UUID
    hq: uuid.UUID
    vn: uuid.UUID
    vn_mfg: uuid.UUID
    vn_mfg_l1: uuid.UUID
    vn_qa: uuid.UUID
    kh: uuid.UUID
    kh_mfg: uuid.UUID
    users: dict[str, uuid.UUID] = field(default_factory=dict)
    totp: dict[str, str] = field(default_factory=dict)
    consumers: dict[str, uuid.UUID] = field(default_factory=dict)

    def code(self, name: str) -> str:
        return pyotp.TOTP(self.totp[name]).now()


async def seed(db: Database) -> Seed:
    sys_ = Actor.system_actor()
    async with db.tx(DbScope.system()) as conn:
        group, hq = await orgs.bootstrap_group(
            conn, group_code="GROUP", group_name="集团", hq_code="HQ", hq_name="总部"
        )
        vn = await orgs.create_org(
            conn, sys_, type_="company", code="VN01", name="越南一厂", parent_id=group.id, default_clearance=2
        )
        vn_mfg = await orgs.create_org(conn, sys_, type_="department", code="VN01-MFG", name="制造部", parent_id=vn.id)
        vn_l1 = await orgs.create_org(conn, sys_, type_="team", code="VN01-MFG-L1", name="一线", parent_id=vn_mfg.id)
        vn_qa = await orgs.create_org(
            conn, sys_, type_="department", code="VN01-QA", name="品质部", parent_id=vn.id, default_clearance=3
        )
        kh = await orgs.create_org(
            conn, sys_, type_="company", code="KH01", name="柬埔寨一厂", parent_id=group.id, default_clearance=1
        )
        kh_mfg = await orgs.create_org(conn, sys_, type_="department", code="KH01-MFG", name="制造部", parent_id=kh.id)
        s = Seed(group.id, hq.id, vn.id, vn_mfg.id, vn_l1.id, vn_qa.id, kh.id, kh_mfg.id)

        async def login_user(name: str, home: uuid.UUID, role: str | None, scope: uuid.UUID | None) -> None:
            c = await users.create_user(
                conn, sys_, display_name=name, home_org_id=home, login_name=name, password=PASSWORD, login_enabled=True
            )
            s.users[name] = c.id
            secret = (await users.user_row(conn, c.id))["totp_secret_enc"]
            s.totp[name] = unseal(secret)
            if role and scope:
                await users.assign_role(conn, sys_, c.id, role, scope)

        await login_user("admin", hq.id, "platform_admin", group.id)
        await login_user("security", hq.id, "data_owner", group.id)
        await login_user("vn_admin", vn.id, "company_admin", vn.id)
        await login_user("kh_admin", kh.id, "company_admin", kh.id)
        for name, home, emp in (
            ("vn_worker", vn_l1.id, "E-VN-001"),
            ("vn_qa", vn_qa.id, "E-VN-002"),
            ("kh_worker", kh_mfg.id, "E-KH-001"),
        ):
            c = await users.create_user(conn, sys_, display_name=name, home_org_id=home, employee_no=emp)
            s.users[name] = c.id
        s.consumers["vn-ai"] = await consumers.create_consumer(
            conn, sys_, client_id="vn-ai", company_id=vn.id, clearance_cap=3
        )
        s.consumers["kh-ai"] = await consumers.create_consumer(
            conn, sys_, client_id="kh-ai", company_id=kh.id, clearance_cap=2, bind_policy="by_employee_no"
        )
        s.consumers["vn-batch"] = await consumers.create_consumer(
            conn, sys_, client_id="vn-batch", company_id=vn.id, clearance_cap=1, bind_policy="app_only"
        )
    return s


async def actor_for(db: Database, s: Seed, name: str) -> Actor:
    async with db.tx(DbScope.system()) as conn:
        return await users.load_actor(conn, s.users[name])
