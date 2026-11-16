"""设备管家真连接：RSA 登录握手 → 声明式 Tool → 门禁 → Grant → REST/MCP（dev 演示）。

依赖：控制面 :18081、数据面 :18082 已 serve；OpenBao :58200；建议已 seed-demo。
凭据：环境变量 EQUIPMENT_API_USERNAME / EQUIPMENT_API_PASSWORD，
或 g:\\gitee\\Agent\\.env / .aip-data/equipment-login.env（勿提交）。

用法：python tools/activate_equipment_path.py
"""

from __future__ import annotations

import json
import pathlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pyotp

ROOT = pathlib.Path(__file__).resolve().parents[1]
CP = "http://127.0.0.1:18081/api/v1"
DP = "http://127.0.0.1:18082"
ADMIN_FILE = ROOT / ".aip-data" / "dev-admin.txt"
DEMO_FILE = ROOT / ".aip-data" / "demo-m5.json"
STAFF_FILE = ROOT / ".aip-data" / "nike-path-staff.json"
OUT_FILE = ROOT / ".aip-data" / "equipment-path.json"
CONNECTOR = "equipment_flyknit"
CRED_REF = "kv/data/equipment/flyknit"
PASSWORD = "y_pWRBigBegRDadKavdb0LPI"
OPENBAO = "http://127.0.0.1:58200"
OPENBAO_TOKEN = "aip-dev-root-token"
EQ_BASE = "http://10.10.130.170:8081"

AUTH_CFG: dict[str, Any] = {
    "mode": "login",
    "path": "/api/v1/backend_login",
    "token_path": "$.data.list.token",
    "ttl_from": "jwt_exp",
    "ttl_skew_s": 120,
    "prepare": [
        {
            "method": "GET",
            "path": "/api/v1/auth/public-key",
            "capture": {"public_key": "$.data.publicKey", "key_id": "$.data.keyId"},
        }
    ],
    "transform": [
        {
            "op": "rsa_encrypt_json",
            "public_key": "{{capture.public_key}}",
            "padding": "PKCS1v15",
            "as": "encrypted_password",
            "plaintext": {
                "password": "{{secret.password}}",
                "ts": "{{now_ms}}",
                "nonce": "{{random_hex:8}}",
            },
        }
    ],
    "body": {
        "username": "{{secret.username}}",
        "encryptedPassword": "{{vars.encrypted_password}}",
        "keyId": "{{capture.key_id}}",
        "deviceId": "{{uuid}}",
        "deviceType": "aip-connector",
    },
}

OPERATIONS: list[dict[str, Any]] = [
    {
        "connector": CONNECTOR,
        "operation": "dashboard_stats",
        "mode": "read",
        "request": {
            "method": "GET",
            "path": "/api/v1/getDeviceDashboardStats",
            "headers": {"Accept": "application/json"},
        },
        "response": {
            "success_when": "$.status == 200",
            "data": "$.data",
        },
        "errors": [{"when": "$.status == 401", "map": "DOWNSTREAM_AUTH_EXPIRED"}],
    },
    {
        "connector": CONNECTOR,
        "operation": "list_equipment",
        "mode": "read",
        "request": {
            "method": "GET",
            "path": "/api/v1/getEquipmentLists",
            "query": {
                "keyword": "{{ args.keyword | default('') }}",
                "department": "{{ args.department | default('') }}",
                "classify": "{{ args.classify | default('') }}",
                "type": "{{ args.type | default('') }}",
                "status": "{{ args.status | default('') }}",
                "page": "{{ args.page | default(1) }}",
                "pageSize": "{{ args.page_size | default(12) }}",
            },
            "headers": {"Accept": "application/json"},
        },
        "response": {
            "success_when": "$.status == 200",
            "data": "$.data.list[*]",
            "fields": {
                "device_number": "$.device_number",
                "device_name": "$.device_name",
                "device_model": "$.device_model",
                "department": "$.department",
                "device_location": "$.device_location",
                "type": "$.type",
                "status": "$.status",
            },
        },
        "errors": [{"when": "$.status == 401", "map": "DOWNSTREAM_AUTH_EXPIRED"}],
    },
]

TARGETS: list[tuple[str, str, str, dict[str, Any]]] = [
    (
        "equipment.flyknit.dashboard",
        "dashboard_stats",
        "查询 Flyknit 设备管家总体看板统计（设备总数、闲置、部门分布等）。",
        {},
    ),
    (
        "equipment.flyknit.list",
        "list_equipment",
        "按关键字/部门等条件查询 Flyknit 设备台账列表。",
        {"keyword": "拉力机", "page": 1, "page_size": 12},
    ),
]


def _admin_secret() -> str:
    text = ADMIN_FILE.read_text(encoding="utf-8")
    m = re.search(r"secret=([A-Z2-7]+)", text)
    if not m:
        raise SystemExit(f"无法从 {ADMIN_FILE} 解析 TOTP secret")
    return m.group(1)


def _load_eq_creds() -> tuple[str, str]:
    import os

    u = (os.environ.get("EQUIPMENT_API_USERNAME") or "").strip()
    p = (os.environ.get("EQUIPMENT_API_PASSWORD") or "").strip()
    if u and p:
        return u, p
    for path in (
        ROOT / ".aip-data" / "equipment-login.env",
        pathlib.Path(r"g:\gitee\Agent\.env"),
    ):
        if not path.exists():
            continue
        vals: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip().strip('"').strip("'")
        u = vals.get("EQUIPMENT_API_USERNAME", u)
        p = vals.get("EQUIPMENT_API_PASSWORD", p)
        if u and p:
            return u, p
    raise SystemExit("未找到 EQUIPMENT_API_USERNAME / EQUIPMENT_API_PASSWORD")


def req(method: str, url: str, body: dict[str, Any] | None = None, token: str | None = None) -> tuple[int, Any]:
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=180) as resp:
            raw = resp.read().decode()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"detail": raw}


def login(name: str, secret: str, password: str = PASSWORD) -> str:
    st, body = req(
        "POST",
        f"{CP}/auth/login",
        {"login_name": name, "password": password, "totp": pyotp.TOTP(secret).now()},
    )
    if st != 200 or not body.get("session_token"):
        raise SystemExit(f"login {name} failed: {st} {body}")
    return str(body["session_token"])


def totp_from_uri(uri: str) -> str:
    q = urllib.parse.parse_qs(urllib.parse.urlparse(uri).query)
    secret = (q.get("secret") or [None])[0]
    if not secret:
        raise SystemExit(f"无 TOTP secret: {uri}")
    return secret


def ensure_staff(admin_token: str, hq_id: str, group_id: str) -> dict[str, str]:
    """返回 login_name → totp_secret（与 activate_nike_path 共用 staff 文件）。"""
    if STAFF_FILE.exists():
        saved = json.loads(STAFF_FILE.read_text(encoding="utf-8"))
        try:
            for name, secret in saved.items():
                login(name, secret)
            return saved
        except SystemExit:
            pass

    st, users = req("GET", f"{CP}/users?limit=100", token=admin_token)
    by_login = {u.get("login_name"): u for u in (users.get("items") or []) if u.get("login_name")}
    secrets: dict[str, str] = {}
    plan = (
        ("dev", "asset_developer", group_id),
        ("bo", "business_owner", hq_id),
        ("security", "data_owner", group_id),
    )
    for name, role, scope in plan:
        if name in by_login:
            raise SystemExit(f"用户 {name} 已存在但本地无 TOTP，请删除后重跑或写入 {STAFF_FILE.name}")
        st, created = req(
            "POST",
            f"{CP}/users",
            {
                "display_name": name,
                "home_org_id": hq_id,
                "login_name": name,
                "password": PASSWORD,
                "login_enabled": True,
            },
            admin_token,
        )
        if st not in (200, 201):
            raise SystemExit(f"create user {name}: {st} {created}")
        uri = created.get("totp_uri") or created.get("totp_provisioning_uri")
        if not uri:
            raise SystemExit(f"create user {name} 未返回 totp_uri: {created}")
        secrets[name] = totp_from_uri(uri)
        st, assigned = req(
            "POST",
            f"{CP}/users/{created['id']}/roles",
            {"role": role, "scope_org_id": scope},
            admin_token,
        )
        if st not in (200, 201):
            raise SystemExit(f"assign {role} to {name}: {st} {assigned}")
        print(f"  staff {name} role={role}")
    STAFF_FILE.write_text(json.dumps(secrets, indent=2), encoding="utf-8")
    return secrets


def write_openbao(username: str, password: str) -> None:
    # KV v2：POST /v1/kv/data/equipment/flyknit
    path = CRED_REF.removeprefix("kv/data/")
    url = f"{OPENBAO}/v1/kv/data/{path}"
    r = httpx.post(
        url,
        headers={"X-Vault-Token": OPENBAO_TOKEN},
        json={"data": {"username": username, "password": password}},
        timeout=10,
    )
    if r.status_code not in (200, 204):
        raise SystemExit(f"OpenBao write failed: {r.status_code} {r.text[:200]}")
    print(f"OpenBao {CRED_REF} written")


def ensure_connection(admin_token: str, security_token: str, company_id: str) -> str:
    st, conns = req("GET", f"{CP}/connections", token=admin_token)
    items = conns.get("items") or []
    existing = next(
        (c for c in items if c.get("system_key") == "equipment_flyknit" and c.get("base_url") == EQ_BASE),
        None,
    )
    if existing:
        cid = existing["id"]
        if existing.get("status") == "pending_approval":
            st, body = req("POST", f"{CP}/connections/{cid}/approve-exception", {}, security_token)
            if st != 200:
                raise SystemExit(f"approve existing: {st} {body}")
            print(f"  approved connection {cid} → {body.get('status')}")
        else:
            print(f"  reuse connection {cid} status={existing.get('status')}")
        return str(cid)

    expires = (datetime.now(UTC) + timedelta(days=60)).isoformat()
    st, body = req(
        "POST",
        f"{CP}/connections",
        {
            "company_id": company_id,
            "system_key": "equipment_flyknit",
            "env": "test",
            "base_url": EQ_BASE,
            "scheme": "http",
            "credential_ref": CRED_REF,
            "transport_exception_reason": "内网设备管家 HTTP API 演示（RSA+JWT）",
            "transport_exception_expires_at": expires,
        },
        admin_token,
    )
    if st not in (200, 201):
        raise SystemExit(f"create connection: {st} {body}")
    cid = body["id"]
    print(f"  created connection {cid} status={body.get('status')}")
    if body.get("status") == "pending_approval":
        st, appr = req("POST", f"{CP}/connections/{cid}/approve-exception", {}, security_token)
        if st != 200 or appr.get("status") != "active":
            raise SystemExit(f"approve: {st} {appr}")
        print(f"  approved → active")
    return str(cid)


def _upsert_connector_config(connection_id: str, config: dict[str, Any]) -> None:
    """无更新 API 时直接写库（仅本地演示）。"""
    import asyncio

    import asyncpg

    async def _run() -> None:
        conn = await asyncpg.connect(
            host="127.0.0.1",
            port=55432,
            user="aip",
            password="aip-dev-only",
            database="aip",
        )
        try:
            import uuid as _uuid

            row = await conn.fetchrow(
                "SELECT id FROM conn.connector_instance WHERE name = $1",
                CONNECTOR,
            )
            payload = json.dumps(config)
            if row:
                await conn.execute(
                    "UPDATE conn.connector_instance SET config = $1::jsonb, "
                    "system_connection_id = $2::uuid, kind = 'declarative', status = 'active' WHERE name = $3",
                    payload,
                    connection_id,
                    CONNECTOR,
                )
            else:
                await conn.execute(
                    "INSERT INTO conn.connector_instance "
                    "(id, name, company_id, kind, system_connection_id, config, concurrency, rate_per_s, burst, status) "
                    "SELECT $1::uuid, $2, company_id, 'declarative', $3::uuid, $4::jsonb, 8, 20, 40, 'active' "
                    "FROM conn.system_connection WHERE id = $3::uuid",
                    str(_uuid.uuid4()),
                    CONNECTOR,
                    connection_id,
                    payload,
                )
        finally:
            await conn.close()

    asyncio.run(_run())


def ensure_connector(admin_token: str, connection_id: str) -> None:
    cfg = {"auth": AUTH_CFG, "operations": OPERATIONS}
    st, listed = req("GET", f"{CP}/connector-instances", token=admin_token)
    items = listed.get("items") or []
    if any(c.get("name") == CONNECTOR for c in items):
        _upsert_connector_config(connection_id, cfg)
        print(f"  updated connector {CONNECTOR} config (RSA auth)")
        return
    st, body = req(
        "POST",
        f"{CP}/connector-instances",
        {
            "name": CONNECTOR,
            "kind": "declarative",
            "system_connection_id": connection_id,
            "config": cfg,
        },
        admin_token,
    )
    if st not in (200, 201):
        # API 失败时回退直写
        _upsert_connector_config(connection_id, cfg)
        print(f"  connector via DB fallback ({st})")
        return
    print(f"  created connector {CONNECTOR}")


def try_ops(admin_token: str, connection_id: str) -> dict[str, Any]:
    """控制面真连接试调（不经门禁），验证 RSA 握手。"""
    out: dict[str, Any] = {}
    for op, params in (
        ("dashboard_stats", {}),
        ("list_equipment", {"keyword": "拉力机", "page": 1, "page_size": 5}),
    ):
        st, body = req(
            "POST",
            f"{CP}/importer/try-operation",
            {
                "connection_id": connection_id,
                "connector_config": {"auth": AUTH_CFG, "operations": OPERATIONS},
                "operation": op,
                "params": params,
            },
            admin_token,
        )
        out[op] = {"http": st, "ok": body.get("ok"), "item_count": body.get("item_count"), "sample": body.get("sample")}
        print(f"  try {op}: http={st} ok={body.get('ok')} items={body.get('item_count')}")
        if st != 200 or not body.get("ok"):
            raise SystemExit(f"try-operation {op} failed: {body}")
    return out


def ensure_asset(token: str, name: str, owner_org_id: str) -> dict[str, Any]:
    st, assets = req("GET", f"{CP}/assets?limit=200", token=token)
    for a in assets.get("items") or []:
        if a.get("name") == name or a.get("capability_id") == name:
            return a
    st, body = req(
        "POST",
        f"{CP}/assets",
        {"type": "tool", "name": name, "owner_org_id": owner_org_id, "classification": 2},
        token,
    )
    if st not in (200, 201):
        raise SystemExit(f"create asset {name}: {st} {body}")
    return body


def publish_one(
    *,
    asset: dict[str, Any],
    op: str,
    description: str,
    test_args: dict[str, Any],
    tokens: dict[str, str],
    company_id: str,
) -> dict[str, Any]:
    cap = asset.get("name") or asset.get("capability_id")
    # 已有 active 版本则跳过门禁
    st, vers = req("GET", f"{CP}/assets/{asset['id']}/versions", token=tokens["dev"])
    for v in vers.get("items") or []:
        if v.get("state") == "active":
            print(f"  {cap} already active {v.get('semver')}")
            return {
                "asset_id": asset["id"],
                "version_id": v["id"],
                "capability_id": cap,
                "operation": op,
                "skipped": True,
            }

    spec = {
        "capability_id": cap,
        "domain": "equipment",
        "name": op,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string"},
                "department": {"type": "string"},
                "classify": {"type": "string"},
                "type": {"type": "string"},
                "status": {"type": "string"},
                "page": {"type": "integer"},
                "page_size": {"type": "integer"},
            },
        },
        "output_schema": (
            {"type": "array", "items": {"type": "object"}}
            if op == "list_equipment"
            else {"type": "object"}
        ),
        "connector_ref": {"connector": CONNECTOR, "operation": op},
        "risk_level": "read",
        "timeout": {"connect_ms": 5000, "read_ms": 30000, "deadline_ms": 45000},
        "data_classification": 2,
        "identity_mode": "service_account",
        "confirm_policy": "auto",
        "app_identity_allowed": True,
        "scope_binding": {},
    }
    semver = "1.0.0"
    st, version = req(
        "POST",
        f"{CP}/assets/{asset['id']}/versions",
        {"semver": semver, "spec": spec},
        tokens["dev"],
    )
    if st not in (200, 201):
        semver = "1.0.1"
        st, version = req(
            "POST",
            f"{CP}/assets/{asset['id']}/versions",
            {"semver": semver, "spec": spec},
            tokens["dev"],
        )
    if st not in (200, 201):
        raise SystemExit(f"create version {cap}: {st} {version}")
    vid = version["id"]
    print(f"  version {cap} {semver} → {vid}")

    for case in (
        {
            "name": "正常查询",
            "kind": "functional",
            "args": test_args,
            "principal": {"company": "HQ", "clearance": 2},
            "expect": {"status": "succeeded"},
        },
        {
            "name": "无授权被拒",
            "kind": "permission",
            "args": test_args,
            "principal": {"company": "HQ", "granted": False},
            "expect": {"status": "failed", "error_code": "PERMISSION_DENIED"},
        },
    ):
        st, body = req("POST", f"{CP}/versions/{vid}/test-cases", case, tokens["dev"])
        if st not in (200, 201):
            raise SystemExit(f"test-case: {st} {body}")

    st, body = req(
        "PUT",
        f"{CP}/versions/{vid}/acceptance",
        {"scenarios": [f"Agent 调用 {cap} 查询设备管家只读数据"]},
        tokens["dev"],
    )
    if st >= 400:
        raise SystemExit(f"acceptance: {st} {body}")

    st, g0 = req("POST", f"{CP}/versions/{vid}/submit", {}, tokens["dev"])
    if st != 200 or not g0.get("passed"):
        raise SystemExit(f"G0 failed: {st} {g0}")
    print("    G0 ok")

    st, lab = req("POST", f"{CP}/versions/{vid}/run-tests", {}, tokens["dev"])
    if st != 200 or not lab.get("passed"):
        raise SystemExit(f"G1-G4 failed: {st} {lab}")
    print("    G1-G4 ok")

    st, approvals_bo = req("GET", f"{CP}/approvals", token=tokens["bo"])
    st, approvals_sec = req("GET", f"{CP}/approvals", token=tokens["security"])

    def _match(items: list[Any], kind: str) -> dict[str, Any] | None:
        pending = [a for a in items if a.get("kind") == kind and a.get("status") == "pending"]
        for a in pending:
            if a.get("subject_id") == vid or str(a.get("subject") or "").startswith(str(cap)):
                return a
        return pending[0] if len(pending) == 1 else None

    g5 = _match(approvals_bo.get("items") or [], "G5")
    g6 = _match(approvals_sec.get("items") or [], "G6")
    if not g5 or not g6:
        raise SystemExit(
            f"missing approvals for {cap} vid={vid} G5={g5} G6={g6} "
            f"bo={[a.get('subject') for a in (approvals_bo.get('items') or []) if a.get('kind')=='G5']} "
            f"sec={[a.get('subject') for a in (approvals_sec.get('items') or []) if a.get('kind')=='G6']}"
        )

    st, d5 = req(
        "POST",
        f"{CP}/approvals/{g5['id']}/decide",
        {"decision": "approve", "results": {"accuracy": 1.0}},
        tokens["bo"],
    )
    if st != 200:
        raise SystemExit(f"G5: {st} {d5}")
    st, d6 = req("POST", f"{CP}/approvals/{g6['id']}/decide", {"decision": "approve"}, tokens["security"])
    if st != 200:
        raise SystemExit(f"G6: {st} {d6}")
    print("    G5/G6 ok")

    rollout = {"env": "test", "scope": {"company_ids": [company_id]}, "exposure": ["rest", "mcp"]}
    st, canary = req("POST", f"{CP}/versions/{vid}/canary", rollout, tokens["admin"])
    if st not in (200, 201):
        raise SystemExit(f"G7 canary: {st} {canary}")
    st, promo = req(
        "POST",
        f"{CP}/versions/{vid}/promote",
        {**rollout, "scope": {"group": True}, "min_calls": 0},
        tokens["admin"],
    )
    if st != 200 or not promo.get("passed"):
        raise SystemExit(f"G8 promote: {st} {promo}")
    print("    G7/G8 active")
    return {
        "asset_id": asset["id"],
        "version_id": vid,
        "capability_id": cap,
        "operation": op,
        "skipped": False,
    }


def grant_all(admin_token: str, asset_ids: list[str], company_id: str, consumer_id: str) -> str:
    st, ceiling = req(
        "POST",
        f"{CP}/grant-ceilings",
        {"company_id": company_id, "asset_set": {"asset_ids": asset_ids}, "max_classification": 2},
        admin_token,
    )
    if st not in (200, 201):
        raise SystemExit(f"ceiling: {st} {ceiling}")
    expires = (datetime.now(UTC) + timedelta(days=90)).isoformat()
    for aid in asset_ids:
        st, g = req(
            "POST",
            f"{CP}/grants",
            {
                "asset_id": aid,
                "grantee_type": "consumer",
                "grantee_id": consumer_id,
                "env": "test",
                "expires_at": expires,
                "ceiling_id": ceiling["id"],
            },
            admin_token,
        )
        if st not in (200, 201):
            if st == 409 or (isinstance(g, dict) and g.get("code") in ("CONFLICT", "ALREADY_EXISTS")):
                print(f"  grant exists for {aid}")
                continue
            raise SystemExit(f"grant {aid}: {st} {g}")
    return str(ceiling["id"])


def invoke_rest(token: str, cap: str, args: dict[str, Any]) -> dict[str, Any]:
    end = time.monotonic() + 120
    while True:
        st, body = req("POST", f"{DP}/api/v1/invoke/{cap}", {"args": args}, token)
        code = (body.get("error") or {}).get("code") or body.get("code")
        if st == 200 and body.get("status") == "succeeded":
            return body
        if code not in ("NOT_FOUND", "PERMISSION_DENIED", "UNAUTHENTICATED") or time.monotonic() > end:
            return {"http_status": st, **(body if isinstance(body, dict) else {"body": body})}
        time.sleep(1)


def invoke_mcp(token: str, cap: str, args: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": cap, "arguments": args},
    }
    end = time.monotonic() + 120
    while True:
        data = json.dumps(payload).encode()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Mcp-Method": "tools/call",
        }
        request = urllib.request.Request(f"{DP}/mcp", data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=60) as resp:
                body = json.loads(resp.read().decode())
                st = resp.status
        except urllib.error.HTTPError as e:
            st = e.code
            raw = e.read().decode()
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                body = {"detail": raw}
        if st == 200 and isinstance(body, dict) and "error" not in body:
            return {"http_status": st, "result": body}
        msg = json.dumps(body, ensure_ascii=False)
        if ("NOT_FOUND" not in msg and "PERMISSION" not in msg) or time.monotonic() > end:
            return {"http_status": st, "body": body}
        time.sleep(1)


def main() -> None:
    if not DEMO_FILE.exists():
        raise SystemExit("缺少 .aip-data/demo-m5.json，请先：python tools/dev.py seed-demo")
    demo = json.loads(DEMO_FILE.read_text(encoding="utf-8"))
    username, password = _load_eq_creds()
    write_openbao(username, password)

    admin_secret = _admin_secret()
    admin_token = login("admin", admin_secret)
    print("login admin ok")

    _st, orgs = req("GET", f"{CP}/orgs?limit=100", token=admin_token)
    items = orgs.get("items") or []
    group = next(o for o in items if o.get("type") == "group")
    hq = next(o for o in items if o.get("code") == "HQ")
    demo_co = next(o for o in items if o.get("code") == "DEMO")

    print("ensure staff…")
    staff = ensure_staff(admin_token, hq["id"], group["id"])
    # DEMO 公司资产的 G5 需要 business_owner 落在 DEMO 范围
    st, users = req("GET", f"{CP}/users?limit=100", token=admin_token)
    bo_user = next((u for u in (users.get("items") or []) if u.get("login_name") == "bo"), None)
    if bo_user:
        req(
            "POST",
            f"{CP}/users/{bo_user['id']}/roles",
            {"role": "business_owner", "scope_org_id": demo_co["id"]},
            admin_token,
        )
    tokens = {
        "admin": admin_token,
        "dev": login("dev", staff["dev"]),
        "bo": login("bo", staff["bo"]),
        "security": login("security", staff["security"]),
    }

    print("ensure connection…")
    cid = ensure_connection(admin_token, tokens["security"], demo_co["id"])

    print("ensure connector…")
    ensure_connector(admin_token, cid)

    print("try-operation (RSA handshake)…")
    try_results = try_ops(admin_token, cid)

    published: list[dict[str, Any]] = []
    for asset_name, op, desc, args in TARGETS:
        print(f"publish {asset_name}…")
        asset = ensure_asset(tokens["dev"], asset_name, demo_co["id"])
        published.append(
            publish_one(
                asset=asset,
                op=op,
                description=desc,
                test_args=args,
                tokens=tokens,
                company_id=demo_co["id"],
            )
        )

    print("grant to demo-agent…")
    consumer_id = demo.get("consumer_id") or demo.get("consumer", {}).get("id")
    if not consumer_id:
        raise SystemExit("demo-m5.json 无 consumer_id")
    ceiling_id = grant_all(
        admin_token,
        [p["asset_id"] for p in published],
        demo_co["id"],
        str(consumer_id),
    )

    agent_token = demo.get("token") or demo.get("static_token")
    if not agent_token:
        raise SystemExit("demo-m5.json 无 token")

    print("invoke REST / MCP…")
    invokes: dict[str, Any] = {}
    for p, (_n, _op, _d, args) in zip(published, TARGETS, strict=True):
        cap = p["capability_id"]
        rest = invoke_rest(agent_token, cap, args)
        mcp = invoke_mcp(agent_token, cap, args)
        invokes[cap] = {
            "rest_status": rest.get("status") or rest.get("http_status"),
            "mcp_http": mcp.get("http_status"),
        }
        print(f"  {cap}: rest={invokes[cap]['rest_status']} mcp={invokes[cap]['mcp_http']}")

    out = {
        "connection_id": cid,
        "connector": CONNECTOR,
        "credential_ref": CRED_REF,
        "try_operation": {
            k: {"ok": v.get("ok"), "item_count": v.get("item_count")} for k, v in try_results.items()
        },
        "published": published,
        "ceiling_id": ceiling_id,
        "invokes": invokes,
        "mcp_url": f"{DP}/mcp",
        "note": "控制台可用「创建 Tool 向导」对同一连接做真连接试；MCP 用 demo-agent Bearer",
    }
    OUT_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {OUT_FILE}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
