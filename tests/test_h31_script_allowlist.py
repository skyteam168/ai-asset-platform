"""H31：带脚本 Skill 须经白名单审批后才能入库。"""

from __future__ import annotations

import datetime as dt
from typing import Any

import httpx
import pytest

from aip.control_plane.app import create_app as create_cp
from aip.control_plane.db import Database, DbScope
from aip.control_plane.gates import service as gates
from aip.control_plane.registry import service as registry
from aip.control_plane.skill import package as pkg
from aip.control_plane.skill import service as skill
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.common.ids import utcnow
from aip.libs.tokens.keys import SigningKey
from iam_seed import PASSWORD, Seed
from test_m4_gates import People, _people
from test_m6_skill import _zip_skill

pytestmark = pytest.mark.infra
KEY = SigningKey.generate()


def _zip_with_script(script_path: str = "scripts/helper.py") -> bytes:
    files = pkg.unpack_zip(_zip_skill(name="scripted-helper"), check_scripts=False)
    files[script_path] = b"print('helper')\n"
    return pkg.make_zip(files)


async def _draft_skill(db: Database, s: Seed, p: People) -> Any:
    async with db.tx(DbScope.system()) as conn:
        aid = await registry.create_asset(
            conn, p.dev, type_="skill", name="skill.scripted_helper", owner_org_id=s.hq, classification=2
        )
        return await registry.create_version(
            conn,
            p.dev,
            aid,
            "1.0.0",
            {"name": "scripted-helper", "description": "带脚本 Skill 白名单验收用描述足够长。"},
        )


async def test_script_rejected_without_allowlist(db: Database, seeded: Seed) -> None:
    p = await _people(db, seeded)
    vid = await _draft_skill(db, seeded, p)
    raw = _zip_with_script()
    with pytest.raises(PlatformError) as ei:
        async with db.tx(DbScope.system()) as conn:
            await skill.attach_package(conn, p.dev, vid, raw, key=KEY)
    assert ei.value.code == ErrorCode.INVALID_ARGUMENT
    assert "脚本" in ei.value.detail


async def test_script_allowed_after_approval(db: Database, seeded: Seed) -> None:
    p = await _people(db, seeded)
    vid = await _draft_skill(db, seeded, p)
    async with db.tx(DbScope.system()) as conn:
        pending = await skill.request_script_allowlist(
            conn, p.dev, vid, path_globs=["scripts/*.py"], reason="辅助脚本经安全审查"
        )
        assert len(pending) == 1 and pending[0]["status"] == "pending"
        approved = await skill.decide_script_allowlist(conn, p.sec2, pending[0]["id"], approve=True)
        assert approved["status"] == "approved"
        result = await skill.attach_package(conn, p.dev, vid, _zip_with_script(), key=KEY)
        assert "scripts/helper.py" in result["files"]
        assert result["script_allowlist"]


async def test_path_outside_allowlist_still_rejected(db: Database, seeded: Seed) -> None:
    p = await _people(db, seeded)
    vid = await _draft_skill(db, seeded, p)
    async with db.tx(DbScope.system()) as conn:
        rows = await skill.request_script_allowlist(conn, p.dev, vid, path_globs=["scripts/*.py"])
        await skill.decide_script_allowlist(conn, p.sec2, rows[0]["id"], approve=True)
        with pytest.raises(PlatformError) as ei:
            await skill.attach_package(conn, p.dev, vid, _zip_with_script("assets/evil.py"), key=KEY)
    assert "脚本" in ei.value.detail


async def test_expired_allowlist_rejected(db: Database, seeded: Seed) -> None:
    p = await _people(db, seeded)
    vid = await _draft_skill(db, seeded, p)
    async with db.tx(DbScope.system()) as conn:
        rows = await skill.request_script_allowlist(
            conn,
            p.dev,
            vid,
            path_globs=["scripts/*.py"],
            expires_at=utcnow() - dt.timedelta(hours=1),
        )
        await skill.decide_script_allowlist(conn, p.sec2, rows[0]["id"], approve=True)
        with pytest.raises(PlatformError):
            await skill.attach_package(conn, p.dev, vid, _zip_with_script(), key=KEY)


async def test_g0_records_script_allowlist_evidence(db: Database, seeded: Seed) -> None:
    p = await _people(db, seeded)
    vid = await _draft_skill(db, seeded, p)
    async with db.tx(DbScope.system()) as conn:
        rows = await skill.request_script_allowlist(conn, p.dev, vid, path_globs=["scripts/*.py"])
        await skill.decide_script_allowlist(conn, p.sec2, rows[0]["id"], approve=True)
        await skill.attach_package(conn, p.dev, vid, _zip_with_script(), key=KEY)
        await gates.set_acceptance(conn, p.dev, vid, {"scenarios": ["脚本白名单"]})
        g0 = await gates.submit(conn, p.dev, vid)
        assert g0["passed"] is True, g0
        evidence = g0.get("evidence") or {}
        assert evidence.get("script_allowlist")
        assert any(x.get("path_glob") == "scripts/*.py" for x in evidence["script_allowlist"])


async def test_script_allowlist_http(db: Database, seeded: Seed) -> None:
    p = await _people(db, seeded)
    vid = await _draft_skill(db, seeded, p)
    transport = httpx.ASGITransport(app=create_cp(db))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as cp:
        login_admin = await cp.post(
            "/api/v1/auth/login",
            json={"login_name": "admin", "password": PASSWORD, "totp": seeded.code("admin")},
        )
        assert login_admin.status_code == 200, login_admin.text
        h_admin = {"Authorization": f"Bearer {login_admin.json()['session_token']}"}
        r = await cp.post(
            f"/api/v1/versions/{vid}/skill-script-allowlist",
            headers=h_admin,
            json={"path_globs": ["scripts/helper.py"], "reason": "http"},
        )
        assert r.status_code == 201, r.text
        entry_id = r.json()["items"][0]["id"]
        login_sec = await cp.post(
            "/api/v1/auth/login",
            json={"login_name": "security", "password": PASSWORD, "totp": seeded.code("security")},
        )
        assert login_sec.status_code == 200, login_sec.text
        h_sec = {"Authorization": f"Bearer {login_sec.json()['session_token']}"}
        d = await cp.post(
            f"/api/v1/skill-script-allowlist/{entry_id}/decide",
            headers=h_sec,
            json={"approve": True},
        )
        assert d.status_code == 200, d.text
        assert d.json()["status"] == "approved"
        listed = await cp.get(f"/api/v1/versions/{vid}/skill-script-allowlist", headers=h_admin)
        assert listed.status_code == 200
        assert listed.json()["items"][0]["status"] == "approved"
