"""方案 B：DBOS Transact 3.2（Python 库，系统表在同一 PostgreSQL 的 dbos schema）。

解释器本身是一个 DBOS workflow：每个 Tool 调用是 DBOS.step（输出被记录，恢复时重放而不重做），
human_task 是 DBOS.recv（带超时，持久化），wait 是 DBOS.sleep（持久化定时器）。
控制面一侧用 DBOSClient 入队、发送决策、查询状态，自身不是 DBOS 应用。
"""

from __future__ import annotations

from typing import Any

import asyncpg
from dbos import DBOS, DBOSClient, EnqueueOptions

from common import DSN, ToolError, call_tool, eval_condition, node, tool_args

QUEUE = "runs"  # Worker 启动后 DBOS.register_queue 注册（3.x 起不能直接构造 Queue）


@DBOS.step(name="tool")
async def tool_step(tool: str, key: str, run_id: str, node_id: str, args: dict[str, Any]) -> Any:
    conn = await asyncpg.connect(DSN)
    try:
        return await call_tool(conn, tool, key, run_id, node_id, args)
    finally:
        await conn.close()


@DBOS.workflow(name="run_dsl")
async def run_dsl(dsl: dict[str, Any], input: dict[str, Any]) -> dict[str, Any]:
    run_id = DBOS.workflow_id
    assert run_id
    ctx: dict[str, Any] = {"input": input}
    done: list[str] = []
    current = dsl["start"]
    while current != "end":
        n = node(dsl, current)
        await DBOS.set_event_async("current", current)
        if n["type"] == "tool":
            try:
                ctx[current] = await tool_step(n["ref"], f"{run_id}:{current}", run_id, current, tool_args(n, ctx))
            except ToolError as e:
                for nid in reversed(done):  # Saga：逆序补偿
                    comp = node(dsl, nid).get("compensation")
                    if comp:
                        await tool_step(comp, f"{run_id}:{nid}:comp", run_id, nid, {})
                await DBOS.set_event_async("current", current)
                return {"status": "compensated", "error": str(e), "ctx": ctx}
            done.append(current)
            current = n["next"]
        elif n["type"] == "condition":
            current = eval_condition(n, ctx)
        elif n["type"] == "wait":
            await DBOS.sleep_async(n["seconds"])
            current = n["next"]
        elif n["type"] == "human_task":
            msg = await DBOS.recv_async(topic=current, timeout_seconds=n["timeout_s"])
            ctx[current] = {"decision": msg if msg is not None else "timeout"}
            current = n["on_timeout"] if msg is None else n["next"]
        else:
            raise ValueError(n["type"])
    await DBOS.set_event_async("current", "end")
    return {"status": "succeeded", "ctx": ctx}


def worker_config(executor_id: str, app_version: str | None) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "name": "aip-workflow-spike",
        "system_database_url": DSN,
        "executor_id": executor_id,
        "log_level": "WARNING",
    }
    if app_version:
        cfg["application_version"] = app_version
    return cfg


class DbosExecutor:
    def __init__(self) -> None:
        self.client = DBOSClient(system_database_url=DSN)

    async def start(self, run_id: str, dsl: dict[str, Any], input: dict[str, Any]) -> None:
        opts: EnqueueOptions = {"workflow_name": "run_dsl", "queue_name": QUEUE, "workflow_id": run_id}
        await self.client.enqueue_async(opts, dsl, input)

    async def decide(self, run_id: str, node_id: str, decision: str) -> None:
        # 幂等键：同一任务的重复提交只投递一次
        await self.client.send_async(run_id, decision, topic=node_id, idempotency_key=f"{run_id}:{node_id}")

    async def status(self, run_id: str) -> dict[str, Any]:
        [w] = self.client.list_workflows(workflow_ids=[run_id]) or [None]
        if w is None:
            return {"status": "missing"}
        if w.status == "SUCCESS":
            return {"status": w.output["status"], "ctx": w.output["ctx"], "current": "end"}
        current = self.client.get_event(run_id, "current", timeout_seconds=0)
        return {"status": {"PENDING": "running", "ENQUEUED": "running"}.get(w.status, w.status.lower()), "current": current,
                "executor": w.executor_id, "app_version": w.app_version}

    async def steps(self, run_id: str) -> list[dict[str, Any]]:
        return [
            {"function_id": s["function_id"], "name": s["function_name"], "error": str(s["error"]) if s["error"] else None}
            for s in self.client.list_workflow_steps(run_id)
        ]

    def close(self) -> None:
        self.client.destroy()
