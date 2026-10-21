"""H7：套件工具选择评测（G2）。"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from aip.control_plane.app import create_app as create_cp
from aip.control_plane.db import Database, DbScope
from aip.control_plane.gates import service as gates
from aip.control_plane.registry import service as registry
from aip.control_plane.testlab.selection_eval import EvalCase, run_selection_eval
from aip.data_plane.llm.allowlist import MemoryAllowlistStore
from aip.data_plane.llm.budget import MemoryBudgetStore
from aip.data_plane.llm.gateway import LlmGateway
from aip.data_plane.workflow.llm import LlmResult
from iam_seed import PASSWORD, Seed
from m2_fixtures import promote, tool_spec
from test_m4_gates import SECRETS, People, _people

MP = "mp_select@1.0.0"
PROFILE = {
    "model": "gpt-test",
    "base_url": "https://llm.example.internal",
    "temperature": 0,
    "max_tokens": 256,
    "api_path": "/v1/chat/completions",
}


class ScriptedLlm:
    """按顺序返回 capability_id（与用例顺序一致）。"""

    def __init__(self, answers: list[str | None]) -> None:
        self.answers = list(answers)
        self.i = 0

    async def complete(self, **_: Any) -> LlmResult:
        if self.i >= len(self.answers):
            raise RuntimeError("scripted llm exhausted")
        a = self.answers[self.i]
        self.i += 1
        return LlmResult(data={"capability_id": a}, tokens=5)


def _cases(cap: str, n: int = 20, *, null_every: int | None = None) -> list[dict[str, Any]]:
    out = []
    for i in range(n):
        expect: str | None = None if (null_every and i % null_every == 0) else cap
        out.append({"name": f"c{i:02d}", "question": f"问题{i} 关于 {cap}", "expect_capability_id": expect})
    return out


async def _publish_tool(db: Database, s: Seed, p: People, cap: str) -> str:
    spec = tool_spec(capability_id=cap, name=cap.split(".")[-1], data_classification=2, risk_level="read")
    async with db.tx(DbScope.system()) as conn:
        aid = await registry.create_asset(conn, p.dev, type_="tool", name=cap, owner_org_id=s.hq, classification=2)
        vid = await registry.create_version(conn, p.dev, aid, "1.0.0", spec)
        await promote(conn, p.dev, vid)
    return f"{cap}@1.0.0"


@pytest.mark.asyncio
async def test_runner_perfect_top1() -> None:
    cap = "ops.demo.list"
    cases = [EvalCase(c["name"], c["question"], c["expect_capability_id"]) for c in _cases(cap)]
    llm = ScriptedLlm([c.expect_capability_id for c in cases])
    ok, results, problems = await run_selection_eval(
        llm,
        company_id=uuid.uuid4(),
        model_profiles=[MP],
        cases=cases,
        candidates=[{"capability_id": cap, "description": "列出演示资源，用于选择评测。"}],
    )
    assert not problems and ok
    assert results[0].top1_hit_rate == 1.0 and results[0].false_call_rate == 0.0


@pytest.mark.asyncio
async def test_runner_false_call_when_expect_null() -> None:
    cap = "ops.demo.list"
    cases = [EvalCase(f"c{i}", f"q{i}", None if i < 2 else cap) for i in range(20)]
    # 前两条期望 null，却返回 tool → 误调用 2/20=0.1
    answers = [cap] * 20
    llm = ScriptedLlm(answers)
    ok, results, _ = await run_selection_eval(
        llm,
        company_id=uuid.uuid4(),
        model_profiles=[MP],
        cases=cases,
        candidates=[{"capability_id": cap, "description": "演示"}],
    )
    assert not ok
    assert results[0].false_call_rate == pytest.approx(0.1)


@pytest.mark.asyncio
async def test_runner_too_few_cases() -> None:
    ok, results, problems = await run_selection_eval(
        ScriptedLlm([]),
        company_id=uuid.uuid4(),
        model_profiles=[MP],
        cases=[EvalCase("c0", "q", "a.b.c")],
        candidates=[{"capability_id": "a.b.c", "description": "x"}],
    )
    assert not ok and results == [] and any("20" in p for p in problems)


@pytest.mark.asyncio
async def test_bundle_g2_pass_with_scripted_llm(db: Database, seeded: Seed) -> None:
    p = await _people(db, seeded)
    cap = f"ops.h7.pass_{uuid.uuid4().hex[:6]}"
    ref = await _publish_tool(db, seeded, p, cap)
    cases = _cases(cap)
    llm = ScriptedLlm([c["expect_capability_id"] for c in cases])
    async with db.tx(DbScope.system()) as conn:
        bname = f"h7p_{uuid.uuid4().hex[:6]}"
        aid = await registry.create_asset(
            conn, p.dev, type_="bundle", name=bname, owner_org_id=seeded.hq, classification=2
        )
        vid = await registry.create_version(
            conn,
            p.dev,
            aid,
            "1.0.0",
            {"name": bname, "description": "H7 选择评测通过套件。", "tools": [ref]},
        )
        await gates.put_selection_eval(conn, p.dev, vid, model_profiles=[MP], cases=cases)
        await gates.set_acceptance(conn, p.dev, vid, {"scenarios": ["选工具"]})
        assert (await gates.submit(conn, p.dev, vid))["passed"]
        lab = await gates.run_lab(conn, p.dev, vid, secrets=SECRETS, llm=llm)
        assert lab["passed"] is True, lab
        assert lab["selection_eval"]["models"][0]["top1_hit_rate"] == 1.0


@pytest.mark.asyncio
async def test_bundle_g2_fail_too_few_cases(db: Database, seeded: Seed) -> None:
    p = await _people(db, seeded)
    cap = f"ops.h7.few_{uuid.uuid4().hex[:6]}"
    ref = await _publish_tool(db, seeded, p, cap)
    async with db.tx(DbScope.system()) as conn:
        bname = f"h7f_{uuid.uuid4().hex[:6]}"
        aid = await registry.create_asset(
            conn, p.dev, type_="bundle", name=bname, owner_org_id=seeded.hq, classification=2
        )
        vid = await registry.create_version(
            conn,
            p.dev,
            aid,
            "1.0.0",
            {"name": bname, "description": "H7 用例不足套件。", "tools": [ref]},
        )
        await gates.put_selection_eval(
            conn, p.dev, vid, model_profiles=[MP], cases=_cases(cap, n=5)
        )
        await gates.set_acceptance(conn, p.dev, vid, {"scenarios": ["x"]})
        assert (await gates.submit(conn, p.dev, vid))["passed"]
        lab = await gates.run_lab(conn, p.dev, vid, secrets=SECRETS, llm=ScriptedLlm([]))
        assert lab["passed"] is False
        assert any("20" in p for p in lab["selection_eval"]["problems"])


@pytest.mark.asyncio
async def test_gateway_allowlist_denies_in_eval(db: Database, seeded: Seed) -> None:
    """白名单外 model_profile → 用例错误 → G2 失败。"""
    p = await _people(db, seeded)
    cap = f"ops.h7.deny_{uuid.uuid4().hex[:6]}"
    ref = await _publish_tool(db, seeded, p, cap)
    from aip.control_plane.iam.orgs import get_org

    async with db.tx(DbScope.system()) as conn:
        cid = (await get_org(conn, seeded.hq)).company_id
    al = MemoryAllowlistStore()
    bd = MemoryBudgetStore()
    await bd.set_daily_limit(cid, 100_000)
    # 不加入白名单
    gw = LlmGateway(
        allowlist=al,
        budget=bd,
        resolve_profile=lambda _r, _e: PROFILE,
        env="test",
        transport=httpx.MockTransport(lambda r: httpx.Response(500)),
    )
    cases = _cases(cap)
    async with db.tx(DbScope.system()) as conn:
        bname = f"h7d_{uuid.uuid4().hex[:6]}"
        aid = await registry.create_asset(
            conn, p.dev, type_="bundle", name=bname, owner_org_id=seeded.hq, classification=2
        )
        vid = await registry.create_version(
            conn,
            p.dev,
            aid,
            "1.0.0",
            {"name": bname, "description": "H7 白名单拒绝套件。", "tools": [ref]},
        )
        await gates.put_selection_eval(conn, p.dev, vid, model_profiles=[MP], cases=cases)
        await gates.set_acceptance(conn, p.dev, vid, {"scenarios": ["x"]})
        assert (await gates.submit(conn, p.dev, vid))["passed"]
        lab = await gates.run_lab(conn, p.dev, vid, secrets=SECRETS, llm=gw)
        assert lab["passed"] is False
        assert lab["selection_eval"]["models"][0]["passed"] is False
        errs = [o.get("error") or "" for o in lab["selection_eval"]["models"][0]["outcomes"]]
        assert any("PERMISSION" in e for e in errs)


async def _login(cp: httpx.AsyncClient, seed: Seed) -> dict[str, str]:
    r = await cp.post(
        "/api/v1/auth/login",
        json={"login_name": "admin", "password": PASSWORD, "totp": seed.code("admin")},
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['session_token']}"}


@pytest.mark.asyncio
async def test_selection_eval_http(db: Database, seeded: Seed) -> None:
    p = await _people(db, seeded)
    cap = f"ops.h7.http_{uuid.uuid4().hex[:6]}"
    ref = await _publish_tool(db, seeded, p, cap)
    async with db.tx(DbScope.system()) as conn:
        bname = f"h7h_{uuid.uuid4().hex[:6]}"
        aid = await registry.create_asset(
            conn, p.dev, type_="bundle", name=bname, owner_org_id=seeded.hq, classification=2
        )
        vid = await registry.create_version(
            conn,
            p.dev,
            aid,
            "1.0.0",
            {"name": bname, "description": "H7 HTTP 评测集套件。", "tools": [ref]},
        )
    transport = httpx.ASGITransport(app=create_cp(db))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as cp:
        h = await _login(cp, seeded)
        body = {"model_profiles": [MP], "cases": _cases(cap)}
        r = await cp.put(f"/api/v1/versions/{vid}/selection-eval", headers=h, json=body)
        assert r.status_code == 200, r.text
        assert len(r.json()["cases"]) == 20
        r = await cp.get(f"/api/v1/versions/{vid}/selection-eval", headers=h)
        assert r.status_code == 200 and r.json()["model_profiles"] == [MP]
