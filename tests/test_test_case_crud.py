"""草稿阶段门禁用例可改删（控制台「草稿可改删」配套 API）。"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from aip.control_plane.db import Database, DbScope
from aip.control_plane.gates import service as gates
from aip.control_plane.iam.authz import Actor
from aip.control_plane.registry import service as registry
from aip.libs.common.errors import ErrorCode, PlatformError
from iam_seed import Seed, actor_for
from m2_fixtures import tool_spec

pytestmark = pytest.mark.infra
SYS = Actor.system_actor()


async def _draft_version(db: Database, seeded: Seed) -> tuple[object, Actor]:
    actor = await actor_for(db, seeded, "admin")
    async with db.tx(DbScope.system()) as conn:
        aid = await registry.create_asset(
            conn, actor, type_="tool", name="case.crud", owner_org_id=seeded.hq, classification=2
        )
        vid = await registry.create_version(conn, actor, aid, "0.1.0", tool_spec())
    return vid, actor


async def test_update_and_delete_test_case_in_draft(db: Database, seeded: Seed) -> None:
    vid, actor = await _draft_version(db, seeded)
    async with db.tx(DbScope.system()) as conn:
        cid = await gates.add_test_case(
            conn,
            actor,
            vid,
            name="旧名",
            kind="functional",
            args={},
            principal={"company": "HQ", "clearance": 2},
            expect={"status": "succeeded"},
        )
        updated = await gates.update_test_case(
            conn,
            actor,
            vid,
            cid,
            name="新名",
            kind="permission",
            args={"x": 1},
            principal={"company": "HQ", "granted": False},
            expect={"status": "failed", "error_code": "PERMISSION_DENIED"},
        )
        assert updated["name"] == "新名" and updated["kind"] == "permission"
        row = (await conn.execute(select(gates.test_case).where(gates.test_case.c.id == cid))).mappings().one()
        assert row["name"] == "新名" and row["kind"] == "permission"
        await gates.delete_test_case(conn, actor, vid, cid)
        left = (await conn.execute(select(gates.test_case).where(gates.test_case.c.id == cid))).first()
        assert left is None


async def test_mutate_case_blocked_after_leave_draft(db: Database, seeded: Seed) -> None:
    vid, actor = await _draft_version(db, seeded)
    async with db.tx(DbScope.system()) as conn:
        cid = await gates.add_test_case(
            conn,
            actor,
            vid,
            name="功能",
            kind="functional",
            args={},
            principal={"company": "HQ"},
            expect={"status": "succeeded"},
        )
        await registry.transition(conn, actor, vid, "testing", "test leave draft")
        with pytest.raises(PlatformError) as e:
            await gates.update_test_case(
                conn,
                actor,
                vid,
                cid,
                name="改不动",
                kind="functional",
                args={},
                principal={"company": "HQ"},
                expect={"status": "succeeded"},
            )
        assert e.value.code == ErrorCode.PRECONDITION_FAILED
        with pytest.raises(PlatformError) as e2:
            await gates.delete_test_case(conn, actor, vid, cid)
        assert e2.value.code == ErrorCode.PRECONDITION_FAILED


async def test_update_missing_case_not_found(db: Database, seeded: Seed) -> None:
    import uuid

    vid, actor = await _draft_version(db, seeded)
    async with db.tx(DbScope.system()) as conn:
        with pytest.raises(PlatformError) as e:
            await gates.update_test_case(
                conn,
                actor,
                vid,
                uuid.uuid4(),
                name="无",
                kind="functional",
                args={},
                principal={},
                expect={"status": "succeeded"},
            )
        assert e.value.code == ErrorCode.NOT_FOUND
