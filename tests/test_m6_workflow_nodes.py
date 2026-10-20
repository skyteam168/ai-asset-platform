"""M6e：Workflow loop / parallel / sub_workflow 节点。"""

from __future__ import annotations

import copy
import pathlib
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from aip.connectors.mock import MockConnector
from aip.control_plane.db import Database
from aip.data_plane.workflow.engine import WorkflowEngine
from aip.libs.workflow.dsl import validate
from iam_seed import app_url
from m3_world import World, connector
from test_m5_workflow import GET_PO, OPS, STATIC, TOOLS, engine_of, settle

pytestmark = pytest.mark.infra

CHILD = "agent.finance.child_po"
PARENT = "agent.finance.parent_po"
LOOP_CAP = "agent.finance.loop_po"
PAR_CAP = "agent.finance.par_po"


def dsl(nodes: list[dict[str, Any]], *, name: str, capability_id: str, **over: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "name": name,
        "capability_id": capability_id,
        "version": "1.0.0",
        "description": "m6e",
        "input_schema": {"type": "object", "properties": {"po": {"type": "string"}}, "required": ["po"]},
        "budget": {"max_steps": 40, "max_llm_calls": 0, "timeout_s": 600},
        "nodes": nodes,
    }
    d.update(over)
    return d


def tnode(nid: str, cap: str, nxt: str, **over: Any) -> dict[str, Any]:
    return {"id": nid, "type": "tool", "ref": f"{cap}@1.0.0", "next": nxt, "args": {"po": "$input.po"}, **over}


CHILD_FLOW = dsl(
    [
        tnode("c1", GET_PO, "fin"),
        {"id": "fin", "type": "end", "output": {"po": "$c1.po_no"}},
    ],
    name="child_po",
    capability_id=CHILD,
)

LOOP_FLOW = dsl(
    [
        {
            "id": "lp",
            "type": "loop",
            "max_iterations": 3,
            "body": "b1",
            "next": "fin",
        },
        tnode("b1", GET_PO, "lp"),
        {"id": "fin", "type": "end", "output": {"iters": "$lp.iteration"}},
    ],
    name="loop_po",
    capability_id=LOOP_CAP,
)

PAR_FLOW = dsl(
    [
        {"id": "par", "type": "parallel", "branches": ["a", "b"], "next": "fin"},
        tnode("a", GET_PO, "par"),
        tnode("b", GET_PO, "par"),
        {"id": "fin", "type": "end", "output": {"done": "$par.done"}},
    ],
    name="par_po",
    capability_id=PAR_CAP,
)

PARENT_FLOW = dsl(
    [
        {
            "id": "sub",
            "type": "sub_workflow",
            "ref": f"{CHILD}@1.0.0",
            "args": {"po": "$input.po"},
            "next": "fin",
        },
        {"id": "fin", "type": "end", "output": {"child": "$sub"}},
    ],
    name="parent_po",
    capability_id=PARENT,
)


@pytest.fixture
async def engine(db: Database) -> AsyncIterator[AsyncEngine]:
    e = create_async_engine(app_url())
    yield e
    await e.dispose()


async def make(tmp: pathlib.Path, engine: AsyncEngine, flows: list[dict[str, Any]]) -> World:
    MockConnector.executed.clear()
    w = World(
        tmp,
        TOOLS,
        [connector("erp", "mock", {"operations": copy.deepcopy(OPS)})],
        workflows=flows,
        runtime_engine=engine,
        static_token=STATIC,
    )
    await w.ready()
    w.runtime.workflow_sync_wait_s = 0
    wf = w.app.state.workflows
    wf.lease_s, wf.poll_s, wf.reconcile_base_s, wf.stale_s = 2.0, 0.05, 0.05, 0.5
    return w


def test_dsl_accepts_loop_parallel_sub() -> None:
    specs = {(t["capability_id"], "1.0.0"): t for t in TOOLS}

    def lookup(c: str, v: str) -> dict[str, Any] | None:
        return specs.get((c, v))

    def wf_lookup(c: str, v: str) -> dict[str, Any] | None:
        return CHILD_FLOW if c == CHILD and v == "1.0.0" else None

    assert validate(LOOP_FLOW, lookup) == []
    assert validate(PAR_FLOW, lookup) == []
    assert validate(PARENT_FLOW, lookup, workflow_lookup=wf_lookup) == []
    bad = copy.deepcopy(LOOP_FLOW)
    bad["nodes"][1]["next"] = "fin"  # 体不指回 loop，且从 fin 不可回 — 仍可达
    # 去掉 max → schema 失败
    del bad["nodes"][0]["max_iterations"]
    assert validate(bad, lookup)


async def test_loop_runs_max_iterations(tmp_path: pathlib.Path, engine: AsyncEngine) -> None:
    w = await make(tmp_path, engine, [LOOP_FLOW])
    r = await w.runtime.invoke(w.principal(), LOOP_CAP, {"po": "PO-1"})
    view = await settle(w, str(r["run_id"]), {"succeeded"})
    assert view["status"] == "succeeded"
    assert view["result"]["iters"] == 3
    # 只读 Tool 不进 MockConnector.executed；用 journal 节点 id（含迭代后缀）计数
    assert sum(1 for s in view["steps"] if str(s["node_id"]).startswith("b1#i")) == 3


async def test_parallel_joins_branches(tmp_path: pathlib.Path, engine: AsyncEngine) -> None:
    w = await make(tmp_path, engine, [PAR_FLOW])
    r = await w.runtime.invoke(w.principal(), PAR_CAP, {"po": "PO-1"})
    view = await settle(w, str(r["run_id"]), {"succeeded"})
    assert view["status"] == "succeeded"
    assert set(view["result"]["done"]) == {"a", "b"}
    assert {s["node_id"] for s in view["steps"] if s["kind"] == "tool"} >= {"a", "b"}


async def test_sub_workflow_waits_for_child(tmp_path: pathlib.Path, engine: AsyncEngine) -> None:
    w = await make(tmp_path, engine, [CHILD_FLOW, PARENT_FLOW])
    r = await w.runtime.invoke(w.principal(), PARENT, {"po": "PO-1"})
    view = await settle(w, str(r["run_id"]), {"succeeded"}, limit_s=15)
    assert view["status"] == "succeeded"
    assert view["result"]["child"]["po"] == "PO-1"
    assert any(s["node_id"] == "sub" and s["status"] == "done" for s in view["steps"])


async def test_sandbox_schema_isolated(tmp_path: pathlib.Path, engine: AsyncEngine) -> None:
    """沙箱引擎只认领 exec_sandbox，不碰生产 exec.wf_run。"""
    w = await make(tmp_path, engine, [LOOP_FLOW])
    prod: WorkflowEngine = engine_of(w)
    sand = WorkflowEngine(
        engine,
        w.runtime,
        prod.audit,
        instance_id="sandbox-worker",
        schema="exec_sandbox",
        lease_s=2.0,
        poll_s=0.05,
    )
    assert await sand.tick() is False
    # 生产 Run 仍由生产引擎推进
    r = await w.runtime.invoke(w.principal(), LOOP_CAP, {"po": "PO-1"})
    assert await sand.tick() is False
    view = await settle(w, str(r["run_id"]), {"succeeded"})
    assert view["status"] == "succeeded"
