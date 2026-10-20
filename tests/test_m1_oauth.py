"""M1：数据面令牌交换、bind_policy、请求认证、快照驱动的撤销（architecture.md 3.1、3.2、4.4）。"""

from __future__ import annotations

import copy
import datetime as dt
import secrets
from typing import Any

import httpx
import jwt
import pytest

from aip.control_plane.db import Database, DbScope
from aip.control_plane.iam import consumers, snapshot, users
from aip.control_plane.iam.authz import Actor
from aip.control_plane.registry import snapshot as release_snapshot
from aip.data_plane.app import create_app
from aip.data_plane.iam_cache import SnapshotRejected
from aip.libs.common.config import get_settings
from aip.libs.tokens import platform_token
from aip.libs.tokens.keys import SigningKey
from aip.libs.tokens.replay import MemoryReplayStore
from iam_seed import Seed

pytestmark = pytest.mark.infra
S = get_settings()
PLATFORM = SigningKey.generate()
VN_KEY = SigningKey.generate()
KH_KEY = SigningKey.generate()


def assertion(key: SigningKey, client_id: str, sub: str, aud: str = S.token_endpoint_aud, **extra: Any) -> str:
    now = int(dt.datetime.now(dt.UTC).timestamp())
    claims = {
        "iss": client_id,
        "sub": sub,
        "aud": aud,
        "iat": now,
        "exp": now + 120,
        "jti": secrets.token_hex(8),
        **extra,
    }
    return jwt.encode(claims, key.private, algorithm="ES256", headers={"kid": key.kid})


class Env:
    def __init__(self, db: Database, seed: Seed) -> None:
        self.db, self.seed = db, seed
        self.app = create_app(key=PLATFORM, replay=MemoryReplayStore())
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="https://api.aip.internal")
        self.vn_token = ""

    async def setup(self) -> None:
        sys_ = Actor.system_actor()
        s = self.seed
        async with self.db.tx(DbScope.system()) as conn:
            await consumers.add_jwk(conn, sys_, s.consumers["vn-ai"], VN_KEY.public_jwk())
            await consumers.add_jwk(conn, sys_, s.consumers["kh-ai"], KH_KEY.public_jwk())
            _, self.vn_token, _ = await consumers.issue_static_token(conn, sys_, s.consumers["vn-ai"])
            _, self.batch_token, _ = await consumers.issue_static_token(conn, sys_, s.consumers["vn-batch"])
            await consumers.create_binding(conn, sys_, s.consumers["vn-ai"], "vn.emp.001", s.users["vn_worker"])
        await self.refresh()

    async def refresh(self) -> dict[str, Any]:
        async with self.db.tx(DbScope.system()) as conn:
            await snapshot.publish(conn, PLATFORM)
            snap = await snapshot.latest(conn)
            # Catalog（M6b）按发布快照过滤；无快照时 PDP 默认拒绝 → 503，测试里同步空目录快照
            await release_snapshot.publish(conn, PLATFORM)
            rel = await release_snapshot.latest(conn)
        assert snap is not None
        self.app.state.iam.load(snap)
        if rel is not None:
            self.app.state.releases.load(rel)
        return snap

    async def exchange(self, client_id: str, key: SigningKey, sub: str, **form: str) -> httpx.Response:
        client_assertion = assertion(key, client_id, client_id)
        data = {
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion(key, client_id, sub),
            "client_id": client_id,
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": client_assertion,
            **form,
        }
        return await self.http.post("/oauth/token", data=data)

    def principal(self, token: str, user_assertion: str | None = None) -> Any:
        return self.app.state.auth.principal_from_request(f"Bearer {token}", user_assertion)


@pytest.fixture
async def env(db: Database, seeded: Seed) -> Env:
    e = Env(db, seeded)
    await e.setup()
    return e


async def test_metadata_endpoints(env: Env) -> None:
    prm = (await env.http.get("/.well-known/oauth-protected-resource")).json()
    assert prm["resource"] == S.mcp_resource and prm["authorization_servers"] == [S.issuer]
    asm = (await env.http.get("/.well-known/oauth-authorization-server")).json()
    assert "urn:ietf:params:oauth:grant-type:jwt-bearer" in asm["grant_types_supported"]
    assert (await env.http.get("/.well-known/jwks.json")).json()["keys"][0]["kid"] == PLATFORM.kid


async def test_token_exchange_issues_auth_only_user_token(env: Env) -> None:
    r = await env.exchange("vn-ai", VN_KEY, "vn.emp.001")
    assert r.status_code == 200, r.text
    tok = r.json()["access_token"]
    claims = jwt.decode(tok, options={"verify_signature": False})
    assert set(claims) <= platform_token.ALLOWED_CLAIMS
    assert claims["sub"] == str(env.seed.users["vn_worker"]) and claims["act"] == {"sub": "vn-ai"}
    assert claims["aud"] == S.mcp_resource
    p = await env.principal(tok)
    assert p.identity_kind == "user" and p.auth_mode == "token_exchange"
    assert p.company_id == str(env.seed.vn)
    assert p.clearance == 2  # min(用户 L2, 消费方上限 L3)
    assert str(env.seed.vn_mfg_l1) in p.data_scope_org_ids and str(env.seed.vn_qa) not in p.data_scope_org_ids
    catalog = await env.http.get("/api/v1/catalog", headers={"Authorization": f"Bearer {tok}"})
    assert catalog.status_code == 200


async def test_strict_policy_rejects_unbound_subject(env: Env) -> None:
    r = await env.exchange("vn-ai", VN_KEY, "vn.emp.999")
    assert r.status_code == 403 and r.json()["code"] == "USER_NOT_BOUND"


async def test_by_employee_no_policy_maps_within_company_only(env: Env) -> None:
    ok = await env.exchange("kh-ai", KH_KEY, "E-KH-001")
    assert ok.status_code == 200, ok.text
    cross = await env.exchange("kh-ai", KH_KEY, "E-VN-001")  # 越南员工工号，不在 KH01
    assert cross.json()["code"] == "USER_NOT_BOUND"


async def test_app_only_consumer_cannot_act_for_users(env: Env) -> None:
    r = await env.http.post(
        "/oauth/token",
        data={"grant_type": "client_credentials"},
        headers={"Authorization": f"Bearer {env.batch_token}"},
    )
    assert r.status_code == 200
    p = await env.principal(r.json()["access_token"])
    assert p.identity_kind == "app" and p.clearance == 1 and str(env.seed.vn_mfg) in p.data_scope_org_ids
    u = await env.http.post(
        "/oauth/token",
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion(VN_KEY, "vn-batch", "vn.emp.001"),
        },
        headers={"Authorization": f"Bearer {env.batch_token}"},
    )
    assert u.status_code in (401, 403)


@pytest.mark.parametrize("bad", ["forged", "expired", "replayed", "wrong_aud"])
async def test_bad_assertions_rejected_at_token_endpoint(env: Env, bad: str) -> None:
    good = assertion(VN_KEY, "vn-ai", "vn.emp.001")
    token = {
        "forged": assertion(KH_KEY, "vn-ai", "vn.emp.001"),
        "expired": jwt.encode(
            {**jwt.decode(good, options={"verify_signature": False}), "iat": 1, "exp": 100},
            VN_KEY.private,
            algorithm="ES256",
            headers={"kid": VN_KEY.kid},
        ),
        "replayed": good,
        "wrong_aud": assertion(VN_KEY, "vn-ai", "vn.emp.001", aud="https://evil.example"),
    }[bad]
    data = {"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": token}
    hdr = {"Authorization": f"Bearer {env.vn_token}"}
    if bad == "replayed":
        assert (await env.http.post("/oauth/token", data=data, headers=hdr)).status_code == 200
    r = await env.http.post("/oauth/token", data=data, headers=hdr)
    assert r.status_code == 401
    assert r.json()["code"] in ("ASSERTION_INVALID", "ASSERTION_EXPIRED", "ASSERTION_REPLAYED")


async def test_header_assertion_mode(env: Env) -> None:
    ua = assertion(VN_KEY, "vn-ai", "vn.emp.001", aud=S.mcp_resource)
    p = await env.principal(env.vn_token, ua)
    assert p.auth_mode == "header_assertion" and p.user_id == str(env.seed.users["vn_worker"])
    app = await env.principal(env.vn_token)
    assert app.identity_kind == "app" and app.auth_mode == "app_token"


async def test_disable_user_takes_effect_with_next_snapshot(env: Env) -> None:
    tok = (await env.exchange("vn-ai", VN_KEY, "vn.emp.001")).json()["access_token"]
    assert (await env.principal(tok)).user_id
    admin = Actor.system_actor()
    async with env.db.tx(DbScope.system()) as conn:
        await users.set_status(conn, admin, env.seed.users["vn_worker"], "left")
    await env.refresh()
    r = await env.http.get("/api/v1/catalog", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 401


async def test_revoked_static_token_rejected_after_snapshot(env: Env) -> None:
    async with env.db.tx(DbScope.system()) as conn:
        cred, tok, _ = await consumers.issue_static_token(conn, Actor.system_actor(), env.seed.consumers["vn-ai"])
    await env.refresh()
    assert (await env.http.get("/api/v1/catalog", headers={"Authorization": f"Bearer {tok}"})).status_code == 200
    async with env.db.tx(DbScope.system()) as conn:
        await consumers.revoke_credential(conn, Actor.system_actor(), cred)
    await env.refresh()
    assert (await env.http.get("/api/v1/catalog", headers={"Authorization": f"Bearer {tok}"})).status_code == 401


async def test_tampered_or_unsigned_snapshot_rejected(env: Env) -> None:
    snap = await env.refresh()
    bad = copy.deepcopy(snap)
    bad["seq"] += 1
    bad["payload"]["seq"] += 1
    bad["payload"]["users"][str(env.seed.users["vn_worker"])]["clearance"] = 4
    with pytest.raises(SnapshotRejected):
        env.app.state.iam.load(bad)
    forged_key = SigningKey.generate()
    _, sig = snapshot.sign(bad["payload"], forged_key)
    bad["sha256"] = snapshot.sign(bad["payload"], forged_key)[0]
    bad["signature"] = sig
    with pytest.raises(SnapshotRejected):
        env.app.state.iam.load(bad)


async def test_data_plane_fails_closed_without_snapshot() -> None:
    app = create_app(key=PLATFORM, replay=MemoryReplayStore())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://x") as c:
        r = await c.get("/api/v1/catalog", headers={"Authorization": "Bearer aipk_whatever"})
        assert r.status_code == 503 and r.json()["code"] == "POLICY_UNAVAILABLE"
        assert (await c.get("/readyz")).json()["checks"]["iam_snapshot"] == "missing"
