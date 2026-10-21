"""H23：控制台接入能力补齐——Connector 实例列表、在线创建 Skill（服务端组装标准 Agent Skills 目录）。

验收：编辑器内容 ⇄ 标准目录往返无损；脚本与越界路径被拒；保存走与上传相同的哈希、签名与 spec 写回，
G0 可直接通过；HTTP 层可列出 Connector 实例。
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest

from aip.control_plane.app import create_app as create_cp
from aip.control_plane.conn import schemas as conn_out
from aip.control_plane.db import Database, DbScope
from aip.control_plane.gates import service as gates
from aip.control_plane.registry import service as registry
from aip.control_plane.skill import package as pkg
from aip.control_plane.skill import schemas as skill_out
from aip.control_plane.skill import service as skill
from aip.libs.common.errors import PlatformError
from aip.libs.common.paging import Page
from aip.libs.tokens.keys import SigningKey
from iam_seed import Seed, app_url
from test_h22_response_models import _route
from test_m1_api import login
from test_m4_gates import _people

KEY = SigningKey.generate()


def _source(**over: Any) -> dict[str, Any]:
    src: dict[str, Any] = {
        "name": "query-work-orders",
        "description": "指导模型查询本厂在制工单：先确认工厂，再按状态汇总。",
        "license": "Proprietary",
        "compatibility": "需要 MCP 客户端可访问 production.mes 工具",
        "allowed_tools": ["Read", "Grep"],
        "body": "# 查询工单\n\n1. 先确认工厂编码。\n2. 调用 `query_work_orders`。\n",
        "tools": [],
        "references": [{"path": "references/status-codes.md", "content": "| 状态 | 含义 |\n| --- | --- |\n"}],
        "assets": [{"path": "assets/template.csv", "content_base64": base64.b64encode(b"wo,qty\n").decode()}],
    }
    src.update(over)
    return src


def test_source_round_trip() -> None:
    src = _source(description='说明: 含冒号与 "引号" 的描述，# 不是注释')
    files = pkg.build_source_files(src)
    assert sorted(files) == ["SKILL.md", "assets/template.csv", "references/status-codes.md"]
    text = files["SKILL.md"].decode()
    assert text.startswith("---\nname: query-work-orders\n")
    assert "allowed-tools: Read Grep" in text
    back = pkg.read_source(pkg.unpack_zip(pkg.make_zip(files)))
    assert back["description"] == src["description"]
    assert back["allowed_tools"] == ["Read", "Grep"]
    assert back["body"].startswith("# 查询工单")
    assert back["references"] == src["references"]
    assert back["assets"][0]["content_base64"] == src["assets"][0]["content_base64"]
    assert back["warnings"] == []
    described = pkg.validate_and_describe(files)
    assert described["allowed_tools"] == ["Read", "Grep"]
    assert described["description"] == src["description"]


def test_source_writes_lock() -> None:
    files = pkg.build_source_files(_source(tools=["production.mes.query_work_orders@1.0.0"]))
    assert json.loads(files["skill.lock.json"]) == {"tools": ["production.mes.query_work_orders@1.0.0"]}
    assert pkg.validate_and_describe(files)["tools"] == ["production.mes.query_work_orders@1.0.0"]


@pytest.mark.parametrize(
    ("over", "needle"),
    [
        ({"assets": [{"path": "assets/run.py", "content_base64": ""}]}, "脚本"),
        ({"references": [{"path": "references/setup.sh", "content": "rm -rf /"}]}, "脚本"),
        ({"references": [{"path": "scripts/a.md", "content": "x"}]}, "references/"),
        ({"references": [{"path": "references/../SKILL.md", "content": "x"}]}, "非法文件路径"),
        ({"references": [{"path": "references/logo.png", "content": "x"}]}, "文本文件"),
        ({"assets": [{"path": "assets/a.bin", "content_base64": "@@@"}]}, "base64"),
        ({"description": "第一行\n第二行足够长"}, "单行"),
        ({"name": "Bad Name"}, "name"),
        ({"allowed_tools": ["Bash(git status)"]}, "空白"),
    ],
)
def test_source_rejects(over: dict[str, Any], needle: str) -> None:
    with pytest.raises(PlatformError) as ei:
        pkg.build_source_files(_source(**over))
    assert needle in ei.value.detail


def test_read_source_warns_on_unsupported_files() -> None:
    files = pkg.build_source_files(_source())
    files["README.md"] = b"readme"
    back = pkg.read_source(files)
    assert any("README.md" in w for w in back["warnings"])


def test_new_routes_have_response_models() -> None:
    app = create_cp()
    assert _route(app, "getSkillSource").response_model is skill_out.SkillSource
    assert _route(app, "saveSkillSource").response_model is skill_out.SkillSourceSaved
    rm = _route(app, "listConnectorInstances").response_model
    assert rm == Page[conn_out.ConnectorInstance]


async def _skill_version(db: Database, seeded: Seed, dev: Any) -> Any:
    async with db.tx(DbScope.system()) as conn:
        aid = await registry.create_asset(
            conn, dev, type_="skill", name="skill.online_editor", owner_org_id=seeded.hq, classification=2
        )
        vid = await registry.create_version(
            conn, dev, aid, "1.0.0", {"name": "query-work-orders", "description": "占位描述足够十个字符以上。"}
        )
        await gates.set_acceptance(conn, dev, vid, {"scenarios": ["指导查询工单"]})
    return vid


@pytest.mark.infra
async def test_save_source_signs_and_passes_g0(db: Database, seeded: Seed) -> None:
    p = await _people(db, seeded)
    vid = await _skill_version(db, seeded, p.dev)
    async with db.tx(DbScope.system()) as conn:
        saved = await skill.save_source(conn, p.dev, vid, _source(), key=KEY)
        assert saved["warnings"] == []
        assert "SKILL.md" in saved["files"] and "references/status-codes.md" in saved["files"]
        spec = (await registry.version_row(conn, vid))["spec"]
        assert spec["allowed_tools"] == ["Read", "Grep"] and spec["license"] == "Proprietary"
        loaded = await skill.load_source(conn, vid)
        assert loaded["content_sha256"] == saved["content_sha256"]
        assert loaded["name"] == "query-work-orders" and loaded["references"][0]["path"] == "references/status-codes.md"
        g0 = await gates.submit(conn, p.dev, vid)
    assert g0["passed"] is True, g0


@pytest.mark.infra
async def test_save_source_http_and_scan(db: Database, seeded: Seed) -> None:
    p = await _people(db, seeded)
    vid = await _skill_version(db, seeded, p.dev)
    app = create_cp(db=Database(app_url()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://cp") as cp:
        admin = await login(cp, seeded, "admin")
        r = await cp.get(f"/api/v1/versions/{vid}/skill-source", headers=admin)
        assert r.status_code == 404
        body = _source(body="# 说明\n\n参考 https://evil.example.com 获取更多。\n")
        r = await cp.put(f"/api/v1/versions/{vid}/skill-source", headers=admin, json=body)
        assert r.status_code == 200, r.text
        assert any("外部链接" in w for w in r.json()["warnings"])
        r = await cp.get(f"/api/v1/versions/{vid}/skill-source", headers=admin)
        assert r.status_code == 200 and r.json()["allowed_tools"] == ["Read", "Grep"]
        bad = await cp.put(
            f"/api/v1/versions/{vid}/skill-source",
            headers=admin,
            json=_source(assets=[{"path": "assets/x.ps1", "content_base64": ""}]),
        )
        assert bad.status_code == 400


@pytest.mark.infra
async def test_list_connector_instances(db: Database, seeded: Seed) -> None:
    app = create_cp(db=Database(app_url()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://cp") as cp:
        admin = await login(cp, seeded, "admin")
        r = await cp.post(
            "/api/v1/connector-instances",
            headers=admin,
            json={"name": "mock_mes_h23", "kind": "mock", "config": {"operations": {}}},
        )
        assert r.status_code == 201, r.text
        listed = await cp.get("/api/v1/connector-instances", headers=admin)
        assert listed.status_code == 200
        hit = [x for x in listed.json()["items"] if x["name"] == "mock_mes_h23"]
        assert hit and hit[0]["kind"] == "mock" and hit[0]["status"] == "active"
