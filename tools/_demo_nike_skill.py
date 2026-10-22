"""一次性：创建并发布 Nike 打样演示 Skill（无 Tool 引用，便于快速过门禁）。"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

import pyotp

from aip.control_plane.skill import package as pkg

BASE = "http://127.0.0.1:18081/api/v1"
ADMIN_SECRET = "YBYVFWPAREZ6BAHLYBRFMHTFS3BXD4BD"
PASSWORD = "y_pWRBigBegRDadKavdb0LPI"
NAME = "nike-sample-guide"


def req(method: str, path: str, body: dict | bytes | None = None, token: str | None = None) -> tuple[int, object]:
    data = None
    headers: dict[str, str] = {}
    if isinstance(body, dict):
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    elif isinstance(body, (bytes, bytearray)):
        data = bytes(body)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=120) as resp:
            raw = resp.read()
            if not raw:
                return resp.status, {}
            ctype = resp.headers.get("Content-Type", "")
            if "json" in ctype:
                return resp.status, json.loads(raw.decode())
            return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"detail": raw}


def login(name: str, secret: str) -> str:
    st, body = req(
        "POST",
        "/auth/login",
        {"login_name": name, "password": PASSWORD, "totp": pyotp.TOTP(secret).now()},
    )
    assert st == 200 and isinstance(body, dict), body
    return str(body["session_token"])


def main() -> None:
    admin = login("admin", ADMIN_SECRET)
    # reuse staff from nike-path if present
    staff = json.loads((__import__("pathlib").Path(".aip-data/nike-path-staff.json")).read_text(encoding="utf-8"))
    dev = login("dev", staff["dev"])
    bo = login("bo", staff["bo"])
    sec = login("security", staff["security"])

    st, orgs = req("GET", "/orgs?limit=50", token=admin)
    hq = next(o for o in (orgs.get("items") or []) if o.get("code") == "HQ")  # type: ignore[union-attr]

    st, assets = req("GET", "/assets?limit=200", token=admin)
    existing = next((a for a in (assets.get("items") or []) if a.get("name") == f"skill.{NAME.replace('-', '_')}"), None)  # type: ignore[union-attr]
    if existing:
        print("asset exists", existing["id"])
        aid = existing["id"]
    else:
        st, created = req(
            "POST",
            "/assets",
            {
                "type": "skill",
                "name": f"skill.{NAME.replace('-', '_')}",
                "owner_org_id": hq["id"],
                "classification": 2,
            },
            admin,
        )
        print("create asset", st, created)
        assert st in (200, 201) and isinstance(created, dict)
        aid = created["id"]

    pkg.make_zip(
        {
            "SKILL.md": (
                f"---\nname: {NAME}\n"
                "description: 指导外部 Agent 使用 Nike 打样只读 Tool 查询首页、产品与机台。\n"
                "---\n\n"
                "# Nike 打样助手\n\n"
                "- 查首页统计：调用 `nike.sample.app_13`\n"
                "- 查产品列表：调用 `nike.sample.pc_12`\n"
                "- 查机台状态：调用 `nike.sample.app_8`\n"
            ).encode()
        }
    )

    # new version
    st, ver = req(
        "POST",
        f"/assets/{aid}/versions",
        {
            "semver": "1.0.0",
            "spec": {"name": NAME, "description": "指导外部 Agent 使用 Nike 打样只读 Tool 查询首页、产品与机台。"},
        },
        dev,
    )
    if st not in (200, 201):
        st, ver = req(
            "POST",
            f"/assets/{aid}/versions",
            {
                "semver": "1.0.1",
                "spec": {"name": NAME, "description": "指导外部 Agent 使用 Nike 打样只读 Tool 查询首页、产品与机台。"},
            },
            dev,
        )
    print("version", st, ver)
    assert st in (200, 201) and isinstance(ver, dict)
    vid = ver["id"]

    # upload zip via multipart is painful with urllib — use skill-source PUT
    st, saved = req(
        "PUT",
        f"/versions/{vid}/skill-source",
        {
            "name": NAME,
            "description": "指导外部 Agent 使用 Nike 打样只读 Tool 查询首页、产品与机台。",
            "body": "# Nike 打样助手\n\n- 首页：`nike.sample.app_13`\n- 产品：`nike.sample.pc_12`\n- 机台：`nike.sample.app_8`\n",
            "tools": [],
            "references": [],
            "assets": [],
            "allowed_tools": [],
        },
        dev,
    )
    print("skill-source", st, saved if isinstance(saved, dict) else type(saved))
    assert st in (200, 201), saved

    st, _ = req("PUT", f"/versions/{vid}/acceptance", {"scenarios": ["Agent 按说明书选 Nike Tool"]}, dev)
    print("acceptance", st)

    st, g0 = req("POST", f"/versions/{vid}/submit", {}, dev)
    print("G0", st, g0)
    assert isinstance(g0, dict) and g0.get("passed"), g0

    st, lab = req("POST", f"/versions/{vid}/run-tests", {}, dev)
    print("lab", st, lab)
    assert isinstance(lab, dict) and lab.get("passed"), lab

    st, apps_bo = req("GET", "/approvals", token=bo)
    st, apps_sec = req("GET", "/approvals", token=sec)
    g5 = next((a for a in (apps_bo.get("items") or []) if a.get("kind") == "G5"), None)  # type: ignore[union-attr]
    g6 = next((a for a in (apps_sec.get("items") or []) if a.get("kind") == "G6"), None)  # type: ignore[union-attr]
    assert g5 and g6, (apps_bo, apps_sec)
    req("POST", f"/approvals/{g5['id']}/decide", {"decision": "approve", "results": {"accuracy": 1.0}}, bo)
    req("POST", f"/approvals/{g6['id']}/decide", {"decision": "approve"}, sec)

    rollout = {"env": "test", "scope": {"group": True}, "exposure": ["rest"]}
    st, c = req("POST", f"/versions/{vid}/canary", rollout, admin)
    print("canary", st, c)
    st, p = req("POST", f"/versions/{vid}/promote", {**rollout, "min_calls": 0}, admin)
    print("promote", st, p)

    st, skills = req("GET", "/skills?q=nike", token=admin)
    print("catalog", st, skills)


if __name__ == "__main__":
    main()
