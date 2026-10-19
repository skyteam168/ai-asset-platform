"""对本地运行中的系统做端到端冒烟（先 python tools/dev.py up、bootstrap、serve）。

python tools/smoke.py
"""

from __future__ import annotations

import datetime as dt
import pathlib
import secrets
import sys
import time

import httpx
import jwt
import pyotp

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from aip.libs.tokens.keys import SigningKey

ROOT = pathlib.Path(__file__).resolve().parents[1]
CP, DP = "http://127.0.0.1:18081", "http://127.0.0.1:18082"
TOKEN_AUD = "https://api.aip.internal/oauth/token"


def step(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{('  ' + detail) if detail else ''}")
    if not ok:
        sys.exit(1)


def creds() -> tuple[str, str]:
    text = (ROOT / ".aip-data" / "dev-admin.txt").read_text(encoding="utf-8")
    password = next(line.split("=", 1)[1] for line in text.splitlines() if line.startswith("password="))
    uri = next(line[line.index("otpauth://") :] for line in text.splitlines() if "otpauth://" in line)
    return password, pyotp.parse_uri(uri).secret


def wait_ready(c: httpx.Client, timeout: float = 30) -> httpx.Response:
    deadline = time.time() + timeout
    r = c.get(f"{DP}/readyz")
    while r.status_code != 200 and time.time() < deadline:
        time.sleep(1)
        r = c.get(f"{DP}/readyz")
    return r


def main() -> None:
    run = secrets.token_hex(3).upper()
    c = httpx.Client(timeout=10)
    step("控制面 /healthz", c.get(f"{CP}/healthz").status_code == 200)
    step("数据面 /healthz", c.get(f"{DP}/healthz").status_code == 200)
    ready = wait_ready(c)
    step("数据面 /readyz（IAM 快照、发布快照、策略已加载）", ready.status_code == 200, str(ready.json()["checks"]))

    password, secret = creds()
    r = c.post(
        f"{CP}/api/v1/auth/login", json={"login_name": "admin", "password": password, "totp": pyotp.TOTP(secret).now()}
    )
    step("管理员登录（口令 + TOTP）", r.status_code == 200, r.text[:120] if r.status_code != 200 else "")
    h = {"Authorization": f"Bearer {r.json()['session_token']}"}
    orgs = c.get(f"{CP}/api/v1/orgs", headers=h).json()["items"]
    group = next(o for o in orgs if o["type"] == "group")

    vn = c.post(
        f"{CP}/api/v1/orgs",
        headers=h,
        json={
            "type": "company",
            "code": f"VN-{run}",
            "name": "越南工厂",
            "parent_id": group["id"],
            "default_clearance": 2,
        },
    ).json()
    dept = c.post(
        f"{CP}/api/v1/orgs",
        headers=h,
        json={"type": "department", "code": f"VN-{run}-MFG", "name": "制造部", "parent_id": vn["id"]},
    ).json()
    step("建公司与部门", "id" in vn and "id" in dept, f"{vn.get('code')} / {dept.get('code')}")
    emp = c.post(
        f"{CP}/api/v1/users",
        headers=h,
        json={"display_name": "阮文安", "home_org_id": dept["id"], "employee_no": f"E-{run}"},
    ).json()
    step("建员工（终端用户）", emp.get("effective_clearance") == 2, f"有效密级 L{emp.get('effective_clearance')}")

    key = SigningKey.generate()
    consumer = c.post(
        f"{CP}/api/v1/consumers",
        headers=h,
        json={"client_id": f"vn-ai-{run.lower()}", "company_id": vn["id"], "clearance_cap": 3},
    ).json()
    jwk = c.post(f"{CP}/api/v1/consumers/{consumer['id']}/jwks", headers=h, json=key.public_jwk())
    bind = c.post(
        f"{CP}/api/v1/consumers/{consumer['id']}/bindings",
        headers=h,
        json={"external_subject": "emp-001", "user_id": emp["id"]},
    )
    step("注册消费方、登记公钥、绑定员工", jwk.status_code == 201 and bind.status_code == 201, consumer["client_id"])

    def signed(sub: str) -> str:
        now = int(dt.datetime.now(dt.UTC).timestamp())
        return jwt.encode(
            {
                "iss": consumer["client_id"],
                "sub": sub,
                "aud": TOKEN_AUD,
                "iat": now,
                "exp": now + 120,
                "jti": secrets.token_hex(8),
            },
            key.private,
            algorithm="ES256",
            headers={"kid": key.kid},
        )

    def exchange(sub: str) -> httpx.Response:
        return c.post(
            f"{DP}/oauth/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": signed(sub),
                "client_id": consumer["client_id"],
                "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                "client_assertion": signed(consumer["client_id"]),
            },
        )

    t0 = time.time()
    tok = exchange("emp-001")
    while tok.status_code != 200 and time.time() - t0 < 15:  # 等数据面拉到新快照
        time.sleep(0.5)
        tok = exchange("emp-001")
    step(
        "数据面令牌交换（RFC 7523）",
        tok.status_code == 200,
        f"快照传播约 {time.time() - t0:.1f}s" if tok.status_code == 200 else tok.text[:160],
    )
    catalog = c.get(f"{DP}/api/v1/catalog", headers={"Authorization": f"Bearer {tok.json()['access_token']}"})
    step("用访问令牌调用数据面目录接口", catalog.status_code == 200)
    unbound = exchange("emp-999")
    step("未绑定员工被拒（strict）", unbound.status_code == 403 and unbound.json()["code"] == "USER_NOT_BOUND")
    print("smoke ok")


if __name__ == "__main__":
    main()
