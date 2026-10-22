"""将 Nike Postman 导入的只读 Tool 草稿走完门禁 → 授权 → REST/MCP 调用（dev 演示）。

依赖：控制面 :18081、数据面 :18082 已 serve；已 import Nike Collection；建议已 seed-demo（demo-agent）。
用法：python tools/activate_nike_path.py
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

import pyotp

ROOT = pathlib.Path(__file__).resolve().parents[1]
CP = "http://127.0.0.1:18081/api/v1"
DP = "http://127.0.0.1:18082"
ADMIN_FILE = ROOT / ".aip-data" / "dev-admin.txt"
DEMO_FILE = ROOT / ".aip-data" / "demo-m5.json"
STAFF_FILE = ROOT / ".aip-data" / "nike-path-staff.json"
OUT_FILE = ROOT / ".aip-data" / "nike-path.json"
CONNECTOR = "nike_demo"
PASSWORD = "y_pWRBigBegRDadKavdb0LPI"

# 导入资产名 → (capability 保持不变, mock 操作名, 说明, 样例响应)
TARGETS: list[tuple[str, str, str, dict[str, Any]]] = [
    (
        "nike.sample.app_13",
        "get_home",
        "查询 Nike 打样 App 首页统计数据，返回订单与机台汇总指标。",
        {"orders_today": 12, "machines_online": 8, "source": "mock"},
    ),
    (
        "nike.sample.pc_12",
        "get_products",
        "查询 Nike 打样产品列表，返回产品编号与开发状态。",
        {"items": [{"sku": "NK-DEMO-01", "status": "dev"}, {"sku": "NK-DEMO-02", "status": "sample"}]},
    ),
    (
        "nike.sample.app_8",
        "get_machines",
        "查询 Nike 打样机台可视化数据，返回机台编号与运行状态。",
        {"machines": [{"id": "M01", "state": "running"}, {"id": "M02", "state": "idle"}]},
    ),
]


def _admin_secret() -> str:
    text = ADMIN_FILE.read_text(encoding="utf-8")
    m = re.search(r"secret=([A-Z2-7]+)", text)
    if not m:
        raise SystemExit(f"无法从 {ADMIN_FILE} 解析 TOTP secret")
    return m.group(1)


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
    """返回 login_name → totp_secret。"""
    if STAFF_FILE.exists():
        saved = json.loads(STAFF_FILE.read_text(encoding="utf-8"))
        # 校验仍能登录
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


def find_asset(admin_token: str, name: str) -> dict[str, Any]:
    _st, page = req("GET", f"{CP}/assets?limit=200", token=admin_token)
    for a in page.get("items") or []:
        if a.get("name") == name:
            return a
    raise SystemExit(f"未找到导入资产 {name}，请先导入 Nike Collection")


def ensure_mock_connector(admin_token: str) -> None:
    st, page = req("GET", f"{CP}/connector-instances?limit=100", token=admin_token)
    if any(c.get("name") == CONNECTOR for c in (page.get("items") or [])):
        print(f"  reuse connector {CONNECTOR}")
        return
    ops = {op: {"response": resp} for _, op, _, resp in TARGETS}
    st, body = req(
        "POST",
        f"{CP}/connector-instances",
        {"name": CONNECTOR, "kind": "mock", "config": {"operations": ops}},
        admin_token,
    )
    if st not in (200, 201):
        raise SystemExit(f"create connector: {st} {body}")
    print(f"  created connector {CONNECTOR}")


def publish_one(
    *,
    asset: dict[str, Any],
    op: str,
    description: str,
    response: dict[str, Any],
    tokens: dict[str, str],
    company_id: str,
) -> dict[str, Any]:
    cap = asset["name"]
    # 若已有 active 版本则跳过门禁
    st, vers = req("GET", f"{CP}/assets/{asset['id']}/versions", token=tokens["admin"])
    for v in vers.get("items") or []:
        st, detail = req("GET", f"{CP}/versions/{v['id']}", token=tokens["admin"])
        if detail.get("state") == "active" and (detail.get("spec") or {}).get("connector_ref", {}).get(
            "connector"
        ) == CONNECTOR:
            print(f"  skip (already active) {cap} {detail.get('semver')}")
            return {"asset_id": asset["id"], "version_id": detail["id"], "capability_id": cap, "skipped": True}

    spec = {
        "capability_id": cap,
        "domain": "nike",
        "name": op,
        "description": description,
        "input_schema": {"type": "object", "properties": {}},
        "output_schema": {"type": "object"},
        "connector_ref": {"connector": CONNECTOR, "operation": op},
        "risk_level": "read",
        "timeout": {"connect_ms": 2000, "read_ms": 5000, "deadline_ms": 8000},
        "data_classification": 2,
        "identity_mode": "service_account",
        "confirm_policy": "auto",
        "app_identity_allowed": True,
        "scope_binding": {},
    }
    st, version = req(
        "POST",
        f"{CP}/assets/{asset['id']}/versions",
        {"semver": "1.0.0", "spec": spec},
        tokens["dev"],
    )
    if st not in (200, 201):
        # 可能已存在 1.0.0：换 1.0.1
        st, version = req(
            "POST",
            f"{CP}/assets/{asset['id']}/versions",
            {"semver": "1.0.1", "spec": spec},
            tokens["dev"],
        )
    if st not in (200, 201):
        raise SystemExit(f"create version {cap}: {st} {version}")
    vid = version["id"]
    print(f"  version {cap} {version.get('semver')} → {vid}")

    for case in (
        {
            "name": "正常查询",
            "kind": "functional",
            "args": {},
            "principal": {"company": "HQ", "clearance": 2},
            "expect": {"status": "succeeded"},
        },
        {
            "name": "无授权被拒",
            "kind": "permission",
            "args": {},
            "principal": {"company": "HQ", "granted": False},
            "expect": {"status": "failed", "error_code": "PERMISSION_DENIED"},
        },
    ):
        st, body = req("POST", f"{CP}/versions/{vid}/test-cases", case, tokens["dev"])
        if st not in (200, 201):
            raise SystemExit(f"test-case {case['name']}: {st} {body}")

    st, body = req(
        "PUT",
        f"{CP}/versions/{vid}/acceptance",
        {"scenarios": [f"Agent 调用 {cap} 获取打样只读数据"]},
        tokens["dev"],
    )
    if st not in (200, 204) and body not in ({}, None):
        # 部分实现返回 200+body
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
    g5 = next((a for a in (approvals_bo.get("items") or []) if a.get("kind") == "G5"), None)
    g6 = next((a for a in (approvals_sec.get("items") or []) if a.get("kind") == "G6"), None)
    if not g5 or not g6:
        raise SystemExit(f"missing approvals G5={g5} G6={g6} bo={approvals_bo} sec={approvals_sec}")

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
        "mock_response": response,
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
            # 可能已授权
            if st == 409 or (isinstance(g, dict) and g.get("code") in ("CONFLICT", "ALREADY_EXISTS")):
                print(f"  grant exists for {aid}")
                continue
            raise SystemExit(f"grant {aid}: {st} {g}")
    return str(ceiling["id"])


def invoke_rest(token: str, cap: str) -> dict[str, Any]:
    end = time.monotonic() + 90
    while True:
        st, body = req(
            "POST",
            f"{DP}/api/v1/invoke/{cap}",
            {"args": {}},
            token,
        )
        # 包装：有的返回 {status,data,error}
        code = (body.get("error") or {}).get("code") or body.get("code")
        if st == 200 and body.get("status") == "succeeded":
            return body
        if code not in ("NOT_FOUND", "PERMISSION_DENIED", "UNAUTHENTICATED") or time.monotonic() > end:
            return {"http_status": st, **(body if isinstance(body, dict) else {"body": body})}
        time.sleep(1)


def invoke_mcp(token: str, cap: str) -> dict[str, Any]:
    """JSON-RPC tools/call（与 test_m4_mcp 一致带 Mcp-Method）。"""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": cap, "arguments": {}},
    }
    end = time.monotonic() + 90
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
    # security 用户若 bootstrap 已有同名，上面会失败；兼容：用新建的 data_owner
    tokens = {
        "admin": admin_token,
        "dev": login("dev", staff["dev"]),
        "bo": login("bo", staff["bo"]),
        "security": login("security", staff["security"]),
    }

    print("ensure mock connector…")
    ensure_mock_connector(admin_token)

    published: list[dict[str, Any]] = []
    for asset_name, op, desc, resp in TARGETS:
        print(f"publish {asset_name}…")
        asset = find_asset(admin_token, asset_name)
        published.append(
            publish_one(
                asset=asset,
                op=op,
                description=desc,
                response=resp,
                tokens=tokens,
                company_id=demo_co["id"],
            )
        )

    print("grant to demo-agent…")
    ceiling_id = grant_all(
        admin_token,
        [p["asset_id"] for p in published],
        demo["company_id"],
        demo["consumer_id"],
    )

    agent_token = demo["token"]
    calls: dict[str, Any] = {}
    for p in published:
        cap = p["capability_id"]
        print(f"invoke REST {cap}…")
        calls[cap] = {"rest": invoke_rest(agent_token, cap)}
        print(f"  rest status={calls[cap]['rest'].get('status')} data={calls[cap]['rest'].get('data')}")
        print(f"invoke MCP {cap}…")
        calls[cap]["mcp"] = invoke_mcp(agent_token, cap)
        mcp_ok = calls[cap]["mcp"].get("http_status") == 200
        print(f"  mcp http={calls[cap]['mcp'].get('http_status')} ok={mcp_ok}")

    out = {
        "connector": CONNECTOR,
        "ceiling_id": ceiling_id,
        "consumer_id": demo["consumer_id"],
        "published": published,
        "calls": calls,
        "finished_at": datetime.now(UTC).isoformat(),
    }
    OUT_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {OUT_FILE.relative_to(ROOT)}")

    failed = [
        c
        for c, r in calls.items()
        if r["rest"].get("status") != "succeeded"
    ]
    if failed:
        print("FAILED capabilities:", failed, file=sys.stderr)
        raise SystemExit(1)
    print("OK：3 个 Nike 只读 Tool 已 Active，demo-agent 可经 REST 调用")


if __name__ == "__main__":
    main()
