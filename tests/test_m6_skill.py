"""M6c：Skill Registry 与 corp-skill。

验收：篡改任一文件后 verify 失败；未授权的引用 Tool 在安装时提示；撤回后 list 显示撤回。
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from aip.cli import corp_skill
from aip.control_plane.app import create_app as create_cp
from aip.control_plane.db import Database, DbScope
from aip.control_plane.gates import service as gates
from aip.control_plane.grant import service as grants
from aip.control_plane.registry import service as registry
from aip.control_plane.skill import package as pkg
from aip.control_plane.skill import service as skill
from aip.libs.common.ids import utcnow
from aip.libs.tokens.keys import SigningKey
from iam_seed import Seed, app_url
from m2_fixtures import tool_spec
from test_h7_selection_eval import MP, ScriptedLlm, _cases
from test_m1_api import login
from test_m4_gates import SECRETS, People, _people

pytestmark = pytest.mark.infra
KEY = SigningKey.generate()


def _zip_skill(
    name: str = "query-work-orders",
    description: str = "指导模型如何查询本厂在制工单并汇总状态。",
    tools: list[str] | None = None,
) -> bytes:
    files: dict[str, bytes] = {
        "SKILL.md": (
            f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\n使用 mes 查询工具。\n"
        ).encode(),
    }
    if tools is not None:
        files["skill.lock.json"] = json.dumps({"tools": tools}, ensure_ascii=False).encode()
    return pkg.make_zip(files)


async def _force_active(conn: Any, actor: Any, vid: Any) -> None:
    """门禁 lab 后到 in_review；测试里硬推到 active（跳过 G5/G6 签字）。"""
    v = await registry.version_row(conn, vid)
    if v["state"] == "in_review":
        await registry.transition(conn, actor, vid, "approved", "test")
    v = await registry.version_row(conn, vid)
    if v["state"] == "approved":
        await registry.transition(conn, actor, vid, "canary", "test")
    v = await registry.version_row(conn, vid)
    if v["state"] == "canary":
        await registry.transition(conn, actor, vid, "active", "test")


async def _publish_tool(db: Database, s: Seed, p: People, cap: str) -> tuple[Any, str]:
    from m2_fixtures import promote

    spec = tool_spec(capability_id=cap, name=cap.split(".")[-1])
    async with db.tx(DbScope.system()) as conn:
        aid = await registry.create_asset(conn, p.dev, type_="tool", name=cap, owner_org_id=s.hq, classification=2)
        vid = await registry.create_version(conn, p.dev, aid, "1.0.0", spec)
        await promote(conn, p.dev, vid)
    return aid, f"{cap}@1.0.0"


async def _skill_draft(db: Database, s: Seed, p: People, zip_bytes: bytes, name: str = "skill.query_wo") -> Any:
    async with db.tx(DbScope.system()) as conn:
        aid = await registry.create_asset(conn, p.dev, type_="skill", name=name, owner_org_id=s.hq, classification=2)
        vid = await registry.create_version(
            conn,
            p.dev,
            aid,
            "1.0.0",
            {"name": "query-work-orders", "description": "占位描述足够十个字符以上。"},
        )
        await gates.set_acceptance(conn, p.dev, vid, {"scenarios": ["指导查询工单"]})
        await skill.attach_package(conn, p.dev, vid, zip_bytes, key=KEY)
    return vid


def test_tamper_fails_local_verify() -> None:
    raw = _zip_skill()
    files = pkg.unpack_zip(raw)
    manifest = pkg.build_manifest(files)
    sig = pkg.sign_manifest(manifest, KEY)
    assert pkg.verify_manifest(manifest, sig, [KEY])
    assert not pkg.verify_manifest(manifest, "00" * 32, [KEY])
    files["SKILL.md"] = files["SKILL.md"] + b"\n<!-- tampered -->\n"
    problems = pkg.local_verify(files, manifest)
    assert any("本地修改" in p for p in problems)


async def test_skill_gates_grant_and_retract(db: Database, seeded: Seed) -> None:
    p = await _people(db, seeded)
    cap = "production.mes.query_work_orders"
    tool_aid, ref = await _publish_tool(db, seeded, p, cap)
    vid = await _skill_draft(db, seeded, p, _zip_skill(tools=[ref]))
    cases = _cases(cap)
    llm = ScriptedLlm([c["expect_capability_id"] for c in cases])

    async with db.tx(DbScope.system()) as conn:
        await gates.put_selection_eval(conn, p.dev, vid, model_profiles=[MP], cases=cases)
        g0 = await gates.submit(conn, p.dev, vid)
        assert g0["passed"] is True, g0
        lab = await gates.run_lab(conn, p.dev, vid, secrets=SECRETS, llm=llm)
        assert lab["passed"] is True, lab

        missing = await skill.missing_tool_grants(
            conn, consumer_id=seeded.consumers["vn-ai"], tool_refs=[ref], env="test"
        )
        assert ref in missing

        ceiling = await grants.create_ceiling(
            conn, p.admin, company_id=seeded.vn, asset_ids=[tool_aid], max_classification=3
        )
        await grants.create_grant(
            conn,
            p.admin,
            asset_id=tool_aid,
            grantee_type="consumer",
            grantee_id=seeded.consumers["vn-ai"],
            env="test",
            expires_at=utcnow() + dt.timedelta(days=30),
            ceiling_id=ceiling,
        )
        assert (
            await skill.missing_tool_grants(
                conn, consumer_id=seeded.consumers["vn-ai"], tool_refs=[ref], env="test"
            )
            == []
        )

        await _force_active(conn, p.dev, vid)
        await registry.create_release(
            conn, p.admin, vid, env="test", channel="stable", scope={"company_ids": [seeded.vn]}, exposure=["rest"]
        )
        items = await skill.list_skills(conn, channel="stable")
        assert any(i["name"] == "query-work-orders" and not i["retracted"] for i in items)

        await registry.transition(conn, p.admin, vid, "suspended", "test retract")
        items2 = await skill.list_skills(conn, channel="stable", include_retracted=True)
        hit = [i for i in items2 if i["version_id"] == str(vid)]
        assert hit and hit[0]["retracted"] is True


async def test_corp_skill_verify_and_api(db: Database, seeded: Seed, tmp_path: Path) -> None:
    p = await _people(db, seeded)
    vid = await _skill_draft(db, seeded, p, _zip_skill(tools=[]))
    async with db.tx(DbScope.system()) as conn:
        assert (await gates.submit(conn, p.dev, vid))["passed"]
        assert (await gates.run_lab(conn, p.dev, vid, secrets=SECRETS))["passed"]
        await _force_active(conn, p.dev, vid)
        await registry.create_release(
            conn, p.admin, vid, env="test", channel="stable", scope={"group": True}, exposure=["rest"]
        )

    app = create_cp(db=Database(app_url()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://cp") as cp:
        admin = await login(cp, seeded, "admin")
        listed = await cp.get("/api/v1/skills", headers=admin)
        assert listed.status_code == 200
        assert any(i["name"] == "query-work-orders" for i in listed.json()["items"])

        man = await cp.get("/api/v1/skills/query-work-orders/versions/1.0.0/manifest", headers=admin)
        assert man.status_code == 200 and man.json().get("manifest")
        dl = await cp.get("/api/v1/skills/query-work-orders/versions/1.0.0/download", headers=admin)
        assert dl.status_code == 200

        files = pkg.unpack_zip(dl.content)
        dest = tmp_path / "query-work-orders"
        dest.mkdir()
        for path, data in files.items():
            (dest / path).parent.mkdir(parents=True, exist_ok=True)
            (dest / path).write_bytes(data)
        (dest / ".aip-skill.json").write_text(
            json.dumps(
                {
                    "name": "query-work-orders",
                    "version": "1.0.0",
                    "manifest": man.json()["manifest"],
                    "signature": man.json()["signature"],
                    "content_sha256": man.json()["content_sha256"],
                }
            ),
            encoding="utf-8",
        )

        class NS:
            path = str(dest)

        corp_skill.cmd_verify(NS())  # type: ignore[arg-type]
        (dest / "SKILL.md").write_bytes((dest / "SKILL.md").read_bytes() + b"\nx")
        with pytest.raises(SystemExit) as ei:
            corp_skill.cmd_verify(NS())  # type: ignore[arg-type]
        assert ei.value.code == 1
