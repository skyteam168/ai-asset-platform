"""M4 端到端验收：控制面与数据面以真实 HTTP 运行（uvicorn），数据面长轮询控制面。

1. 读类 Tool 经 HTTP 从 Draft 走到 Active（开发者 → 业务 Owner 验收 → 数据 Owner 审核 → 平台管理员灰度与全量）；
2. 越南分公司消费方以员工身份经 MCP（官方 SDK 客户端）查询本厂工单成功，查询他厂被拒；
3. 停用员工后数据面在数秒内拒绝（目标 ≤ 30 秒）；
4. 事件回传：by_employee_no 匹配回写绑定、insecure 连接指纹回写控制面。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import pathlib
import secrets
import socket
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
import pyotp
import pytest
import uvicorn
from mcp.client.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from sqlalchemy import select

from aip.connectors.credentials import MemorySecretStore
from aip.control_plane.app import create_app as create_cp
from aip.control_plane.conn import service as conn_service
from aip.control_plane.db import Database, DbScope
from aip.control_plane.iam import tables as iam_t
from aip.control_plane.iam import users
from aip.control_plane.iam.authz import Actor
from aip.data_plane.app import create_app as create_dp
from aip.libs.common.config import get_settings
from aip.libs.tokens.keys import SigningKey, platform_key
from aip.libs.tokens.replay import MemoryReplayStore
from iam_seed import PASSWORD, Seed, app_url
from tls_fixtures import Server, make_cert

pytestmark = pytest.mark.infra
S = get_settings()
SYS = Actor.system_actor()
CAP = "production.mes.query_work_orders"
ROWS = [{"wo_no": "WO-20261019-001", "qty": 1200, "status": "open"}]
VN_KEY = SigningKey.generate()
KH_KEY = SigningKey.generate()


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def _serve(app: Any, port: int) -> tuple[uvicorn.Server, asyncio.Task[None]]:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    return server, task


@dataclass
class Stack:
    cp: str
    dp: str
    data_plane: Any


@pytest.fixture
async def stack(seeded: Seed, tmp_path: pathlib.Path) -> AsyncIterator[Stack]:
    cp_port, dp_port = _port(), _port()
    cp_app = create_cp(db=Database(app_url()))
    cp_app.state.secrets = MemorySecretStore()
    dp_app = create_dp(
        key=platform_key(),
        replay=MemoryReplayStore(),
        data_dir=tmp_path,
        control_plane_url=f"http://127.0.0.1:{cp_port}",
        secrets=MemorySecretStore(),
    )
    servers = [await _serve(cp_app, cp_port), await _serve(dp_app, dp_port)]
    yield Stack(f"http://127.0.0.1:{cp_port}", f"http://127.0.0.1:{dp_port}", dp_app)
    for server, task in reversed(servers):
        server.should_exit = True
        await task


async def login(c: httpx.AsyncClient, name: str, secret: str) -> dict[str, str]:
    r = await c.post(
        "/api/v1/auth/login", json={"login_name": name, "password": PASSWORD, "totp": pyotp.TOTP(secret).now()}
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['session_token']}"}


async def make_staff(db: Database, s: Seed) -> dict[str, str]:
    """补充开发者、业务 Owner；返回各人的 TOTP 种子。"""
    from aip.libs.tokens.secrets_util import unseal

    totp = dict(s.totp)
    async with db.tx(DbScope.system()) as conn:
        for name, role, scope in (("dev", "asset_developer", s.group), ("bo", "business_owner", s.hq)):
            c = await users.create_user(
                conn, SYS, display_name=name, home_org_id=s.hq, login_name=name, password=PASSWORD, login_enabled=True
            )
            await users.assign_role(conn, SYS, c.id, role, scope)
            totp[name] = unseal((await users.user_row(conn, c.id))["totp_secret_enc"])
        for code, org in (("F001", s.vn), ("F900", s.kh)):
            await conn.execute(
                iam_t.org_external_code.insert().values(
                    org_id=org, system_key="mes", external_code=code, company_id=org
                )
            )
    return totp


def assertion(key: SigningKey, client_id: str, sub: str) -> str:
    now = int(dt.datetime.now(dt.UTC).timestamp())
    return jwt.encode(
        {
            "iss": client_id,
            "sub": sub,
            "aud": S.token_endpoint_aud,
            "iat": now,
            "exp": now + 120,
            "jti": secrets.token_hex(8),
        },
        key.private,
        algorithm="ES256",
        headers={"kid": key.kid},
    )


async def exchange(
    dp: httpx.AsyncClient, key: SigningKey, client_id: str, sub: str, timeout_s: float = 15
) -> httpx.Response:
    """令牌交换；数据面刚启动或快照尚未同步时短暂重试。"""
    deadline = time.monotonic() + timeout_s
    while True:
        r = await dp.post(
            "/oauth/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion(key, client_id, sub),
                "client_id": client_id,
                "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                "client_assertion": assertion(key, client_id, client_id),
            },
        )
        if r.status_code == 200 or time.monotonic() > deadline:
            return r
        await asyncio.sleep(0.3)


async def test_end_to_end_publish_and_call_over_mcp(stack: Stack, seeded: Seed, db: Database) -> None:
    totp = await make_staff(db, seeded)
    async with httpx.AsyncClient(base_url=stack.cp, timeout=30) as cp:
        admin, dev = await login(cp, "admin", totp["admin"]), await login(cp, "dev", totp["dev"])
        bo, sec = await login(cp, "bo", totp["bo"]), await login(cp, "security", totp["security"])

        # ---- 1. 发布：Draft → Active（全部经 HTTP） ----
        r = await cp.post(
            "/api/v1/connector-instances",
            headers=admin,
            json={"name": "mes_vn01", "kind": "mock", "config": {"operations": {"q": {"response": ROWS}}}},
        )
        assert r.status_code == 201, r.text
        asset = (
            await cp.post(
                "/api/v1/assets",
                headers=dev,
                json={"type": "tool", "name": "mes.work_orders", "owner_org_id": str(seeded.hq), "classification": 2},
            )
        ).json()
        spec = {
            "capability_id": CAP,
            "domain": "production",
            "name": "query_work_orders",
            "description": "查询指定工厂的生产工单，按状态过滤，返回工单号、计划数量和状态。",
            "input_schema": {
                "type": "object",
                "properties": {"factory_id": {"type": "string"}, "status": {"type": "string"}},
                "required": ["factory_id"],
            },
            "output_schema": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "wo_no": {"type": "string"},
                        "qty": {"type": "integer"},
                        "status": {"type": "string"},
                    },
                },
            },
            "connector_ref": {"connector": "mes_vn01", "operation": "q"},
            "risk_level": "read",
            "timeout": {"connect_ms": 2000, "read_ms": 5000, "deadline_ms": 8000},
            "data_classification": 2,
            "identity_mode": "service_account",
            "confirm_policy": "auto",
            "scope_binding": {"factory_id": {"dimension": "company", "map": "by_code", "system_key": "mes"}},
        }
        version = (
            await cp.post(f"/api/v1/assets/{asset['id']}/versions", headers=dev, json={"semver": "1.0.0", "spec": spec})
        ).json()
        vid = version["id"]
        for case in (
            {
                "name": "本厂查询",
                "kind": "functional",
                "args": {"factory_id": "F001"},
                "principal": {"company": "VN01"},
                "expect": {"status": "succeeded"},
            },
            {
                "name": "他厂被拒",
                "kind": "permission",
                "args": {"factory_id": "F900"},
                "principal": {"company": "VN01"},
                "expect": {"status": "failed", "error_code": "OUT_OF_SCOPE"},
            },
        ):
            assert (await cp.post(f"/api/v1/versions/{vid}/test-cases", headers=dev, json=case)).status_code == 201
        await cp.put(f"/api/v1/versions/{vid}/acceptance", headers=dev, json={"scenarios": ["车间主管查询本厂工单"]})
        assert (await cp.post(f"/api/v1/versions/{vid}/submit", headers=dev)).json()["passed"]
        lab = (await cp.post(f"/api/v1/versions/{vid}/run-tests", headers=dev)).json()
        assert lab["passed"], lab
        pending_bo = (await cp.get("/api/v1/approvals", headers=bo)).json()["items"]
        pending_sec = (await cp.get("/api/v1/approvals", headers=sec)).json()["items"]
        assert [a["kind"] for a in pending_bo] == ["G5"] and [a["kind"] for a in pending_sec] == ["G6"]
        assert (await cp.get("/api/v1/approvals", headers=dev)).json()["items"] == [], "提交人看不到自己的审批单"
        r = await cp.post(
            f"/api/v1/approvals/{pending_bo[0]['id']}/decide",
            headers=bo,
            json={"decision": "approve", "results": {"accuracy": 1.0}},
        )
        assert r.json()["status"] == "approved"
        r = await cp.post(f"/api/v1/approvals/{pending_sec[0]['id']}/decide", headers=sec, json={"decision": "approve"})
        assert r.json()["status"] == "approved"
        rollout = {"env": "test", "scope": {"company_ids": [str(seeded.vn)]}, "exposure": ["rest", "mcp"]}
        assert (await cp.post(f"/api/v1/versions/{vid}/canary", headers=admin, json=rollout)).status_code == 201
        done = (
            await cp.post(
                f"/api/v1/versions/{vid}/promote",
                headers=admin,
                json={**rollout, "scope": {"group": True}, "min_calls": 0},
            )
        ).json()
        assert done["passed"], done
        gates = [g["gate"] for g in (await cp.get(f"/api/v1/versions/{vid}/gates", headers=dev)).json()["items"]]
        assert gates == ["G0", "G1", "G2", "G5", "G6", "G7", "G8"]

        # ---- 2. 授权与消费方配置 ----
        ceiling = (
            await cp.post(
                "/api/v1/grant-ceilings",
                headers=admin,
                json={"company_id": str(seeded.vn), "asset_set": {"asset_ids": [asset["id"]]}, "max_classification": 2},
            )
        ).json()
        vn_ai = str(seeded.consumers["vn-ai"])
        r = await cp.post(
            "/api/v1/grants",
            headers=admin,
            json={
                "asset_id": asset["id"],
                "grantee_type": "consumer",
                "grantee_id": vn_ai,
                "env": "test",
                "expires_at": (dt.datetime.now(dt.UTC) + dt.timedelta(days=30)).isoformat(),
                "ceiling_id": ceiling["id"],
            },
        )
        assert r.status_code == 201, r.text
        await cp.post(f"/api/v1/consumers/{vn_ai}/jwks", headers=admin, json=VN_KEY.public_jwk())
        vn_admin = await login(cp, "vn_admin", totp["vn_admin"])
        r = await cp.post(
            f"/api/v1/consumers/{vn_ai}/bindings",
            headers=vn_admin,
            json={"external_subject": "vn.emp.001", "user_id": str(seeded.users["vn_worker"])},
        )
        assert r.status_code == 201

    # ---- 3. 员工经 MCP 调用：本厂成功、他厂被拒 ----
    async with httpx.AsyncClient(base_url=stack.dp, timeout=30) as dp:
        tok = await exchange(dp, VN_KEY, "vn-ai", "vn.emp.001")
        assert tok.status_code == 200, tok.text
        bearer = {"Authorization": f"Bearer {tok.json()['access_token']}"}
        mcp = Client(
            streamable_http_client(f"{stack.dp}/mcp", http_client=create_mcp_http_client(headers=bearer)), mode="auto"
        )
        deadline = time.monotonic() + 15
        async with mcp as client:
            names: list[str] = []
            while CAP not in names and time.monotonic() < deadline:  # 等发布快照到达
                names = [t.name for t in (await client.list_tools()).tools]
                await asyncio.sleep(0.3)
            assert names == [CAP]
            own = await client.call_tool(CAP, {"factory_id": "F001"})
            other = await client.call_tool(CAP, {"factory_id": "F900"})
        assert own.is_error is False and own.structured_content == {"items": ROWS}
        assert other.is_error is True and other.meta["aip.internal/error"]["code"] == "OUT_OF_SCOPE"

        # ---- 4. 停用员工：数据面多快拒绝 ----
        start = time.monotonic()
        async with db.tx(DbScope.system()) as conn:
            await users.set_status(conn, SYS, seeded.users["vn_worker"], "left")
        while (await dp.get("/api/v1/catalog", headers=bearer)).status_code == 200:
            assert time.monotonic() - start < 30, "停用 30 秒内必须生效"
            await asyncio.sleep(0.1)
        propagation = time.monotonic() - start
    print(f"\n停用传播耗时 {propagation:.2f}s")
    assert propagation < 5


async def test_event_channel_writes_back_bindings_and_fingerprints(stack: Stack, seeded: Seed, db: Database) -> None:
    # by_employee_no：KH 消费方按工号匹配员工，回写绑定
    async with db.tx(DbScope.system()) as conn:
        from aip.control_plane.iam import consumers

        await consumers.add_jwk(conn, SYS, seeded.consumers["kh-ai"], KH_KEY.public_jwk())
    async with httpx.AsyncClient(base_url=stack.dp, timeout=30) as dp:
        r = await exchange(dp, KH_KEY, "kh-ai", "E-KH-001")
        assert r.status_code == 200, r.text
    deadline = time.monotonic() + 10
    row = None
    while row is None and time.monotonic() < deadline:
        async with db.tx(DbScope.system()) as conn:
            row = (
                (
                    await conn.execute(
                        select(iam_t.user_binding).where(iam_t.user_binding.c.external_subject == "E-KH-001")
                    )
                )
                .mappings()
                .first()
            )
        await asyncio.sleep(0.2)
    assert row is not None and row["user_id"] == seeded.users["kh_worker"] and row["created_by"] is None

    # insecure 连接首次握手的指纹回写到控制面
    cert = make_cert()
    srv = Server(cert)
    try:
        async with db.tx(DbScope.system()) as conn:
            admin = await users.load_actor(conn, seeded.users["admin"])
            sec = await users.load_actor(conn, seeded.users["security"])
            cid = await conn_service.create_connection(
                conn,
                admin,
                company_id=seeded.vn,
                data={
                    "system_key": "qms",
                    "env": "test",
                    "base_url": srv.url(),
                    "scheme": "https",
                    "tls_mode": "insecure",
                    "tls_check_hostname": False,
                    "transport_exception_reason": "旧 QMS 自签证书",
                    "transport_exception_expires_at": (dt.datetime.now(dt.UTC) + dt.timedelta(days=30)).isoformat(),
                },
            )
            await conn_service.approve_exception(conn, sec, cid)
            row_ = await conn_service.row(conn, cid)
        cfg = conn_service.to_config(row_)
        stack.data_plane.state.runtime.registry.sink(
            "tls.fingerprint_seen", {"connection_id": str(cid), "fingerprint": "ab" * 32}
        )
        from aip.libs.httpclient import build_client

        async with build_client(cfg, on_event=stack.data_plane.state.events) as c:
            await c.get("/ping")
        deadline = time.monotonic() + 10
        seen = None
        while not seen and time.monotonic() < deadline:
            async with db.tx(DbScope.system()) as conn:
                seen = (await conn_service.row(conn, cid))["tls_last_seen_fingerprint"]
            await asyncio.sleep(0.2)
        assert seen == "ab" * 32  # 首个回传的指纹被记录；之后的不覆盖
    finally:
        srv.stop()
