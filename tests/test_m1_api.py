"""M1：控制面 HTTP 流程与断言调试器；数据面经 HTTP 拉取快照。"""

from __future__ import annotations

import datetime as dt
import secrets

import httpx
import jwt
import pyotp
import pytest

from aip.control_plane.app import create_app as create_cp
from aip.control_plane.db import Database
from aip.data_plane.app import create_app as create_dp
from aip.libs.common.config import get_settings
from aip.libs.tokens.keys import SigningKey
from aip.libs.tokens.replay import MemoryReplayStore
from iam_seed import PASSWORD, Seed, app_url

pytestmark = pytest.mark.infra
S = get_settings()


@pytest.fixture
async def cp(seeded: Seed) -> httpx.AsyncClient:
    app = create_cp(db=Database(app_url()))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://api.aip.internal")


async def login(cp: httpx.AsyncClient, seed: Seed, name: str) -> dict[str, str]:
    r = await cp.post("/api/v1/auth/login", json={"login_name": name, "password": PASSWORD, "totp": seed.code(name)})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['session_token']}"}


async def test_full_console_flow(cp: httpx.AsyncClient, seeded: Seed) -> None:
    admin = await login(cp, seeded, "admin")
    me = (await cp.get("/api/v1/me", headers=admin)).json()
    assert {"role": "platform_admin", "scope_org_id": str(seeded.group)} in me["roles"]

    # 平台管理员建消费方、登记公钥、签发令牌
    key = SigningKey.generate()
    c = await cp.post(
        "/api/v1/consumers",
        headers=admin,
        json={"client_id": "vn-mes-agent", "company_id": str(seeded.vn), "clearance_cap": 2},
    )
    assert c.status_code == 201, c.text
    cid = c.json()["id"]
    assert (await cp.post(f"/api/v1/consumers/{cid}/jwks", headers=admin, json=key.public_jwk())).status_code == 201
    cred = (await cp.post(f"/api/v1/consumers/{cid}/credentials", headers=admin, json={})).json()
    assert cred["token"].startswith("aipk_")

    # 分公司管理员：建部门、建可登录用户（返回一次 TOTP）、绑定员工
    vn = await login(cp, seeded, "vn_admin")
    dept = await cp.post(
        "/api/v1/orgs",
        headers=vn,
        json={"type": "department", "code": "VN01-WH", "name": "仓库", "parent_id": str(seeded.vn)},
    )
    assert dept.status_code == 201, dept.text
    u = await cp.post(
        "/api/v1/users",
        headers=vn,
        json={
            "display_name": "仓管员",
            "home_org_id": dept.json()["id"],
            "login_name": "vn_wh",
            "password": PASSWORD,
            "login_enabled": True,
        },
    )
    assert u.status_code == 201 and u.json()["totp_provisioning_uri"].startswith("otpauth://")
    assert u.json()["effective_clearance"] == 2
    b = await cp.post(
        f"/api/v1/consumers/{cid}/bindings", headers=vn, json={"external_subject": "wh-01", "user_id": u.json()["id"]}
    )
    assert b.status_code == 201, b.text

    # 新用户可以用 TOTP 登录
    secret = pyotp.parse_uri(u.json()["totp_provisioning_uri"]).secret
    r = await cp.post(
        "/api/v1/auth/login", json={"login_name": "vn_wh", "password": PASSWORD, "totp": pyotp.TOTP(secret).now()}
    )
    assert r.status_code == 200

    # 断言调试器：验签通过并给出 Principal；不消耗 jti，同一断言可以反复调试
    now = int(dt.datetime.now(dt.UTC).timestamp())
    a = jwt.encode(
        {
            "iss": "vn-mes-agent",
            "sub": "wh-01",
            "aud": S.mcp_resource,
            "iat": now,
            "exp": now + 60,
            "jti": secrets.token_hex(6),
        },
        key.private,
        algorithm="ES256",
        headers={"kid": key.kid},
    )
    for _ in range(2):
        d = (await cp.post("/api/v1/assertion-debug", headers=vn, json={"consumer_id": cid, "assertion": a})).json()
        assert d["signature_valid"] and d["principal"]["user_id"] == u.json()["id"]
        assert d["principal"]["clearance"] == 2
    bad = (
        await cp.post("/api/v1/assertion-debug", headers=vn, json={"consumer_id": cid, "assertion": a[:-4] + "AAAA"})
    ).json()
    assert not bad["signature_valid"] and bad["errors"]


async def test_http_scope_and_error_contract(cp: httpx.AsyncClient, seeded: Seed) -> None:
    kh = await login(cp, seeded, "kh_admin")
    r = await cp.post(
        "/api/v1/orgs",
        headers=kh,
        json={"type": "department", "code": "VN01-X", "name": "x", "parent_id": str(seeded.vn)},
    )
    assert r.status_code == 404 and r.headers["content-type"].startswith("application/problem+json")
    listed = (await cp.get("/api/v1/orgs", headers=kh)).json()["items"]
    assert {o["company_id"] for o in listed} == {str(seeded.kh)}
    assert (await cp.get("/api/v1/me")).json()["code"] == "UNAUTHENTICATED"
    bad = await cp.post("/api/v1/orgs", headers=kh, json={"type": "planet"})
    assert bad.status_code == 400 and bad.json()["code"] == "INVALID_ARGUMENT" and bad.json()["errors"]


async def test_clearance_flow_over_http(cp: httpx.AsyncClient, seeded: Seed) -> None:
    vn = await login(cp, seeded, "vn_admin")
    req = await cp.post(
        "/api/v1/clearance-requests",
        headers=vn,
        json={
            "user_id": str(seeded.users["vn_worker"]),
            "requested_level": 3,
            "reason": "质量追溯",
            "expires_at": (dt.datetime.now(dt.UTC) + dt.timedelta(days=7)).isoformat(),
        },
    )
    assert req.status_code == 201
    rid = req.json()["id"]
    denied = await cp.post(f"/api/v1/clearance-requests/{rid}/decide", headers=vn, json={"decision": "approve"})
    assert denied.status_code == 403
    sec = await login(cp, seeded, "security")
    ok = await cp.post(f"/api/v1/clearance-requests/{rid}/decide", headers=sec, json={"decision": "approve"})
    assert ok.json()["status"] == "approved"
    assert (await cp.get(f"/api/v1/users/{seeded.users['vn_worker']}", headers=vn)).json()["effective_clearance"] == 3


async def test_snapshot_endpoint_requires_service_token_and_feeds_data_plane(
    cp: httpx.AsyncClient, seeded: Seed
) -> None:
    assert (await cp.get("/api/v1/snapshots/iam")).status_code == 401
    admin = await login(cp, seeded, "admin")
    assert (await cp.get("/api/v1/snapshots/iam", headers=admin)).status_code == 401
    r = await cp.get("/api/v1/snapshots/iam", headers={"Authorization": f"Bearer {S.internal_service_token}"})
    assert r.status_code == 200
    from aip.libs.tokens.keys import platform_key

    dp = create_dp(key=platform_key(), replay=MemoryReplayStore())
    dp.state.iam.load(r.json())
    assert dp.state.iam.seq == r.json()["seq"]
    again = await cp.get(
        "/api/v1/snapshots/iam",
        params={"after_seq": r.json()["seq"]},
        headers={"Authorization": f"Bearer {S.internal_service_token}"},
    )
    assert again.status_code == 204
