"""H32：Python SDK SkillRegistryClient 对接控制面 /skills*。"""

from __future__ import annotations

import json

import httpx
import pytest
from httpx import ASGITransport

from aip.control_plane.app import create_app as create_cp
from aip.control_plane.db import Database, DbScope
from aip.control_plane.gates import service as gates
from aip.control_plane.registry import service as registry
from aip.control_plane.skill import package as pkg
from aip.control_plane.skill import service as skill
from aip.libs.tokens.keys import SigningKey
from aip.sdk import SkillRegistryClient
from iam_seed import Seed, app_url
from test_m1_api import login
from test_m4_gates import SECRETS, _people
from test_m6_skill import _force_active, _zip_skill

pytestmark = pytest.mark.infra
KEY = SigningKey.generate()


async def test_sdk_list_and_download_skill(db: Database, seeded: Seed, tmp_path) -> None:
    p = await _people(db, seeded)
    # 无 Tool 引用：G2 不强制评测集，便于 SDK 下载验收
    raw = _zip_skill(name="sdk-demo-skill", tools=None)
    async with db.tx(DbScope.system()) as conn:
        aid = await registry.create_asset(
            conn, p.dev, type_="skill", name="skill.sdk_demo", owner_org_id=seeded.hq, classification=2
        )
        vid = await registry.create_version(
            conn,
            p.dev,
            aid,
            "1.0.0",
            {"name": "sdk-demo-skill", "description": "SDK 下载演示 Skill 描述足够长。"},
        )
        await gates.set_acceptance(conn, p.dev, vid, {"scenarios": ["sdk"]})
        await skill.attach_package(conn, p.dev, vid, raw, key=KEY)
        g0 = await gates.submit(conn, p.dev, vid)
        assert g0["passed"], g0
        lab = await gates.run_lab(conn, p.dev, vid, secrets=SECRETS)
        assert lab["passed"], lab
        await _force_active(conn, p.dev, vid)
        await registry.create_release(
            conn, p.admin, vid, env="test", channel="stable", scope={"group": True}, exposure=["rest"]
        )

    app = create_cp(db=Database(app_url()))
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        token = (await login(http, seeded, "admin"))["Authorization"].removeprefix("Bearer ")

    async with SkillRegistryClient("http://test", access_token=token, transport=transport) as c:
        listed = await c.list_skills(q="sdk-demo")
        names = [i.get("name") for i in listed.get("items") or []]
        assert "sdk-demo-skill" in names
        manifest = await c.get_manifest("sdk-demo-skill", "1.0.0")
        assert manifest.get("content_sha256")
        check = await c.install_check(
            consumer_id=str(seeded.consumers["vn-ai"]), name="sdk-demo-skill", version="1.0.0"
        )
        assert "missing_grants" in check
        path = await c.download_to("sdk-demo-skill", "1.0.0", tmp_path)
        assert path.stat().st_size > 0
        files = pkg.unpack_zip(path.read_bytes())
        assert "SKILL.md" in files
