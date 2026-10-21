"""H35：Skill 包 SeaweedFS 双写 / 双读。"""

from __future__ import annotations

import pytest

from aip.control_plane.db import Database, DbScope
from aip.control_plane.registry import service as registry
from aip.control_plane.skill import package as pkg
from aip.control_plane.skill import service as skill
from aip.control_plane.skill import storage as skill_store
from aip.libs.common.config import get_settings
from aip.libs.tokens.keys import SigningKey
from iam_seed import Seed
from test_m4_gates import _people
from test_m6_skill import _zip_skill

pytestmark = pytest.mark.infra
KEY = SigningKey.generate()


async def test_attach_writes_object_and_download_prefers_s3(
    db: Database, seeded: Seed, monkeypatch
) -> None:
    monkeypatch.setenv("AIP_SKILL_OBJECT_STORAGE", "true")
    get_settings.cache_clear()
    p = await _people(db, seeded)
    raw = _zip_skill(name="s3-demo-skill", tools=None)
    async with db.tx(DbScope.system()) as conn:
        aid = await registry.create_asset(
            conn, p.dev, type_="skill", name="skill.s3_demo", owner_org_id=seeded.hq, classification=2
        )
        vid = await registry.create_version(
            conn,
            p.dev,
            aid,
            "1.0.0",
            {"name": "s3-demo-skill", "description": "SeaweedFS 存包演示描述足够十个字符。"},
        )
        attached = await skill.attach_package(conn, p.dev, vid, raw, key=KEY)
        assert attached.get("object_key")
        row = await skill.package_row(conn, vid)
        assert row["object_key"] == attached["object_key"]
        # 对象可读
        from_s3 = await skill_store.get_skill_zip(str(row["object_key"]))
        assert pkg.unpack_zip(from_s3, check_scripts=False)["SKILL.md"]
        # 清空 bytea 模拟仅对象存储；双读应仍成功
        await conn.execute(
            skill.skill_package.update()
            .where(skill.skill_package.c.version_id == vid)
            .values(zip_bytes=b"")
        )
        data, filename = await skill.download_zip(conn, vid)
        assert filename.endswith(".zip")
        assert b"SKILL.md" in data or pkg.unpack_zip(data, check_scripts=False)
    get_settings.cache_clear()


async def test_download_falls_back_to_bytea(db: Database, seeded: Seed, monkeypatch) -> None:
    monkeypatch.setenv("AIP_SKILL_OBJECT_STORAGE", "false")
    get_settings.cache_clear()
    p = await _people(db, seeded)
    raw = _zip_skill(name="bytea-skill", tools=None)
    async with db.tx(DbScope.system()) as conn:
        aid = await registry.create_asset(
            conn, p.dev, type_="skill", name="skill.bytea_demo", owner_org_id=seeded.hq, classification=2
        )
        vid = await registry.create_version(
            conn,
            p.dev,
            aid,
            "1.0.0",
            {"name": "bytea-skill", "description": "bytea 回落演示描述足够十个字符以上。"},
        )
        attached = await skill.attach_package(conn, p.dev, vid, raw, key=KEY)
        assert attached.get("object_key") is None
        data, _ = await skill.download_zip(conn, vid)
        assert "SKILL.md" in pkg.unpack_zip(data, check_scripts=False)
    get_settings.cache_clear()
