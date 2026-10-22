"""M1：组织树、范围检查、RLS、密级审批、登录与会话、批量导入（architecture.md 2、3.4、14 M1）。"""

from __future__ import annotations

import datetime as dt
import uuid

import pyotp
import pytest
from sqlalchemy import func, select, text, update

from aip.control_plane.db import Database, DbScope
from aip.control_plane.iam import auth, orgs, snapshot, users
from aip.control_plane.iam import tables as t
from aip.control_plane.iam.authz import Actor
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.common.ids import utcnow
from aip.libs.tokens.keys import SigningKey
from iam_seed import PASSWORD, Seed, actor_for

pytestmark = pytest.mark.infra


async def _payload(db: Database) -> dict:  # type: ignore[type-arg]
    async with db.tx(DbScope.system()) as conn:
        return await snapshot.build_payload(conn)


# ---------- 组织树 ----------


async def test_closure_and_path_maintained_on_create_and_move(db: Database, seeded: Seed) -> None:
    admin = await actor_for(db, seeded, "vn_admin")
    async with db.tx(admin.db_scope()) as conn:
        await orgs.move_org(conn, admin, seeded.vn_mfg_l1, seeded.vn_qa)
        ancestors = {
            r[0]
            for r in await conn.execute(
                select(t.org_closure.c.ancestor_id).where(t.org_closure.c.descendant_id == seeded.vn_mfg_l1)
            )
        }
        moved = await orgs.get_org(conn, seeded.vn_mfg_l1)
    assert ancestors == {seeded.group, seeded.vn, seeded.vn_qa, seeded.vn_mfg_l1}
    assert moved.parent_id == seeded.vn_qa
    assert moved.path.split(".")[-2] == "n" + seeded.vn_qa.hex


async def test_org_move_updates_data_scope_automatically(db: Database, seeded: Seed) -> None:
    """验收：组织迁移后数据范围自动更新（vn_qa 用户的范围随 L1 迁入而扩大，vn_worker 的组织链随之变化）。"""
    before = await _payload(db)
    worker = str(seeded.users["vn_worker"])
    assert str(seeded.vn_mfg) in before["users"][worker]["org_ids"]
    assert str(seeded.vn_mfg_l1) not in before["users"][str(seeded.users["vn_qa"])]["data_scope_org_ids"]
    admin = await actor_for(db, seeded, "vn_admin")
    async with db.tx(admin.db_scope()) as conn:
        await orgs.move_org(conn, admin, seeded.vn_mfg_l1, seeded.vn_qa)
    after = await _payload(db)
    assert str(seeded.vn_mfg_l1) in after["users"][str(seeded.users["vn_qa"])]["data_scope_org_ids"]
    assert str(seeded.vn_mfg) not in after["users"][worker]["org_ids"]
    assert str(seeded.vn_qa) in after["users"][worker]["org_ids"]


async def test_cannot_move_into_own_subtree_or_exceed_depth(db: Database, seeded: Seed) -> None:
    admin = await actor_for(db, seeded, "admin")
    async with db.tx(admin.db_scope()) as conn:
        with pytest.raises(PlatformError, match="子树"):
            await orgs.move_org(conn, admin, seeded.vn_mfg, seeded.vn_mfg_l1)
        parent = seeded.vn_mfg_l1
        with pytest.raises(PlatformError, match="深度"):
            for i in range(6):
                parent = (await orgs.create_org(conn, admin, type_="team", code=f"T{i}", name="t", parent_id=parent)).id


async def test_disable_org_removes_subtree_from_snapshot(db: Database, seeded: Seed) -> None:
    admin = await actor_for(db, seeded, "vn_admin")
    async with db.tx(admin.db_scope()) as conn:
        await orgs.disable_org(conn, admin, seeded.vn_mfg)
    p = await _payload(db)
    assert str(seeded.vn_mfg_l1) not in p["orgs"]
    assert p["users"][str(seeded.users["vn_worker"])]["status"] == "disabled"


# ---------- 分公司管理员只能改本公司子树 ----------


async def test_company_admin_confined_to_own_subtree(db: Database, seeded: Seed) -> None:
    vn = await actor_for(db, seeded, "vn_admin")
    async with db.tx(vn.db_scope()) as conn:
        dept = await orgs.create_org(conn, vn, type_="department", code="VN01-HR", name="人事", parent_id=seeded.vn)
        assert dept.company_id == seeded.vn
        # 看不到 KH 的节点（RLS），自然也改不了
        with pytest.raises(PlatformError) as e:
            await orgs.create_org(conn, vn, type_="department", code="KH01-X", name="x", parent_id=seeded.kh)
        assert e.value.code == ErrorCode.NOT_FOUND
        with pytest.raises(PlatformError) as e:
            await users.create_user(conn, vn, display_name="x", home_org_id=seeded.kh_mfg)
        assert e.value.code in (ErrorCode.NOT_FOUND, ErrorCode.PERMISSION_DENIED)
        for denied in (
            orgs.create_org(conn, vn, type_="company", code="VN02", name="x", parent_id=seeded.group),
            users.assign_role(conn, vn, seeded.users["vn_worker"], "platform_admin", seeded.vn),
            orgs.disable_org(conn, vn, seeded.vn),
        ):
            with pytest.raises(PlatformError):
                await denied


async def test_company_admin_cannot_move_across_companies_even_with_visibility(db: Database, seeded: Seed) -> None:
    """即使用全局可见的事务，范围检查仍按生效节点拒绝（RLS 与 RBAC 双重保护）。"""
    vn = await actor_for(db, seeded, "vn_admin")
    async with db.tx(DbScope.system()) as conn:
        with pytest.raises(PlatformError) as e:
            await orgs.move_org(conn, vn, seeded.vn_mfg, seeded.kh)
        assert e.value.code == ErrorCode.PERMISSION_DENIED
        with pytest.raises(PlatformError) as e:
            await orgs.create_org(conn, vn, type_="department", code="KH01-X", name="x", parent_id=seeded.kh)
        assert e.value.code == ErrorCode.PERMISSION_DENIED


async def test_platform_admin_cross_company_move_rehomes_users(db: Database, seeded: Seed) -> None:
    admin = await actor_for(db, seeded, "admin")
    async with db.tx(admin.db_scope()) as conn:
        await orgs.move_org(conn, admin, seeded.vn_mfg, seeded.kh)
        u = await users.user_row(conn, seeded.users["vn_worker"])
    assert u["company_id"] == seeded.kh


# ---------- RLS：A 公司身份查不到 B 公司数据（每张表一个用例） ----------


async def _seed_all_tables(db: Database, s: Seed) -> None:
    sys_ = Actor.system_actor()
    from aip.control_plane.iam import consumers

    async with db.tx(DbScope.system()) as conn:
        await conn.execute(
            t.org_external_code.insert().values(org_id=s.kh, system_key="mes", external_code="F900", company_id=s.kh)
        )
        await conn.execute(t.business_role.insert().values(company_id=s.kh, code="purchase_manager", name="采购经理"))
        await users.grant_data_scope(conn, sys_, s.users["kh_worker"], s.kh)
        await users.request_clearance(
            conn, sys_, s.users["kh_worker"], 2, "需要查看质量数据", utcnow() + dt.timedelta(days=30)
        )
        await consumers.issue_static_token(conn, sys_, s.consumers["kh-ai"])
        await consumers.add_jwk(
            conn, sys_, s.consumers["kh-ai"], {"kty": "EC", "crv": "P-256", "kid": "k1", **_ec_pub()}
        )
        await consumers.create_binding(conn, sys_, s.consumers["kh-ai"], "kh.emp.1", s.users["kh_worker"])
        await conn.execute(
            t.agent_profile.insert().values(
                id=uuid.uuid4(), consumer_id=s.consumers["kh-ai"], company_id=s.kh, name="production", domain_ids=[]
            )
        )
        await conn.execute(
            t.external_login.insert().values(
                provider="oidc",
                external_id="kh-seed-ext",
                user_id=s.users["kh_admin"],
                company_id=s.kh,
                created_at=utcnow(),
                created_by=s.users["kh_admin"],
            )
        )
        await conn.execute(
            t.webauthn_credential.insert().values(
                id=uuid.uuid4(),
                user_id=s.users["kh_admin"],
                company_id=s.kh,
                credential_id="cred-kh-seed",
                public_key_cose=b"\x01\x02",
                sign_count=0,
                transports=[],
                nickname="seed",
                created_at=utcnow(),
            )
        )
        await conn.execute(
            t.webauthn_challenge.insert().values(
                id=uuid.uuid4(),
                user_id=s.users["kh_admin"],
                company_id=s.kh,
                kind="register",
                challenge="chal-kh-seed",
                expires_at=utcnow() + dt.timedelta(minutes=5),
                created_at=utcnow(),
            )
        )
    async with db.tx(DbScope.system()) as conn:
        await auth.login(conn, "kh_admin", PASSWORD, pyotp.TOTP(s.totp["kh_admin"]).now())


def _ec_pub() -> dict[str, str]:
    jwk = SigningKey.generate().public_jwk()
    return {"x": jwk["x"], "y": jwk["y"]}


@pytest.mark.parametrize("table", [tb.name for tb in t.md.sorted_tables if tb.name != "iam_snapshot"])
async def test_rls_company_isolation(db: Database, seeded: Seed, table: str) -> None:
    await _seed_all_tables(db, seeded)
    tb = t.md.tables[f"iam.{table}"]
    async with db.tx(DbScope.system()) as conn:
        kh_rows = await conn.execute(select(func.count()).select_from(tb).where(tb.c.company_id == seeded.kh))
        assert kh_rows.scalar_one() > 0, f"种子数据没有覆盖 {table}"
    async with db.tx(DbScope(company_ids=frozenset({seeded.vn}))) as conn:
        visible = await conn.execute(select(func.count()).select_from(tb).where(tb.c.company_id == seeded.kh))
        assert visible.scalar_one() == 0
        total_foreign = await conn.execute(select(func.count()).select_from(tb).where(tb.c.company_id != seeded.vn))
        assert total_foreign.scalar_one() == 0


async def test_rls_blocks_cross_company_writes(db: Database, seeded: Seed) -> None:
    async with db.tx(DbScope(company_ids=frozenset({seeded.vn}))) as conn:
        with pytest.raises(Exception, match="row-level security"):
            await conn.execute(
                t.domain_event.insert().values(company_id=seeded.kh, kind="x", priority="normal", payload={})
            )
    async with db.tx(DbScope(company_ids=frozenset({seeded.vn}))) as conn:
        res = await conn.execute(
            update(t.iam_user).where(t.iam_user.c.id == seeded.users["kh_worker"]).values(display_name="hacked")
        )
        assert res.rowcount == 0


async def test_app_role_is_not_superuser(db: Database) -> None:
    async with db.tx(DbScope()) as conn:
        row = (
            await conn.execute(text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"))
        ).one()
    assert row == (False, False)


# ---------- 密级 ----------


async def test_clearance_raise_requires_data_owner_approval(db: Database, seeded: Seed) -> None:
    worker = seeded.users["vn_worker"]
    vn = await actor_for(db, seeded, "vn_admin")
    sec = await actor_for(db, seeded, "security")
    async with db.tx(vn.db_scope()) as conn:
        assert await users.effective_clearance(conn, worker) == 2  # 继承 VN01 默认值
        # 分公司管理员没有直接修改密级的入口，只能发起申请，也不能审批
        req = await users.request_clearance(conn, vn, worker, 3, "参与质量追溯项目", utcnow() + dt.timedelta(days=30))
        with pytest.raises(PlatformError) as e:
            await users.decide_clearance(conn, vn, req, approve=True)
        assert e.value.code == ErrorCode.PERMISSION_DENIED
        assert await users.effective_clearance(conn, worker) == 2
    async with db.tx(sec.db_scope()) as conn:
        assert await users.decide_clearance(conn, sec, req, approve=True) == "approved"
        assert await users.effective_clearance(conn, worker) == 3
        with pytest.raises(PlatformError):
            await users.decide_clearance(conn, sec, req, approve=True)  # 已处理


async def test_clearance_self_approval_forbidden_and_expiry_falls_back(db: Database, seeded: Seed) -> None:
    sec = await actor_for(db, seeded, "security")
    async with db.tx(sec.db_scope()) as conn:
        req = await users.request_clearance(
            conn, sec, seeded.users["security"], 4, "自我提升", utcnow() + dt.timedelta(days=1)
        )
        with pytest.raises(PlatformError, match="自己"):
            await users.decide_clearance(conn, sec, req, approve=True)
        worker = seeded.users["vn_qa"]
        assert await users.effective_clearance(conn, worker) == 3  # VN01-QA 默认 L3
        await conn.execute(
            update(t.iam_user)
            .where(t.iam_user.c.id == worker)
            .values(clearance_override=4, clearance_expires_at=utcnow() - dt.timedelta(seconds=1))
        )
        assert await users.effective_clearance(conn, worker) == 3


# ---------- 登录与会话 ----------


async def test_login_requires_totp_and_locks_after_failures(db: Database, seeded: Seed) -> None:
    async with db.tx(DbScope.system()) as conn:
        with pytest.raises(PlatformError) as e:
            await auth.login(conn, "vn_admin", PASSWORD, None)
        assert e.value.code == ErrorCode.MFA_REQUIRED
        token, _, uid = await auth.login(conn, "vn_admin", PASSWORD, seeded.code("vn_admin"))
        assert uid == seeded.users["vn_admin"]
        assert await auth.resolve_session(conn, token) == uid
    for _ in range(5):
        async with db.tx(DbScope.system()) as conn:
            with pytest.raises(PlatformError):
                await auth.login(conn, "vn_admin", "wrong-password!!", seeded.code("vn_admin"))
    async with db.tx(DbScope.system()) as conn:
        with pytest.raises(PlatformError) as e:
            await auth.login(conn, "vn_admin", PASSWORD, seeded.code("vn_admin"))
        assert e.value.code == ErrorCode.ACCOUNT_LOCKED


async def test_disabled_user_sessions_are_revoked(db: Database, seeded: Seed) -> None:
    async with db.tx(DbScope.system()) as conn:
        token, _, _ = await auth.login(conn, "kh_admin", PASSWORD, seeded.code("kh_admin"))
    admin = await actor_for(db, seeded, "admin")
    async with db.tx(admin.db_scope()) as conn:
        await users.set_status(conn, admin, seeded.users["kh_admin"], "disabled")
    async with db.tx(DbScope.system()) as conn:
        with pytest.raises(PlatformError) as e:
            await auth.resolve_session(conn, token)
        assert e.value.code == ErrorCode.UNAUTHENTICATED


async def test_unknown_user_and_wrong_password_look_the_same(db: Database, seeded: Seed) -> None:
    async with db.tx(DbScope.system()) as conn:
        with pytest.raises(PlatformError) as a:
            await auth.login(conn, "nobody", PASSWORD, "000000")
        with pytest.raises(PlatformError) as b:
            await auth.login(conn, "vn_admin", "wrong-password!!", "000000")
    assert (a.value.code, a.value.detail) == (b.value.code, b.value.detail)


# ---------- 批量导入 ----------


async def test_import_dry_run_then_apply(db: Database, seeded: Seed) -> None:
    csv_text = (
        "display_name,employee_no,home_org_code,email\n"
        "阮文安,E-VN-100,VN01-MFG,\n"
        "已存在,E-VN-001,VN01-MFG-L1,\n"
        "越权,E-KH-100,KH01-MFG,\n"
        ",E-VN-101,VN01-MFG,\n"
    )
    vn = await actor_for(db, seeded, "vn_admin")
    async with db.tx(vn.db_scope()) as conn:
        dry = await users.import_users_csv(conn, vn, csv_text, dry_run=True)
        assert (dry.created, dry.unchanged, len(dry.errors)) == (1, 1, 2)
        assert (
            await conn.execute(
                select(func.count()).select_from(t.iam_user).where(t.iam_user.c.employee_no == "E-VN-100")
            )
        ).scalar_one() == 0
        real = await users.import_users_csv(conn, vn, csv_text, dry_run=False)
        assert real.created == 1
        row = (await conn.execute(select(t.iam_user).where(t.iam_user.c.employee_no == "E-VN-100"))).mappings().one()
    assert row["import_batch_id"] == real.batch_id
    assert row["company_id"] == seeded.vn


async def test_import_rollback_deletes_unused_and_disables_used(db: Database, seeded: Seed) -> None:
    from aip.control_plane.iam import consumers

    csv_text = "display_name,employee_no,home_org_code,email\n甲,E-RB-1,VN01-MFG,\n乙,E-RB-2,VN01-MFG,\n"
    vn = await actor_for(db, seeded, "vn_admin")
    async with db.tx(vn.db_scope()) as conn:
        res = await users.import_users_csv(conn, vn, csv_text, dry_run=False)
        used = (await conn.execute(select(t.iam_user.c.id).where(t.iam_user.c.employee_no == "E-RB-2"))).scalar_one()
    async with db.tx(DbScope.system()) as conn:
        await consumers.create_binding(conn, Actor.system_actor(), seeded.consumers["vn-ai"], "rb2", used)
    async with db.tx(vn.db_scope()) as conn:
        assert await users.rollback_import(conn, vn, res.batch_id) == {"deleted": 1, "disabled": 1}
        left = {
            r.employee_no: r.status
            for r in (
                await conn.execute(
                    select(t.iam_user.c.employee_no, t.iam_user.c.status).where(
                        t.iam_user.c.import_batch_id == res.batch_id
                    )
                )
            )
        }
    assert left == {"E-RB-2": "disabled"}
    kh = await actor_for(db, seeded, "kh_admin")
    async with db.tx(kh.db_scope()) as conn:
        with pytest.raises(PlatformError) as e:
            await users.rollback_import(conn, kh, res.batch_id)
    assert e.value.code == ErrorCode.NOT_FOUND
