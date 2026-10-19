"""M2 / P4：OPA 五方交集（表驱动 ≥ 40 例，运行在真实 OPA sidecar 上）与 PolicyService 集成。"""

from __future__ import annotations

import copy
import datetime as dt
import json
import pathlib
import shutil
import subprocess
from typing import Any

import httpx
import pytest

from aip.control_plane.db import Database, DbScope
from aip.control_plane.grant import service as grants
from aip.control_plane.iam import consumers, users
from aip.control_plane.iam.authz import Actor
from aip.control_plane.registry import service as registry
from aip.control_plane.registry.snapshot import policy_bundle
from aip.data_plane.app import create_app as create_dp
from aip.data_plane.pdp import OpaClient
from aip.libs.common.config import get_settings
from aip.libs.common.ids import utcnow
from aip.libs.tokens.replay import MemoryReplayStore
from iam_seed import Seed
from m2_fixtures import KEY, Published, publish_tool, snapshots, tool_spec

pytestmark = pytest.mark.infra
ROOT = pathlib.Path(__file__).resolve().parents[1]

BASE: dict[str, Any] = {
    "principal": {
        "identity_kind": "user",
        "user_id": "u1",
        "company_id": "c_vn",
        "clearance": 2,
        "roles": [],
        "client_id": "vn-ai",
        "agent_profile_id": None,
    },
    "facts": {
        "release_in_scope": True,
        "app_grant": True,
        "within_ceiling": True,
        "user_grant_required": False,
        "user_grant": None,
        "scope_check": {"factory_id": {"org_id": "o1", "in_scope": True}},
    },
    "resource": {
        "capability_id": "production.mes.query_work_orders",
        "version": "1.0.0",
        "classification": 2,
        "risk": "read",
        "release_status": "active",
        "confirm_policy": "auto",
        "app_identity_allowed": False,
        "transport_exception": False,
        "exposure": ["rest", "mcp"],
        "required_scopes": [],
    },
    "context": {"env": "prod", "channel": "stable", "exposure": "mcp"},
}


def case(
    name: str, expect: str, reason: str | None = None, **patch: Any
) -> tuple[str, dict[str, Any], str, str | None]:
    """patch 形如 facts__app_grant=False（双下划线表示嵌套）；值为 DELETE 表示删除该字段。"""
    doc = copy.deepcopy(BASE)
    for path, value in patch.items():
        *parents, leaf = path.split("__")
        node = doc
        for p in parents:
            node = node[p]
        if value is DELETE:
            node.pop(leaf, None)
        else:
            node[leaf] = value
    return name, doc, expect, reason


DELETE = object()
OUT = {"factory_id": {"org_id": "o2", "in_scope": False}}

CASES = [
    case("baseline user read allowed", "allow"),
    case("deprecated still callable", "allow", resource__release_status="deprecated"),
    case("canary callable", "allow", resource__release_status="canary"),
    case("suspended denied", "deny", "RELEASE_NOT_AVAILABLE", resource__release_status="suspended"),
    case("retired denied", "deny", "RELEASE_NOT_AVAILABLE", resource__release_status="retired"),
    case("unknown status denied", "deny", "RELEASE_NOT_AVAILABLE", resource__release_status="weird"),
    case("missing release status denied", "deny", "RELEASE_NOT_AVAILABLE", resource__release_status=DELETE),
    case("consumer outside release scope", "deny", "OUT_OF_RELEASE_SCOPE", facts__release_in_scope=False),
    case("no grant", "deny", "NO_GRANT", facts__app_grant=False),
    case("grant fact missing", "deny", "NO_GRANT", facts__app_grant=DELETE),
    case("grant beyond ceiling", "deny", "EXCEEDS_CEILING", facts__within_ceiling=False),
    case("ceiling fact missing", "deny", "EXCEEDS_CEILING", facts__within_ceiling=DELETE),
    case(
        "user layer narrowing: not granted",
        "deny",
        "USER_NOT_GRANTED",
        facts__user_grant_required=True,
        facts__user_grant=False,
    ),
    case("user layer narrowing: granted", "allow", facts__user_grant_required=True, facts__user_grant=True),
    case(
        "user layer narrowing: unknown",
        "deny",
        "USER_NOT_GRANTED",
        facts__user_grant_required=True,
        facts__user_grant=None,
    ),
    case(
        "app identity ignores user narrowing",
        "allow",
        principal__identity_kind="app",
        principal__user_id=None,
        facts__user_grant_required=True,
        facts__user_grant=None,
    ),
    case("clearance equal allowed", "allow", principal__clearance=2, resource__classification=2),
    case("clearance higher allowed", "allow", principal__clearance=4, resource__classification=3),
    case("clearance too low", "deny", "CLASSIFICATION", principal__clearance=1, resource__classification=2),
    case("L4 asset for L3 user", "deny", "CLASSIFICATION", principal__clearance=3, resource__classification=4),
    case("clearance missing", "deny", "CLASSIFICATION", principal__clearance=DELETE),
    case("clearance wrong type", "deny", "CLASSIFICATION", principal__clearance="4"),
    case("data scope out of range", "deny", "OUT_OF_SCOPE:factory_id", facts__scope_check=OUT),
    case(
        "one of two args out of scope",
        "deny",
        "OUT_OF_SCOPE:dept_id",
        facts__scope_check={"factory_id": {"in_scope": True}, "dept_id": {"in_scope": False}},
    ),
    case("no scope binding at all", "allow", facts__scope_check={}),
    case("scope check missing", "deny", "SCOPE_CHECK_MISSING", facts__scope_check=DELETE),
    case("scope check malformed", "deny", "SCOPE_CHECK_MISSING", facts__scope_check=["x"]),
    case("app identity read allowed", "allow", principal__identity_kind="app", principal__user_id=None),
    case(
        "app identity write denied by default",
        "deny",
        "APP_IDENTITY_NOT_ALLOWED",
        principal__identity_kind="app",
        resource__risk="write",
        resource__confirm_policy="user_confirm",
    ),
    case(
        "app identity write explicitly allowed",
        "require_confirmation",
        principal__identity_kind="app",
        resource__risk="write",
        resource__app_identity_allowed=True,
        resource__confirm_policy="approval",
    ),
    case("unknown identity kind", "deny", "APP_IDENTITY_NOT_ALLOWED", principal__identity_kind="robot"),
    case(
        "write needs user confirmation",
        "require_confirmation",
        resource__risk="write",
        resource__confirm_policy="user_confirm",
    ),
    case(
        "write with approval policy",
        "require_confirmation",
        resource__risk="write",
        resource__confirm_policy="approval",
    ),
    case("write auto policy allowed", "allow", resource__risk="write", resource__confirm_policy="auto"),
    case("invalid confirm policy denied", "deny", "DEFAULT_DENY", resource__confirm_policy="maybe"),
    case(
        "danger over mcp denied",
        "deny",
        "EXPOSURE_NOT_ALLOWED",
        resource__risk="danger",
        resource__confirm_policy="approval",
    ),
    case(
        "danger internal requires approval",
        "require_confirmation",
        resource__risk="danger",
        resource__confirm_policy="approval",
        context__exposure="internal",
    ),
    case("exposure not enabled", "deny", "EXPOSURE_NOT_ALLOWED", resource__exposure=["rest"]),
    case("rest exposure allowed", "allow", context__exposure="rest", resource__exposure=["rest"]),
    case("a2a not enabled", "deny", "EXPOSURE_NOT_ALLOWED", context__exposure="a2a"),
    case(
        "required role present",
        "allow",
        resource__required_scopes=["business_owner"],
        principal__roles=["business_owner"],
    ),
    case("required role missing", "deny", "MISSING_ROLE", resource__required_scopes=["data_owner"]),
    case("multiple failures all reported", "deny", "NO_GRANT", facts__app_grant=False, principal__clearance=1),
    case("empty input denied", "deny", "RELEASE_NOT_AVAILABLE"),
]


@pytest.fixture
async def opa() -> OpaClient:
    client = OpaClient(get_settings().opa_url)
    await client.load_bundle(policy_bundle())
    return client


def test_case_count() -> None:
    assert len(CASES) >= 40


@pytest.mark.parametrize(("name", "input_", "expect", "reason"), CASES, ids=[c[0] for c in CASES])
async def test_policy_table(opa: OpaClient, name: str, input_: dict[str, Any], expect: str, reason: str | None) -> None:
    if name == "empty input denied":
        input_ = {}
    d = await opa.evaluate(input_)
    assert d.result == expect, (name, d)
    if reason:
        assert reason in d.reasons, (name, d.reasons)
    if expect == "require_confirmation":
        assert d.kind == input_["resource"]["confirm_policy"]


def test_multiple_failures_are_all_listed() -> None:
    _, doc, _, _ = case("x", "deny", facts__app_grant=False, principal__clearance=1, facts__scope_check=OUT)
    assert doc["facts"]["app_grant"] is False


async def test_multiple_reasons_returned(opa: OpaClient) -> None:
    _, doc, _, _ = case("x", "deny", facts__app_grant=False, principal__clearance=1, facts__scope_check=OUT)
    d = await opa.evaluate(doc)
    assert {"NO_GRANT", "CLASSIFICATION", "OUT_OF_SCOPE:factory_id"} <= set(d.reasons)


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not available")
def test_rego_unit_tests_with_opa_cli() -> None:
    r = subprocess.run(
        ["docker", "run", "--rm", "-v", f"{ROOT / 'policies'}:/p", "openpolicyagent/opa:1.10.0", "test", "/p", "-v"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PASS" in r.stdout


# ---------- PolicyService 集成：真实快照 → 事实 → OPA → 审计 ----------


class Dp:
    def __init__(self, tmp: pathlib.Path) -> None:
        self.app = create_dp(key=KEY, replay=MemoryReplayStore(), data_dir=tmp)
        self.policy = self.app.state.policy
        self.spool_dir = tmp / "audit-spool"

    async def load(self, db: Database) -> None:
        iam, rel = await snapshots(db)
        self.app.state.iam.load(iam)
        self.app.state.releases.load(rel)
        await self.policy.sync_policy()

    def principal(self, client_id: str, user_id: Any = None) -> Any:
        r = self.app.state.auth.resolver
        return (
            r.for_user(client_id, str(user_id), auth_mode="token_exchange", check_binding=False)
            if user_id
            else r.for_app(client_id, auth_mode="app_token")
        )

    def audit_lines(self) -> list[dict[str, Any]]:
        return [
            json.loads(x)
            for p in sorted(self.spool_dir.glob("*.jsonl"))
            for x in p.read_text(encoding="utf-8").splitlines()
        ]


@pytest.fixture
async def world(db: Database, seeded: Seed, tmp_path: pathlib.Path) -> tuple[Dp, Published]:
    pub = await publish_tool(db, seeded)
    dp = Dp(tmp_path)
    await dp.load(db)
    return dp, pub


CAP = "production.mes.query_work_orders"


async def test_user_in_scope_allowed_and_audited(world: tuple[Dp, Published], seeded: Seed) -> None:
    dp, _ = world
    p = dp.principal("vn-ai", seeded.users["vn_worker"])
    d = await dp.policy.decide(p, CAP, {"factory_id": "F001", "status": "open"}, exposure="mcp")
    assert d.result == "allow", d
    rec = dp.audit_lines()[-1]
    assert rec["kind"] == "pdp.decision" and rec["record"]["decision"] == "allow"
    assert rec["record"]["end_user_id"] == str(seeded.users["vn_worker"])
    assert "F001" not in json.dumps(rec), "审计不得记录参数原值"


async def test_cross_company_argument_denied(world: tuple[Dp, Published], seeded: Seed) -> None:
    dp, _ = world
    p = dp.principal("vn-ai", seeded.users["vn_worker"])
    d = await dp.policy.decide(p, CAP, {"factory_id": "F900"}, exposure="mcp")  # KH01 工厂
    assert d.result == "deny" and "OUT_OF_SCOPE:factory_id" in d.reasons
    missing = await dp.policy.decide(p, CAP, {}, exposure="mcp")
    assert "OUT_OF_SCOPE:factory_id" in missing.reasons
    unknown = await dp.policy.decide(p, CAP, {"factory_id": "F404"}, exposure="mcp")
    assert unknown.result == "deny"


async def test_consumer_without_grant_denied(world: tuple[Dp, Published], seeded: Seed, db: Database) -> None:
    dp, _ = world
    async with db.tx(DbScope.system()) as conn:
        await consumers.create_binding(
            conn, Actor.system_actor(), seeded.consumers["kh-ai"], "E-KH-001", seeded.users["kh_worker"]
        )
    await dp.load(db)
    p = dp.principal("kh-ai", seeded.users["kh_worker"])
    d = await dp.policy.decide(p, CAP, {"factory_id": "F900"}, exposure="mcp")
    assert d.result == "deny" and "NO_GRANT" in d.reasons


async def test_ceiling_revocation_invalidates_downstream_grants(
    world: tuple[Dp, Published], seeded: Seed, db: Database
) -> None:
    dp, pub = world
    async with db.tx(DbScope.system()) as conn:
        await grants.revoke_ceiling(conn, Actor.system_actor(), pub.ceiling_vn)
    await dp.load(db)
    d = await dp.policy.decide(
        dp.principal("vn-ai", seeded.users["vn_worker"]), CAP, {"factory_id": "F001"}, exposure="mcp"
    )
    assert d.result == "deny" and "EXCEEDS_CEILING" in d.reasons


async def test_clearance_cap_and_classification(db: Database, seeded: Seed, tmp_path: pathlib.Path) -> None:
    pub = await publish_tool(
        db, seeded, spec=tool_spec(data_classification=3), classification=3, name="qa.defect_report"
    )
    dp = Dp(tmp_path)
    await dp.load(db)
    worker = dp.principal("vn-ai", seeded.users["vn_worker"])  # 用户 L2
    assert (await dp.policy.decide(worker, CAP, {"factory_id": "F001"}, exposure="mcp")).reasons == ["CLASSIFICATION"]
    async with db.tx(DbScope.system()) as conn:
        req = await users.request_clearance(
            conn, Actor.system_actor(), seeded.users["vn_worker"], 3, "质量追溯项目", utcnow() + dt.timedelta(days=7)
        )
        sec = await users.load_actor(conn, seeded.users["security"])
        await users.decide_clearance(conn, sec, req, approve=True)
    await dp.load(db)
    worker = dp.principal("vn-ai", seeded.users["vn_worker"])
    assert (await dp.policy.decide(worker, CAP, {"factory_id": "F001"}, exposure="mcp")).result == "allow"
    assert pub.asset_id


async def test_user_layer_narrowing(world: tuple[Dp, Published], seeded: Seed, db: Database) -> None:
    dp, pub = world
    async with db.tx(DbScope.system()) as conn:
        await grants.create_grant(
            conn,
            Actor.system_actor(),
            asset_id=pub.asset_id,
            grantee_type="org_unit",
            grantee_id=seeded.vn_qa,
            env="test",
            expires_at=utcnow() + dt.timedelta(days=1),
            ceiling_id=pub.ceiling_vn,
        )
    await dp.load(db)
    args = {"factory_id": "F001"}
    worker = await dp.policy.decide(dp.principal("vn-ai", seeded.users["vn_worker"]), CAP, args, exposure="mcp")
    qa = await dp.policy.decide(dp.principal("vn-ai", seeded.users["vn_qa"]), CAP, args, exposure="mcp")
    app = await dp.policy.decide(dp.principal("vn-ai"), CAP, args, exposure="mcp")
    assert worker.reasons == ["USER_NOT_GRANTED"]
    assert qa.result == "allow" and app.result == "allow"


async def test_suspended_release_denied(world: tuple[Dp, Published], seeded: Seed, db: Database) -> None:
    dp, pub = world
    async with db.tx(DbScope.system()) as conn:
        await registry.transition(conn, Actor.system_actor(), pub.version_id, "suspended", "risk")
    await dp.load(db)
    d = await dp.policy.decide(dp.principal("vn-ai", seeded.users["vn_worker"]), CAP, {"factory_id": "F001"})
    assert d.reasons == ["RELEASE_NOT_AVAILABLE"]


async def test_fail_closed_without_iam_snapshot(db: Database, seeded: Seed, tmp_path: pathlib.Path) -> None:
    """验收：IAM 快照缺失时全部拒绝。"""
    await publish_tool(db, seeded)
    dp = Dp(tmp_path)
    _, rel = await snapshots(db)
    dp.app.state.releases.load(rel)
    loaded = await snapshots(db)
    other = create_dp(key=KEY, replay=MemoryReplayStore(), data_dir=tmp_path / "x")
    other.state.iam.load(loaded[0])
    principal = other.state.auth.resolver.for_user(
        "vn-ai", str(seeded.users["vn_worker"]), auth_mode="token_exchange", check_binding=False
    )
    d = await dp.policy.decide(principal, CAP, {"factory_id": "F001"}, exposure="mcp")
    assert d.result == "deny" and d.reasons == ["POLICY_UNAVAILABLE"]
    assert dp.audit_lines()[-1]["record"]["decision"] == "deny"


async def test_fail_closed_when_opa_unreachable(world: tuple[Dp, Published], seeded: Seed) -> None:
    dp, _ = world
    dp.policy.opa._http = httpx.AsyncClient(base_url="http://127.0.0.1:9", timeout=0.3)
    d = await dp.policy.decide(dp.principal("vn-ai", seeded.users["vn_worker"]), CAP, {"factory_id": "F001"})
    assert d.result == "deny" and d.reasons == ["POLICY_UNAVAILABLE"]


async def test_unauthenticated_and_unknown_capability_denied(world: tuple[Dp, Published], seeded: Seed) -> None:
    dp, _ = world
    assert (await dp.policy.decide(None, CAP, {})).reasons == ["UNAUTHENTICATED"]
    d = await dp.policy.decide(dp.principal("vn-ai", seeded.users["vn_worker"]), "x.y.z", {})
    assert d.reasons == ["NOT_FOUND"]
