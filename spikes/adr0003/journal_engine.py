"""方案 A：自研 step journal（architecture.md 8.5 原设计）。

- wf_run：一行一个 Run，带当前节点、上下文、状态、租约（lease_owner / lease_until）与唤醒时间 wake_at。
- wf_step：step journal，(run_id, node_id) 唯一；started 后崩溃的步骤用同一幂等键重做。
- wf_task：human_task，带截止时间；超时与 wait 都靠 wake_at 扫描，不在内存里计时。
- Worker 用 FOR UPDATE SKIP LOCKED 认领；租约过期的 Run 任何 Worker 都能接手（不依赖原进程回来）。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from typing import Any

import asyncpg

from common import DSN, ToolError, call_tool, eval_condition, node, tool_args

SCHEMA = """
CREATE SCHEMA IF NOT EXISTS j;
CREATE TABLE IF NOT EXISTS j.wf_run (
  id text PRIMARY KEY, dsl jsonb NOT NULL, ctx jsonb NOT NULL, current text NOT NULL,
  status text NOT NULL,              -- running / waiting / succeeded / compensated / failed
  lease_owner text, lease_until timestamptz, wake_at timestamptz,
  created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS wf_run_claim ON j.wf_run (status, lease_until, wake_at);
CREATE TABLE IF NOT EXISTS j.wf_step (
  run_id text, node_id text, seq int, kind text, status text, output jsonb, attempts int DEFAULT 0,
  started_at timestamptz, finished_at timestamptz, executor text, PRIMARY KEY (run_id, node_id)
);
CREATE TABLE IF NOT EXISTS j.wf_task (
  run_id text, node_id text, status text, deadline timestamptz, decided_at timestamptz,
  PRIMARY KEY (run_id, node_id)
);
"""

CLAIM = """
UPDATE j.wf_run SET lease_owner = $1, lease_until = now() + make_interval(secs => $2), status = 'running', updated_at = now()
WHERE id = (
  SELECT id FROM j.wf_run
  WHERE (status = 'running' AND (lease_until IS NULL OR lease_until < now()))
     OR (status = 'waiting' AND wake_at <= now())
  ORDER BY updated_at FOR UPDATE SKIP LOCKED LIMIT 1
) RETURNING id, dsl, ctx, current
"""


class JournalExecutor:
    """控制面一侧：启动、决策、查询。"""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def start(self, run_id: str, dsl: dict[str, Any], input: dict[str, Any]) -> None:
        await self.pool.execute(
            "INSERT INTO j.wf_run(id, dsl, ctx, current, status) VALUES ($1, $2, $3, $4, 'running') ON CONFLICT DO NOTHING",
            run_id, json.dumps(dsl), json.dumps({"input": input}), dsl["start"],
        )

    async def decide(self, run_id: str, node_id: str, decision: str) -> bool:
        """只接受仍在 pending 的任务；任务还没建（Run 尚未走到）或已决策时返回 False，调用方据此提示。"""
        async with self.pool.acquire() as conn, conn.transaction():
            ok = await conn.fetchval(
                "UPDATE j.wf_task SET status = $3, decided_at = now() WHERE run_id = $1 AND node_id = $2 AND status = 'pending' RETURNING 1",
                run_id, node_id, decision,
            )
            if ok:  # 唤醒 Run：下一次扫描即可认领
                await conn.execute("UPDATE j.wf_run SET wake_at = now() WHERE id = $1 AND status = 'waiting'", run_id)
            return bool(ok)

    async def status(self, run_id: str) -> dict[str, Any]:
        r = await self.pool.fetchrow("SELECT status, current, ctx FROM j.wf_run WHERE id = $1", run_id)
        return {"status": r["status"], "current": r["current"], "ctx": json.loads(r["ctx"])} if r else {"status": "missing"}

    async def steps(self, run_id: str) -> list[dict[str, Any]]:
        rows = await self.pool.fetch("SELECT node_id, kind, status, attempts, executor FROM j.wf_step WHERE run_id = $1 ORDER BY seq", run_id)
        return [dict(r) for r in rows]


class JournalWorker:
    def __init__(self, pool: asyncpg.Pool, executor_id: str, lease_s: float = 3.0) -> None:
        self.pool, self.me, self.lease_s = pool, executor_id, lease_s

    async def run_forever(self, poll_s: float = 0.2) -> None:
        while True:
            if not await self.tick():
                await asyncio.sleep(poll_s)

    async def tick(self) -> bool:
        row = await self.pool.fetchrow(CLAIM, self.me, self.lease_s)
        if row is None:
            return False
        await self.drive(row["id"], json.loads(row["dsl"]), json.loads(row["ctx"]), row["current"])
        return True

    async def _renew(self, conn: asyncpg.Connection, run_id: str) -> None:
        owned = await conn.fetchval(
            "UPDATE j.wf_run SET lease_until = now() + make_interval(secs => $3) WHERE id = $1 AND lease_owner = $2 RETURNING 1",
            run_id, self.me, self.lease_s,
        )
        if not owned:
            raise RuntimeError("租约已被他人接手")

    async def drive(self, run_id: str, dsl: dict[str, Any], ctx: dict[str, Any], current: str) -> None:
        async with self.pool.acquire() as conn:
            while current != "end":
                n = node(dsl, current)
                await self._renew(conn, run_id)
                if n["type"] == "tool":
                    try:
                        ctx[current] = await self._tool_step(conn, run_id, n, ctx)
                    except ToolError as e:
                        await self._compensate(conn, run_id, dsl, current, str(e))
                        return
                    nxt = n["next"]
                elif n["type"] == "condition":
                    nxt = eval_condition(n, ctx)
                elif n["type"] == "wait":
                    out = await self._journal(conn, run_id, n, {"until": (_now() + dt.timedelta(seconds=n["seconds"])).isoformat()})
                    until = dt.datetime.fromisoformat(out["until"])
                    if _now() < until:
                        return await self._park(conn, run_id, ctx, current, until)
                    nxt = n["next"]
                elif n["type"] == "human_task":
                    deadline = _now() + dt.timedelta(seconds=n["timeout_s"])
                    await conn.execute(
                        "INSERT INTO j.wf_task VALUES ($1, $2, 'pending', $3, NULL) ON CONFLICT DO NOTHING", run_id, current, deadline
                    )
                    t = await conn.fetchrow("SELECT status, deadline FROM j.wf_task WHERE run_id = $1 AND node_id = $2", run_id, current)
                    if t["status"] == "pending" and _now() < t["deadline"]:
                        return await self._park(conn, run_id, ctx, current, t["deadline"])
                    if t["status"] == "pending":
                        await conn.execute("UPDATE j.wf_task SET status = 'timeout' WHERE run_id = $1 AND node_id = $2", run_id, current)
                        nxt = n["on_timeout"]
                        ctx[current] = {"decision": "timeout"}
                    else:
                        ctx[current] = {"decision": t["status"]}
                        nxt = n["next"]
                    await self._journal(conn, run_id, n, ctx[current])
                else:
                    raise ValueError(n["type"])
                # 步骤完成与指针前移在同一事务：崩溃后要么重做这一步，要么从下一步继续
                await conn.execute(
                    "UPDATE j.wf_run SET current = $2, ctx = $3, updated_at = now() WHERE id = $1 AND lease_owner = $4",
                    run_id, nxt, json.dumps(ctx), self.me,
                )
                current = nxt
            await conn.execute("UPDATE j.wf_run SET status = 'succeeded', lease_owner = NULL WHERE id = $1", run_id)

    async def _journal(self, conn: asyncpg.Connection, run_id: str, n: dict[str, Any], output: Any) -> Any:
        """非 Tool 步骤：首次写入，之后读回首次的值（保证重放确定性）。"""
        await conn.execute(
            """INSERT INTO j.wf_step(run_id, node_id, seq, kind, status, output, started_at, finished_at, executor)
               VALUES ($1, $2, (SELECT count(*) FROM j.wf_step WHERE run_id = $1), $3, 'done', $4, now(), now(), $5)
               ON CONFLICT DO NOTHING""",
            run_id, n["id"], n["type"], json.dumps(output), self.me,
        )
        return json.loads(await conn.fetchval("SELECT output FROM j.wf_step WHERE run_id = $1 AND node_id = $2", run_id, n["id"]))

    async def _tool_step(self, conn: asyncpg.Connection, run_id: str, n: dict[str, Any], ctx: dict[str, Any]) -> Any:
        done = await conn.fetchrow("SELECT status, output FROM j.wf_step WHERE run_id = $1 AND node_id = $2", run_id, n["id"])
        if done and done["status"] == "done":
            return json.loads(done["output"])
        await conn.execute(
            """INSERT INTO j.wf_step(run_id, node_id, seq, kind, status, attempts, started_at, executor)
               VALUES ($1, $2, (SELECT count(*) FROM j.wf_step WHERE run_id = $1), 'tool', 'started', 1, now(), $3)
               ON CONFLICT (run_id, node_id) DO UPDATE SET attempts = j.wf_step.attempts + 1, executor = $3""",
            run_id, n["id"], self.me,
        )
        try:
            out = await call_tool(conn, n["ref"], f"{run_id}:{n['id']}", run_id, n["id"], tool_args(n, ctx))
        except ToolError:
            await conn.execute("UPDATE j.wf_step SET status = 'failed', finished_at = now() WHERE run_id = $1 AND node_id = $2", run_id, n["id"])
            raise
        await conn.execute(
            "UPDATE j.wf_step SET status = 'done', output = $3, finished_at = now() WHERE run_id = $1 AND node_id = $2",
            run_id, n["id"], json.dumps(out),
        )
        return out

    async def _compensate(self, conn: asyncpg.Connection, run_id: str, dsl: dict[str, Any], failed: str, why: str) -> None:
        """Saga：已完成且声明了补偿的写步骤按逆序补偿；补偿本身也带幂等键，可重入。"""
        rows = await conn.fetch(
            "SELECT node_id FROM j.wf_step WHERE run_id = $1 AND kind = 'tool' AND status = 'done' ORDER BY seq DESC", run_id
        )
        for r in rows:
            comp = node(dsl, r["node_id"]).get("compensation")
            if comp:
                await call_tool(conn, comp, f"{run_id}:{r['node_id']}:comp", run_id, r["node_id"], {})
                await conn.execute("UPDATE j.wf_step SET status = 'compensated' WHERE run_id = $1 AND node_id = $2", run_id, r["node_id"])
        await conn.execute(
            "UPDATE j.wf_run SET status = 'compensated', current = $2, ctx = ctx || $3, lease_owner = NULL WHERE id = $1",
            run_id, failed, json.dumps({"error": why}),
        )

    async def _park(self, conn: asyncpg.Connection, run_id: str, ctx: dict[str, Any], current: str, until: dt.datetime) -> None:
        await conn.execute(
            # 决策可能在建任务与挂起之间到达：那样就立即可再认领
            """UPDATE j.wf_run SET status = 'waiting', current = $3, ctx = $4, lease_owner = NULL, lease_until = NULL,
                 wake_at = CASE WHEN EXISTS (SELECT 1 FROM j.wf_task WHERE run_id = $1 AND node_id = $3 AND status <> 'pending')
                                THEN now() ELSE $2 END
               WHERE id = $1""",
            run_id, until, current, json.dumps(ctx),
        )


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


async def connect() -> asyncpg.Pool:
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA)
    return pool
