"""ADR-0003 spike 公共部分：DSL 子集解释规则、假 Tool、崩溃注入、WorkflowExecutor 抽象。

两个引擎（自研 step journal、DBOS Transact）共用这里的语义，只在“怎么持久化与恢复”上不同，
这样同一套测试可以公平比较。DSL 是 architecture.md 6.3 的子集：tool、condition、human_task、wait、end；
loop、parallel、sub_workflow、llm 节点与预算属于解释器层，和引擎选择无关，不在 spike 范围。
"""

from __future__ import annotations

import os
from typing import Any, Protocol

import asyncpg

DSN = os.environ.get("SPIKE_DSN", "postgresql://aip:aip-dev-only@127.0.0.1:55432/aip_spike")

FAKE_SCHEMA = """
CREATE SCHEMA IF NOT EXISTS fake;
CREATE TABLE IF NOT EXISTS fake.attempt (id bigserial PRIMARY KEY, run_id text, node_id text, tool text, at timestamptz DEFAULT now());
CREATE TABLE IF NOT EXISTS fake.effect (key text PRIMARY KEY, run_id text, tool text, seq bigserial, at timestamptz DEFAULT now());
CREATE TABLE IF NOT EXISTS fake.crash_marker (key text PRIMARY KEY);
"""


class ToolError(Exception):
    """业务失败（不可重试），触发补偿。"""


class WorkflowExecutor(Protocol):
    """architecture.md 8.5 的引擎抽象；平台其余部分只依赖它。"""

    async def start(self, run_id: str, dsl: dict[str, Any], input: dict[str, Any]) -> None: ...
    async def decide(self, run_id: str, node_id: str, decision: str) -> None: ...
    async def status(self, run_id: str) -> dict[str, Any]: ...
    async def steps(self, run_id: str) -> list[dict[str, Any]]: ...


# ---------- DSL 解释规则（确定性：只依赖已记录的步骤输出） ----------


def node(dsl: dict[str, Any], node_id: str) -> dict[str, Any]:
    return next(n for n in dsl["nodes"] if n["id"] == node_id)


def lookup(ctx: dict[str, Any], path: str) -> Any:
    cur: Any = ctx
    for part in path.split("."):
        cur = cur[part]
    return cur


def eval_condition(n: dict[str, Any], ctx: dict[str, Any]) -> str:
    """condition 节点：{"var": "n1.risk", "eq": "high"}；只读数据，不执行代码。"""
    return n["then"] if lookup(ctx, n["var"]) == n["eq"] else n["else"]


def tool_args(n: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    return {k: (lookup(ctx, v[1:]) if isinstance(v, str) and v.startswith("$") else v) for k, v in n.get("args", {}).items()}


# ---------- 假 Tool：每次调用记 attempt；写操作按幂等键去重记 effect ----------


async def call_tool(conn: asyncpg.Connection, tool: str, key: str, run_id: str, node_id: str, args: dict[str, Any]) -> dict[str, Any]:
    await conn.execute("INSERT INTO fake.attempt(run_id, node_id, tool) VALUES ($1, $2, $3)", run_id, node_id, tool)
    if tool == "erp.get_po":
        amount = int(args.get("amount", 0))
        return {"amount": amount, "risk": "high" if amount > 1000 else "low"}
    if tool in ("erp.approve", "erp.reserve", "erp.revoke", "erp.release", "erp.notify"):
        await conn.execute(
            "INSERT INTO fake.effect(key, run_id, tool) VALUES ($1, $2, $3) ON CONFLICT (key) DO NOTHING", key, run_id, tool
        )
        return {"ok": True}
    if tool == "erp.crash_after_effect":
        # 最坏时机：下游已生效，但引擎还没记下步骤完成，进程就被杀
        await conn.execute(
            "INSERT INTO fake.effect(key, run_id, tool) VALUES ($1, $2, $3) ON CONFLICT (key) DO NOTHING", key, run_id, tool
        )
        first = await conn.fetchval("INSERT INTO fake.crash_marker(key) VALUES ($1) ON CONFLICT DO NOTHING RETURNING key", key)
        if first:
            os._exit(137)  # 模拟 kill -9
        return {"ok": True}
    if tool == "erp.fail":
        raise ToolError("下游返回业务失败")
    raise ToolError(f"未知工具 {tool}")


# ---------- 测试用流程 ----------

LINEAR = {
    "name": "po_review",
    "start": "n1",
    "nodes": [
        {"id": "n1", "type": "tool", "ref": "erp.get_po", "args": {"amount": "$input.amount"}, "next": "n2"},
        {"id": "n2", "type": "condition", "var": "n1.risk", "eq": "high", "then": "n3", "else": "n4"},
        {"id": "n3", "type": "tool", "ref": "erp.notify", "next": "n4"},
        {"id": "n4", "type": "tool", "ref": "erp.approve", "compensation": "erp.revoke", "next": "end"},
    ],
}

CRASHY = {
    "name": "crashy",
    "start": "n1",
    "nodes": [
        {"id": "n1", "type": "tool", "ref": "erp.reserve", "compensation": "erp.release", "next": "n2"},
        {"id": "n2", "type": "tool", "ref": "erp.crash_after_effect", "next": "n3"},
        {"id": "n3", "type": "tool", "ref": "erp.approve", "next": "end"},
    ],
}

SAGA = {
    "name": "saga",
    "start": "n1",
    "nodes": [
        {"id": "n1", "type": "tool", "ref": "erp.reserve", "compensation": "erp.release", "next": "n2"},
        {"id": "n2", "type": "tool", "ref": "erp.approve", "compensation": "erp.revoke", "next": "n3"},
        {"id": "n3", "type": "tool", "ref": "erp.fail", "next": "end"},
    ],
}


def human(timeout_s: float, wait_s: float = 0) -> dict[str, Any]:
    nodes: list[dict[str, Any]] = []
    if wait_s:
        nodes.append({"id": "w", "type": "wait", "seconds": wait_s, "next": "h"})
    nodes += [
        {"id": "h", "type": "human_task", "timeout_s": timeout_s, "on_timeout": "esc", "next": "c"},
        {"id": "c", "type": "condition", "var": "h.decision", "eq": "approve", "then": "ok", "else": "end"},
        {"id": "ok", "type": "tool", "ref": "erp.approve", "next": "end"},
        {"id": "esc", "type": "tool", "ref": "erp.notify", "next": "end"},
    ]
    return {"name": "human", "start": nodes[0]["id"], "nodes": nodes}
