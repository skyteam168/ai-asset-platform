"""同一套场景分别跑两个引擎。Worker 是独立进程，用 TerminateProcess（等同 kill -9）或进程内 os._exit 模拟崩溃。

运行：.venv/Scripts/python -m pytest -q -s test_spike.py
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from common import CRASHY, DSN, FAKE_SCHEMA, LINEAR, SAGA, human

HERE = Path(__file__).parent
RESULTS: list[str] = []


class Workers:
    def __init__(self, engine: str) -> None:
        self.engine, self.procs = engine, {}  # type: ignore[var-annotated]

    def start(self, eid: str, version: str | None = "v1", recover: str | None = None) -> subprocess.Popen[bytes]:
        args = [sys.executable, "worker.py", self.engine, eid] + ([version] if version else [])
        p = subprocess.Popen(args, cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        assert p.stdout and p.stdout.readline().strip() == b"ready"
        self.procs[eid] = p
        return p

    def kill(self, eid: str) -> None:
        self.procs[eid].kill()
        self.procs[eid].wait()

    def stop_all(self) -> None:
        for p in self.procs.values():
            if p.poll() is None:
                p.kill()
                p.wait()


@pytest.fixture(params=["journal", "dbos"])
async def env(request: pytest.FixtureRequest) -> AsyncIterator[tuple[str, Any, Workers, asyncpg.Pool]]:
    engine: str = request.param
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    await pool.execute("DROP SCHEMA IF EXISTS j CASCADE; DROP SCHEMA IF EXISTS fake CASCADE; DROP SCHEMA IF EXISTS dbos CASCADE;")
    await pool.execute(FAKE_SCHEMA)
    workers = Workers(engine)
    if engine == "journal":
        from journal_engine import JournalExecutor, connect

        jpool = await connect()
        ex: Any = JournalExecutor(jpool)
    else:
        workers.start("bootstrap")  # 让 DBOS 建好系统表
        workers.kill("bootstrap")
        from dbos_engine import DbosExecutor

        ex = DbosExecutor()
    try:
        yield engine, ex, workers, pool
    finally:
        workers.stop_all()
        if engine == "dbos":
            ex.close()
        else:
            await jpool.close()
        await pool.close()


async def wait_status(ex: Any, run_id: str, want: set[str], timeout: float = 15) -> dict[str, Any]:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        s = await ex.status(run_id)
        if s["status"] in want:
            return s
        await asyncio.sleep(0.1)
    return await ex.status(run_id)


async def wait_until(pred: Callable[[], Any], timeout: float = 15) -> bool:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if await pred():
            return True
        await asyncio.sleep(0.1)
    return False


async def effects(pool: asyncpg.Pool, run_id: str) -> list[str]:
    return [r["tool"] for r in await pool.fetch("SELECT tool FROM fake.effect WHERE run_id = $1 ORDER BY seq", run_id)]


async def attempts(pool: asyncpg.Pool, run_id: str) -> dict[str, int]:
    rows = await pool.fetch("SELECT node_id, count(*) n FROM fake.attempt WHERE run_id = $1 GROUP BY node_id", run_id)
    return {r["node_id"]: r["n"] for r in rows}


def rid() -> str:
    return "run-" + uuid.uuid4().hex[:12]


def note(engine: str, msg: str) -> None:
    RESULTS.append(f"[{engine}] {msg}")


# ---------------- 场景 ----------------


async def test_linear_branches(env: Any) -> None:
    engine, ex, w, pool = env
    w.start("w1")
    hi, lo = rid(), rid()
    await ex.start(hi, LINEAR, {"amount": 5000})
    await ex.start(lo, LINEAR, {"amount": 10})
    assert (await wait_status(ex, hi, {"succeeded"}))["status"] == "succeeded"
    assert (await wait_status(ex, lo, {"succeeded"}))["status"] == "succeeded"
    assert await effects(pool, hi) == ["erp.notify", "erp.approve"]
    assert await effects(pool, lo) == ["erp.approve"]
    note(engine, f"步骤可观测：{await ex.steps(hi)}")


async def test_crash_mid_step_recovers(env: Any) -> None:
    """写操作已生效、步骤未记完成时进程被杀；同一 executor 重启后完成，每个副作用只生效一次。"""
    engine, ex, w, pool = env
    p = w.start("w1")
    r = rid()
    await ex.start(r, CRASHY, {})
    p.wait(timeout=15)  # 进程在 n2 内部 os._exit(137)
    assert p.returncode == 137
    t0 = time.monotonic()
    w.start("w1")
    assert (await wait_status(ex, r, {"succeeded"}))["status"] == "succeeded"
    assert await effects(pool, r) == ["erp.reserve", "erp.crash_after_effect", "erp.approve"]
    a = await attempts(pool, r)
    assert a["n1"] == 1 and a["n2"] == 2 and a["n3"] == 1  # 已完成的步骤不重做；中断的步骤用同一幂等键重做
    note(engine, f"同一 executor 重启后恢复耗时 {time.monotonic() - t0:.2f}s；尝试次数 {a}")


async def test_orphaned_run_taken_over_by_other_worker(env: Any) -> None:
    """Worker w1 崩溃后再也不回来（K8s 缩容 / 节点故障换了 Pod 名），只有 w2 在跑。"""
    engine, ex, w, pool = env
    w.start("w2")
    p = w.start("w1")
    # 先让 w2 闲着、w1 认领：journal 两者竞争，故只留 w1 认领再起 w2
    w.kill("w2")
    r = rid()
    await ex.start(r, CRASHY, {})
    p.wait(timeout=15)
    t0 = time.monotonic()
    w.start("w2")
    s = await wait_status(ex, r, {"succeeded"}, timeout=10)
    if engine == "journal":
        assert s["status"] == "succeeded"
        note(engine, f"孤儿 Run：租约到期后 w2 自动接手，{time.monotonic() - t0:.2f}s 完成")
    else:
        assert s["status"] != "succeeded", s
        note(engine, f"孤儿 Run：10 秒内无人接手（状态 {s['status']}，归属 executor {s.get('executor')}）；"
                     "开源版无心跳，需要自己判定死亡 executor 并调用恢复")
        # 补救：由存活进程按死亡 executor 恢复（私有 API DBOS._recover_pending_workflows）
        w.kill("w2")
        w.procs["w2"] = subprocess.Popen(
            [sys.executable, "-c", "import asyncio,sys;sys.argv=['x'];from dbos import DBOS;from dbos_engine import worker_config;"
             "\nasync def m():\n DBOS(config=worker_config('w2','v1'));DBOS.launch();DBOS._recover_pending_workflows(['w1']);"
             "await asyncio.Event().wait()\nasyncio.run(m())"],
            cwd=HERE,
        )
        assert (await wait_status(ex, r, {"succeeded"}))["status"] == "succeeded"
        note(engine, "显式恢复 w1 的 workflow 后由 w2 完成")
    assert await effects(pool, r) == ["erp.reserve", "erp.crash_after_effect", "erp.approve"]


async def test_saga_compensates_in_reverse(env: Any) -> None:
    engine, ex, w, pool = env
    w.start("w1")
    r = rid()
    await ex.start(r, SAGA, {})
    assert (await wait_status(ex, r, {"compensated"}))["status"] == "compensated"
    assert await effects(pool, r) == ["erp.reserve", "erp.approve", "erp.revoke", "erp.release"]


async def test_human_task_approve_and_duplicate_decision(env: Any) -> None:
    engine, ex, w, pool = env
    w.start("w1")
    r = rid()
    await ex.start(r, human(timeout_s=30), {})
    assert await wait_until(lambda: _current_is(ex, r, "h"))
    await ex.decide(r, "h", "approve")
    await ex.decide(r, "h", "approve")  # 重复提交
    s = await wait_status(ex, r, {"succeeded"})
    assert s["status"] == "succeeded" and s["ctx"]["h"]["decision"] == "approve"
    assert await effects(pool, r) == ["erp.approve"]


async def test_human_task_timeout_survives_crash(env: Any) -> None:
    """等待人工期间 Worker 被杀，重启后超时仍按原截止时间触发，走升级分支。"""
    engine, ex, w, pool = env
    w.start("w1")
    r = rid()
    t0 = time.monotonic()
    await ex.start(r, human(timeout_s=3), {})
    assert await wait_until(lambda: _current_is(ex, r, "h"))
    w.kill("w1")
    await asyncio.sleep(4)  # 截止时间在 Worker 不在时已过
    w.start("w1")
    s = await wait_status(ex, r, {"succeeded"})
    assert s["status"] == "succeeded" and s["ctx"]["h"]["decision"] == "timeout"
    assert await effects(pool, r) == ["erp.notify"]
    note(engine, f"人工任务超时（3s）跨崩溃生效，总耗时 {time.monotonic() - t0:.2f}s")


async def test_durable_wait_then_approve(env: Any) -> None:
    engine, ex, w, pool = env
    w.start("w1")
    r = rid()
    t0 = time.monotonic()
    await ex.start(r, human(timeout_s=30, wait_s=3), {})
    assert await wait_until(lambda: _current_is(ex, r, "w"))
    w.kill("w1")
    w.start("w1")
    assert await wait_until(lambda: _current_is(ex, r, "h"))
    waited = time.monotonic() - t0
    if engine == "dbos" and waited < 2.9:
        wall0 = time.time() - waited
        for st in ex.client.list_workflow_steps(r):
            print("DIAG", st["function_id"], st["function_name"],
                  st["started_at_epoch_ms"] and round(st["started_at_epoch_ms"] / 1000 - wall0, 2),
                  st["completed_at_epoch_ms"] and round(st["completed_at_epoch_ms"] / 1000 - wall0, 2), st["output"])
    assert waited >= 2.9, waited  # 重启没有让定时器重新计时或提前触发
    await ex.decide(r, "h", "approve")
    assert (await wait_status(ex, r, {"succeeded"}))["status"] == "succeeded"
    note(engine, f"wait 3s 跨重启，实际 {waited:.2f}s 后到达人工任务")


async def test_deploy_new_version_keeps_inflight_runs(env: Any) -> None:
    """等待人工的 Run 跨一次发布（新版本代码）后仍能继续。"""
    engine, ex, w, pool = env
    w.start("w1", version="v1")
    r = rid()
    await ex.start(r, human(timeout_s=60), {})
    assert await wait_until(lambda: _current_is(ex, r, "h"))
    w.kill("w1")
    w.start("w1", version="v2")
    await ex.decide(r, "h", "approve")
    s = await wait_status(ex, r, {"succeeded"}, timeout=8)
    if engine == "journal":
        assert s["status"] == "succeeded"
        note(engine, "发布新版本后在途 Run 继续（DSL 与版本钉在 Run 上，引擎代码无版本概念）")
    else:
        assert s["status"] != "succeeded", s
        note(engine, f"application_version 从 v1 换成 v2 后在途 Run 不被恢复（状态 {s['status']}，版本 {s.get('app_version')}）；"
                     "默认版本是源码哈希，任何代码发布都会触发；须固定 application_version 并自行管理不兼容变更")


async def test_throughput(env: Any) -> None:
    engine, ex, w, pool = env
    w.start("w1")
    w.start("w2")
    ids = [rid() for _ in range(100)]
    t0 = time.monotonic()
    for r in ids:
        await ex.start(r, LINEAR, {"amount": 10})
    for r in ids:
        assert (await wait_status(ex, r, {"succeeded"}, timeout=120))["status"] == "succeeded"
    dt = time.monotonic() - t0
    dup = await pool.fetchval("SELECT count(*) FROM fake.attempt GROUP BY run_id, node_id ORDER BY count(*) DESC LIMIT 1")
    assert dup == 1  # 两个 Worker 不会重复执行同一步
    note(engine, f"吞吐：100 个 Run（各 2 个 Tool 步），2 个 Worker，{dt:.1f}s（{100 / dt:.1f} run/s）")


async def _current_is(ex: Any, r: str, n: str) -> bool:
    """到达节点 n 并已挂起等待（journal 的 human_task / wait 挂起后状态为 waiting；DBOS 在 recv / sleep 中仍是 running）。"""
    s = await ex.status(r)
    return s.get("current") == n and s["status"] in ("waiting", "running") and (
        type(ex).__name__ != "JournalExecutor" or s["status"] == "waiting"
    )


def teardown_module() -> None:
    print("\n==== spike 观察 ====")
    for line in RESULTS:
        print(line)
